"""
Task abstraction for the voice commander.

A Task is a multi-step plan produced by the LLM in response to one user
utterance. Each Step is either:

  * action  — fire-and-forget verb (move/produce/auto_place/...). Always
              advances the cursor on the next tick (we don't try to confirm
              that the order actually executed; if it matters, follow it
              with a wait step).
  * wait    — block until a Predicate becomes true. The cursor only
              advances once `evaluate(predicate, snapshot) == True`. If a
              `timeout_ticks` is set and we exceed it without satisfying
              the predicate, the Task fails.
  * branch  — evaluate a Predicate and pick the `then` or `else` step
              list. Both branches are inlined into the parent step list at
              the cursor (so `cursor` semantics stay simple).

Tasks are serialised to JSON and persisted to ~/.vibera/tasks.json by the
daemon so a crash/restart doesn't lose state mid-build.

Schema (JSON, what the LLM emits):

    {
      "intent": "build a barracks and place it near base",
      "steps": [
        {"kind": "action", "verb": "produce",
         "params": {"item": "tent", "count": 1}},
        {"kind": "wait",
         "until": {"kind": "queue_item_done", "args": {"item": "tent"}},
         "timeout_ticks": 1500},
        {"kind": "action", "verb": "auto_place",
         "params": {"item": "tent"}}
      ]
    }
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Optional


# --- Task states ------------------------------------------------------------
# pending  : created but daemon hasn't ticked it yet
# active   : daemon is executing steps
# done     : all steps completed successfully
# partial  : all steps executed but >=1 action step rejected (continued anyway)
# failed   : a wait timed out / fatal error / unknown verb (cursor halted)
# cancelled: user (or LLM) explicitly cancelled
TaskState = str


@dataclass
class Step:
    """One step in a Task.

    For kind=='action':  `verb` + `params` (params get splatted into the
                         OpenRAClient method call).
    For kind=='wait':    `until` is a Predicate dict, `timeout_ticks` is
                         optional (None = no timeout).
    For kind=='branch':  `until` is the condition, `then`/`otherwise` are
                         lists of Step dicts to splice in.
    """
    kind: str                                          # "action" | "wait" | "branch"
    verb: Optional[str] = None
    params: dict[str, Any] = field(default_factory=dict)
    until: Optional[dict[str, Any]] = None             # Predicate dict
    timeout_ticks: Optional[int] = None
    then: list[dict[str, Any]] = field(default_factory=list)
    otherwise: list[dict[str, Any]] = field(default_factory=list)

    # Per-step runtime bookkeeping (filled by daemon, persisted across ticks).
    started_tick: Optional[int] = None
    note: Optional[str] = None                         # last server response / error
    failed: bool = False                               # action step rejected/errored
                                                       # (cursor still advances; task
                                                       # ends up in `partial` state)

    @classmethod
    def from_dict(cls, d: dict) -> "Step":
        return cls(
            kind=d["kind"],
            verb=d.get("verb"),
            params=dict(d.get("params") or {}),
            until=d.get("until"),
            timeout_ticks=d.get("timeout_ticks"),
            then=list(d.get("then") or []),
            otherwise=list(d.get("otherwise") or []),
            started_tick=d.get("started_tick"),
            note=d.get("note"),
            failed=bool(d.get("failed", False)),
        )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Task:
    id: str
    intent: str                 # natural-language description (from utterance)
    steps: list[Step]
    cursor: int = 0             # index of the NEXT step to execute
    state: TaskState = "pending"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    error: Optional[str] = None

    # Optional original utterance for UI / debugging.
    utterance: Optional[str] = None

    @classmethod
    def new(cls, intent: str, steps: list[dict[str, Any]],
            utterance: Optional[str] = None) -> "Task":
        return cls(
            id=uuid.uuid4().hex[:8],
            intent=intent,
            steps=[Step.from_dict(s) for s in steps],
            utterance=utterance,
        )

    @classmethod
    def from_dict(cls, d: dict) -> "Task":
        return cls(
            id=d["id"],
            intent=d.get("intent", ""),
            steps=[Step.from_dict(s) for s in d.get("steps", [])],
            cursor=d.get("cursor", 0),
            state=d.get("state", "pending"),
            created_at=d.get("created_at", time.time()),
            updated_at=d.get("updated_at", time.time()),
            error=d.get("error"),
            utterance=d.get("utterance"),
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "intent": self.intent,
            "steps": [s.to_dict() for s in self.steps],
            "cursor": self.cursor,
            "state": self.state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "error": self.error,
            "utterance": self.utterance,
        }

    # --- Helpers used by the daemon -----------------------------------------

    @property
    def is_terminal(self) -> bool:
        return self.state in ("done", "partial", "failed", "cancelled")

    @property
    def current_step(self) -> Optional[Step]:
        if self.cursor < 0 or self.cursor >= len(self.steps):
            return None
        return self.steps[self.cursor]

    def advance(self) -> None:
        self.cursor += 1
        self.updated_at = time.time()
        if self.cursor >= len(self.steps):
            # End of plan: done if every step succeeded, else partial.
            any_failed = any(s.failed for s in self.steps)
            self.state = "partial" if any_failed else "done"
            if any_failed and not self.error:
                # Surface a concise summary so the UI has something to render.
                bad = [
                    f"step {i}: {s.note or 'rejected'}"
                    for i, s in enumerate(self.steps) if s.failed
                ]
                self.error = "; ".join(bad)

    def fail(self, error: str) -> None:
        self.state = "failed"
        self.error = error
        self.updated_at = time.time()

    def cancel(self) -> None:
        self.state = "cancelled"
        self.updated_at = time.time()

    def splice(self, replacement: list[Step]) -> None:
        """Replace the current step with `replacement` (used by branch).
        Cursor stays put so the first replacement step runs next tick."""
        self.steps[self.cursor:self.cursor + 1] = replacement
        self.updated_at = time.time()


# --- Persistence ------------------------------------------------------------

def dumps(tasks: list[Task]) -> str:
    return json.dumps([t.to_dict() for t in tasks], indent=2, ensure_ascii=False)


def loads(text: str) -> list[Task]:
    if not text.strip():
        return []
    return [Task.from_dict(d) for d in json.loads(text)]


if __name__ == "__main__":
    # Round-trip self-test: build the canonical "barracks then place" plan,
    # serialise, parse back, and walk the cursor through.
    t = Task.new(
        intent="build barracks and place near base",
        utterance="build a barracks at a good spot",
        steps=[
            {"kind": "action", "verb": "produce",
             "params": {"item": "tent", "count": 1}},
            {"kind": "wait",
             "until": {"kind": "queue_item_done", "args": {"item": "tent"}},
             "timeout_ticks": 1500},
            {"kind": "action", "verb": "auto_place",
             "params": {"item": "tent"}},
        ],
    )
    blob = dumps([t])
    print(blob)
    [t2] = loads(blob)
    assert t2.id == t.id
    assert len(t2.steps) == 3
    assert t2.steps[0].verb == "produce"
    assert t2.steps[1].until["kind"] == "queue_item_done"
    print(f"\nround-trip ok. current step: {t2.current_step.kind} ({t2.current_step.verb})")
