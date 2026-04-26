"""
Predicate evaluator. A predicate is a small JSON dict {kind, args} that the
daemon evaluates against a fresh `Snapshot` to decide whether to advance a
`wait` step.

Keep this file dependency-light (just openra_client types) — the LLM
prompt mirrors the supported `kind` values, so adding a new one is a
matter of adding a case here AND extending the prompt's allowed list.

Supported kinds:

  queue_item_done    args.item: str
        True iff some friendly queue's `current` is the named item AND
        `done == True`. This is the right wait for "build X then place X".

  queue_item_built   args.item: str, [args.since_tick: int]
        True iff some friendly queue is currently NOT producing `item`
        AND we have at least one owned actor of that type (i.e. we
        produced and placed it). Useful for unit-producing queues where
        the production simply spawns the unit.

  any_owned_of_type  args.type: str, [args.min_count: int = 1]
        True iff we own >= min_count actors of the given type.

  no_owned_of_type   args.type: str
        True iff we own zero actors of the given type. ("they're all dead")

  actor_dead         args.actor: int
        True iff snapshot has no actor with that id (i.e. died/sold).

  actor_at_cell      args.actor: int, args.x, args.y, [args.radius: int = 1]
        True iff actor exists and its (x,y) is within Chebyshev `radius`
        of target. Useful for "wait until tank arrives at chokepoint".

  cash_geq           args.amount: int
        True iff self_state.cash + spendable >= amount.

  tick_after         args.tick: int
        True iff snapshot.tick >= the given absolute tick. Use with
        Step.started_tick + delta to express "wait N ticks".

Unknown predicate kinds raise PredicateError; the daemon catches and
fails the task, so the user gets a clear error in the floating chat.
"""
from __future__ import annotations

from typing import Any

from .openra_client import Snapshot


class PredicateError(Exception):
    pass


def _args(p: dict) -> dict:
    return p.get("args") or {}


def evaluate(predicate: dict, snap: Snapshot) -> bool:
    """Evaluate `predicate` against `snap`. Returns True iff satisfied.

    Never raises for normal "not yet" cases — only raises PredicateError
    for malformed predicates so we fail fast in the daemon."""
    if not predicate or "kind" not in predicate:
        raise PredicateError(f"bad predicate (no kind): {predicate}")

    kind = predicate["kind"]
    a = _args(predicate)

    if kind == "queue_item_done":
        item = a.get("item")
        if not item:
            raise PredicateError("queue_item_done needs args.item")
        if not snap.self_state:
            return False
        for q in snap.self_state.queues:
            if q.current and q.current.item == item and q.current.done:
                return True
        # In RA1 the queues live on self_state, but be defensive about
        # other mods that might put queues on actors directly:
        for actor in snap.actors:
            if not actor.mine:
                continue
            for q in actor.queues:
                if q.current and q.current.item == item and q.current.done:
                    return True
        return False

    if kind == "any_owned_of_type":
        t = a.get("type")
        if not t:
            raise PredicateError("any_owned_of_type needs args.type")
        min_count = int(a.get("min_count", 1))
        return sum(1 for x in snap.actors if x.mine and x.type == t) >= min_count

    if kind == "no_owned_of_type":
        t = a.get("type")
        if not t:
            raise PredicateError("no_owned_of_type needs args.type")
        return not any(x.mine and x.type == t for x in snap.actors)

    if kind == "queue_item_built":
        # Item is no longer in any queue's current/queued list. Conservative
        # signal that production finished.
        item = a.get("item")
        if not item:
            raise PredicateError("queue_item_built needs args.item")
        queues = []
        if snap.self_state:
            queues.extend(snap.self_state.queues)
        for actor in snap.actors:
            if actor.mine:
                queues.extend(actor.queues)
        for q in queues:
            if q.current and q.current.item == item:
                return False
            for qd in q.queued:
                if qd.item == item:
                    return False
        return True

    if kind == "actor_dead":
        aid = a.get("actor")
        if aid is None:
            raise PredicateError("actor_dead needs args.actor")
        return not any(x.id == int(aid) for x in snap.actors)

    if kind == "actor_at_cell":
        aid = a.get("actor")
        if aid is None or "x" not in a or "y" not in a:
            raise PredicateError("actor_at_cell needs args.actor,x,y")
        radius = int(a.get("radius", 1))
        tx, ty = int(a["x"]), int(a["y"])
        for x in snap.actors:
            if x.id == int(aid):
                return max(abs(x.x - tx), abs(x.y - ty)) <= radius
        return False                      # actor missing => not there

    if kind == "cash_geq":
        amount = int(a.get("amount", 0))
        if not snap.self_state:
            return False
        s = snap.self_state
        return (s.cash + s.spendable) >= amount

    if kind == "tick_after":
        return snap.tick >= int(a.get("tick", 0))

    raise PredicateError(f"unknown predicate kind: {kind}")


# Names exported to the LLM in the prompt. Keep this aligned with the
# branches above so prompt validation is trivial.
SUPPORTED_KINDS = (
    "queue_item_done",
    "queue_item_built",
    "any_owned_of_type",
    "no_owned_of_type",
    "actor_dead",
    "actor_at_cell",
    "cash_geq",
    "tick_after",
)


if __name__ == "__main__":
    # Tiny offline self-test using fabricated snapshots.
    from openra_client import Actor, MapInfo, Queue, QueueItem, SelfState, Snapshot

    snap = Snapshot(
        tick=1234,
        local_player="Greece",
        self_state=SelfState(
            cash=500, resources=0, resource_cap=0, spendable=200,
            power_provided=100, power_drained=20, power_excess=80, power_state="Normal",
            queues=[Queue(
                type="Building", host_actor=99, host_type="player",
                buildable=["tent", "powr"],
                current=QueueItem(item="tent", done=True, paused=False, remaining_time=0),
                queued=[],
            )],
        ),
        actors=[
            Actor(id=1, type="mcv", owner="Greece", mine=True, x=50, y=60,
                  hp=400, max_hp=400, idle=True, stance=None, queues=[]),
        ],
        map=MapInfo(width=128, height=128, tileset="TEMPERAT", base_center=(50, 60)),
    )

    assert evaluate({"kind": "queue_item_done", "args": {"item": "tent"}}, snap) is True
    assert evaluate({"kind": "queue_item_done", "args": {"item": "powr"}}, snap) is False
    assert evaluate({"kind": "any_owned_of_type", "args": {"type": "mcv"}}, snap) is True
    assert evaluate({"kind": "no_owned_of_type", "args": {"type": "tent"}}, snap) is True
    assert evaluate({"kind": "actor_dead", "args": {"actor": 999}}, snap) is True
    assert evaluate({"kind": "actor_dead", "args": {"actor": 1}}, snap) is False
    assert evaluate({"kind": "actor_at_cell",
                     "args": {"actor": 1, "x": 51, "y": 61, "radius": 1}}, snap) is True
    assert evaluate({"kind": "cash_geq", "args": {"amount": 700}}, snap) is True
    assert evaluate({"kind": "cash_geq", "args": {"amount": 701}}, snap) is False
    assert evaluate({"kind": "tick_after", "args": {"tick": 1000}}, snap) is True
    assert evaluate({"kind": "tick_after", "args": {"tick": 9999}}, snap) is False
    print("predicates: all self-tests passed")
