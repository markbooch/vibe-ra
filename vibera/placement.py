"""
SmartPlacer — building-aware placement scoring.

Why
---
The default `auto_place` (engine spiral from base center) is fine for
"powr / silo / dome" but objectively bad for two cases:

  * **Refineries** — proc dropped at base center forces harvesters into
    long round-trips. Pro play puts the 2nd refinery directly next to a
    fresh ore patch.
  * **Defenses** — tsla/pbox dropped at base center never see combat.
    Pro play puts towers on the line from base toward the enemy /
    nearest spotted threat.

Strategy
--------
Per-item "anchor" + spiral candidates around the anchor, each tried via
`client.place(item, x, y)`. First call returning ok=true wins. Falls
back to the engine's `auto_place` if every candidate is rejected (e.g.
prereq lost mid-flight, footprint conflict, etc.) so we never deadlock
on a Done item.

Anchor rules (faction-agnostic):

  * proc            -> nearest friendly harvester (ore proxy)
  * silo            -> adjacent to most-recent proc (chains with belt)
  * tsla/pbox/ftur  -> halfway between base center and nearest enemy or
    /gun/sam/agun     toward map's enemy half if no enemy spotted
  * powr/apwr       -> base center (cheapest to defend)
  * dome/atek/stek  -> base center
  * weap/barr/tent  -> base center, slightly away from current factories
  * default         -> auto_place fallback

Network cost
------------
Each candidate is one round trip (~1-2 ms). We try at most CANDIDATE_CAP
(=12) cells in spiral order; worst case ~25 ms before fallback. Acceptable
at 1 Hz placement rate.

Shape
-----
Pure function `pick(item, snap, client) -> dict` — same return shape as
`client.auto_place`. Reactor wraps it.
"""
from __future__ import annotations

import logging
import math
from typing import Iterable, Optional

from .openra_client import Actor, OpenRAClient, Snapshot

log = logging.getLogger("vibera.placement")

CANDIDATE_CAP = 12          # at most this many `place` round trips per call
SPIRAL_RADIUS = 6           # cells to spiral out from anchor

# Items we have specific anchor logic for. Everything else falls through
# to auto_place silently.
NEAR_ORE      = {"proc"}
NEAR_PROC     = {"silo"}
NEAR_ENEMY    = {"tsla", "pbox", "ftur", "gun", "sam", "agun", "hbox"}
NEAR_BASE     = {"powr", "apwr", "dome", "atek", "stek", "weap", "barr",
                 "tent", "afld", "fix", "kenn", "fcom", "spen", "syrd",
                 "hpad", "pen"}

# Building footprint (best-effort; spiral handles 1-cell error). Width=height
# for simplicity. Off by 1 doesn't matter — engine rejects, we try next.
FOOTPRINT = {
    "fact": 3, "weap": 3, "afld": 3, "syrd": 3, "spen": 3, "atek": 2,
    "stek": 2, "dome": 2, "proc": 3, "powr": 2, "apwr": 3, "barr": 2,
    "tent": 2, "silo": 1, "fix": 3, "fcom": 1, "hpad": 2, "pen": 3,
    "kenn": 1, "tsla": 1, "pbox": 1, "ftur": 1, "gun": 1, "sam": 2,
    "agun": 1, "hbox": 1, "gap": 2,
}


def pick(item: str,
         snap: Snapshot,
         client: OpenRAClient) -> dict:
    """Return the result of a successful `place` (or `auto_place`)."""
    anchor = _anchor_for(item, snap)
    if anchor is None:
        return _fallback(item, client, "no anchor")

    cx, cy = anchor
    candidates = list(_spiral(cx, cy, SPIRAL_RADIUS))[:CANDIDATE_CAP]

    last_err: Optional[str] = None
    for x, y in candidates:
        try:
            res = client.place(item=item, x=int(x), y=int(y))
        except Exception as e:
            last_err = f"network: {e}"
            continue
        if isinstance(res, dict) and res.get("ok"):
            log.info("SmartPlacer placed %s @ (%d,%d) anchor=(%d,%d)",
                     item, x, y, cx, cy)
            return res
        if isinstance(res, dict):
            last_err = res.get("error") or "rejected"

    log.info("SmartPlacer: %d candidates rejected for %s (last=%s); "
             "falling back to auto_place", len(candidates), item, last_err)
    return _fallback(item, client, last_err or "all candidates rejected")


def _fallback(item: str, client: OpenRAClient, why: str) -> dict:
    try:
        res = client.auto_place(item=item)
        if isinstance(res, dict) and res.get("ok"):
            log.info("SmartPlacer fallback auto_place(%s) OK @ (%s,%s) [%s]",
                     item, res.get("x"), res.get("y"), why)
        else:
            log.info("SmartPlacer fallback auto_place(%s) rejected: %s [%s]",
                     item, res, why)
        return res
    except Exception as e:
        log.warning("SmartPlacer fallback crashed for %s: %s", item, e)
        return {"ok": False, "error": str(e)}


def _anchor_for(item: str, snap: Snapshot) -> Optional[tuple[int, int]]:
    if snap.map and snap.map.base_center:
        bx, by = snap.map.base_center
    else:
        bx = by = 0

    if item in NEAR_ORE:
        target = _nearest_harvester(snap, bx, by)
        if target is not None:
            return target
        return (bx, by)

    if item in NEAR_PROC:
        proc = _last_proc(snap)
        if proc is not None:
            return (proc.x + 2, proc.y)
        return (bx, by)

    if item in NEAR_ENEMY:
        target = _toward_enemy(snap, bx, by)
        if target is not None:
            return target
        return (bx, by)

    if item in NEAR_BASE:
        # Drift slightly off base center so we don't pile every building
        # on the same spiral start (which then collides 50% of the time).
        return (bx + 1, by + 1)

    # Unknown item — let the caller fallback.
    return None


def _nearest_harvester(snap: Snapshot, bx: int, by: int
                       ) -> Optional[tuple[int, int]]:
    harvs = [a for a in snap.mine() if a.type == "harv"]
    if not harvs:
        return None
    # Pick the harvester farthest from base — it's at the edge of the
    # current ore field, where the new proc should go to shorten its run.
    best = max(harvs, key=lambda a: (a.x - bx) ** 2 + (a.y - by) ** 2)
    return (best.x, best.y)


def _last_proc(snap: Snapshot) -> Optional[Actor]:
    procs = [a for a in snap.mine() if a.type == "proc"]
    if not procs:
        return None
    # Highest id = most recently constructed.
    return max(procs, key=lambda a: a.id)


def _toward_enemy(snap: Snapshot, bx: int, by: int
                  ) -> Optional[tuple[int, int]]:
    """Halfway from base toward the closest spotted enemy. If we have no
    enemies in the snapshot (fog), aim at the map mirror of base center."""
    enemies = snap.enemies()
    if enemies:
        # Closest enemy = most likely attack vector
        e = min(enemies, key=lambda a: (a.x - bx) ** 2 + (a.y - by) ** 2)
        # Halfway, but cap at 8 cells from base so we stay in defendable
        # territory (out past 8 the tower can't be repaired safely).
        dx, dy = e.x - bx, e.y - by
        dist = math.sqrt(dx * dx + dy * dy) or 1.0
        scale = min(0.5, 8.0 / dist)
        return (int(bx + dx * scale), int(by + dy * scale))
    if snap.map:
        mw, mh = snap.map.width, snap.map.height
        # Step 8 cells toward map mirror of base
        dx = (mw - bx) - bx
        dy = (mh - by) - by
        dist = math.sqrt(dx * dx + dy * dy) or 1.0
        scale = min(1.0, 8.0 / dist)
        return (int(bx + dx * scale), int(by + dy * scale))
    return None


def _spiral(cx: int, cy: int, radius: int) -> Iterable[tuple[int, int]]:
    """Yield (x,y) starting at (cx,cy) and spiraling outward in rings.
    Coarse — we don't need true Manhattan ordering, just "close-ish first"."""
    yield (cx, cy)
    for r in range(1, radius + 1):
        # Top + bottom rows
        for dx in range(-r, r + 1):
            yield (cx + dx, cy - r)
            yield (cx + dx, cy + r)
        # Left + right cols (excluding corners we already did)
        for dy in range(-r + 1, r):
            yield (cx - r, cy + dy)
            yield (cx + r, cy + dy)


# --- Smoke -----------------------------------------------------------------

if __name__ == "__main__":
    from openra_client import Actor, MapInfo, SelfState
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    class FakeClient:
        def __init__(self, accept_at=None):
            self.accept_at = accept_at         # (x,y) tuple to accept; else reject all
            self.calls: list[tuple] = []

        def place(self, item, x, y, variant=0, factory_id=None):
            self.calls.append(("place", item, x, y))
            if self.accept_at and (x, y) == self.accept_at:
                return {"ok": True, "x": x, "y": y}
            return {"ok": False, "error": "not buildable"}

        def auto_place(self, item, variant=0, max_radius=20):
            self.calls.append(("auto_place", item))
            return {"ok": True, "x": 0, "y": 0}

    snap = Snapshot(
        tick=1, local_player="Multi0",
        self_state=SelfState(cash=2000, resources=0, resource_cap=0,
                             spendable=2000, power_provided=200,
                             power_drained=100, power_excess=100,
                             power_state="Normal", queues=[]),
        actors=[
            Actor(id=1, type="fact", owner="Multi0", mine=True,
                  x=10, y=10, hp=1000, max_hp=1000, idle=True,
                  stance=None, queues=[]),
            Actor(id=2, type="harv", owner="Multi0", mine=True,
                  x=18, y=14, hp=1000, max_hp=1000, idle=True,
                  stance=None, queues=[]),
            Actor(id=3, type="proc", owner="Multi0", mine=True,
                  x=12, y=10, hp=1000, max_hp=1000, idle=True,
                  stance=None, queues=[]),
            Actor(id=99, type="powr", owner="Enemy", mine=False,
                  x=40, y=30, hp=500, max_hp=500, idle=True,
                  stance=None, queues=[]),
        ],
        map=MapInfo(width=64, height=64, tileset="TEMP", base_center=(10, 10)),
    )

    # 1) Anchor for proc -> nearest harvester
    a = _anchor_for("proc", snap)
    assert a == (18, 14), a
    print("[OK] proc anchor =", a)

    # 2) Anchor for tsla -> halfway base->enemy, capped at 8 cells out
    a = _anchor_for("tsla", snap)
    assert a is not None and a != (10, 10), a
    print("[OK] tsla anchor =", a)

    # 3) Anchor for silo -> next to last proc
    a = _anchor_for("silo", snap)
    assert a == (14, 10), a
    print("[OK] silo anchor =", a)

    # 4) Place succeeds at first candidate
    fc = FakeClient(accept_at=(18, 14))
    res = pick("proc", snap, fc)
    assert res["ok"], res
    assert fc.calls[0] == ("place", "proc", 18, 14), fc.calls
    print("[OK] proc placed at first try")

    # 5) Place rejected -> falls back to auto_place
    fc = FakeClient(accept_at=None)
    res = pick("proc", snap, fc)
    assert res["ok"], res
    assert any(c[0] == "auto_place" for c in fc.calls), fc.calls
    print("[OK] fallback works after %d rejections" %
          sum(1 for c in fc.calls if c[0] == "place"))

    print("placement smoke pass")
