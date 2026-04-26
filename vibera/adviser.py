"""
Adviser loop — the AI staff officer (event-driven).

What changed vs v1
------------------
v1 polled the LLM every 15s regardless of whether anything happened.
That spent ~4 calls/min × ~2k tokens = 8k tokens/min idle. Worse, the
adviser ran in lock-step with build_order during the opening, often
suggesting redundant actions that autopilot then suppressed — burning
tokens for nothing.

v2 (this file) is event-driven. The adviser only consults the LLM when
the world transitions in a way that warrants tactical / strategic input:

    OpeningComplete        — opening sequence done; LLM picks tech path
    UnderAttack            — burst damage on a friendly; LLM responds
    EnemySpotted           — first sighting of an enemy unit/type
    PowerStateChanged Low* — power went Low/Critical; LLM unstucks it
    EconomyIdle            — proc up, queues empty, no inflight; LLM
                              proposes army composition or expansion

Plus a long-period (60s) fallback TickEvent watchdog so the LLM still
gets a turn in case our event taxonomy missed a transition. This is the
safety net, not the primary path.

Token budget
------------
Eyeballed: 3-5 LLM calls per 5 minutes of normal play. Compared to v1's
20 calls/5min, that's ~75-85% reduction with strictly more relevant
calls.

Fog of war
----------
The C# layer (ExternalControl.BuildSnapshotJson) already filters
non-friendly actors against LocalPlayer.Shroud.IsVisible. The snapshot
this module passes to the LLM is therefore the player's view, not the
engine's god view. EnemySpotted only fires for actually-spotted enemies.
The prompt should respect this — we tell the LLM "you see only what the
player sees" so it doesn't reason about invisible enemies.

Trigger context
---------------
Each trigger ships a tiny `trigger` block on top of the lean state, e.g.
    {"trigger": {"kind": "under_attack", "actor_type": "powr",
                 "actor_id": 42, "delta_hp": -45}, ...}
This lets the LLM focus on the change rather than re-reading the whole
state each call.

Debouncing
----------
* UnderAttack: at most one consult per UNDER_ATTACK_DEBOUNCE_SECONDS
  globally (suppress storm of damage events from one engagement).
* EnemySpotted: debounced per actor_type (one infantry sighting per
  type per ENEMY_SPOTTED_DEBOUNCE_SECONDS — re-spotting more e1s
  doesn't help).
* OpeningComplete + EconomyIdle + PowerStateChanged: not debounced
  (rare transitions, want immediate response).
* 60s fallback tick: only fires if the LLM hasn't been consulted in
  FALLBACK_TICK_SECONDS for any reason.

Toggles
-------
advisory_enabled  : if False, skip LLM calls and UI pushes.
autopilot_enabled : if True, high-confidence suggestions get auto-added.

Both default to v1 semantics. autopilot_off + advisory_on = "tell me,
let me ack"; both on = unattended play. All toggle access is RLock'd.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional

from .events import (
    EconomyIdle, EnemySpotted, Event, EventBus, OpeningComplete,
    PowerStateChanged, TickEvent, UnderAttack,
)
from .openra_client import Snapshot
from .task import Task

log = logging.getLogger("vibera.adviser")

# How long a high-confidence intent must be silent before autopilot will
# fire it again. Prevents the loop from spamming "build powr" while the
# previous build is still in queue.
AUTOPILOT_DEDUPE_SECONDS = 60.0
# Trigger debounce floors.
UNDER_ATTACK_DEBOUNCE_SECONDS = 30.0
ENEMY_SPOTTED_DEBOUNCE_SECONDS = 60.0
POWER_LOW_DEBOUNCE_SECONDS = 60.0
ECONOMY_IDLE_DEBOUNCE_SECONDS = 90.0
# Safety net — never let the LLM stay silent longer than this if either
# advisory or autopilot is on. Catches missed transitions.
FALLBACK_TICK_SECONDS = 60.0


class AdviserLoop:
    def __init__(self,
                 bus: EventBus,
                 add_task: Callable[[Task], None],
                 tasks_provider: Optional[Callable[[], list]] = None,
                 on_advice: Optional[Callable[[dict], None]] = None,
                 advisory_enabled: bool = True,
                 autopilot_enabled: bool = False,
                 build_order_active: Optional[Callable[[], bool]] = None):
        self.bus = bus
        self._add_task = add_task
        self._tasks_provider = tasks_provider
        self._on_advice = on_advice
        # While BO runs, suppress autopilot economic verbs (so the two
        # writers don't fight). Combat verbs are still allowed through.
        self._build_order_active = build_order_active

        self._advisory_enabled = advisory_enabled
        self._autopilot_enabled = autopilot_enabled

        self._lock = threading.RLock()
        self._autopilot_recent: dict[str, float] = {}
        self._last_advice: Optional[dict] = None

        # Latest snapshot from the bus (refreshed on every TickEvent).
        # Reads are cheap and lock-free is fine — pump thread writes,
        # adviser thread reads. Worst case we use the snapshot from
        # one tick ago; that's totally acceptable for an LLM call.
        self._latest_snapshot: Optional[Snapshot] = None

        # Per-trigger last-fired timestamps for debouncing.
        self._last_consult_ts: float = 0.0
        self._last_under_attack_ts: float = 0.0
        self._last_enemy_spotted_ts: dict[str, float] = {}
        self._last_power_low_ts: float = 0.0
        self._last_economy_idle_ts: float = 0.0

    # --- Lifecycle ----------------------------------------------------------

    def start(self) -> None:
        # Initialize fallback clock now so we don't fire a fallback_tick
        # on the very first TickEvent. The grace period equals
        # FALLBACK_TICK_SECONDS from subscription.
        self._last_consult_ts = time.time()
        self.bus.subscribe("adviser", self._on_event)
        log.info("AdviserLoop subscribed (advisory=%s, autopilot=%s)",
                 self._advisory_enabled, self._autopilot_enabled)

    def stop(self) -> None:
        # Bus.close handles unsubscribe + thread join; nothing to do here.
        pass

    # --- Toggles (thread-safe) ---------------------------------------------

    def set_advisory(self, enabled: bool) -> None:
        with self._lock:
            self._advisory_enabled = enabled
        log.info("adviser: advisory=%s", enabled)

    def set_autopilot(self, enabled: bool) -> None:
        with self._lock:
            self._autopilot_enabled = enabled
            if not enabled:
                self._autopilot_recent.clear()
        log.info("adviser: autopilot=%s", enabled)

    @property
    def advisory_enabled(self) -> bool:
        with self._lock:
            return self._advisory_enabled

    @property
    def autopilot_enabled(self) -> bool:
        with self._lock:
            return self._autopilot_enabled

    def last_advice(self) -> Optional[dict]:
        with self._lock:
            return self._last_advice

    # --- Event dispatch ----------------------------------------------------

    def _on_event(self, ev: Event) -> None:
        # Always refresh the latest snapshot from TickEvents; some other
        # handlers will need it.
        if isinstance(ev, TickEvent) and ev.snapshot is not None:
            self._latest_snapshot = ev.snapshot
            # Fallback heartbeat — only if both toggles allow consult AND
            # we've been silent past FALLBACK_TICK_SECONDS.
            now = time.time()
            if (now - self._last_consult_ts) >= FALLBACK_TICK_SECONDS:
                self._consult({"kind": "fallback_tick",
                               "since_last_consult_s": int(now - self._last_consult_ts)})
            return

        # Triggers below all need a snapshot to send to the LLM. If we
        # haven't had a TickEvent yet, drop the trigger (extremely brief
        # window at startup).
        if self._latest_snapshot is None:
            return

        if isinstance(ev, OpeningComplete):
            self._consult({"kind": "opening_complete",
                           "reason": ev.reason})
            return

        if isinstance(ev, UnderAttack):
            now = time.time()
            if now - self._last_under_attack_ts < UNDER_ATTACK_DEBOUNCE_SECONDS:
                return
            self._last_under_attack_ts = now
            self._consult({
                "kind": "under_attack",
                "actor_id": ev.actor_id,
                "actor_type": ev.actor_type,
                "delta_hp": ev.delta_hp,
            })
            return

        if isinstance(ev, EnemySpotted):
            now = time.time()
            last = self._last_enemy_spotted_ts.get(ev.actor_type, 0.0)
            if now - last < ENEMY_SPOTTED_DEBOUNCE_SECONDS:
                return
            self._last_enemy_spotted_ts[ev.actor_type] = now
            self._consult({
                "kind": "enemy_spotted",
                "actor_id": ev.actor_id,
                "actor_type": ev.actor_type,
                "distance_cells": ev.distance,
                "owner": ev.owner,
            })
            return

        if isinstance(ev, PowerStateChanged):
            if ev.new not in ("Low", "Critical"):
                return
            now = time.time()
            if now - self._last_power_low_ts < POWER_LOW_DEBOUNCE_SECONDS:
                return
            self._last_power_low_ts = now
            self._consult({
                "kind": "power_state",
                "old": ev.old, "new": ev.new,
            })
            return

        if isinstance(ev, EconomyIdle):
            now = time.time()
            if now - self._last_economy_idle_ts < ECONOMY_IDLE_DEBOUNCE_SECONDS:
                return
            self._last_economy_idle_ts = now
            self._consult({"kind": "economy_idle"})
            return

    # --- LLM consult --------------------------------------------------------

    def _consult(self, trigger: dict) -> None:
        with self._lock:
            advisory = self._advisory_enabled
            autopilot = self._autopilot_enabled
        if not advisory and not autopilot:
            return  # both off → don't burn API quota

        snap = self._latest_snapshot
        if snap is None:
            return

        self._last_consult_ts = time.time()
        log.info("adviser consult trigger=%s tick=%d", trigger.get("kind"), snap.tick)
        advice = self._call_llm(snap, trigger)
        with self._lock:
            self._last_advice = advice

        # Surface adviser failures in the log — UI shows "adviser offline" when
        # _error is present and we want to know why without inspecting
        # the live dict.
        if "_error" in advice:
            raw = (advice.get("_raw") or "")[:200].replace("\n", " ")
            log.warning("adviser advice error: %s | raw=%s",
                        advice["_error"], raw)

        if advisory and self._on_advice:
            try:
                self._on_advice(advice)
            except Exception:                       # pragma: no cover
                log.exception("on_advice callback raised")

        if autopilot and "_error" not in advice:
            self._maybe_autopilot(advice)

    def _call_llm(self, snap: Snapshot, trigger: dict) -> dict:
        # Local imports so an import failure (e.g. missing google-genai)
        # surfaces as an _error in the advice dict instead of killing
        # the subscriber thread.
        try:
            import json as _json
            from task_translator import propose_advice
            from voice_commander import snapshot_to_lean_state
        except Exception as e:
            return {"_error": f"adviser import failed: {e}",
                    "commentary": "", "suggestions": []}

        try:
            state = snapshot_to_lean_state(snap)
            # The trigger block tells the model WHY it's being consulted
            # this turn — focuses reasoning on the transition rather than
            # re-deriving everything from the lean state.
            state["trigger"] = trigger
            # Recent task history — last 10, with the latest step's note
            # so the LLM sees rejection reasons and doesn't re-suggest
            # already-failed actions.
            if self._tasks_provider:
                try:
                    tasks = self._tasks_provider() or []
                    history: list[dict[str, Any]] = []
                    for t in tasks[-10:]:
                        last_note = ""
                        try:
                            steps = getattr(t, "steps", None) or []
                            cur = min(getattr(t, "cursor", 0), len(steps) - 1)
                            if 0 <= cur < len(steps):
                                last_note = (getattr(steps[cur], "note", "")
                                             or "")[:120]
                        except Exception:
                            pass
                        history.append({
                            "id": getattr(t, "id", "?")[:8],
                            "state": getattr(t, "state", "?"),
                            "intent": (getattr(t, "intent", "") or "")[:60],
                            "last_note": last_note,
                        })
                    state["recent_tasks"] = history
                except Exception:                   # pragma: no cover
                    log.exception("tasks_provider failed; skipping history")
            advice = propose_advice(_json.dumps(state, ensure_ascii=False))
        except Exception as e:
            return {"_error": f"adviser call failed: {e}",
                    "commentary": "", "suggestions": []}
        return advice

    # --- Autopilot ---------------------------------------------------------

    def _maybe_autopilot(self, advice: dict) -> None:
        now = time.time()
        with self._lock:
            self._autopilot_recent = {
                k: ts for k, ts in self._autopilot_recent.items()
                if now - ts < AUTOPILOT_DEDUPE_SECONDS
            }

        bo_active = bool(self._build_order_active and self._build_order_active())
        ECON_VERBS = {"produce", "auto_place", "place", "deploy"}

        for sug in advice.get("suggestions", []):
            if sug.get("confidence") != "high":
                continue
            plan = sug.get("task_plan") or {}
            steps = plan.get("steps") or []
            intent = str(plan.get("intent") or sug.get("title") or "")
            dedupe_key = intent
            first_econ_verb: Optional[str] = None
            for s in steps:
                if s.get("kind") == "action":
                    p = s.get("params") or {}
                    item = p.get("item") or p.get("target_id") or ""
                    verb = s.get("verb", "?")
                    if first_econ_verb is None and verb in ECON_VERBS:
                        first_econ_verb = verb
                    dedupe_key = f"{verb}:{item}"
                    break
            if not intent and not dedupe_key:
                continue

            if bo_active and first_econ_verb is not None:
                log.info(
                    "autopilot suppress %r (build_order active, verb=%s)",
                    intent, first_econ_verb)
                continue

            with self._lock:
                last = self._autopilot_recent.get(dedupe_key)
                if last is not None and now - last < AUTOPILOT_DEDUPE_SECONDS:
                    log.info("autopilot skip %r (recent, key=%s)",
                             intent, dedupe_key)
                    continue
                self._autopilot_recent[dedupe_key] = now

            try:
                task = Task.new(
                    intent=intent,
                    steps=steps,
                    utterance="<autopilot>",
                )
                self._add_task(task)
                log.info("autopilot fired: %s (key=%s)", intent, dedupe_key)
            except Exception as e:                  # pragma: no cover
                log.exception("autopilot add_task failed: %s", e)


if __name__ == "__main__":
    # Smoke: verify event dispatch + debouncing without calling the LLM.
    # We monkey-patch _call_llm to record calls.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    from openra_client import SelfState, Snapshot

    bus = EventBus()
    calls: list[dict] = []

    a = AdviserLoop(
        bus=bus,
        add_task=lambda t: None,
        tasks_provider=lambda: [],
        on_advice=lambda adv: None,
        advisory_enabled=True,
        autopilot_enabled=False,
    )
    a._call_llm = lambda snap, trigger: {        # type: ignore[assignment]
        "commentary": "", "suggestions": [], **{"_trigger": trigger}}
    # Capture every consult.
    orig_consult = a._consult

    def cap(trigger):
        calls.append(trigger)
        orig_consult(trigger)
    a._consult = cap                                # type: ignore[assignment]
    a.start()

    snap = Snapshot(
        tick=100, local_player="Multi0",
        self_state=SelfState(
            cash=1000, resources=0, resource_cap=0, spendable=1000,
            power_provided=0, power_drained=0, power_excess=0,
            power_state="Normal", queues=[]),
        actors=[])

    # Need a tick first so adviser has a snapshot.
    bus.emit(TickEvent(tick=100, snapshot=snap))
    time.sleep(0.1)

    bus.emit(OpeningComplete(reason="test"))
    bus.emit(UnderAttack(actor_id=1, actor_type="powr", delta_hp=-50))
    bus.emit(UnderAttack(actor_id=2, actor_type="proc", delta_hp=-40))  # debounced
    bus.emit(EnemySpotted(actor_id=99, actor_type="e1", distance=20))
    bus.emit(EnemySpotted(actor_id=100, actor_type="e1", distance=22))  # debounced (same type)
    bus.emit(EnemySpotted(actor_id=101, actor_type="e2", distance=25))  # different type → fires
    bus.emit(PowerStateChanged(old="Normal", new="Low"))
    bus.emit(PowerStateChanged(old="Low", new="Normal"))                # no consult (recovered)
    bus.emit(EconomyIdle())

    time.sleep(0.3)
    bus.close()
    kinds = [c["kind"] for c in calls]
    print("triggers:", kinds)
    assert "opening_complete" in kinds
    assert kinds.count("under_attack") == 1, kinds
    assert kinds.count("enemy_spotted") == 2, kinds
    assert "power_state" in kinds
    assert "economy_idle" in kinds
    print("[OK] adviser smoke pass")
