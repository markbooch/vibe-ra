"""LLM Commander — standing strategic Plan that rule reactors execute.

Why this exists
===============
Rule reactors (ArmyProducer / DefenseLayer / EconomyScaler / ArmyCommander)
ship reliably and beat Easy AI on hardcoded heuristics, but they cannot
*reason* about:

  * Where to send produced units (rally point) — currently they sit at
    factory exit until COUNTER fires.
  * When to tech up (atek / stek / dome / ftur upgrades) — the BO ends
    at dome and there's no codified next step.
  * Which army mix matches the situation — fixed 2:1 e1:e3 + fixed
    6 tank : 3 v2rl : 1 apc rotation regardless of what enemy fielded.
  * Which side of the base to fortify — pbox is placed on whichever
    open cell is closest to enemy *building* center, not where waves
    actually come from.

We promote Gemini from one-shot adviser to standing planner. Every
COMMANDER_INTERVAL_S seconds (default 10) it reads the snapshot +
last-known enemy quadrant + current task history and outputs a `Plan`:

    {army_mix, rally, aggression, tech_next, defense_quadrant, reason}

Reactors read PlanStore.get(tick) and either follow the plan or, if the
plan is missing/expired, fall back to their hardcoded defaults. This is
a *graceful degrade* — API outage → reactors keep playing, just
dumber.

Cost: ~500 in / ~150 out tokens per call, every 10 s = ~5k tokens/min.
flash-lite ≈ $0.005/game.

Threading: Commander runs LLM call in a daemon thread so the tick
subscriber returns immediately. PlanStore is RLock-guarded.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable, Optional

from .events import Event, EventBus, OpeningComplete, TickEvent
from .openra_client import Snapshot

log = logging.getLogger("vibera.commander")

# Gemini call cadence — every 10 s after BO completes. Plan TTL is 2.5×
# cadence so a single missed call doesn't strand reactors.
COMMANDER_INTERVAL_S = 10.0
PLAN_TTL_S = 25.0
TICKS_PER_SEC = 25
COMMANDER_INTERVAL_TICKS = int(COMMANDER_INTERVAL_S * TICKS_PER_SEC)
PLAN_TTL_TICKS = int(PLAN_TTL_S * TICKS_PER_SEC)

PROMPT_PATH = Path(__file__).parent / "prompts" / "commander_prompt.md"

# Whitelist of valid army items the LLM may name. Anything outside this
# set is dropped on parse — reactors must never receive an unknown
# build code.
VALID_ARMY = {"e1", "e3", "e2", "e4", "1tnk", "2tnk", "3tnk", "4tnk",
              "arty", "v2rl", "apc", "jeep", "ftrk", "mgg", "harv",
              "dog"}
VALID_TECH = {"atek", "stek", "dome", "fix", "weap", "afld", "hpad",
              "ftur", "tsla", "agun", "sam", "gap", "iron", "pdox",
              "mslo", None}
VALID_QUADRANTS = {"NW", "NE", "SW", "SE", None}
VALID_AGGRESSION = {"defend", "harass", "push", "allin"}


# ---------------------------------------------------------------------------
# Plan + PlanStore
# ---------------------------------------------------------------------------

@dataclass
class Plan:
    army_mix: dict[str, int] = field(default_factory=dict)
    rally: Optional[tuple[int, int]] = None
    aggression: str = "defend"
    tech_next: Optional[str] = None
    defense_quadrant: Optional[str] = None
    expires_tick: int = 0
    reason: str = ""
    issued_tick: int = 0

    def as_log_dict(self) -> dict:
        d = asdict(self)
        d["rally"] = list(self.rally) if self.rally else None
        return d


class PlanStore:
    """Thread-safe holder. Reactors call `.get(snap.tick)` per tick;
    None means 'no fresh plan, use your defaults'."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._plan: Optional[Plan] = None

    def set(self, plan: Plan) -> None:
        with self._lock:
            self._plan = plan

    def get(self, current_tick: int) -> Optional[Plan]:
        with self._lock:
            p = self._plan
            if p is None:
                return None
            if current_tick > p.expires_tick:
                return None
            return p

    def latest_unsafe(self) -> Optional[Plan]:
        """For UI / debug — may return expired plan."""
        with self._lock:
            return self._plan


# ---------------------------------------------------------------------------
# Commander
# ---------------------------------------------------------------------------

class Commander:
    """Calls Gemini every COMMANDER_INTERVAL_S to produce a Plan."""

    def __init__(self,
                 bus: EventBus,
                 store: PlanStore,
                 is_build_order_active: Callable[[], bool],
                 is_master_enabled: Callable[[], bool] = lambda: True,
                 tasks_provider: Optional[Callable[[], list]] = None,
                 scout_provider: Optional[Callable[[], Optional[str]]] = None,
                 model: Optional[str] = None) -> None:
        self.bus = bus
        self.store = store
        self._bo_active = is_build_order_active
        self._master = is_master_enabled
        self._tasks = tasks_provider
        self._scout_quadrant = scout_provider
        self._model = model or os.environ.get(
            "GEMINI_COMMANDER_MODEL",
            os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite"))
        self._armed = False
        self._last_call_tick = -10**9
        self._inflight = False
        self._inflight_lock = threading.Lock()
        self._call_count = 0
        self._error_count = 0
        bus.subscribe("commander", self._on_event)

    def _on_event(self, ev: Event) -> None:
        if isinstance(ev, OpeningComplete):
            self._armed = True
            log.info("Commander armed")
            return
        if not self._armed:
            return
        if not self._master():
            return
        if not isinstance(ev, TickEvent) or ev.snapshot is None:
            return
        snap = ev.snapshot
        if snap.tick - self._last_call_tick < COMMANDER_INTERVAL_TICKS:
            return
        # Skip if a previous call still in-flight (Gemini can be slow).
        with self._inflight_lock:
            if self._inflight:
                return
            self._inflight = True
        self._last_call_tick = snap.tick
        threading.Thread(
            target=self._call_async,
            args=(snap,),
            name="commander-llm",
            daemon=True,
        ).start()

    # --- LLM call ----------------------------------------------------------

    def _call_async(self, snap: Snapshot) -> None:
        try:
            plan = self._call_gemini(snap)
            if plan is not None:
                self.store.set(plan)
                log.info("Commander plan: agg=%s mix=%s rally=%s tech=%s def=%s — %s",
                         plan.aggression, plan.army_mix, plan.rally,
                         plan.tech_next, plan.defense_quadrant, plan.reason[:80])
        except Exception:
            self._error_count += 1
            log.exception("Commander call failed")
        finally:
            with self._inflight_lock:
                self._inflight = False
            self._call_count += 1

    def _call_gemini(self, snap: Snapshot) -> Optional[Plan]:
        # Lazy imports — adviser path already proves these resolve.
        from google import genai
        from google.genai import types
        from .voice_commander import snapshot_to_lean_state

        api_key = (os.environ.get("GEMINI_API_KEY")
                   or os.environ.get("GOOGLE_API_KEY"))
        if not api_key:
            log.warning("Commander: no GEMINI_API_KEY — skipping")
            return None

        state = snapshot_to_lean_state(snap)
        # Inject faction + flat buildable set + owned buildings list — the
        # LLM repeatedly suggested wrong-faction items / un-prereq'd tech
        # because these were buried inside per-queue blobs at the bottom.
        try:
            from .army_reactors import _detect_faction, _buildable_set
            state["faction"] = _detect_faction(snap)
            state["buildable"] = sorted(_buildable_set(snap))
            state["buildings"] = sorted({
                a.type for a in snap.mine()
                if a.type in {"fact", "powr", "apwr", "proc", "barr", "tent",
                              "weap", "dome", "atek", "stek", "iron", "fix",
                              "hpad", "afld", "silo", "kenn", "spen",
                              "ftur", "tsla", "pbox", "hbox", "gun", "agun",
                              "sam", "gap", "mslo", "pdox"}
            })
        except Exception:
            log.exception("Commander: failed to inject faction/buildable")
        if self._scout_quadrant is not None:
            try:
                state["enemy_quadrant"] = self._scout_quadrant()
            except Exception:
                pass
        if self._tasks is not None:
            try:
                tasks = self._tasks() or []
                state["recent_tasks"] = [
                    {"intent": (getattr(t, "intent", "") or "")[:60],
                     "state": getattr(t, "state", "?")}
                    for t in tasks[-8:]
                ]
            except Exception:
                pass
        prev = self.store.latest_unsafe()
        if prev is not None:
            state["previous_plan"] = prev.as_log_dict()

        try:
            system = PROMPT_PATH.read_text(encoding="utf-8")
        except FileNotFoundError:
            log.error("Commander: prompt missing at %s", PROMPT_PATH)
            return None

        client = genai.Client(api_key=api_key)
        cfg_kwargs: dict[str, Any] = dict(
            system_instruction=system,
            response_mime_type="application/json",
            temperature=0.4,
            max_output_tokens=600,
        )
        if self._model.startswith("gemini-3"):
            cfg_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)

        t0 = time.time()
        resp = client.models.generate_content(
            model=self._model,
            contents=(f"<game_state>\n{json.dumps(state, ensure_ascii=False)}\n"
                      f"</game_state>\n\nOutput a JSON Plan."),
            config=types.GenerateContentConfig(**cfg_kwargs),
        )
        latency = time.time() - t0
        text = (resp.text or "").strip()
        if text.startswith("```"):
            # Strip markdown fence (json model occasionally adds one).
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as e:
            log.warning("Commander: JSON parse failed (%s): %r", e, text[:200])
            return None
        log.info("Commander Gemini latency=%.2fs", latency)
        return self._parse_plan(raw, snap.tick)

    @staticmethod
    def _parse_plan(raw: dict, current_tick: int) -> Optional[Plan]:
        # Defensive parse — drop unknown items, clamp coords, normalize.
        if not isinstance(raw, dict):
            return None
        mix_raw = raw.get("army_mix") or {}
        mix: dict[str, int] = {}
        if isinstance(mix_raw, dict):
            for k, v in mix_raw.items():
                if k in VALID_ARMY:
                    try:
                        n = max(0, min(20, int(v)))
                        if n > 0:
                            mix[k] = n
                    except (TypeError, ValueError):
                        continue
        rally = None
        rr = raw.get("rally")
        if isinstance(rr, (list, tuple)) and len(rr) == 2:
            try:
                rally = (int(rr[0]), int(rr[1]))
            except (TypeError, ValueError):
                rally = None
        agg = raw.get("aggression")
        if agg not in VALID_AGGRESSION:
            agg = "defend"
        tech = raw.get("tech_next")
        if tech not in VALID_TECH:
            tech = None
        quad = raw.get("defense_quadrant")
        if quad not in VALID_QUADRANTS:
            quad = None
        reason = (raw.get("reason") or "")[:200]
        return Plan(
            army_mix=mix,
            rally=rally,
            aggression=agg,
            tech_next=tech,
            defense_quadrant=quad,
            expires_tick=current_tick + PLAN_TTL_TICKS,
            issued_tick=current_tick,
            reason=reason,
        )


# ---------------------------------------------------------------------------
# Smoke
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    store = PlanStore()
    print("default get:", store.get(0))
    p = Commander._parse_plan(
        {"army_mix": {"1tnk": 4, "e1": 2, "bogus": 9},
         "rally": [50, 60], "aggression": "push",
         "tech_next": "atek", "defense_quadrant": "NW",
         "reason": "Enemy massing tanks NW; counter with arty + 1tnk."},
        current_tick=1000,
    )
    print("parsed:", p.as_log_dict())
    store.set(p)
    print("get fresh:", store.get(1100))
    print("get expired:", store.get(p.expires_tick + 5))
