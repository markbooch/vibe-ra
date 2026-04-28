"""
Rule-based reactors — zero-token, sub-second responders to game events.

Three reactors today:

* AutoPlacer
    Subscribe: QueueItemDone (queue_type in {Building, Defense})
    Action:    client.auto_place(item=...)
    Why:       Replaces the old `[produce, wait queue_item_done, auto_place]`
               three-step task. The task only needs to call `produce`; the
               engine's "Done" signal triggers placement automatically.
               Result: zero LLM tokens, <1s placement after Done.

* Recovery
    Subscribe: TickEvent
    Actions:
      a) Building queue current=Done && >RECOVER_PLACE_TICKS old → fire
         auto_place again (handles the case where AutoPlacer's first call
         raced ahead of the engine, e.g. cell occupied). Avoids today's
         "queue stuck on Done" deadlock.
      b) Daemon active task older than RECOVER_TASK_SECONDS → cancel it
         and emit TaskStuck (surface to UI; let upper layers retry).
      c) BuildOrder active for >BO_TIMEOUT_SECONDS → emit OpeningComplete
         to release the LLM brake. (BO sets done=True itself when it
         actually finishes; this is the safety net.)
      d) Disconnected for >RECOVER_RECONNECT_LOG seconds → log warning.

* StanceNudger
    Subscribe: ActorSpawned (mine=True, has stance)
    Action:    client.stance(actor_id, "Defend")
    Why:       RA1 default ReturnFire makes units passive; Defend keeps
               them shooting back when shot from behind. Used to live in
               the daemon tick loop; promoted to a reactor so it fires
               within 1s of a unit appearing instead of up to 10s.

Reactor orchestration
---------------------
All reactors take the bus + a shared OpenRAClient. Commands go through
OpenRAClient.call() which is locked — safe to dispatch from N reactor
threads + the daemon's command thread.

We deliberately keep reactors stateless across restarts: their only
in-memory state is "what we've already done this game" (e.g. nudged set,
seen-done timestamps). This is recoverable — at worst we re-nudge an
actor or re-attempt an already-placed building, both no-ops.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from .events import (
    ActorSpawned, DisconnectedEvent, Event, EventBus, OpeningComplete,
    QueueItemDone, TaskStuck, TickEvent,
)
from .openra_client import OpenRAClient, Snapshot
from . import placement

log = logging.getLogger("vibera.reactors")


# Queue types that actually need a `place` call. Vehicles / infantry / etc.
# spawn from their factory and need no placement.
PLACEABLE_QUEUE_TYPES = {"Building", "Defense"}

# Recovery thresholds
RECOVER_PLACE_TICKS = 75            # ~3s @ 25 ticks/s — re-fire auto_place
RECOVER_TASK_SECONDS = 300.0        # 5 min — cancel any active task older than this
BO_TIMEOUT_SECONDS = 600.0          # 10 min — declare opening "complete" by force.
                                    # Set high enough that a healthy economy
                                    # gets through the full template (incl.
                                    # dome at the tail). Recovery is purely
                                    # the safety net for hard stalls.
RECOVER_RECONNECT_LOG_SECONDS = 30.0


# --- AutoPlacer ------------------------------------------------------------


class AutoPlacer:
    """Place a building the moment its production finishes.

    Uses placement.SmartPlacer for anchor-aware positioning (proc near
    ore, towers toward enemy, …), with engine `auto_place` as fallback.
    Needs the latest snapshot, which it stashes from TickEvent.
    """

    def __init__(self, bus: EventBus, client: OpenRAClient,
                 is_master_enabled: Callable[[], bool] = lambda: True):
        self.bus = bus
        self.client = client
        self._enabled = is_master_enabled
        self._latest_snap: Optional[Snapshot] = None
        bus.subscribe("auto-placer", self._on_event)

    def _on_event(self, ev: Event) -> None:
        if isinstance(ev, TickEvent) and ev.snapshot is not None:
            self._latest_snap = ev.snapshot
            return
        if not isinstance(ev, QueueItemDone):
            return
        if not self._enabled():
            return
        if ev.queue_type not in PLACEABLE_QUEUE_TYPES:
            return
        item = ev.item
        snap = self._latest_snap
        try:
            if snap is not None:
                res = placement.pick(item, snap, self.client)
            else:
                res = self.client.auto_place(item=item)
        except Exception as e:
            log.warning("AutoPlacer: place(%s) failed: %s", item, e)
            return
        if isinstance(res, dict) and res.get("ok"):
            log.info("AutoPlacer placed %s @ (%s,%s)",
                     item, res.get("x"), res.get("y"))
        else:
            log.info("AutoPlacer place(%s) rejected: %s", item, res)


# --- StanceNudger ----------------------------------------------------------


class StanceNudger:
    """Switch newly-spawned friendly combat units to Defend stance."""

    def __init__(self, bus: EventBus, client: OpenRAClient,
                 is_master_enabled: Callable[[], bool] = lambda: True):
        self.bus = bus
        self.client = client
        self._enabled = is_master_enabled
        self._nudged: set[int] = set()
        self._lock = threading.Lock()
        bus.subscribe("stance-nudger", self._on_event)

    def _on_event(self, ev: Event) -> None:
        if not isinstance(ev, ActorSpawned):
            return
        if not ev.mine:
            return
        if not self._enabled():
            return
        with self._lock:
            if ev.actor_id in self._nudged:
                return

        # Best-effort: not all actor types support stance. Engine returns
        # ok=false silently if not — we only cache on success so we'll
        # retry briefly until the actor either takes the stance or dies.
        try:
            res = self.client.stance(ev.actor_id, "Defend")
        except Exception as e:
            log.debug("StanceNudger stance(#%d) failed: %s", ev.actor_id, e)
            return
        if isinstance(res, dict) and res.get("ok"):
            with self._lock:
                self._nudged.add(ev.actor_id)
            log.debug("StanceNudger #%d (%s) -> Defend",
                      ev.actor_id, ev.actor_type)


# --- Recovery --------------------------------------------------------------


class Recovery:
    """Watchdog reactor — covers gaps the other reactors can't.

    Needs a few capability hooks rather than concrete components, so we
    can wire it without circular imports:
        get_active_tasks()    -> list of Task-like objs with .id, .age_seconds()
        cancel_task(task_id)  -> bool
        is_build_order_active() -> bool
        bo_started_at         -> wall-clock time when BO started, or None
    """

    def __init__(self,
                 bus: EventBus,
                 client: OpenRAClient,
                 get_active_tasks: Callable[[], list],
                 cancel_task: Callable[[str], bool],
                 is_build_order_active: Optional[Callable[[], bool]] = None,
                 build_order_started_at: Optional[Callable[[], Optional[float]]] = None):
        self.bus = bus
        self.client = client
        self.get_active_tasks = get_active_tasks
        self.cancel_task = cancel_task
        self.is_build_order_active = is_build_order_active or (lambda: False)
        self.build_order_started_at = build_order_started_at or (lambda: None)

        # (queue_type, host_actor, item) -> first tick we saw it Done
        self._first_done_tick: dict[tuple[str, int, str], int] = {}
        # task_id -> we already asked AutoPlacer to retry; don't spam.
        self._task_cancelled: set[str] = set()
        self._opening_force_emitted = False
        self._disconnected_since: Optional[float] = None

        bus.subscribe("recovery", self._on_event)

    def _on_event(self, ev: Event) -> None:
        if isinstance(ev, DisconnectedEvent):
            self._disconnected_since = time.time()
            return
        if not isinstance(ev, TickEvent) or ev.snapshot is None:
            # Connected event resets disconnected timer
            from .events import ConnectedEvent
            if isinstance(ev, ConnectedEvent):
                self._disconnected_since = None
            return

        snap = ev.snapshot
        tick = snap.tick

        # --- (a) stale Done items in building/defense queues ---
        cur_done_keys: set[tuple[str, int, str]] = set()
        if snap.self_state is not None:
            for q in snap.self_state.queues:
                if q.type not in PLACEABLE_QUEUE_TYPES:
                    continue
                if not (q.current and q.current.done):
                    continue
                key = (q.type, q.host_actor, q.current.item)
                cur_done_keys.add(key)
                first = self._first_done_tick.setdefault(key, tick)
                age = tick - first
                if age >= RECOVER_PLACE_TICKS:
                    log.warning(
                        "Recovery: %s in %s queue stuck Done for %d ticks; "
                        "re-firing auto_place", key[2], key[0], age)
                    try:
                        self.client.auto_place(item=key[2])
                    except Exception as e:
                        log.warning("Recovery auto_place retry failed: %s", e)
                    # Reset the timer so we don't hammer every tick — give
                    # the engine RECOVER_PLACE_TICKS more to settle.
                    self._first_done_tick[key] = tick

        # GC: drop entries for items that are no longer Done.
        for k in list(self._first_done_tick.keys()):
            if k not in cur_done_keys:
                self._first_done_tick.pop(k, None)

        # --- (b) ancient active tasks ---
        try:
            tasks = self.get_active_tasks() or []
        except Exception:                   # pragma: no cover
            tasks = []
        now = time.time()
        for t in tasks:
            tid = getattr(t, "id", None)
            if not tid or tid in self._task_cancelled:
                continue
            # Try to determine task age. Task.created is iso string; fall
            # back to "no age" if we can't parse it.
            age = self._task_age_seconds(t, now)
            if age is None or age < RECOVER_TASK_SECONDS:
                continue
            log.warning("Recovery: task %s age=%.0fs > %.0fs; cancelling",
                        tid, age, RECOVER_TASK_SECONDS)
            try:
                self.cancel_task(tid)
            except Exception as e:
                log.warning("Recovery cancel %s failed: %s", tid, e)
            self._task_cancelled.add(tid)
            self.bus.emit(TaskStuck(
                tick=tick, task_id=tid,
                reason=f"age {int(age)}s exceeded recovery limit"))

        # --- (c) BuildOrder timeout ---
        if (not self._opening_force_emitted
                and self.is_build_order_active()):
            started = self.build_order_started_at()
            if started is not None and (now - started) >= BO_TIMEOUT_SECONDS:
                log.warning("Recovery: BuildOrder running for %.0fs > %.0fs; "
                            "force-emitting OpeningComplete",
                            now - started, BO_TIMEOUT_SECONDS)
                self._opening_force_emitted = True
                self.bus.emit(OpeningComplete(
                    tick=tick,
                    reason="recovery timeout — opening took too long"))

        # --- (d) reconnect monitor ---
        if self._disconnected_since is not None:
            elapsed = now - self._disconnected_since
            if elapsed >= RECOVER_RECONNECT_LOG_SECONDS:
                log.error("Recovery: still disconnected after %.0fs", elapsed)
                # Reset so we log again every interval, not every tick.
                self._disconnected_since = now

    @staticmethod
    def _task_age_seconds(task, now: float) -> Optional[float]:
        # Task.created_at is wall-clock epoch (see task.py).
        ts = getattr(task, "created_at", None)
        if ts is None:
            return None
        try:
            return now - float(ts)
        except (TypeError, ValueError):
            return None


# --- Repairer --------------------------------------------------------------


# Building type set used to filter Actor.type for repair targeting. Mirrors
# placement.FOOTPRINT keys (kept as a literal here to avoid an import cycle
# and to make it easy to tune independently).
BUILDING_TYPES = {
    "fact", "weap", "afld", "syrd", "spen", "atek", "stek", "dome",
    "proc", "powr", "apwr", "barr", "tent", "silo", "fix", "fcom",
    "hpad", "pen", "kenn", "tsla", "pbox", "ftur", "gun", "sam",
    "agun", "hbox", "gap",
}

REPAIR_LOW_RATIO = 0.50    # turn ON when HP <= 50%
REPAIR_HIGH_RATIO = 0.90   # turn OFF when HP >= 90%


class Repairer:
    """Auto-toggle building repair when HP drops below 50%, off above 90%.

    The engine's `repair` verb is a toggle (no on/off field), so we track
    intent in `_repaired_ids`. Set membership = "we asked for repair on";
    next time HP rises above the high threshold we toggle again to turn
    it off so we don't drain cash on full-HP buildings.

    Edge cases handled:
      * Building destroyed: GC the id from the set on each tick.
      * HP fluctuates around 50%: the high/low gap (40 pts) gives plenty
        of hysteresis — no flapping.
      * Repair toggle rejected (e.g. no power, full): we just retry next
        tick, which is harmless.
    """

    def __init__(self, bus: EventBus, client: OpenRAClient,
                 is_master_enabled: Callable[[], bool] = lambda: True):
        self.bus = bus
        self.client = client
        self._enabled = is_master_enabled
        self._repaired_ids: set[int] = set()
        bus.subscribe("repairer", self._on_event)

    def _on_event(self, ev: Event) -> None:
        if not isinstance(ev, TickEvent) or ev.snapshot is None:
            return
        if not self._enabled():
            return
        snap = ev.snapshot
        live_ids: set[int] = set()
        for a in snap.mine():
            if a.type not in BUILDING_TYPES:
                continue
            if a.max_hp <= 0:
                continue
            live_ids.add(a.id)
            ratio = a.hp / a.max_hp
            in_set = a.id in self._repaired_ids
            if not in_set and ratio <= REPAIR_LOW_RATIO:
                try:
                    self.client.repair(a.id)
                    self._repaired_ids.add(a.id)
                    log.info("Repairer ON #%d (%s) hp=%.0f%%",
                             a.id, a.type, ratio * 100)
                except Exception as e:
                    log.debug("Repairer toggle on #%d failed: %s", a.id, e)
            elif in_set and ratio >= REPAIR_HIGH_RATIO:
                try:
                    self.client.repair(a.id)
                    self._repaired_ids.discard(a.id)
                    log.info("Repairer OFF #%d (%s) hp=%.0f%%",
                             a.id, a.type, ratio * 100)
                except Exception as e:
                    log.debug("Repairer toggle off #%d failed: %s", a.id, e)
        # GC: drop ids no longer present (building destroyed).
        for stale in list(self._repaired_ids):
            if stale not in live_ids:
                self._repaired_ids.discard(stale)


if __name__ == "__main__":
    # Smoke: AutoPlacer fires on a synthetic QueueItemDone using a fake
    # client. No live OpenRA needed.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    class FakeClient:
        def __init__(self):
            self.calls: list[str] = []

        def auto_place(self, item: str):
            self.calls.append(item)
            return {"ok": True, "x": 50, "y": 50}

        def stance(self, actor_id: int, stance: str):
            self.calls.append(f"stance:{actor_id}:{stance}")
            return {"ok": True}

        def repair(self, building_id: int):
            self.calls.append(f"repair:{building_id}")
            return {"ok": True}

        def place(self, item, x, y, variant=0, factory_id=None):
            self.calls.append(f"place:{item}:{x},{y}")
            return {"ok": False, "error": "smoke"}

    bus = EventBus()
    fc = FakeClient()
    AutoPlacer(bus, fc)               # type: ignore[arg-type]
    StanceNudger(bus, fc)             # type: ignore[arg-type]
    Repairer(bus, fc)                 # type: ignore[arg-type]

    bus.emit(QueueItemDone(tick=10, queue_type="Building", item="powr"))
    bus.emit(QueueItemDone(tick=11, queue_type="Vehicle", item="harv"))   # ignored
    bus.emit(ActorSpawned(tick=12, actor_id=42, actor_type="e1", mine=True))
    bus.emit(ActorSpawned(tick=13, actor_id=43, actor_type="proc", mine=True))  # buildings have no stance but client returns ok in fake

    # Tick with a damaged building -> Repairer toggles ON.
    from openra_client import Actor as _A, MapInfo as _M, Snapshot as _S, SelfState as _SS
    snap = _S(
        tick=14, local_player="P",
        self_state=_SS(cash=0, resources=0, resource_cap=0, spendable=0,
                       power_provided=0, power_drained=0, power_excess=0,
                       power_state="Normal", queues=[]),
        actors=[
            _A(id=900, type="powr", owner="P", mine=True, x=0, y=0,
               hp=200, max_hp=1000, idle=True, stance=None, queues=[]),
        ],
        map=_M(width=64, height=64, tileset="T", base_center=(0, 0)),
    )
    bus.emit(TickEvent(tick=14, snapshot=snap))

    time.sleep(0.3)
    bus.close()
    print("calls:", fc.calls)
    assert "powr" in fc.calls, fc.calls
    assert "harv" not in fc.calls
    assert any("stance:42" in c for c in fc.calls)
    assert "repair:900" in fc.calls, fc.calls
    print("[OK] reactors smoke pass")
