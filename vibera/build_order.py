"""
BuildOrderRunner — deterministic RA1 opening sequence (event-driven).

Why
---
Two hours of live play with the v0.3 adviser prompt proved the LLM cannot
reliably execute a known-good economic build order. Hard-code the opening
in Python; free the LLM for tactics + voice translation.

Event-driven model
------------------
Subscribes to TickEvent + QueueItemDone + ActorSpawned.

* On TickEvent: re-evaluate the OPENING goals against the snapshot. If
  the next goal isn't being built and isn't owned, submit a 1-step
  produce task (or deploy task for the MCV). Placement is handled by
  reactors.AutoPlacer — we no longer have wait/auto_place steps in the
  task itself.
* On ActorSpawned (mine=True, type matches a goal): fast-path advance
  without waiting for the next tick (sub-second goal check after the
  building lands).
* When all goals are satisfied: emit OpeningComplete and set done=True.

Race / dedupe rules
-------------------
* `_inflight_ids` records daemon task IDs we submitted so we don't fire
  twice. Cleared as tasks become terminal.
* `_last_submit_tick` per-item floor (50 ticks ≈ 2 s).
* If the engine completes a goal via player action, the snapshot
  owned-count satisfies it naturally.

Failure
-------
Validator rejection / executor `partial` is surfaced via the daemon's
task list. We DO NOT auto-retry on a different item; we re-evaluate from
the next snapshot. If a goal permanently can't be built, the runner
stalls on it — Recovery (reactors.Recovery) will force OpeningComplete
after BO_TIMEOUT_SECONDS so the LLM takes over.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from .events import (
    ActorSpawned, Event, EventBus, OpeningComplete, QueueItemDone, TickEvent,
)
from .openra_client import Snapshot
from .task import Task

log = logging.getLogger("vibera.build_order")

MIN_RESUBMIT_TICKS = 50          # ~2 s at 25 ticks/s


@dataclass
class Goal:
    """One row in the opening sequence."""
    item: str
    target: int
    kind: str = "build"          # "build" | "deploy_mcv"


# Faction-agnostic opening. `BARRACKS` resolved at runtime to barr/tent.
OPENING_TEMPLATE: list[Goal] = [
    Goal(item="mcv",   target=0, kind="deploy_mcv"),
    Goal(item="powr",  target=1),
    Goal(item="proc",  target=1),
    Goal(item="BARRACKS", target=1),
    Goal(item="weap",  target=1),
    Goal(item="harv",  target=2),     # proc gives 1 free
    Goal(item="powr",  target=2),
    # dome (Radar Dome) unlocks medium tank (2tnk) for Allied and reveals
    # mini-map. Without it our veh queue is stuck on light tanks. Soviet
    # has the same prereq for tier-2 vehicles. Cheap (1500), gates tech.
    Goal(item="dome",  target=1),
]


class BuildOrderRunner:
    def __init__(self,
                 bus: EventBus,
                 add_task: Callable[[Task], None],
                 tasks_provider: Callable[[], list],
                 is_master_enabled: Callable[[], bool] = lambda: True):
        self.bus = bus
        self._add_task = add_task
        self._tasks_provider = tasks_provider
        self._master_enabled = is_master_enabled

        self._lock = threading.RLock()
        self._last_submit_tick: dict[str, int] = {}
        self._inflight_ids: set[str] = set()
        self._faction_barracks: Optional[str] = None
        self.done: bool = False
        self._started_at: Optional[float] = None
        self._opening_emitted = False
        # Latest snapshot stashed from TickEvent so fast-path handlers
        # (ActorSpawned / QueueItemDone) can re-evaluate without waiting
        # for the next tick. None until the first TickEvent arrives.
        self._latest_snapshot: Optional[Snapshot] = None

    # --- Lifecycle ----------------------------------------------------------

    def start(self) -> None:
        self._started_at = time.time()
        self.bus.subscribe("build-order", self._on_event)
        log.info("BuildOrderRunner subscribed to bus")

    def is_active(self) -> bool:
        return not self.done

    def started_at(self) -> Optional[float]:
        return self._started_at

    # --- Event handler ------------------------------------------------------

    def _on_event(self, ev: Event) -> None:
        # External force-complete from Recovery.
        if isinstance(ev, OpeningComplete):
            with self._lock:
                self.done = True
            return

        # Master switch off → BO is silent (but we keep listening so that
        # if the user re-enables automation mid-game we can resume).
        if not self._master_enabled():
            # Stash latest snap regardless so we can resume cleanly.
            if isinstance(ev, TickEvent) and ev.snapshot is not None:
                self._latest_snapshot = ev.snapshot
            return

        # Fast-path: a building / deployable just spawned that matches a
        # goal — re-evaluate immediately rather than waiting for next tick.
        if isinstance(ev, (ActorSpawned, QueueItemDone)) and not self.done:
            # We need a snapshot to re-evaluate; pull from the bus's
            # latest. Cheapest path: stash latest snap from TickEvent.
            snap = self._latest_snapshot
            if snap is not None:
                self._reevaluate(snap)
            return

        if not isinstance(ev, TickEvent) or ev.snapshot is None:
            return
        self._latest_snapshot = ev.snapshot
        if self.done:
            return
        self._reevaluate(ev.snapshot)

    # --- Goal walker --------------------------------------------------------

    def _reevaluate(self, snap: Snapshot) -> None:
        if snap.self_state is None:
            return

        buildable: set[str] = set()
        for q in snap.self_state.queues:
            buildable.update(q.buildable)

        if self._faction_barracks is None:
            if "barr" in buildable:
                self._faction_barracks = "barr"
            elif "tent" in buildable:
                self._faction_barracks = "tent"

        owned_count: dict[str, int] = {}
        for a in snap.mine():
            owned_count[a.type] = owned_count.get(a.type, 0) + 1

        # Reap inflight tasks that have terminated.
        live_tasks = {t.id: t for t in self._tasks_provider()}
        with self._lock:
            self._inflight_ids = {
                tid for tid in self._inflight_ids
                if tid in live_tasks and not live_tasks[tid].is_terminal
            }

        for goal in OPENING_TEMPLATE:
            target_item = self._resolve_item(goal.item)
            if target_item is None:
                continue

            if goal.kind == "deploy_mcv":
                if owned_count.get("fact", 0) >= 1:
                    continue
                mcvs = [a for a in snap.mine() if a.type == "mcv"]
                if not mcvs:
                    log.warning("BuildOrderRunner: no MCV and no ConYard — opening aborted")
                    self._mark_done("no MCV")
                    return
                last = self._last_submit_tick.get("mcv", -10**9)
                if snap.tick - last < MIN_RESUBMIT_TICKS:
                    return
                if any(t.intent == "build_order deploy:mcv"
                       for t in live_tasks.values() if not t.is_terminal):
                    return
                self._submit_deploy(mcvs[0].id, snap.tick)
                return

            # kind == "build"
            have = owned_count.get(target_item, 0)
            if have >= goal.target:
                continue

            if target_item not in buildable:
                # Prereq not satisfied yet — wait, don't skip ahead.
                return

            if self._is_being_built(snap, target_item):
                return
            if any(t.intent.endswith(f"build:{target_item}")
                   for t in live_tasks.values() if not t.is_terminal):
                return

            last = self._last_submit_tick.get(target_item, -10**9)
            if snap.tick - last < MIN_RESUBMIT_TICKS:
                return

            self._submit_build(target_item, snap.tick)
            return

        # All goals satisfied.
        log.info("BuildOrderRunner: opening complete at tick %d", snap.tick)
        self._mark_done(f"all goals met @ tick {snap.tick}")

    def _mark_done(self, reason: str) -> None:
        with self._lock:
            if self.done:
                return
            self.done = True
        if not self._opening_emitted:
            self._opening_emitted = True
            self.bus.emit(OpeningComplete(reason=reason))

    # --- Submission helpers -------------------------------------------------

    def _resolve_item(self, item: str) -> Optional[str]:
        if item == "BARRACKS":
            return self._faction_barracks
        return item

    def _is_being_built(self, snap: Snapshot, item: str) -> bool:
        if snap.self_state is None:
            return False
        for q in snap.self_state.queues:
            if q.current and q.current.item == item:
                return True
            for qi in q.queued:
                if qi.item == item:
                    return True
        return False

    def _submit_build(self, item: str, tick: int) -> None:
        # Single-step task: produce only. AutoPlacer reactor handles the
        # placement when the engine signals Done. Recovery handles the
        # rare case where AutoPlacer's call raced (e.g. cell occupied).
        task = Task.new(
            intent=f"build_order build:{item}",
            steps=[
                {"kind": "action", "verb": "produce",
                 "params": {"item": item, "count": 1}},
            ],
            utterance="<build_order>",
        )
        self._add_task(task)
        with self._lock:
            self._inflight_ids.add(task.id)
            self._last_submit_tick[item] = tick
        log.info("BuildOrderRunner submitted build:%s (task=%s)", item, task.id)

    def _submit_deploy(self, actor_id: int, tick: int) -> None:
        task = Task.new(
            intent="build_order deploy:mcv",
            steps=[
                {"kind": "action", "verb": "deploy",
                 "params": {"actor_id": int(actor_id)}},
            ],
            utterance="<build_order>",
        )
        self._add_task(task)
        with self._lock:
            self._inflight_ids.add(task.id)
            self._last_submit_tick["mcv"] = tick
        log.info("BuildOrderRunner submitted deploy MCV #%d (task=%s)",
                 actor_id, task.id)


if __name__ == "__main__":
    # Smoke harness: drive the runner against a synthetic event stream.
    from openra_client import Actor, Queue, SelfState, Snapshot

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    submitted: list[Task] = []
    bus = EventBus()
    runner = BuildOrderRunner(
        bus=bus,
        add_task=submitted.append,
        tasks_provider=lambda: [],
    )
    runner.start()

    # Stage 0: undeployed MCV, no buildables.
    snap = Snapshot(
        tick=100,
        local_player="Multi0",
        self_state=SelfState(
            cash=10000, resources=0, resource_cap=0, spendable=10000,
            power_provided=0, power_drained=0, power_excess=0,
            power_state="Normal",
            queues=[],
        ),
        actors=[
            Actor(id=1, type="mcv", owner="Multi0", mine=True,
                  x=10, y=10, hp=600, max_hp=600, idle=True,
                  stance=None, queues=[]),
        ],
    )
    bus.emit(TickEvent(tick=snap.tick, snapshot=snap))
    time.sleep(0.2)
    assert any(t.intent == "build_order deploy:mcv" for t in submitted), \
        f"expected deploy task, got {[t.intent for t in submitted]}"
    print("[OK] stage 0: deploy MCV submitted")

    # Stage 1: MCV deployed → ConYard exists, powr buildable.
    snap.actors = [
        Actor(id=2, type="fact", owner="Multi0", mine=True,
              x=10, y=10, hp=1000, max_hp=1000, idle=True,
              stance=None, queues=[]),
    ]
    snap.self_state.queues = [
        Queue(type="Building", host_actor=2, host_type="player",
              buildable=["powr", "proc", "tent", "barr"],
              current=None, queued=[]),
    ]
    snap.tick = 200
    bus.emit(TickEvent(tick=snap.tick, snapshot=snap))
    time.sleep(0.2)
    intents = [t.intent for t in submitted]
    assert "build_order build:powr" in intents, \
        f"expected powr build, got {intents}"
    print("[OK] stage 1: powr submitted")

    bus.close()
    print("smoke ok; submitted:", [t.intent for t in submitted])
