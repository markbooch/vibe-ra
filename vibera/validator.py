"""
Plan validator — code-level correctness gate for tasks before they hit the
daemon's executor.

Why this exists
---------------
Two hours of live play with the v0.3 adviser prompt showed that pure prompt
engineering can't reliably enforce RA1's tech tree. The LLM kept queueing
`weap` without `proc`, `1tnk` without `weap`, etc. Failures landed deep in
the executor as "rejected: no enabled queue can build X" — by then the task
is already marked `partial`, the LLM has moved on, and the player has lost
20 seconds of build time.

The validator runs synchronously inside `TaskDaemon.add_task` against the
last-known snapshot, before the task is appended to the active list. If a
plan is invalid we still persist it (so the LLM sees the failure in
`recent_tasks` and can self-correct), but we mark `state="failed"` with a
specific reason and skip execution.

Scope
-----
We only check things that are deterministic from a single snapshot:

  * `produce` item must appear in some `self_state.queues[*].buildable`.
    This naturally enforces the full prereq tree (engine populates it).
  * `auto_place` / `place`: only `item` must be present. We do NOT check
    for a preceding `wait queue_item_done` anymore — building placement
    is now handled automatically by reactors.AutoPlacer the moment the
    engine signals Done. Tasks that still include explicit place steps
    (e.g. legacy plans from the LLM) will simply succeed-or-no-op since
    by the time they execute, the reactor has already placed the item.
  * `deploy` requires an actor_id that exists in `self_units` and resolves
    to type `mcv` (the only deploy target we support today).
  * Cost sanity: warn when `spendable < cost * 0.9` for a `produce` step.
    Soft warning only — money may arrive next tick.

Anything we can't decide statically (queue currently busy, cell occupied,
target died) stays in the executor's hands.

Returns (ok: bool, reason: str). `reason` is empty on ok, otherwise a
single human-readable line that goes into `task.error`.
"""
from __future__ import annotations

import logging
from typing import Optional

from .openra_client import Snapshot
from .task import Task

log = logging.getLogger("vibera.validator")


# RA1 unit/structure costs (credits). Cross-checked with mods/ra/rules/*.yaml
# derived from OpenRA RA1 mod rules. Buildings only — units we don't validate
# cost on (queues handle it).
COST: dict[str, int] = {
    # Buildings — economy / power
    "fact": 2500, "powr": 300,  "apwr": 500,
    "proc": 1400, "silo": 150,  "harv": 1400,
    # Buildings — production
    "barr": 400,  "tent": 400,  "weap": 2000,
    "kenn": 200,  "afld": 2000, "hpad": 1000,
    "spen": 1500, "syrd": 2000, "atek": 1750,
    # Buildings — tech / support
    "dome": 1500, "fix":  1200, "iron": 2500,
    "pdox": 1750, "tsla_lab": 1500,
    # Defense
    "gun":  600,  "agun": 800,  "ftur": 600,
    "tsla": 1500, "sam":  750,  "pbox": 600,
    "hbox": 800,  "brik": 100,  "sbag": 50,
    # Infantry
    "e1": 100, "e2": 160, "e3": 300, "e4": 200,
    "e6": 500, "e7": 1500, "spy": 500, "medi": 300,
    "thf": 500, "shok": 600,
    # Vehicles
    "1tnk": 600, "2tnk": 800, "3tnk": 950, "4tnk": 1500,
    "jeep": 600, "apc": 800,  "arty": 800, "ftrk": 700,
    "mcv": 2500, "mnly": 800,
}

# Hint table: when the engine says X is not buildable, this often means a
# specific prereq is missing. Used purely to make the failure note more
# actionable for the LLM. None means "unclear, generic message".
LIKELY_PREREQ: dict[str, str] = {
    "weap":      "proc (radar dome path also needs proc)",
    "dome":      "proc",
    "apwr":      "dome",
    "fix":       "weap",
    "atek":      "dome (advanced tech path)",
    "tsla":      "weap (Soviet) — defense unlocks via vehicle factory",
    "iron":      "tsla_lab",
    "pdox":      "atek",
    "1tnk":      "weap",
    "2tnk":      "weap",
    "3tnk":      "weap + atek (Soviet heavy)",
    "4tnk":      "weap + atek",
    "harv":      "proc",
    "mcv":       "weap + atek + service depot",
    "e3":        "barr/tent",
    "e1":        "barr/tent",
    "spy":       "barr + dome",
    "medi":      "tent",
    "thf":       "barr + dome",
    "shok":      "barr + tsla_lab",
}


def validate_plan(task: Task, snapshot: Optional[Snapshot]) -> tuple[bool, str]:
    """Inspect a Task against the latest snapshot.

    Returns (True, "") if the plan should run, or (False, reason) if it
    should be rejected without ever reaching the executor. Rejected tasks
    still get persisted by the daemon so the LLM sees them in
    recent_tasks history and can adapt.
    """
    if snapshot is None or snapshot.self_state is None:
        # No snapshot yet means we just started up. Don't block — the
        # executor will catch real rejections; pre-flight only adds value
        # once we know the world.
        return True, ""

    self_state = snapshot.self_state
    # Union of every buildable across every owned queue. Engine-truth.
    buildable: set[str] = set()
    for q in self_state.queues:
        buildable.update(q.buildable)

    # Map type_id -> Actor for deploy lookups. Use the engine's reported
    # `type` (lowercase rules name like "mcv", not display name).
    mine_by_id = {a.id: a for a in snapshot.mine()}

    for i, step in enumerate(task.steps):
        if step.kind != "action":
            continue
        verb = step.verb or ""
        params = step.params or {}

        if verb == "produce":
            item = (params.get("item") or "").lower()
            if not item:
                return False, f"step {i}: produce missing `item`"
            if item not in buildable:
                hint = LIKELY_PREREQ.get(item)
                msg = (f"step {i}: '{item}' not buildable now"
                       + (f" (likely missing prereq: {hint})" if hint else ""))
                return False, msg
            cost = COST.get(item)
            if cost is not None:
                count = int(params.get("count") or 1)
                need = cost * max(1, count)
                # Soft warning only — log but don't reject. The LLM may
                # have queued production on purpose for when the harvester
                # returns.
                if self_state.spendable < int(need * 0.9):
                    log.warning(
                        "task %s step %d: low cash for %s x%d "
                        "(need ~%d, have %d spendable)",
                        task.id, i, item, count, need, self_state.spendable,
                    )

        elif verb == "auto_place" or verb == "place":
            item = (params.get("item") or "").lower()
            if not item:
                return False, f"step {i}: {verb} missing `item`"
            # No "must follow wait" check anymore — placement is now
            # handled automatically by the AutoPlacer reactor in response
            # to QueueItemDone events. If the LLM still emits an explicit
            # wait+place sequence, that's fine: by the time the executor
            # runs it the building is usually already placed, so the
            # engine returns ok=false harmlessly.

        elif verb == "deploy":
            actor_id = params.get("actor_id")
            if actor_id is None:
                return False, f"step {i}: deploy missing `actor_id`"
            try:
                actor_id = int(actor_id)
            except (TypeError, ValueError):
                return False, f"step {i}: deploy `actor_id` must be int, got {actor_id!r}"
            actor = mine_by_id.get(actor_id)
            if actor is None:
                return False, f"step {i}: deploy actor #{actor_id} not in self_units"
            # Only MCV deploy is meaningful in the bootstrap path. Other
            # transforms (gap-gen pack? not in RA1) — keep this strict
            # until we have a real use case.
            if actor.type != "mcv":
                return False, (f"step {i}: deploy only supported for mcv, "
                               f"actor #{actor_id} is {actor.type}")

        # Other verbs (move/attack/attack_move/guard/stop/stance/sell/
        # repair/harvest) are runtime-only — no useful static check.

    return True, ""


if __name__ == "__main__":
    # Smoke test: build a few synthetic plans and a fake snapshot,
    # exercise both happy and unhappy paths.
    from openra_client import Actor, SelfState, Snapshot, Queue

    snap = Snapshot(
        tick=0,
        local_player="Multi0",
        self_state=SelfState(
            cash=2000, resources=0, resource_cap=0, spendable=2000,
            power_provided=100, power_drained=20, power_excess=80,
            power_state="Normal",
            queues=[
                Queue(type="Building", host_actor=1, host_type="player",
                      buildable=["powr", "proc", "tent", "barr"],
                      current=None, queued=[]),
            ],
        ),
        actors=[
            Actor(id=42, type="mcv", owner="Multi0", mine=True,
                  x=10, y=10, hp=600, max_hp=600, idle=True,
                  stance=None, queues=[]),
        ],
    )

    cases = [
        # ok: produce buildable item
        (Task.new("ok produce", [
            {"kind": "action", "verb": "produce",
             "params": {"item": "powr", "count": 1}},
        ]), True),
        # bad: weap not buildable yet (no proc)
        (Task.new("bad weap", [
            {"kind": "action", "verb": "produce",
             "params": {"item": "weap", "count": 1}},
        ]), False),
        # ok: produce + wait + auto_place sequence (legacy plan still passes)
        (Task.new("ok place", [
            {"kind": "action", "verb": "produce", "params": {"item": "tent"}},
            {"kind": "wait",
             "until": {"kind": "queue_item_done", "args": {"item": "tent"}},
             "timeout_ticks": 1500},
            {"kind": "action", "verb": "auto_place", "params": {"item": "tent"}},
        ]), True),
        # ok: auto_place without wait — AutoPlacer reactor handles timing
        (Task.new("ok place no wait", [
            {"kind": "action", "verb": "produce", "params": {"item": "tent"}},
            {"kind": "action", "verb": "auto_place", "params": {"item": "tent"}},
        ]), True),
        # bad: auto_place missing item
        (Task.new("bad place no item", [
            {"kind": "action", "verb": "auto_place", "params": {}},
        ]), False),
        # ok: deploy MCV
        (Task.new("ok deploy", [
            {"kind": "action", "verb": "deploy", "params": {"actor_id": 42}},
        ]), True),
        # bad: deploy non-existent actor
        (Task.new("bad deploy missing", [
            {"kind": "action", "verb": "deploy", "params": {"actor_id": 999}},
        ]), False),
    ]
    for t, expected in cases:
        ok, reason = validate_plan(t, snap)
        marker = "OK" if ok == expected else "FAIL"
        print(f"[{marker}] {t.intent}: ok={ok} reason={reason!r}")
