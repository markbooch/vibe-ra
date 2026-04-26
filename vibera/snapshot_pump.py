"""
SnapshotPump — sole owner of the OpenRA socket; 1Hz tick + diff → events.

Responsibilities
----------------
1. Maintain the single OpenRAClient instance (other components borrow it
   via `pump.client`, all calls go through OpenRAClient._call_lock).
2. Tick at SNAPSHOT_HZ (default 1Hz). On each tick:
      a. snapshot()
      b. emit TickEvent(snapshot) first — synchronous "heartbeat".
      c. diff against previous snapshot → emit transition events:
         ActorSpawned / ActorDied / QueueItemStarted / QueueItemDone /
         QueueIdle / PowerStateChanged / EnemySpotted / UnderAttack
3. Reconnect on connection loss; emit Disconnected then Connected.
4. Expose `last_snapshot()` for sync consumers (UI status bar, etc.) and
   `client` for command dispatchers.

Design notes
------------
* Diff is keyed by actor id and (queue_type, host_actor). Queue
  identification: an actor may host multiple queues (e.g. RA1 PlayerActor
  holds Building/Defense/Infantry/Vehicle); diffing per-(host, type) is
  enough.
* QueueItemDone fires the moment we observe `done=True`. We don't fire it
  again for the same item until the queue's `current` becomes a different
  item. This avoids floods while a Done item waits for placement.
* UnderAttack threshold = 30hp single-tick drop on a friendly. We
  deliberately avoid integrating over time — burst damage matters more
  than chip damage for triggering the LLM.
* EnemySpotted fires once per enemy actor id, ever. The C# layer
  fog-filters non-friendly actors via `LocalPlayer.Shroud.IsVisible`, so
  enemies in the snapshot are only those the player can currently see.
  When an enemy goes back under fog it disappears from the snapshot —
  but we deliberately do NOT emit ActorDied for it (we can't tell
  out-of-vision from dead). Re-entries are also silent so the adviser
  isn't paged on every patrol pass.
* ActorSpawned fires for every new friendly id, and for the first
  sighting of each enemy id (paired with EnemySpotted at that moment).
* ActorDied fires for friendlies only.
* TickEvent is always emitted; diff events are best-effort. If diff
  computation crashes, we still emit the TickEvent — subscribers reliant
  on snapshot stay alive.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from typing import Optional

from .events import (
    ActorDied, ActorSpawned, ConnectedEvent, DisconnectedEvent, EnemySpotted,
    EventBus, PowerStateChanged, QueueIdle, QueueItemDone, QueueItemStarted,
    TickEvent, UnderAttack,
)
from .openra_client import OpenRAClient, Snapshot

log = logging.getLogger("vibera.pump")

SNAPSHOT_HZ = 1.0                     # one snapshot/sec
RECONNECT_BACKOFF = 2.0               # seconds between reconnect attempts
UNDER_ATTACK_HP_DROP = 30             # min Δhp to trigger UnderAttack
ENEMY_SPOTTED_RADIUS = 60             # distance hint cap (cells)


class SnapshotPump:
    def __init__(self,
                 bus: EventBus,
                 host: str = "127.0.0.1",
                 port: int = 7778,
                 hz: float = SNAPSHOT_HZ):
        self.bus = bus
        self.host = host
        self.port = port
        self.period = 1.0 / max(0.1, hz)
        self.client = OpenRAClient(host=host, port=port, timeout=5.0)

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._connected = False

        # Diff state (only the pump thread reads/writes these).
        self._prev: Optional[Snapshot] = None
        self._prev_actor_hp: dict[int, int] = {}
        self._prev_queue_current: dict[tuple[str, int], Optional[str]] = {}
        # Once we've fired QueueItemDone for (host, type, item) we don't
        # re-fire while item is still the current one.
        self._done_emitted: set[tuple[str, int, str]] = set()
        # Friendlies — reliable lifecycle: spawn when first seen, die when
        # they leave the snapshot. Engine never hides our own units from us.
        self._known_friendly_ids: set[int] = set()
        # Enemies — fog-filtered. We can't tell "out of vision" from "dead",
        # so we don't emit ActorDied for enemies. ActorSpawned fires once
        # ever (first time spotted); subsequent re-entries are silent so
        # the adviser isn't paged every time a scout re-discovers the
        # same patrol path.
        self._spotted_enemy_ids: set[int] = set()
        self._prev_power_state: Optional[str] = None

        # Last snapshot pointer for sync consumers.
        self._last_lock = threading.Lock()
        self._last_snapshot: Optional[Snapshot] = None
        self._last_error: Optional[str] = None

    # --- Lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="vibera-snapshot-pump", daemon=True)
        self._thread.start()
        log.info("SnapshotPump started @ %.1fHz", 1.0 / self.period)

    def stop(self, join_timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(join_timeout)
        try:
            self.client.close()
        except Exception:
            pass

    # --- Sync accessors ----------------------------------------------------

    def last_snapshot(self) -> Optional[Snapshot]:
        with self._last_lock:
            return self._last_snapshot

    def last_error(self) -> Optional[str]:
        with self._last_lock:
            return self._last_error

    # --- Loop --------------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            t0 = time.time()
            try:
                self._tick_once()
            except Exception as e:                  # pragma: no cover
                log.exception("pump tick crashed: %s", e)
                with self._last_lock:
                    self._last_error = f"crash: {e}"
            elapsed = time.time() - t0
            self._stop.wait(max(0.0, self.period - elapsed))

    def _tick_once(self) -> None:
        if not self._connected:
            self._try_connect()
            if not self._connected:
                self._stop.wait(RECONNECT_BACKOFF - self.period
                                if RECONNECT_BACKOFF > self.period else 0)
                return

        try:
            snap = self.client.snapshot()
        except Exception as e:
            with self._last_lock:
                self._last_error = f"snapshot failed: {e}"
            log.warning("snapshot failed: %s; dropping connection", e)
            self._drop_connection(reason=str(e))
            return

        with self._last_lock:
            self._last_snapshot = snap
            self._last_error = None

        # Heartbeat first so any subscriber that wants "the latest world"
        # sees it before per-event reactors do.
        self.bus.emit(TickEvent(tick=snap.tick, snapshot=snap))

        try:
            self._emit_diffs(snap)
        except Exception:                           # pragma: no cover
            log.exception("diff computation failed at tick %d", snap.tick)

        self._prev = snap

    # --- Connection mgmt ---------------------------------------------------

    def _try_connect(self) -> None:
        try:
            # Reset the client (rebuild socket).
            try:
                self.client.close()
            except Exception:
                pass
            self.client.connect()
            if not self.client.ping():
                raise RuntimeError("ping failed after connect")
            self._connected = True
            self._reset_diff_state()
            self.bus.emit(ConnectedEvent())
            log.info("connected to %s:%d", self.host, self.port)
        except Exception as e:
            with self._last_lock:
                self._last_error = f"connect failed: {e}"
            log.warning("connect failed: %s", e)

    def _drop_connection(self, reason: str = "") -> None:
        was_connected = self._connected
        self._connected = False
        try:
            self.client.close()
        except Exception:
            pass
        if was_connected:
            self.bus.emit(DisconnectedEvent(reason=reason))

    def _reset_diff_state(self) -> None:
        self._prev = None
        self._prev_actor_hp.clear()
        self._prev_queue_current.clear()
        self._done_emitted.clear()
        self._known_friendly_ids.clear()
        self._spotted_enemy_ids.clear()
        self._prev_power_state = None

    # --- Diffing -----------------------------------------------------------

    def _emit_diffs(self, snap: Snapshot) -> None:
        tick = snap.tick

        # --- Actor lifecycle (friendlies are reliable; enemies are fog-gated) ---
        cur_friendly_ids = {a.id for a in snap.actors if a.mine}

        # Friendlies: classic spawn/die diff.
        for a in snap.actors:
            if not a.mine:
                continue
            if a.id not in self._known_friendly_ids:
                self.bus.emit(ActorSpawned(
                    tick=tick, actor_id=a.id, actor_type=a.type,
                    mine=True, owner=a.owner))
        for prev_id in self._known_friendly_ids - cur_friendly_ids:
            prev_a = self._lookup_actor(self._prev, prev_id)
            self.bus.emit(ActorDied(
                tick=tick, actor_id=prev_id,
                actor_type=prev_a.type if prev_a else "",
                mine=True))
        self._known_friendly_ids = cur_friendly_ids

        # Enemies: fire ActorSpawned + EnemySpotted exactly once per id, ever.
        # We do NOT emit ActorDied for enemies — losing them from the
        # snapshot could mean fog, retreat, or actual death; we can't tell.
        # Adviser triggers on EnemySpotted (a transition we can be sure of).
        # _spotted_enemy_ids never shrinks within a game.

        # --- Under-attack on friendlies ---
        for a in snap.actors:
            if not a.mine:
                continue
            prev_hp = self._prev_actor_hp.get(a.id)
            if prev_hp is not None and a.hp < prev_hp:
                drop = prev_hp - a.hp
                if drop >= UNDER_ATTACK_HP_DROP:
                    self.bus.emit(UnderAttack(
                        tick=tick, actor_id=a.id, actor_type=a.type,
                        delta_hp=-drop))
            self._prev_actor_hp[a.id] = a.hp
        # GC dead actors out of hp map
        for dead_id in list(self._prev_actor_hp.keys()):
            if dead_id not in cur_friendly_ids:
                self._prev_actor_hp.pop(dead_id, None)

        # --- Enemy spotted (first time we see an enemy actor) ---
        # Per-tick coalescing: when fog lifts on a player scroll we can
        # discover dozens of enemies in one snapshot. Adviser only needs
        # ONE EnemySpotted per unique type per tick (the strategic signal
        # is "rocket infantry is here", not "rocket infantry #482, #483,
        # #484…"). Reactors that need full counts read the snapshot
        # directly. This caps the per-tick burst at ~unique-types and
        # stops the bus queue from overflowing.
        base_center = snap.map.base_center if snap.map else None
        spotted_types_this_tick: set[str] = set()
        for a in snap.enemies():
            if a.id in self._spotted_enemy_ids:
                continue
            self._spotted_enemy_ids.add(a.id)
            # ActorSpawned remains per-id (cheap, low volume).
            self.bus.emit(ActorSpawned(
                tick=tick, actor_id=a.id, actor_type=a.type,
                mine=False, owner=a.owner))
            if a.type in spotted_types_this_tick:
                continue
            spotted_types_this_tick.add(a.type)
            dist = -1
            if base_center is not None:
                dx = a.x - base_center[0]
                dy = a.y - base_center[1]
                dist = int(math.sqrt(dx*dx + dy*dy))
            self.bus.emit(EnemySpotted(
                tick=tick, actor_id=a.id, actor_type=a.type,
                owner=a.owner, distance=dist))

        # --- Power state ---
        if snap.self_state is not None:
            ps = snap.self_state.power_state
            if ps != self._prev_power_state:
                self.bus.emit(PowerStateChanged(
                    tick=tick, old=self._prev_power_state, new=ps))
                self._prev_power_state = ps

            # --- Queues ---
            for q in snap.self_state.queues:
                key = (q.type, q.host_actor)
                cur_item = q.current.item if q.current else None
                cur_done = bool(q.current and q.current.done)
                prev_item = self._prev_queue_current.get(key)

                if cur_item != prev_item:
                    # Started a new item (or transition from None to X)
                    if cur_item is not None:
                        self.bus.emit(QueueItemStarted(
                            tick=tick, queue_type=q.type, item=cur_item,
                            host_actor=q.host_actor))
                        # New current item — clear the done-emitted gate
                        # for this slot (only one current item per queue).
                        self._done_emitted = {
                            k for k in self._done_emitted
                            if not (k[0] == q.type and k[1] == q.host_actor)
                        }
                    if cur_item is None:
                        # Was building → now nothing in queue → idle.
                        if not q.queued:
                            self.bus.emit(QueueIdle(
                                tick=tick, queue_type=q.type,
                                host_actor=q.host_actor))
                    self._prev_queue_current[key] = cur_item

                # QueueItemDone gate (current item flipped to done=true).
                if cur_item is not None and cur_done:
                    de_key = (q.type, q.host_actor, cur_item)
                    if de_key not in self._done_emitted:
                        self._done_emitted.add(de_key)
                        self.bus.emit(QueueItemDone(
                            tick=tick, queue_type=q.type, item=cur_item,
                            host_actor=q.host_actor))

    @staticmethod
    def _lookup_actor(snap: Optional[Snapshot], actor_id: int):
        if snap is None:
            return None
        for a in snap.actors:
            if a.id == actor_id:
                return a
        return None


if __name__ == "__main__":
    # Smoke harness — runs against live OpenRA, prints events for 30s.
    import sys
    from events import Event, EventBus

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    bus = EventBus()

    def printer(name: str):
        def _h(ev: Event) -> None:
            if isinstance(ev, TickEvent):
                return  # too noisy
            d = {k: v for k, v in ev.__dict__.items()
                 if k != "ts" and not k.startswith("_")}
            print(f"[{name}] {type(ev).__name__} {d}")
        return _h

    bus.subscribe("printer", printer("ev"))
    pump = SnapshotPump(bus)
    pump.start()
    try:
        time.sleep(30)
    except KeyboardInterrupt:
        pass
    finally:
        pump.stop()
        bus.close()
        sys.exit(0)
