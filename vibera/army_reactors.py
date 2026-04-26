"""
Army / economy / combat reactors — code-ified RA1 mid-game playbook.

Why
---
Live test of the v2 event-driven stack proved that even with a perfect
opening BO, leaving the mid-game to the LLM cratered:
  * BO ended at 3:00 with zero combat units, zero defense.
  * Adviser fired single-actor `attack` tasks per EnemySpotted (no
    massing); JSON schema failed 3×/min; queue overflowed at 40+
    dropped EnemySpotted/sec when the player scrolled the camera.

So we follow ADR-001's recipe (BO success): code the standard playbook,
demote the LLM to a tiny strategic stance picker (separate work).

Five reactors in this file. All share the same shape as BuildOrderRunner:
subscribe to events, look at the latest snapshot, submit a one-step Task
through the daemon (validator + executor still run). Per-action tick
throttle prevents spam.

* ArmyProducer
    Trigger:   TickEvent + QueueIdle (Vehicle / Infantry queues)
    Action:    produce(3tnk|2tnk) when Vehicle queue empty;
               produce(e1) when Infantry queue empty AND ≤4 inf owned.
    Why:       "Never let weap/barr idle" is RA1 rule #1.

* DefenseLayer
    Trigger:   TickEvent (after weap exists)
    Action:    produce(tsla|pbox) up to DEFENSE_TARGET, ≥60s apart.
    Why:       Tower coverage stops Easy AI's first poke.

* EconomyScaler
    Trigger:   TickEvent
    Action:    produce(proc) when cash ≥ 2500 and only 1 proc;
               produce(harv) when harv-count < 2×proc-count.
    Why:       Sustained income > one-shot army.

* ArmyCommander
    Trigger:   TickEvent
    Action:    when ≥ ARMY_MASS_THRESHOLD idle combat units exist,
               attack-move every one of them to the map's enemy half.
               On UnderAttack: recall every combat unit to base center.
    Why:       Mass beats trickle. Single-actor attacks lose 1-for-1.

* Scout
    Trigger:   OpeningComplete (one-shot)
    Action:    pick a fast unit (dog/jeep/e1) and attack-move to map
               quadrants in turn.
    Why:       Fog blinds the LLM's strategy picker; need vision.

All reactors honour `is_build_order_active()` — they stay quiet until
the opening declares done, so they don't fight BO over the queue.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from .events import (
    ActorDied, Event, EventBus, OpeningComplete, QueueIdle, TickEvent,
    UnderAttack,
)
from .openra_client import Actor, Snapshot
from .task import Task

log = logging.getLogger("vibera.army_reactors")


# ---------------------------------------------------------------------------
# RA1 type knowledge
# ---------------------------------------------------------------------------
# Faction inferred at runtime: if "tsla" appears in any Defense queue's
# buildable list we're Soviet, otherwise Allied. (Neutral fallbacks chosen
# to be the cheapest sensible option.)

SOVIET_TANK = "3tnk"      # heavy tank
ALLIED_TANK = "2tnk"      # medium tank
SOVIET_INF  = "e1"        # rifle infantry
ALLIED_INF  = "e1"        # rifle infantry (both factions)
SOVIET_DEF  = "tsla"      # Tesla coil
ALLIED_DEF  = "pbox"      # pillbox
SOVIET_SCOUT = "dog"
ALLIED_SCOUT = "jeep"

COMBAT_TYPES = {
    # Vehicles
    "1tnk", "2tnk", "3tnk", "4tnk", "ttnk", "v2rl", "arty", "apc",
    "jeep", "mgg",
    # Infantry
    "e1", "e2", "e3", "e4", "e6", "dog", "shok", "medi", "mech",
}

# Vehicle / Infantry are the queue.type strings used by the engine.
QT_VEHICLE = "Vehicle"
QT_INFANTRY = "Infantry"
QT_BUILDING = "Building"
QT_DEFENSE = "Defense"

# Throttles -----------------------------------------------------------------
TICKS_PER_SEC = 25                       # OpenRA default
SUBMIT_FLOOR_TICKS = TICKS_PER_SEC * 2   # 2 s general floor
DEFENSE_INTERVAL_TICKS = TICKS_PER_SEC * 60  # 60 s between towers
DEFENSE_TARGET = 2

# Army Commander ------------------------------------------------------------
ARMY_MASS_THRESHOLD = 8                  # base threshold; bumped per failure
ARMY_MASS_FAIL_BUMP = 4                  # +4 idle combat units per failed push
ARMY_MASS_CAP = 24                       # threshold ceiling
ARMY_PUSH_COOLDOWN_TICKS = TICKS_PER_SEC * 30
ARMY_RECALL_COOLDOWN_TICKS = TICKS_PER_SEC * 8
ARMY_AVOID_RADIUS = 6                    # skip targets within N cells of a known-bad point
ARMY_AVOID_RETENTION_S = 240             # forget a failed point after 4 min

# Counter-attack: when our base is hit, send army to the *attacker's*
# location (== victim's tile, anything attacking sits within range).
# Cooldown so a single salvo doesn't fire 30 counter orders.
COUNTER_ATTACK_COOLDOWN_S = 6.0
# DangerScanRadius (OpenRA StateBase.cs:88): if any own building is within
# this many cells of the unit, never flee — defend instead. We don't flee
# at all in the new design, but we still use this to *expand* defense
# coverage when the attack is at-base vs forward.
HOME_DEFENSE_RADIUS = 12

# Vehicle production rotation: 6 tanks : 3 v2rl : 1 apc per cycle.
VEH_ROTATION = (
    "tank", "tank", "tank", "v2rl",
    "tank", "tank", "tank", "v2rl",
    "tank", "apc",
)

# Infantry: keep barr/tent always warm — only stop when broke.
INF_CASH_RESERVE = 500                   # keep this much for vehicles before queueing inf

# Infantry rotation: 2 rifle (e1) : 1 rocket (e3). Pros run rifle:rocket
# = 2:1 (Orb / GameReplays BO consensus): rifle is the damage dealer,
# rocket cracks armour & buildings. Pure e1 loses to anything armoured.
INF_ROTATION = ("e1", "e1", "e3")

# For Plan-driven mix splitting: which items belong to which queue.
_INF_ITEMS = {"e1", "e2", "e3", "e4", "dog"}
_VEH_ITEMS = {"1tnk", "2tnk", "3tnk", "4tnk", "arty", "v2rl",
              "apc", "jeep", "ftrk", "mgg", "harv"}

# Plan-item substitution: when the LLM names a unit not in our
# faction's roster (or not yet unlocked), try these alternates in order.
# This lets the prompt stay simple ("ask for arty") and the runtime
# absorb the faction asymmetry.
_VEH_SUBSTITUTE = {
    # Allied light/medium <-> Soviet heavy
    "1tnk": ("2tnk", "3tnk", "apc"),
    "2tnk": ("3tnk", "1tnk", "apc"),
    "3tnk": ("2tnk", "1tnk", "apc"),
    "4tnk": ("3tnk", "2tnk", "1tnk"),
    # Artillery cross-faction
    "v2rl": ("arty", "3tnk", "2tnk"),
    "arty": ("v2rl", "2tnk", "3tnk"),
    # Misc
    "ftrk": ("apc", "3tnk", "2tnk"),
    "jeep": ("apc", "1tnk", "2tnk"),
    "mgg":  ("apc", "1tnk", "2tnk"),
    "apc":  ("3tnk", "2tnk", "1tnk"),
}

_INF_SUBSTITUTE = {
    "e2": ("e1",),
    "e4": ("e3", "e1"),
    "e3": ("e1",),
    "dog": ("e1",),
    "e1": ("e3",),
}


def _expand_mix(mix: dict[str, int], allowed: set[str]) -> tuple[str, ...]:
    """Turn a weight dict like {1tnk:3, arty:2} into a deterministic
    cycle list ('1tnk','1tnk','1tnk','arty','arty'). Items outside
    `allowed` are dropped (so vehicle mix never injects infantry)."""
    out: list[str] = []
    for k, v in sorted(mix.items()):
        if k not in allowed:
            continue
        try:
            n = max(0, int(v))
        except (TypeError, ValueError):
            continue
        out.extend([k] * n)
    return tuple(out)

# Diagnostics: log a "why no tank" summary every N ticks when veh queue idle.
DIAG_VEH_LOG_INTERVAL_TICKS = TICKS_PER_SEC * 15

# Scout rotation -----------------------------------------------------------
SCOUT_INTERVAL_TICKS = TICKS_PER_SEC * 40    # rotate scout corner every 40 s
SCOUT_PRODUCE_THROTTLE_TICKS = TICKS_PER_SEC * 30
SCOUT_EARLY_ARM_TICKS = TICKS_PER_SEC * 45   # arm during BO after 45 s if first idle inf exists
SCOUT_TARGET_COUNT = 2                       # send 2 scouts to opposite corners

# Economy: silo / power predictive thresholds
SILO_RES_RATIO = 0.80    # build silo if resources/cap > 80 %
POWR_EXCESS_FLOOR = 30   # build powr if power_excess < 30 (preventive)

# Floating-cash multi-production: when cash > FLOAT_CASH_FLOOR and we
# still have headroom under the RAX/WF caps, build another barracks/WF.
# Pro RA1 rule (Orb / GameReplays): never sit on >$2000 — convert excess
# into more production buildings (RAX cap 7, WF cap 4 in OpenRA).
FLOAT_CASH_FLOOR = 2000
RAX_CAP = 4              # start conservative; will raise once stable
WF_CAP = 2               # second WF doubles vehicle output


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _detect_faction(snap: Snapshot) -> str:
    """Return 'soviet' or 'allied' (default 'soviet' if undetermined)."""
    if snap.self_state is None:
        return "soviet"
    for q in snap.self_state.queues:
        for item in q.buildable:
            if item == "tsla":
                return "soviet"
            if item in ("pbox", "gun"):
                return "allied"
    return "soviet"


def _buildable_set(snap: Snapshot) -> set[str]:
    if snap.self_state is None:
        return set()
    s: set[str] = set()
    for q in snap.self_state.queues:
        s.update(q.buildable)
    return s


def _queues_by_type(snap: Snapshot, qtype: str) -> list:
    if snap.self_state is None:
        return []
    return [q for q in snap.self_state.queues if q.type == qtype]


def _owned_count(snap: Snapshot, type_filter) -> int:
    if isinstance(type_filter, str):
        type_filter = {type_filter}
    return sum(1 for a in snap.mine() if a.type in type_filter)


def _queue_inflight(snap: Snapshot, item: str) -> bool:
    if snap.self_state is None:
        return False
    for q in snap.self_state.queues:
        if q.current and q.current.item == item:
            return True
        for qi in q.queued:
            if qi.item == item:
                return True
    return False


def _live_intent_active(tasks_provider: Callable[[], list], intent: str) -> bool:
    for t in tasks_provider() or []:
        if t.intent == intent and not t.is_terminal:
            return True
    return False


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------

class _ReactorBase:
    """Common scaffolding: lock, last-submit-tick map, gate on BO."""

    def __init__(self,
                 name: str,
                 bus: EventBus,
                 add_task: Callable[[Task], None],
                 tasks_provider: Callable[[], list],
                 is_build_order_active: Callable[[], bool],
                 is_master_enabled: Callable[[], bool] = lambda: True):
        self.name = name
        self.bus = bus
        self._add_task = add_task
        self._tasks_provider = tasks_provider
        self._bo_active = is_build_order_active
        self._master_enabled = is_master_enabled
        self._lock = threading.RLock()
        self._last_submit: dict[str, int] = {}

    def _gated(self) -> bool:
        """True if we should stay silent (master off or BO still running)."""
        return (not self._master_enabled()) or self._bo_active()

    def _throttled(self, key: str, tick: int, floor_ticks: int) -> bool:
        last = self._last_submit.get(key, -10**9)
        return (tick - last) < floor_ticks

    def _record(self, key: str, tick: int) -> None:
        self._last_submit[key] = tick

    def _submit(self, intent: str, steps: list[dict]) -> None:
        task = Task.new(intent=intent, steps=steps, utterance=f"<{self.name}>")
        self._add_task(task)
        log.info("%s submitted %s (task=%s)", self.name, intent, task.id)


# ---------------------------------------------------------------------------
# R1 ArmyProducer
# ---------------------------------------------------------------------------

class ArmyProducer(_ReactorBase):
    """Keep weap and barr queues warm.

    Vehicle queue: rotate through VEH_ROTATION (6 tank : 3 v2rl : 1 apc).
    Infantry queue: produce e1 whenever idle and we have INF_CASH_RESERVE+
                    cash to spare (no hard count cap — pop will gate).

    Periodic diagnostics: when veh queue is idle but we still skip, log
    why every DIAG_VEH_LOG_INTERVAL_TICKS so we can debug "0 tanks" runs.
    """

    def __init__(self, bus, add_task, tasks_provider, is_build_order_active,
                 is_master_enabled=lambda: True,
                 plan_provider=None):
        super().__init__("army_producer", bus, add_task, tasks_provider,
                         is_build_order_active, is_master_enabled)
        self._veh_cycle_idx = 0
        self._inf_cycle_idx = 0
        self._last_veh_diag_tick = -10**9
        # Optional Commander hook — callable(tick) -> Plan|None. When the
        # current plan has a non-empty army_mix we use it as the cycle
        # source; otherwise we fall back to hardcoded VEH/INF_ROTATION.
        self._plan_provider = plan_provider or (lambda _t: None)
        bus.subscribe("army-producer", self._on_event)

    def _current_cycles(self, tick: int) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Return (veh_cycle, inf_cycle) — Plan-driven if available,
        else default rotations. Empty tuple means 'use default for that queue'."""
        plan = self._plan_provider(tick)
        if plan is None or not plan.army_mix:
            return VEH_ROTATION, INF_ROTATION
        veh = _expand_mix(plan.army_mix, _VEH_ITEMS) or VEH_ROTATION
        inf = _expand_mix(plan.army_mix, _INF_ITEMS) or INF_ROTATION
        return veh, inf

    def _on_event(self, ev: Event) -> None:
        if self._gated():
            return
        snap: Optional[Snapshot] = None
        if isinstance(ev, TickEvent):
            snap = ev.snapshot
        elif isinstance(ev, QueueIdle):
            return
        if snap is None or snap.self_state is None:
            return

        faction = _detect_faction(snap)
        tank = SOVIET_TANK if faction == "soviet" else ALLIED_TANK
        inf = SOVIET_INF if faction == "soviet" else ALLIED_INF
        buildable = _buildable_set(snap)
        tick = snap.tick
        cash = snap.self_state.cash
        veh_cycle, inf_cycle = self._current_cycles(tick)

        # --- Vehicle queue: rotate tank / v2rl / apc ---
        veh_qs = _queues_by_type(snap, QT_VEHICLE)
        if veh_qs:
            idle = any(q.current is None and not q.queued for q in veh_qs)
            if idle:
                # Resolve current rotation slot to a concrete item.
                want_slot = veh_cycle[self._veh_cycle_idx % len(veh_cycle)]
                want = self._resolve_veh_item(want_slot, tank, buildable)
                skip_reason = self._veh_skip_reason(want, buildable, snap, tick)
                if skip_reason is None:
                    self._submit(f"army build:{want}",
                                 [{"kind": "action", "verb": "produce",
                                   "params": {"item": want, "count": 1}}])
                    self._record(f"prod:{want}", tick)
                    self._veh_cycle_idx += 1
                else:
                    # Skipped: try advancing rotation once so we don't get
                    # stuck on (e.g.) v2rl when stek isn't built yet.
                    self._maybe_advance_rotation(skip_reason)
                    self._diag_veh_skip(snap, want, skip_reason, buildable, tick)

        # --- Infantry queue: rotate e1/e1/e3 (rifle:rocket = 2:1) ---
        inf_qs = _queues_by_type(snap, QT_INFANTRY)
        if inf_qs:
            idle = any(q.current is None and not q.queued for q in inf_qs)
            if idle and cash >= INF_CASH_RESERVE:
                # Resolve rotation slot, with fallback to e1 if e3 not yet
                # buildable (no barracks tier-up, etc.).
                want = inf_cycle[self._inf_cycle_idx % len(inf_cycle)]
                if want not in buildable:
                    sub = None
                    for alt in _INF_SUBSTITUTE.get(want, ()):
                        if alt in buildable:
                            sub = alt
                            break
                    if sub is None and inf in buildable:
                        sub = inf
                    if sub is None:
                        # Advance index so we don't get pinned next tick.
                        self._inf_cycle_idx += 1
                        want = None
                    else:
                        want = sub
                if (want is not None
                        and not _queue_inflight(snap, want)
                        and not _live_intent_active(self._tasks_provider,
                                                    f"army build:{want}")
                        and not self._throttled(f"prod:{want}", tick,
                                                SUBMIT_FLOOR_TICKS)):
                    self._submit(f"army build:{want}",
                                 [{"kind": "action", "verb": "produce",
                                   "params": {"item": want, "count": 1}}])
                    self._record(f"prod:{want}", tick)
                    self._inf_cycle_idx += 1

    def _resolve_veh_item(self, slot: str, tank: str,
                          buildable: set[str]) -> str:
        if slot == "tank":
            # Prefer faction's "main" tank but fall back through the
            # tier ladder so we never starve the queue waiting for tech:
            # Soviet:  3tnk → 1tnk
            # Allied:  2tnk → 1tnk
            for candidate in (tank, "1tnk"):
                if candidate in buildable:
                    return candidate
            return tank
        if slot == "v2rl":
            # v2rl is Soviet-only; for Allied substitute arty (artillery).
            if "v2rl" in buildable:
                return "v2rl"
            if "arty" in buildable:
                return "arty"
            # No artillery yet — fall back to whichever tank is buildable.
            return self._resolve_veh_item("tank", tank, buildable)
        if slot == "apc":
            if "apc" in buildable:
                return "apc"
            return self._resolve_veh_item("tank", tank, buildable)
        # Concrete code from a Commander Plan (e.g. "1tnk", "arty").
        # Try direct first, then walk the substitute table, then give up
        # to the faction main tank.
        if slot in buildable:
            return slot
        for alt in _VEH_SUBSTITUTE.get(slot, ()):
            if alt in buildable:
                return alt
        return self._resolve_veh_item("tank", tank, buildable)

    def _veh_skip_reason(self, item: str, buildable: set[str],
                         snap: Snapshot, tick: int) -> Optional[str]:
        if item not in buildable:
            return f"not-buildable({item})"
        if _queue_inflight(snap, item):
            return f"inflight({item})"
        if _live_intent_active(self._tasks_provider, f"army build:{item}"):
            return f"live-intent({item})"
        if self._throttled(f"prod:{item}", tick, SUBMIT_FLOOR_TICKS):
            return f"throttled({item})"
        return None

    def _maybe_advance_rotation(self, reason: str) -> None:
        # If the only blocker is "not buildable" (prereq missing), skip
        # this slot and try the next one next tick. Otherwise hold.
        if reason.startswith("not-buildable"):
            self._veh_cycle_idx += 1

    def _diag_veh_skip(self, snap: Snapshot, want: str, reason: str,
                       buildable: set[str], tick: int) -> None:
        if tick - self._last_veh_diag_tick < DIAG_VEH_LOG_INTERVAL_TICKS:
            return
        self._last_veh_diag_tick = tick
        veh_buildable = sorted(b for b in buildable
                               if b in {"1tnk", "2tnk", "3tnk", "4tnk", "ttnk",
                                        "v2rl", "arty", "apc", "harv", "mcv",
                                        "jeep", "ftrk", "mgg"})
        log.info("ArmyProducer veh-idle skip: want=%s reason=%s "
                 "veh_buildable=%s cash=%d cycle_idx=%d",
                 want, reason, veh_buildable, snap.self_state.cash,
                 self._veh_cycle_idx)


# ---------------------------------------------------------------------------
# R2 DefenseLayer
# ---------------------------------------------------------------------------

class DefenseLayer(_ReactorBase):
    """Drop DEFENSE_TARGET towers, ≥60s apart, once weap is up."""

    def __init__(self, bus, add_task, tasks_provider, is_build_order_active,
                 is_master_enabled=lambda: True):
        super().__init__("defense_layer", bus, add_task, tasks_provider,
                         is_build_order_active, is_master_enabled)
        bus.subscribe("defense-layer", self._on_event)

    def _on_event(self, ev: Event) -> None:
        if self._gated():
            return
        if not isinstance(ev, TickEvent) or ev.snapshot is None:
            return
        snap = ev.snapshot
        if snap.self_state is None:
            return
        # Need a weap up to make defense matter (and prereqs).
        if _owned_count(snap, "weap") < 1:
            return

        faction = _detect_faction(snap)
        tower = SOVIET_DEF if faction == "soviet" else ALLIED_DEF
        buildable = _buildable_set(snap)
        if tower not in buildable:
            return

        owned = _owned_count(snap, tower)
        if owned >= DEFENSE_TARGET:
            return
        if _queue_inflight(snap, tower):
            return
        if _live_intent_active(self._tasks_provider, f"defense build:{tower}"):
            return
        if self._throttled(f"def:{tower}", snap.tick, DEFENSE_INTERVAL_TICKS):
            return

        self._submit(f"defense build:{tower}",
                     [{"kind": "action", "verb": "produce",
                       "params": {"item": tower, "count": 1}}])
        self._record(f"def:{tower}", snap.tick)


# ---------------------------------------------------------------------------
# R2b TechBuilder — submits Plan.tech_next once when economy allows
# ---------------------------------------------------------------------------

# Cost gate so we don't tank into a tech in the middle of needing units.
TECH_CASH_FLOOR = 1200
TECH_INTERVAL_TICKS = TICKS_PER_SEC * 30   # at most one tech submit / 30 s

# Prereq chain (RA1). When the LLM asks for X but X isn't buildable yet,
# we walk this map to find the first prereq that IS buildable and submit
# THAT instead, so the chain unblocks itself over successive ticks.
# Order in tuple = preference (first listed = "closest" prereq).
_TECH_PREREQ = {
    "stek":  ("dome", "powr"),
    "atek":  ("dome", "powr"),
    "iron":  ("stek",),
    "apwr":  ("stek",),
    "tsla":  ("dome", "apwr"),
    "agun":  ("atek",),
    "hbox":  ("atek",),
    "gap":   ("atek",),
    "mslo":  ("stek",),
    "pdox":  ("atek",),
    # Defensive towers need a barracks of the right faction. The Commander
    # may ask for ftur (Soviet) or pbox (Allied); if not buildable, the
    # missing prereq is usually the barracks itself.
    "ftur":  ("barr",),
    "pbox":  ("tent",),
    "gun":   ("tent",),
    "fix":   ("dome",),
    "dome":  ("proc",),   # safety: should already be in BO
}


class TechBuilder(_ReactorBase):
    """Submits the building named in `Plan.tech_next` exactly once.

    Idempotent: skips if the building is already owned, already in queue,
    or already in our task list. Cash-gated so the LLM can't bankrupt us
    by demanding a 1500-credit tech with $200 in the bank.
    """

    def __init__(self, bus, add_task, tasks_provider, is_build_order_active,
                 is_master_enabled=lambda: True, plan_provider=None):
        super().__init__("tech_builder", bus, add_task, tasks_provider,
                         is_build_order_active, is_master_enabled)
        self._plan_provider = plan_provider or (lambda _t: None)
        bus.subscribe("tech-builder", self._on_event)

    def _on_event(self, ev: Event) -> None:
        if self._gated():
            return
        if not isinstance(ev, TickEvent) or ev.snapshot is None:
            return
        snap = ev.snapshot
        if snap.self_state is None:
            return
        plan = self._plan_provider(snap.tick)
        if plan is None or plan.tech_next is None:
            return
        wanted = plan.tech_next
        # Gate by cash and throttle so we never spam.
        if snap.self_state.cash < TECH_CASH_FLOOR:
            return
        # Resolve `wanted` against current buildable + prereq chain.
        # If wanted is already owned, we're done with this plan item.
        if _owned_count(snap, wanted) > 0:
            return
        buildable = _buildable_set(snap)
        item = self._resolve_tech(wanted, buildable, snap)
        if item is None:
            return
        if self._throttled(f"tech:{item}", snap.tick, TECH_INTERVAL_TICKS):
            return
        if _owned_count(snap, item) > 0:
            return
        if _queue_inflight(snap, item):
            return
        if _live_intent_active(self._tasks_provider, f"tech build:{item}"):
            return
        self._submit(f"tech build:{item}",
                     [{"kind": "action", "verb": "produce",
                       "params": {"item": item, "count": 1}}])
        self._record(f"tech:{item}", snap.tick)
        if item == wanted:
            log.info("TechBuilder: plan tech_next=%s submitted (cash=%d)",
                     item, snap.self_state.cash)
        else:
            log.info("TechBuilder: plan wants %s but not buildable; "
                     "submitting prereq %s instead (cash=%d)",
                     wanted, item, snap.self_state.cash)

    @staticmethod
    def _resolve_tech(wanted: str, buildable: set[str],
                      snap: Snapshot) -> Optional[str]:
        """Return the first item we can actually build to advance toward
        `wanted`. If wanted is buildable, return it. Otherwise walk the
        prereq chain (BFS, depth ≤ 3) and return the first buildable
        prereq that we don't already own. None = nothing actionable."""
        if wanted in buildable:
            return wanted
        # BFS over the prereq graph.
        visited = {wanted}
        frontier = list(_TECH_PREREQ.get(wanted, ()))
        depth = 0
        while frontier and depth < 3:
            next_frontier: list[str] = []
            for prereq in frontier:
                if prereq in visited:
                    continue
                visited.add(prereq)
                if _owned_count(snap, prereq) > 0:
                    # Already have it; no need to (re)build.
                    continue
                if prereq in buildable:
                    return prereq
                next_frontier.extend(_TECH_PREREQ.get(prereq, ()))
            frontier = next_frontier
            depth += 1
        return None


# ---------------------------------------------------------------------------
# R3 EconomyScaler
# ---------------------------------------------------------------------------

class EconomyScaler(_ReactorBase):
    """Sustained income engine.

    Five sub-rules, evaluated each tick (with per-key throttle):

      a) **2nd refinery** — when only 1 proc and cash >= 2500.
      b) **Harvester top-up** — keep `harv-count == 2 * proc-count`.
      c) **Silo** — when stored ore exceeds SILO_RES_RATIO of cap, drop
         a silo so future harvester returns aren't wasted.
      d) **Power preventive** — when power_excess < POWR_EXCESS_FLOOR
         build a powr (apwr if Allied + atek up). Cheaper than recovering
         from a Low-Power state mid-fight.
      e) **Harvester reactive** — also subscribes to ActorDied(type=harv)
         and immediately submits a replacement (cap 1 in flight).
      f) **Float-cash multi-prod** — when cash > FLOAT_CASH_FLOOR, build
         another RAX (cap RAX_CAP), then another WF (cap WF_CAP). Pro
         RA1 rule: never sit on >$2000.

    Order matters: refinery first, then silo (chains naturally with new
    proc), then power, then harv top-up. We "return" after each fired
    intent so we never spam two econ tasks in one tick.
    """

    PROC_TARGET = 2
    PROC_CASH_FLOOR = 2500
    HARV_PER_PROC = 2

    def __init__(self, bus, add_task, tasks_provider, is_build_order_active,
                 is_master_enabled=lambda: True):
        super().__init__("economy_scaler", bus, add_task, tasks_provider,
                         is_build_order_active, is_master_enabled)
        bus.subscribe("economy-scaler", self._on_event)

    def _on_event(self, ev: Event) -> None:
        # Reactive harvester replacement — fires even if no tick yet.
        if isinstance(ev, ActorDied) and ev.mine and ev.actor_type == "harv":
            if self._gated():
                return
            # We don't have a snap inline; rely on next TickEvent's path
            # (which will see harv_n < want and submit). To bias toward
            # speed we just clear our throttle for harv so the next tick
            # fires immediately.
            self._last_submit.pop("econ:harv", None)
            log.info("EconomyScaler: harv #%d died — fast-path replacement armed",
                     ev.actor_id)
            return

        if self._gated():
            return
        if not isinstance(ev, TickEvent) or ev.snapshot is None:
            return
        snap = ev.snapshot
        if snap.self_state is None:
            return
        ss = snap.self_state
        cash = ss.cash
        tick = snap.tick
        buildable = _buildable_set(snap)

        proc_n = _owned_count(snap, "proc")
        harv_n = _owned_count(snap, "harv")

        # --- (a) second refinery ---
        if (proc_n < self.PROC_TARGET
                and "proc" in buildable
                and cash >= self.PROC_CASH_FLOOR
                and not _queue_inflight(snap, "proc")
                and not _live_intent_active(self._tasks_provider, "econ build:proc")
                and not self._throttled("econ:proc", tick, SUBMIT_FLOOR_TICKS)):
            self._submit("econ build:proc",
                         [{"kind": "action", "verb": "produce",
                           "params": {"item": "proc", "count": 1}}])
            self._record("econ:proc", tick)
            return

        # --- (c) silo before ore overflows ---
        cap = max(ss.resource_cap, 1)
        ratio = ss.resources / cap
        silo_n = _owned_count(snap, "silo")
        if (ratio >= SILO_RES_RATIO
                and silo_n < 2                  # cap silos; spam wastes cash
                and "silo" in buildable
                and not _queue_inflight(snap, "silo")
                and not _live_intent_active(self._tasks_provider, "econ build:silo")
                and not self._throttled("econ:silo", tick, SUBMIT_FLOOR_TICKS)):
            self._submit("econ build:silo",
                         [{"kind": "action", "verb": "produce",
                           "params": {"item": "silo", "count": 1}}])
            self._record("econ:silo", tick)
            return

        # --- (d) power preventive ---
        # Choose apwr if buildable (Soviet Tesla tech, Allied advanced),
        # else fall back to powr.
        powr_item = "apwr" if "apwr" in buildable else (
            "powr" if "powr" in buildable else None)
        if (powr_item is not None
                and ss.power_excess < POWR_EXCESS_FLOOR
                and not _queue_inflight(snap, powr_item)
                and not _live_intent_active(self._tasks_provider,
                                            f"econ build:{powr_item}")
                and not self._throttled(f"econ:{powr_item}", tick,
                                        SUBMIT_FLOOR_TICKS)):
            self._submit(f"econ build:{powr_item}",
                         [{"kind": "action", "verb": "produce",
                           "params": {"item": powr_item, "count": 1}}])
            self._record(f"econ:{powr_item}", tick)
            return

        # --- (b) harvester top-up ---
        want = max(self.HARV_PER_PROC * proc_n, 0)
        if (proc_n >= 1
                and harv_n < want
                and "harv" in buildable
                and not _queue_inflight(snap, "harv")
                and not _live_intent_active(self._tasks_provider, "econ build:harv")
                and not self._throttled("econ:harv", tick, SUBMIT_FLOOR_TICKS)):
            self._submit("econ build:harv",
                         [{"kind": "action", "verb": "produce",
                           "params": {"item": "harv", "count": 1}}])
            self._record("econ:harv", tick)
            return

        # --- (f) floating-cash multi-production ---
        # When we're sitting on >$2000 the cash is wasted. Convert it
        # into more RAX (cap RAX_CAP) and a 2nd WF (cap WF_CAP) so we
        # actually spend the income on units. RAX is preferred over WF
        # because infantry is the damage dealer (Orb's rule).
        if cash > FLOAT_CASH_FLOOR:
            faction = _detect_faction(snap)
            rax_item = "barr" if faction == "soviet" else "tent"
            rax_n = _owned_count(snap, rax_item)
            if (rax_item in buildable
                    and rax_n < RAX_CAP
                    and not _queue_inflight(snap, rax_item)
                    and not _live_intent_active(self._tasks_provider,
                                                f"econ build:{rax_item}")
                    and not self._throttled(f"econ:{rax_item}", tick,
                                            SUBMIT_FLOOR_TICKS)):
                self._submit(f"econ build:{rax_item}",
                             [{"kind": "action", "verb": "produce",
                               "params": {"item": rax_item, "count": 1}}])
                self._record(f"econ:{rax_item}", tick)
                return
            wf_n = _owned_count(snap, "weap")
            if ("weap" in buildable
                    and wf_n < WF_CAP
                    and not _queue_inflight(snap, "weap")
                    and not _live_intent_active(self._tasks_provider,
                                                "econ build:weap")
                    and not self._throttled("econ:weap", tick,
                                            SUBMIT_FLOOR_TICKS)):
                self._submit("econ build:weap",
                             [{"kind": "action", "verb": "produce",
                               "params": {"item": "weap", "count": 1}}])
                self._record("econ:weap", tick)
                return


# ---------------------------------------------------------------------------
# R4 ArmyCommander
# ---------------------------------------------------------------------------

class ArmyCommander:
    """Mass + push; counter-attack on UnderAttack (no retreat).

    Replaces the old "RECALL to base" logic which caused a permanent
    retreat loop. Now: when something of ours is hit, we send the army
    to the victim's tile (the attacker is necessarily within weapon range
    of the victim, so attack-move there engages them). Inspired by
    OpenRA's ProtectOwn squad behaviour (SquadManagerBotModule.cs:497).

    Bypasses the daemon Task path on purpose — issues N single-actor
    attack-move calls back-to-back through the locked OpenRAClient.
    Wrapping each call in a Task would explode the daemon queue and
    double round-trip latency for no benefit; orders are idempotent.
    """

    def __init__(self,
                 bus: EventBus,
                 client,
                 is_build_order_active: Callable[[], bool],
                 is_master_enabled: Callable[[], bool] = lambda: True,
                 plan_provider=None):
        self.bus = bus
        self.client = client
        self._bo_active = is_build_order_active
        self._master_enabled = is_master_enabled
        self._plan_provider = plan_provider or (lambda _t: None)
        self._lock = threading.RLock()
        self._last_push_tick = -10**9
        self._last_push_size = 0
        self._last_recall_wall = 0.0
        self._recall_pending = False
        self._counter_victim_id = 0
        self._last_target: Optional[tuple[int, int]] = None
        # Failure memory: each entry (target_xy, wall_time).
        self._avoid: list[tuple[tuple[int, int], float]] = []
        # Number of consecutive pushes that ended in RECALL — bumps mass req.
        self._fail_streak = 0
        bus.subscribe("army-commander", self._on_event)

    def _on_event(self, ev: Event) -> None:
        if (not self._master_enabled()) or self._bo_active():
            # Still consume any pending recall once BO ends; for now drop.
            return
        if isinstance(ev, UnderAttack):
            self._maybe_recall(ev)
            return
        if not isinstance(ev, TickEvent) or ev.snapshot is None:
            return
        # Consume any recall request first; if we recalled this tick, skip
        # push so we don't immediately turn around.
        if self._recall_pending:
            self._consume_recall(ev.snapshot)
            return
        self._maybe_push(ev.snapshot)

    # --- Push -----------------------------------------------------------------
    def _maybe_push(self, snap: Snapshot) -> None:
        if snap.tick - self._last_push_tick < ARMY_PUSH_COOLDOWN_TICKS:
            return
        combat = [a for a in snap.mine()
                  if a.type in COMBAT_TYPES and a.idle]
        # Escalating mass threshold: each failed push (RECALL) adds bump.
        threshold = min(
            ARMY_MASS_THRESHOLD + ARMY_MASS_FAIL_BUMP * self._fail_streak,
            ARMY_MASS_CAP,
        )
        if len(combat) < threshold:
            return

        # If the previous push survived to here without a RECALL clearing
        # _last_target, treat it as successful and decay the fail streak.
        if self._last_target is not None and self._fail_streak > 0:
            self._fail_streak = max(self._fail_streak - 1, 0)
            log.info("ArmyCommander prev push survived; fail_streak -> %d",
                     self._fail_streak)

        target = self._pick_target(snap)
        if target is None:
            return
        cx, cy = target
        log.info("ArmyCommander pushing %d units to (%d,%d) "
                 "[threshold=%d fail_streak=%d avoid=%d]",
                 len(combat), cx, cy, threshold, self._fail_streak,
                 len(self._avoid))
        for a in combat:
            try:
                self.client.attack_move(a.id, cx, cy)
            except Exception as e:
                log.debug("attack_move(#%d) failed: %s", a.id, e)
        self._last_push_tick = snap.tick
        self._last_push_size = len(combat)
        self._last_target = (cx, cy)

    def _pick_target(self, snap: Snapshot) -> Optional[tuple[int, int]]:
        # Trim expired avoid entries.
        wall = time.time()
        self._avoid = [(p, t) for (p, t) in self._avoid
                       if wall - t < ARMY_AVOID_RETENTION_S]
        bad_pts = [p for (p, _) in self._avoid]

        def too_close(x: int, y: int) -> bool:
            for (bx, by) in bad_pts:
                if abs(x - bx) + abs(y - by) <= ARMY_AVOID_RADIUS:
                    return True
            return False

        plan = self._plan_provider(snap.tick)

        # Plan-driven rally point — when commander says "defend" or
        # "harass", we hold the rally instead of charging deep. Push/
        # allin still run the enemy-priority loop below.
        if (plan is not None and plan.rally is not None
                and plan.aggression in ("defend", "harass")):
            rx, ry = plan.rally
            if not too_close(rx, ry):
                return (rx, ry)

        # Enemy-priority order: 'allin' inverts to hit production / CY first
        # (kill the opponent), default order hits economic backbone first.
        if plan is not None and plan.aggression == "allin":
            priority = ["fact", "weap", "afld", "barr", "tent",
                        "proc", "powr", "apwr", "tsla", "pbox",
                        "ftur", "gun", "harv", "mcv"]
        else:
            priority = ["weap", "afld", "proc", "harv",
                        "barr", "tent", "powr", "apwr",
                        "tsla", "pbox", "ftur", "gun",
                        "fact", "mcv"]
        enemies = snap.enemies()
        for typ in priority:
            for e in enemies:
                if e.type == typ and not too_close(e.x, e.y):
                    return (e.x, e.y)
        # No good targets after filter — try anything not in avoid list.
        for e in enemies:
            if not too_close(e.x, e.y):
                return (e.x, e.y)
        # All targets avoided: pick first enemy anyway (better than idle).
        if enemies:
            return (enemies[0].x, enemies[0].y)

        # Fallback: aim across the map, mirrored from base center, but
        # CLAMP to map bounds (mirror used to yield negatives when base
        # was near the far edge → 16 units would walk off-map).
        if snap.map and snap.map.base_center:
            bx, by = snap.map.base_center
            mw, mh = snap.map.width, snap.map.height
            tx = max(2, min(mw - 3, mw - bx))
            ty = max(2, min(mh - 3, mh - by))
            # If mirror collapses to ~base (we ARE near map center), aim
            # at the geometric center instead so we at least move out.
            if abs(tx - bx) + abs(ty - by) < 8:
                tx, ty = mw // 2, mh // 2
            if not too_close(tx, ty):
                return (tx, ty)
        # Final fallback: map center.
        if snap.map:
            return (snap.map.width // 2, snap.map.height // 2)
        return None

    # --- Counter-attack -------------------------------------------------------
    # Old behaviour ("RECALL → walk all combat units back to base center")
    # caused a permanent retreat loop: enemy poke → 64 RECALLs → army
    # vacationed at base while base burned. New behaviour follows OpenRA's
    # ProtectOwn (SquadManagerBotModule.cs:497-540): when one of our
    # actors is attacked, send the army to the *victim's* tile. The
    # attacker is necessarily within weapon range of the victim, so
    # attack-move there will engage them. Never run away.
    def _maybe_recall(self, ev: UnderAttack) -> None:
        wall = time.time()
        with self._lock:
            if wall - self._last_recall_wall < COUNTER_ATTACK_COOLDOWN_S:
                return
            self._last_recall_wall = wall
            self._recall_pending = True
            self._counter_victim_id = int(ev.actor_id)
        log.info("ArmyCommander COUNTER armed by hit on #%d (%s)",
                 ev.actor_id, ev.actor_type)

    def _consume_recall(self, snap: Snapshot) -> None:
        self._recall_pending = False
        victim_id = getattr(self, "_counter_victim_id", 0)
        # Find the victim in the snapshot to get its location.
        victim_xy: Optional[tuple[int, int]] = None
        for a in snap.mine():
            if a.id == victim_id:
                victim_xy = (a.x, a.y)
                break
        if victim_xy is None:
            # Victim already dead — fall back to nearest enemy actor to
            # base, so we at least move toward whoever's still shooting.
            base = (snap.map.base_center if snap.map and snap.map.base_center
                    else None)
            if base is None:
                return
            bx, by = base
            best = None
            best_d = 10**9
            for e in snap.enemies():
                d = abs(e.x - bx) + abs(e.y - by)
                if d < best_d:
                    best_d = d
                    best = (e.x, e.y)
            if best is None:
                return
            victim_xy = best
        cx, cy = victim_xy
        sent = 0
        for a in snap.mine():
            if a.type not in COMBAT_TYPES:
                continue
            try:
                self.client.attack_move(a.id, cx, cy)
                sent += 1
            except Exception:
                pass
        # Reset push cooldown so a forward push doesn't trigger this tick.
        self._last_push_tick = snap.tick
        # Mark target so a follow-up RECALL doesn't blacklist this point
        # (it's our own base — blacklisting it would handicap us).
        self._last_target = None
        log.info("ArmyCommander COUNTER %d units -> (%d,%d) victim=#%d",
                 sent, cx, cy, victim_id)


# ---------------------------------------------------------------------------
# R5 Scout
# ---------------------------------------------------------------------------

class Scout:
    """Rotating dual-scout — every SCOUT_INTERVAL_TICKS (40 s) push the two
    most idle infantry to *opposite* map corners. Arms early: as soon as
    the first idle rifle exists during BO, we start scouting (no need to
    wait for OpeningComplete — Easy AI rushes).

    Why dual + opposite: single scout takes 4 cycles to map all corners
    (~3 min) and only reveals one quadrant. Two-and-opposite reveals 2
    quadrants per cycle; full map in ~80 s.

    enemy_quadrant: NW/NE/SW/SE bucket of last-seen enemies relative to
    base center, exposed for future defense-orientation logic
    (DefenseLayer can bias pbox toward the threatened quadrant).
    """

    def __init__(self,
                 bus: EventBus,
                 client,
                 is_build_order_active: Callable[[], bool],
                 add_task: Optional[Callable[[Task], None]] = None,
                 tasks_provider: Optional[Callable[[], list]] = None,
                 is_master_enabled: Callable[[], bool] = lambda: True):
        self.bus = bus
        self.client = client
        self._bo_active = is_build_order_active
        self._add_task = add_task
        self._tasks_provider = tasks_provider
        self._master_enabled = is_master_enabled
        self._armed = False
        self._first_seen_tick: Optional[int] = None
        self._corner_idx = 0
        self._last_scout_tick = -10**9
        self._last_produce_tick = -10**9
        self._sent_ids: set[int] = set()
        self.enemy_quadrant: Optional[str] = None  # 'NW'|'NE'|'SW'|'SE'
        bus.subscribe("scout", self._on_event)

    def _on_event(self, ev: Event) -> None:
        if isinstance(ev, OpeningComplete):
            self._armed = True
            return
        if not isinstance(ev, TickEvent) or ev.snapshot is None:
            return
        if not self._master_enabled():
            return
        snap = ev.snapshot
        # Early-arm: even during BO, start scouting once we've had an idle
        # rifle for ~45 s. Easy AI's first wave can hit before BO completes.
        if not self._armed:
            if self._find_scout(snap) is not None:
                if self._first_seen_tick is None:
                    self._first_seen_tick = snap.tick
                elif snap.tick - self._first_seen_tick >= SCOUT_EARLY_ARM_TICKS:
                    self._armed = True
                    log.info("Scout: early-arm during BO")
            else:
                self._first_seen_tick = None
            if not self._armed:
                return
        # Track enemy quadrant cheaply on every tick (used by defense layer
        # in future). Cost: one pass over enemies, no allocations on miss.
        self._update_enemy_quadrant(snap)
        if snap.tick - self._last_scout_tick < SCOUT_INTERVAL_TICKS:
            return

        targets = self._next_corners(snap, SCOUT_TARGET_COUNT)
        if not targets:
            return
        sent = 0
        used: set[int] = set()
        for cx, cy in targets:
            scout = self._find_scout(snap, exclude=used)
            if scout is None:
                self._maybe_produce_scout(snap)
                break
            try:
                self.client.attack_move(scout.id, cx, cy)
                log.info("Scout #%d (%s) -> corner[%d] (%d,%d) OK",
                         scout.id, scout.type,
                         (self._corner_idx + sent) % 4, cx, cy)
                used.add(scout.id)
                self._sent_ids.add(scout.id)
                sent += 1
            except Exception as e:
                log.info("Scout #%d attack_move FAILED (%d,%d): %s",
                         scout.id, cx, cy, e)
        if sent:
            self._last_scout_tick = snap.tick
            self._corner_idx = (self._corner_idx + sent) % 4

    # --- helpers ------------------------------------------------------------

    def _update_enemy_quadrant(self, snap: Snapshot) -> None:
        enemies = list(snap.enemies()) if hasattr(snap, "enemies") else []
        if not enemies:
            return
        bx_by = snap.map.base_center if snap.map and snap.map.base_center else None
        if bx_by is None:
            return
        bx, by = bx_by
        # Pick nearest enemy (most actionable threat).
        best = None
        best_d2 = 10**9
        for a in enemies:
            if a.x is None or a.y is None:
                continue
            d2 = (a.x - bx) ** 2 + (a.y - by) ** 2
            if d2 < best_d2:
                best_d2 = d2
                best = a
        if best is None:
            return
        ns = "S" if best.y >= by else "N"
        ew = "E" if best.x >= bx else "W"
        q = ns + ew
        if q != self.enemy_quadrant:
            log.info("Scout: enemy_quadrant=%s (nearest #%d %s @%d,%d base=%d,%d)",
                     q, best.id, best.type, best.x, best.y, bx, by)
            self.enemy_quadrant = q

    def _maybe_produce_scout(self, snap: Snapshot) -> None:
        if self._add_task is None or self._tasks_provider is None:
            return
        if snap.tick - self._last_produce_tick < SCOUT_PRODUCE_THROTTLE_TICKS:
            return
        faction = _detect_faction(snap)
        item = SOVIET_SCOUT if faction == "soviet" else ALLIED_SCOUT
        buildable = _buildable_set(snap)
        if item not in buildable:
            # Fall back to rifle infantry (cheap, both factions).
            if "e1" in buildable:
                item = "e1"
            else:
                return
        if _queue_inflight(snap, item):
            return
        if _live_intent_active(self._tasks_provider, f"scout build:{item}"):
            return
        task = Task.new(intent=f"scout build:{item}",
                        steps=[{"kind": "action", "verb": "produce",
                                "params": {"item": item, "count": 1}}],
                        utterance="<scout>")
        self._add_task(task)
        self._last_produce_tick = snap.tick
        log.info("Scout: no idle scout — producing %s (task=%s)",
                 item, task.id)

    @staticmethod
    def _find_scout(snap: Snapshot, exclude: Optional[set[int]] = None) -> Optional[Actor]:
        # Prefer dog (Soviet) > jeep (Allied) > any rifle infantry.
        ex = exclude or set()
        for t in ("dog", "jeep", "e1"):
            for a in snap.mine():
                if a.type == t and a.idle and a.id not in ex:
                    return a
        return None

    def _next_corners(self, snap: Snapshot, n: int) -> list[tuple[int, int]]:
        if not snap.map:
            return []
        mw, mh = snap.map.width, snap.map.height
        corners = [(0, 0), (mw - 1, 0), (mw - 1, mh - 1), (0, mh - 1)]
        # Send to opposite corners (idx, idx+2) for max map coverage.
        out = []
        for i in range(n):
            out.append(corners[(self._corner_idx + i * 2) % 4])
        return out


# ---------------------------------------------------------------------------
# Smoke
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from openra_client import Actor, MapInfo, Queue, SelfState

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    submitted: list[Task] = []
    bus = EventBus()

    class FakeClient:
        def __init__(self):
            self.calls: list[str] = []

        def attack_move(self, actor_id, x, y, queued=False):
            self.calls.append(f"am:{actor_id}:{x},{y}")
            return {"ok": True}

    fc = FakeClient()
    bo_active_flag = [True]

    ArmyProducer(bus, submitted.append, lambda: list(submitted),
                 lambda: bo_active_flag[0])
    DefenseLayer(bus, submitted.append, lambda: list(submitted),
                 lambda: bo_active_flag[0])
    EconomyScaler(bus, submitted.append, lambda: list(submitted),
                  lambda: bo_active_flag[0])
    ArmyCommander(bus, fc, lambda: bo_active_flag[0])
    Scout(bus, fc, lambda: bo_active_flag[0],
          add_task=submitted.append,
          tasks_provider=lambda: list(submitted))

    # Stage 0: BO active → no submissions.
    snap = Snapshot(
        tick=1000, local_player="Multi0",
        self_state=SelfState(
            cash=5000, resources=0, resource_cap=0, spendable=5000,
            power_provided=200, power_drained=100, power_excess=100,
            power_state="Normal",
            queues=[
                Queue(type="Vehicle", host_actor=2, host_type="player",
                      buildable=["3tnk", "harv"], current=None, queued=[]),
                Queue(type="Infantry", host_actor=2, host_type="player",
                      buildable=["e1"], current=None, queued=[]),
                Queue(type="Defense", host_actor=2, host_type="player",
                      buildable=["tsla"], current=None, queued=[]),
                Queue(type="Building", host_actor=2, host_type="player",
                      buildable=["proc", "powr"], current=None, queued=[]),
            ],
        ),
        actors=[
            Actor(id=2, type="fact", owner="Multi0", mine=True,
                  x=10, y=10, hp=1000, max_hp=1000, idle=True,
                  stance=None, queues=[]),
            Actor(id=3, type="weap", owner="Multi0", mine=True,
                  x=12, y=10, hp=1000, max_hp=1000, idle=True,
                  stance=None, queues=[]),
            Actor(id=4, type="proc", owner="Multi0", mine=True,
                  x=14, y=10, hp=1000, max_hp=1000, idle=True,
                  stance=None, queues=[]),
            Actor(id=5, type="harv", owner="Multi0", mine=True,
                  x=16, y=10, hp=1000, max_hp=1000, idle=True,
                  stance=None, queues=[]),
        ],
        map=MapInfo(width=64, height=64, tileset="TEMPERATE",
                    base_center=(10, 10)),
    )
    bus.emit(TickEvent(tick=snap.tick, snapshot=snap))
    time.sleep(0.2)
    assert not submitted, f"BO active should suppress all reactors, got {[t.intent for t in submitted]}"
    print("[OK] BO active gate works")

    # Stage 1: BO done → Army/Defense/Econ all fire.
    bo_active_flag[0] = False
    snap.tick = 2000
    bus.emit(TickEvent(tick=snap.tick, snapshot=snap))
    time.sleep(0.3)
    intents = sorted(t.intent for t in submitted)
    assert "army build:3tnk" in intents, intents
    assert "army build:e1" in intents, intents
    assert "defense build:tsla" in intents, intents
    assert "econ build:proc" in intents, intents
    print("[OK] Stage 1 reactors fired:", intents)

    # Stage 2: throttle — same tick again should NOT duplicate any
    # already-submitted intent. (harv may legitimately fire now since
    # it's a different key from proc; we only assert no duplicates.)
    before = sorted(t.intent for t in submitted)
    bus.emit(TickEvent(tick=snap.tick, snapshot=snap))
    time.sleep(0.2)
    after = sorted(t.intent for t in submitted)
    duplicates = [i for i in before if after.count(i) > before.count(i)]
    assert not duplicates, f"throttle broken: duplicates={duplicates}"
    print("[OK] no duplicate submissions; total intents now:", after)

    # Stage 3: army push — 8 idle 3tnks → fire attack-move on all.
    snap.tick = 5000  # past throttle
    snap.actors = list(snap.actors) + [
        Actor(id=100 + i, type="3tnk", owner="Multi0", mine=True,
              x=10, y=10, hp=300, max_hp=300, idle=True,
              stance="Defend", queues=[]) for i in range(8)
    ] + [
        Actor(id=999, type="powr", owner="Enemy", mine=False,
              x=50, y=50, hp=500, max_hp=500, idle=True,
              stance=None, queues=[]),
    ]
    bus.emit(TickEvent(tick=snap.tick, snapshot=snap))
    time.sleep(0.3)
    assert sum(1 for c in fc.calls if c.startswith("am:")) >= 8, fc.calls
    print("[OK] ArmyCommander pushed", sum(1 for c in fc.calls if c.startswith("am:")), "units")

    # Stage 4: Scout fires after OpeningComplete.
    fc.calls.clear()
    bus.emit(OpeningComplete(reason="test"))
    time.sleep(0.1)
    snap.tick = 6000
    snap.actors = list(snap.actors) + [
        Actor(id=200, type="dog", owner="Multi0", mine=True,
              x=10, y=10, hp=20, max_hp=20, idle=True,
              stance=None, queues=[]),
    ]
    bus.emit(TickEvent(tick=snap.tick, snapshot=snap))
    time.sleep(0.2)
    assert any(c.startswith("am:200:") for c in fc.calls), fc.calls
    print("[OK] Scout dispatched dog")

    bus.close()
    print("army_reactors smoke pass")
