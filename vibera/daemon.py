"""
Task daemon — task executor in the event-driven architecture.

In the new model the daemon does NOT own the socket and does NOT poll.
The SnapshotPump owns the OpenRAClient and emits TickEvent at 1Hz; the
daemon subscribes and advances its task list each tick. Stance nudging
moved to reactors.StanceNudger.

Public API (thread-safe — all mutations go through `self._lock`):

    daemon.add_task(task)
    daemon.cancel_task(task_id)
    daemon.snapshot_tasks()      # -> deep copy of current task list
    daemon.last_snapshot()       # -> last Snapshot or None
    daemon.last_error()          # -> str or None

The daemon is robust to OpenRA being down — the pump simply stops emitting
TickEvents. Tasks DO NOT advance on the in-between ticks (no events
arrive; cursor sits put).
"""
from __future__ import annotations

import copy
import json
import logging
import os
import threading
from pathlib import Path
from typing import Callable, Optional

from .events import Event, EventBus, TickEvent
from .openra_client import OpenRAClient, Snapshot
from .predicates import PredicateError, evaluate
from .task import Step, Task
from .validator import validate_plan

log = logging.getLogger("vibera.daemon")

STATE_DIR = Path.home() / ".vibera"
STATE_FILE = STATE_DIR / "tasks.json"


# Map of action verb -> OpenRAClient method name. Keep this aligned with
# the LLM prompt's allowed verb list. We deliberately don't expose
# low-level `place(x,y)` to the LLM — `auto_place` is always preferred.
VERB_DISPATCH: dict[str, str] = {
    "move":         "move",
    "attack_move":  "attack_move",
    "attack":       "attack",
    "guard":        "guard",
    "stop":         "stop",
    "stance":       "stance",
    "produce":      "produce",
    "place":        "place",
    "auto_place":   "auto_place",
    "sell":         "sell",
    "repair":       "repair",
    "harvest":      "harvest",
    "deploy":       "deploy",
}


class TaskDaemon:
    """Executes Tasks against an OpenRAClient. Driven by TickEvents from
    the bus rather than its own timer."""

    def __init__(self,
                 bus: EventBus,
                 client: OpenRAClient,
                 state_file: Path = STATE_FILE,
                 on_change: Optional[Callable[[], None]] = None):
        self.bus = bus
        self.client = client
        self.state_file = state_file
        self.on_change = on_change          # called whenever a task list changes

        self._tasks: list[Task] = []
        self._lock = threading.RLock()
        self._last_snapshot: Optional[Snapshot] = None
        self._last_error: Optional[str] = None

    # --- Lifecycle ----------------------------------------------------------

    def start(self) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self._load_state()
        self.bus.subscribe("task-daemon", self._on_event)
        log.info("daemon subscribed to bus, %d existing tasks loaded",
                 len(self._tasks))

    def stop(self) -> None:
        # Bus shutdown handles thread join; we just close the client to
        # be polite if the bus owner forgets. Pump owns connection
        # lifecycle in normal operation.
        pass

    # --- Public API ---------------------------------------------------------

    def add_task(self, task: Task) -> None:
        # Pre-flight against the latest snapshot. Catches plans that the
        # engine would reject. Rejected tasks are still persisted so the
        # LLM sees them in recent_tasks history.
        ok, reason = validate_plan(task, self._last_snapshot)
        if not ok:
            log.warning("validator rejected task %s: %s", task.id, reason)
            task.fail(f"validator: {reason}")
        with self._lock:
            self._tasks.append(task)
            self._persist()
        self._notify()

    def cancel_task(self, task_id: str) -> bool:
        with self._lock:
            for t in self._tasks:
                if t.id == task_id and not t.is_terminal:
                    t.cancel()
                    self._persist()
                    self._notify()
                    return True
        return False

    def snapshot_tasks(self) -> list[Task]:
        with self._lock:
            return copy.deepcopy(self._tasks)

    def active_tasks(self) -> list[Task]:
        with self._lock:
            return [copy.deepcopy(t) for t in self._tasks
                    if not t.is_terminal]

    def last_snapshot(self) -> Optional[Snapshot]:
        return self._last_snapshot

    def last_error(self) -> Optional[str]:
        return self._last_error

    def clear_done(self, max_keep: int = 20) -> None:
        with self._lock:
            terminal = [t for t in self._tasks if t.is_terminal]
            if len(terminal) <= max_keep:
                return
            keep_ids = {t.id for t in terminal[-max_keep:]}
            self._tasks = [
                t for t in self._tasks
                if not t.is_terminal or t.id in keep_ids
            ]
            self._persist()
        self._notify()

    # --- Bus subscriber -----------------------------------------------------

    def _on_event(self, ev: Event) -> None:
        if not isinstance(ev, TickEvent) or ev.snapshot is None:
            return
        self._last_snapshot = ev.snapshot
        self._last_error = None

        with self._lock:
            active = [t for t in self._tasks if not t.is_terminal]

        if not active:
            return

        changed = False
        for t in active:
            try:
                if self._advance_task(t, self.client, ev.snapshot):
                    changed = True
            except Exception as e:
                log.exception("advance task %s failed: %s", t.id, e)
                t.fail(str(e))
                changed = True

        if changed:
            with self._lock:
                self._persist()
            self._notify()

    def _advance_task(self, task: Task, client: OpenRAClient, snap: Snapshot) -> bool:
        if task.state == "pending":
            task.state = "active"

        moved = False
        for _ in range(16):
            step = task.current_step
            if step is None:
                break

            if step.kind == "action":
                self._exec_action(step, client, snap, task)
                if task.state == "failed":
                    moved = True
                    break
                task.advance()
                moved = True
                continue

            if step.kind == "wait":
                if step.started_tick is None:
                    step.started_tick = snap.tick
                    moved = True
                if not step.until:
                    task.fail("wait step has no `until` predicate")
                    moved = True
                    break
                try:
                    satisfied = evaluate(step.until, snap)
                except PredicateError as e:
                    task.fail(f"bad predicate: {e}")
                    moved = True
                    break
                if satisfied:
                    step.note = f"satisfied at tick {snap.tick}"
                    task.advance()
                    moved = True
                    continue
                if step.timeout_ticks is not None:
                    elapsed = snap.tick - (step.started_tick or snap.tick)
                    if elapsed >= step.timeout_ticks:
                        task.fail(
                            f"wait timed out after {elapsed} ticks: "
                            f"{step.until}")
                        moved = True
                        break
                break

            if step.kind == "branch":
                if not step.until:
                    task.fail("branch step has no `until` predicate")
                    moved = True
                    break
                try:
                    satisfied = evaluate(step.until, snap)
                except PredicateError as e:
                    task.fail(f"bad predicate: {e}")
                    moved = True
                    break
                chosen = step.then if satisfied else step.otherwise
                replacement = [Step.from_dict(s) for s in chosen]
                task.splice(replacement)
                moved = True
                continue

            task.fail(f"unknown step kind: {step.kind}")
            moved = True
            break

        return moved

    def _exec_action(self, step: Step, client: OpenRAClient,
                     snap: Snapshot, task: Task) -> None:
        verb = step.verb or ""
        method_name = VERB_DISPATCH.get(verb)
        if not method_name:
            task.fail(f"unknown verb: {verb}")
            return

        method = getattr(client, method_name, None)
        if method is None:
            task.fail(f"client has no method for verb: {verb}")
            return

        try:
            result = method(**(step.params or {}))
        except TypeError as e:
            task.fail(f"bad params for {verb}: {e}")
            return
        except Exception as e:
            # Transport-level failure: stop this task. Pump owns reconnect.
            task.fail(f"{verb} call failed: {e}")
            return

        step.note = json.dumps(result, ensure_ascii=False)
        if isinstance(result, dict) and not result.get("ok", False):
            step.failed = True
            step.note = f"rejected: {result.get('error', result)}"

    # --- Persistence --------------------------------------------------------

    def _persist(self) -> None:
        try:
            tmp = self.state_file.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps([t.to_dict() for t in self._tasks],
                           indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(tmp, self.state_file)
        except Exception as e:                          # pragma: no cover
            log.exception("persist failed: %s", e)

    def _load_state(self) -> None:
        if not self.state_file.exists():
            return
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            with self._lock:
                self._tasks = [Task.from_dict(d) for d in data]
        except Exception as e:                          # pragma: no cover
            log.exception("load state failed: %s", e)

    def _notify(self) -> None:
        if self.on_change:
            try:
                self.on_change()
            except Exception:                           # pragma: no cover
                log.exception("on_change callback raised")


if __name__ == "__main__":
    # Smoke harness: pump + daemon + a one-step build-power task.
    # Requires running OpenRA with External trait + an MCV/ConYard.
    import sys
    import time
    from snapshot_pump import SnapshotPump

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    bus = EventBus()
    pump = SnapshotPump(bus)
    pump.start()
    d = TaskDaemon(bus, pump.client)
    d.start()

    try:
        d.add_task(Task.new(
            intent="smoke: build power",
            utterance="<smoke test>",
            steps=[
                {"kind": "action", "verb": "produce",
                 "params": {"item": "powr", "count": 1}},
            ],
        ))
        for _ in range(60):
            time.sleep(2.0)
            for t in d.snapshot_tasks():
                print(f"{t.id} {t.state} cur={t.cursor}/{len(t.steps)} "
                      f"err={t.error or ''}")
            if all(t.is_terminal for t in d.snapshot_tasks()):
                break
    except KeyboardInterrupt:
        pass
    finally:
        pump.stop()
        bus.close()
        sys.exit(0)
