"""
EventBus + event dataclasses for the event-driven architecture.

Why
---
Old polling stack had three threads (daemon 10s, build_order 5s, adviser
15s) each ticking independently. Latency stacked: a finished build could
sit Done for 5-15s before anyone reacted, which fed cascade failures
(queue blocked → build_order stalled → adviser kept advising "build
power plant" → autopilot fired anyway → wasted ore + queue spam).

New model: ONE thread (SnapshotPump) reads the game at 1Hz, computes
diffs, and emits typed events. Reactors / daemon / build_order / adviser
are all subscribers. Latency drops to <1s, LLM calls only fire on
real-world transitions, and recovery logic gets a single source of truth.

Bus design
----------
* Per-subscriber thread + Queue. `emit()` is non-blocking (puts on every
  subscriber queue). A slow subscriber can never stall the pump.
* Dropping events on a full queue is preferred over blocking — except for
  TickEvent, which is the heartbeat (we always want the latest).
* Each subscriber sees events in emit order. No global ordering across
  subscribers (and we don't need it).
* `unsubscribe` joins cleanly; `close` joins all.

Event taxonomy (only what we actually use today)
------------------------------------------------
TickEvent              — 1Hz heartbeat carrying the latest snapshot.
ConnectedEvent         — pump (re)connected.
DisconnectedEvent      — pump lost the socket.
ActorSpawned           — friendly or enemy actor first seen.
ActorDied              — previously-seen actor gone (or HP→0).
QueueItemStarted       — queue.current.item changed to non-None.
QueueItemDone          — queue.current.done flipped True.
QueueIdle              — queue went current=None with empty queued list.
PowerStateChanged      — Normal/Low/Critical transition.
EnemySpotted           — first time an enemy actor enters our snapshot.
UnderAttack            — friendly actor HP dropped fast (Δhp ≥ threshold).
OpeningComplete        — BuildOrderRunner declares the opening done.
EconomyIdle            — proc + at least 1 harv exist, queues all idle, no inflight tasks.
TaskStuck              — Recovery reactor: a task is past its recovery deadline.

We deliberately don't fire fine-grained "BuildingPlaced" / "PowerUp" /
etc. — the diff-based ones above cover them. Add new types only when a
reactor genuinely can't be written against the existing set.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .openra_client import Snapshot

log = logging.getLogger("vibera.events")


# --- Event types -----------------------------------------------------------


@dataclass
class Event:
    """Base. `ts` = wall-clock seconds; `tick` = engine tick (or -1 if
    pre-snapshot, e.g. ConnectedEvent on first attempt)."""
    ts: float = field(default_factory=time.time)
    tick: int = -1


@dataclass
class TickEvent(Event):
    """1Hz heartbeat. Always carries the latest snapshot. Subscribers that
    need 'current state' work off this. Fires before any diff events
    derived from the same snapshot, so a subscriber that consumes both
    sees TickEvent → diffs in order."""
    snapshot: Optional[Snapshot] = None


@dataclass
class ConnectedEvent(Event):
    pass


@dataclass
class DisconnectedEvent(Event):
    reason: str = ""


@dataclass
class ActorSpawned(Event):
    actor_id: int = 0
    actor_type: str = ""
    mine: bool = False
    owner: Optional[str] = None


@dataclass
class ActorDied(Event):
    actor_id: int = 0
    actor_type: str = ""
    mine: bool = False


@dataclass
class QueueItemStarted(Event):
    queue_type: str = ""        # "Building" | "Defense" | "Infantry" | ...
    item: str = ""
    host_actor: int = 0


@dataclass
class QueueItemDone(Event):
    queue_type: str = ""
    item: str = ""
    host_actor: int = 0


@dataclass
class QueueIdle(Event):
    queue_type: str = ""
    host_actor: int = 0


@dataclass
class PowerStateChanged(Event):
    old: Optional[str] = None
    new: Optional[str] = None


@dataclass
class EnemySpotted(Event):
    actor_id: int = 0
    actor_type: str = ""
    owner: Optional[str] = None
    distance: int = -1          # cells from base center, -1 if unknown


@dataclass
class UnderAttack(Event):
    actor_id: int = 0
    actor_type: str = ""
    delta_hp: int = 0           # negative


@dataclass
class OpeningComplete(Event):
    """Emitted by BuildOrderRunner when its opening goals are satisfied
    OR by Recovery if BO has stalled too long."""
    reason: str = ""


@dataclass
class EconomyIdle(Event):
    """Proc up, at least 1 harv, all queues empty, no inflight tasks. The
    LLM should propose tech / army composition."""
    pass


@dataclass
class TaskStuck(Event):
    task_id: str = ""
    reason: str = ""


# --- Bus -------------------------------------------------------------------


# Per-subscriber bounded queue. 64 deep is plenty for 1Hz tick + occasional
# bursts; full means subscriber is misbehaving and we drop to protect the
# pump. TickEvent is treated specially: drop oldest TickEvent first.
_QUEUE_DEPTH = 64
_SHUTDOWN = object()


class _Subscriber:
    __slots__ = ("name", "fn", "q", "thread", "drops", "stop")

    def __init__(self, name: str, fn: Callable[[Event], None]):
        self.name = name
        self.fn = fn
        self.q: queue.Queue = queue.Queue(maxsize=_QUEUE_DEPTH)
        self.thread: Optional[threading.Thread] = None
        self.drops = 0
        self.stop = threading.Event()

    def start(self) -> None:
        self.thread = threading.Thread(
            target=self._run, name=f"vibera-sub-{self.name}", daemon=True)
        self.thread.start()

    def _run(self) -> None:
        while True:
            ev = self.q.get()
            if ev is _SHUTDOWN:
                return
            try:
                self.fn(ev)
            except Exception:                       # pragma: no cover
                log.exception("subscriber %r crashed on %s",
                              self.name, type(ev).__name__)

    def deliver(self, ev: Event) -> None:
        try:
            self.q.put_nowait(ev)
        except queue.Full:
            # Try once to drop a stale low-priority event to make room
            # for this one. Priority order (LOWEST first to evict):
            #   TickEvent  — heartbeat, the next one is <=1s away
            #   EnemySpotted — informational; reactors read snapshot anyway
            # Anything else (UnderAttack, QueueItemDone, ActorDied, …)
            # is preserved if at all possible.
            self.drops += 1
            if isinstance(ev, (TickEvent, EnemySpotted)):
                # Cheapest path: drop ONE of OURSELF to keep queue bounded.
                try:
                    self.q.get_nowait()
                    self.q.put_nowait(ev)
                except (queue.Empty, queue.Full):
                    pass
            else:
                # Important event — try to evict a lower-priority item.
                evicted = self._evict_low_priority_one()
                if evicted:
                    try:
                        self.q.put_nowait(ev)
                    except queue.Full:
                        pass
            log.warning("subscriber %r queue full; dropped %s (total drops=%d)",
                        self.name, type(ev).__name__, self.drops)

    def _evict_low_priority_one(self) -> bool:
        """Pop one TickEvent or EnemySpotted to free a slot. Returns
        True if anything was evicted. We have to drain into a list
        because Queue has no peek; cheap at depth 64."""
        try:
            buf = []
            evicted = False
            # Drain
            while True:
                try:
                    buf.append(self.q.get_nowait())
                except queue.Empty:
                    break
            # Repush, skipping ONE low-priority event
            for item in buf:
                if (not evicted
                        and item is not _SHUTDOWN
                        and isinstance(item, (TickEvent, EnemySpotted))):
                    evicted = True
                    continue
                try:
                    self.q.put_nowait(item)
                except queue.Full:
                    pass
            return evicted
        except Exception:
            return False

    def shutdown(self, join_timeout: float = 1.0) -> None:
        try:
            self.q.put_nowait(_SHUTDOWN)
        except queue.Full:
            # Make room.
            try:
                self.q.get_nowait()
                self.q.put_nowait(_SHUTDOWN)
            except Exception:
                pass
        if self.thread:
            self.thread.join(join_timeout)


class EventBus:
    def __init__(self) -> None:
        self._subs: list[_Subscriber] = []
        self._lock = threading.RLock()
        self._closed = False

    def subscribe(self, name: str, fn: Callable[[Event], None]) -> None:
        """Register `fn` as a subscriber. Each subscriber runs on its own
        thread, so heavy work in `fn` is fine — it can't stall the pump.
        `name` is purely for logging / debugging."""
        sub = _Subscriber(name, fn)
        with self._lock:
            if self._closed:
                raise RuntimeError("bus is closed")
            self._subs.append(sub)
        sub.start()
        log.info("subscribed: %s", name)

    def emit(self, ev: Event) -> None:
        if self._closed:
            return
        with self._lock:
            subs = list(self._subs)
        for s in subs:
            s.deliver(ev)

    def close(self, join_timeout: float = 2.0) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            subs = list(self._subs)
            self._subs.clear()
        for s in subs:
            s.shutdown(join_timeout)


# --- Smoke -----------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    bus = EventBus()
    received: list[str] = []
    lock = threading.Lock()

    def handler(name: str):
        def _h(ev: Event) -> None:
            with lock:
                received.append(f"{name}:{type(ev).__name__}")
            time.sleep(0.01)  # simulate work
        return _h

    bus.subscribe("a", handler("a"))
    bus.subscribe("b", handler("b"))

    bus.emit(TickEvent(tick=1))
    bus.emit(QueueItemDone(tick=1, queue_type="Building", item="powr"))
    bus.emit(ActorSpawned(tick=2, actor_id=42, actor_type="powr", mine=True))

    time.sleep(0.2)
    bus.close()
    print("received:", received)
    assert any("a:TickEvent" in r for r in received)
    assert any("b:QueueItemDone" in r for r in received)
    assert any("a:ActorSpawned" in r for r in received)
    print("[OK] EventBus smoke pass")
