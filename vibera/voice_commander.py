"""
Voice Commander bridge: text utterance → OpenRA RA1 unit orders.

Pipeline:
  1. SNAPSHOT the live game over TCP.
  2. Convert the snapshot into a "lean state" JSON the LLM understands.
  3. Send {utterance, lean state} to the Gemini translator → structured actions.
  4. Map each action to one or more OpenRA Order JSON commands.
  5. Send them over TCP. Game tick executes via World.IssueOrder (lockstep-safe).

Run an interactive REPL:
    GEMINI_API_KEY=... python3 voice_commander.py

Or one-shot:
    GEMINI_API_KEY=... python3 -m vibera.voice_commander "send all tanks to attack the base"
"""
from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Optional

from .openra_client import OpenRAClient, Snapshot, Actor

# -- Reuse existing translator + prompt -----------------------------------------
from .llm_translator import translate

# RA1 actor type aliases (lower-case keys → english label given to LLM).
# Source: OpenRA mods/ra/rules/{infantry,vehicles,structures}.yaml
RA1_TYPE_LABELS = {
    # infantry
    "e1":   "Rifle Infantry",
    "e2":   "Grenadier",
    "e3":   "Rocket Soldier",
    "e4":   "Flamethrower",
    "e6":   "Engineer",
    "e7":   "Tanya",
    "spy":  "Spy",
    "medi": "Medic",
    "mech": "Mechanic",
    "thf":  "Thief",
    "shok": "Shock Trooper",
    # ground vehicles
    "1tnk": "Light Tank",
    "2tnk": "Medium Tank",
    "3tnk": "Heavy Tank",
    "4tnk": "Mammoth Tank",
    "jeep": "Ranger Jeep",
    "apc":  "APC",
    "arty": "Artillery",
    "ftrk": "Mobile Flak",
    "ttnk": "Tesla Tank",
    "harv": "Ore Truck",
    "mcv":  "MCV",
    "mnly": "Mine Layer",
    "v2rl": "V2 Rocket",
    # buildings
    "fact": "Construction Yard",
    "powr": "Power Plant",
    "apwr": "Advanced Power",
    "barr": "Soviet Barracks",
    "tent": "Allied Barracks",
    "weap": "War Factory",
    "proc": "Refinery",
    "silo": "Silo",
    "hpad": "Helipad",
    "afld": "Airfield",
    "dome": "Radar Dome",
    "atek": "Tech Center",
    "stek": "Soviet Tech Center",
    "pbox": "Pillbox",
    "hbox": "Camo Pillbox",
    "gun":  "Turret",
    "ftur": "Flame Tower",
    "tsla": "Tesla Coil",
    "agun": "AA Gun",
    "sam":  "SAM Site",
    "iron": "Iron Curtain",
    "miss": "Tech Missile Silo",
    "pdox": "Chronosphere",
    "fix":  "Service Depot",
    # walls & misc
    "sbag": "Sandbag",
    "cycl": "Chain-link Fence",
    "brik": "Concrete Wall",
    "barb": "Barbed Wire",
    "wood": "Wooden Fence",
}


def label_for(actor_type: str) -> str:
    return RA1_TYPE_LABELS.get(actor_type.lower(), actor_type)


# -------------------------------------------------------------------------------
# Snapshot -> lean state JSON for the LLM
# -------------------------------------------------------------------------------

def snapshot_to_lean_state(snap: Snapshot) -> dict:
    """Drop the redundant fields and add labels to make the LLM's job easier."""
    def to_unit(a: Actor) -> dict:
        d: dict[str, Any] = {
            "id": a.id,
            "kind": label_for(a.type),
            "type_id": a.type,
            "cell": [a.x, a.y],
        }
        if a.max_hp:
            d["hp_pct"] = round(100 * a.hp / a.max_hp)
        if not a.idle:
            d["busy"] = True
        # Attach factory production queues if present (only friendly factories have these).
        if a.queues:
            d["queues"] = [
                {
                    "type": q.type,                                        # e.g. "Building" | "Infantry" | "Vehicle"
                    "buildable": q.buildable,                              # rules names like ["e1","e2","e6",...]
                    "current": (
                        {"item": q.current.item, "done": q.current.done,
                         "remaining": q.current.remaining_time}
                        if q.current else None
                    ),
                    "queued_count": len(q.queued),
                }
                for q in a.queues
            ]
        return d

    mine = [to_unit(a) for a in snap.mine()]
    enemies = [to_unit(a) for a in snap.enemies()]

    # Compute crude self-base centroid + front-line for the prompt's reasoning.
    def centroid(units: list[Actor]) -> Optional[list[int]]:
        if not units:
            return None
        n = len(units)
        return [sum(u.x for u in units) // n, sum(u.y for u in units) // n]

    self_units = snap.mine()
    enemy_units = snap.enemies()

    state: dict[str, Any] = {
        "tick": snap.tick,
        "self_player": snap.local_player,
        "self_base_pos": centroid([a for a in self_units if a.type in ("fact", "proc", "powr", "apwr", "barr", "tent", "weap")]) or centroid(self_units),
        "enemy_centroid":  centroid(enemy_units),
        "self_units": mine,
        "enemy_units": enemies,
    }
    if snap.self_state:
        s = snap.self_state
        state["economy"] = {
            "cash": s.cash,
            "ore_in_silos": s.resources,
            "ore_capacity": s.resource_cap,
            "spendable": s.spendable,
        }
        state["power"] = {
            "provided": s.power_provided,
            "drained": s.power_drained,
            "excess": s.power_excess,
            "state": s.power_state,    # Normal | Low | Critical
        }
        # Production queues — flat list, indexed by type. Trait auto-finds
        # the right host actor at execution time, so the LLM only needs to
        # name the item.
        state["queues"] = [
            {
                "type": q.type,                                  # Building | Defense | Infantry | Vehicle | Ship | Aircraft
                "buildable": q.buildable,                        # rules names the player CAN currently produce
                "current": (
                    {"item": q.current.item, "done": q.current.done,
                     "remaining": q.current.remaining_time}
                    if q.current else None
                ),
                "queued_count": len(q.queued),
            }
            for q in s.queues
        ]
    return state


# -------------------------------------------------------------------------------
# Action executor
# -------------------------------------------------------------------------------

VERB_TO_OPENRA = {
    "move":         "move",
    "attack_move":  "attackmove",
    "attackmove":   "attackmove",
    "attack":       "attack",
    "stop":         "stop",
    "hold":         "stop",       # plus stance Defend
    "guard":        "guard",
    "scatter":      "stop",       # no native scatter via single actor
    "retreat":      "move",       # target should be a safe cell from the LLM
    # Economy / build verbs — handled by their own branch in the executor.
    "produce":      "produce",
    "place":        "place",
    "sell":         "sell",
    "repair":       "repair",
    "harvest":      "harvest",
    "deploy":       "deploy",
}

# Verbs whose selector.unit_ids list contains buildings/harvesters we own
# but which we still need to validate against the snapshot.
ECONOMY_VERBS = {"produce", "place", "sell", "repair", "harvest", "deploy"}


def execute_actions(client: OpenRAClient, actions: list[dict], state: dict) -> list[str]:
    """Translate each LLM action into one or more OpenRA orders. Returns log lines."""
    log: list[str] = []
    self_ids = {u["id"] for u in state.get("self_units", [])}

    for idx, action in enumerate(actions):
        verb = (action.get("verb") or "").lower()
        op = VERB_TO_OPENRA.get(verb)
        sel = (action.get("selector") or {})
        target = (action.get("target") or {})
        params = (action.get("params") or {})
        unit_ids = sel.get("unit_ids") or []

        # Filter out hallucinated IDs (keep only ones we own).
        unit_ids = [int(i) for i in unit_ids if int(i) in self_ids]

        if op is None:
            log.append(f"[{idx}] {verb}: unsupported verb — skipped")
            continue

        # ---- Economy / build ------------------------------------------------
        if verb in ECONOMY_VERBS:
            execute_economy(client, idx, verb, unit_ids, target, params, state, log)
            continue

        # ---- Combat / movement ---------------------------------------------
        if not unit_ids:
            log.append(f"[{idx}] {verb}: no valid friendly unit IDs in selector — skipped")
            continue

        # Hold = stop + force Defend stance.
        if verb == "hold":
            for aid in unit_ids:
                _safe(client.stop, aid, log, f"[{idx}] hold/stop #{aid}")
                _safe(client.stance, aid, "Defend", log, f"[{idx}] hold/stance #{aid}")
            continue

        if op == "stop":
            for aid in unit_ids:
                _safe(client.stop, aid, log, f"[{idx}] stop #{aid}")
            continue

        # Verbs that need a target.
        kind = (target.get("kind") or "").lower()
        all_units_by_id = {u["id"]: u for u in (state.get("self_units", []) + state.get("enemy_units", []))}

        def resolve_cell() -> Optional[tuple[int, int]]:
            cell = target.get("cell")
            if cell and len(cell) >= 2:
                return int(cell[0]), int(cell[1])
            tid = target.get("unit_id")
            if tid is not None:
                u = all_units_by_id.get(int(tid))
                if u:
                    cx, cy = u["cell"]
                    return int(cx), int(cy)
            return None

        if op in ("move", "attackmove"):
            xy = resolve_cell()
            if xy is None:
                log.append(f"[{idx}] {verb}: missing/unresolved target — skipped")
                continue
            x, y = xy
            for aid in unit_ids:
                method = client.attack_move if op == "attackmove" else client.move
                _safe(method, aid, x, y, log, f"[{idx}] {op} #{aid} -> ({x},{y})")
            continue

        if op == "attack":
            tid = target.get("unit_id")
            if tid is None:
                # Cell attack? route through attack-move.
                xy = resolve_cell()
                if xy is None:
                    log.append(f"[{idx}] attack: missing target — skipped")
                    continue
                x, y = xy
                for aid in unit_ids:
                    _safe(client.attack_move, aid, x, y, log,
                          f"[{idx}] attack@cell #{aid} -> ({x},{y}) (as attackmove)")
                continue
            for aid in unit_ids:
                _safe(client.attack, aid, int(tid), log, f"[{idx}] attack #{aid} -> #{tid}")
            continue

        if op == "guard":
            tid = target.get("unit_id")
            if tid is None:
                log.append(f"[{idx}] guard: missing target.unit_id — skipped")
                continue
            for aid in unit_ids:
                _safe(client.guard, aid, int(tid), log, f"[{idx}] guard #{aid} -> #{tid}")
            continue

        log.append(f"[{idx}] {verb}: nothing executed")

    return log


def execute_economy(client: OpenRAClient, idx: int, verb: str,
                    unit_ids: list[int], target: dict, params: dict,
                    state: dict, log: list[str]) -> None:
    """produce / place / sell / repair / harvest.

    - produce / place: trait auto-finds the queue actor; selector is OPTIONAL.
    - sell / repair:   selector MUST be the building's id (one).
    - harvest:         selector MUST be one or more harvester ids.
    """
    subject = unit_ids[0] if unit_ids else None

    if verb == "produce":
        # {"verb":"produce","params":{"item":"e1","count":5,"queued":true}}
        item = params.get("item")
        if not item:
            log.append(f"[{idx}] produce: missing params.item — skipped")
            return
        count  = int(params.get("count") or 1)
        queued = bool(params.get("queued", True))
        _safe(client.produce, str(item), count, queued, log,
              f"[{idx}] produce {count}x {item}")
        return

    if verb == "place":
        # {"verb":"place","target":{"cell":[x,y]},"params":{"item":"powr","variant":0}}
        cell = target.get("cell")
        if not cell or len(cell) < 2:
            log.append(f"[{idx}] place: missing target.cell — skipped")
            return
        item = params.get("item")
        if not item:
            log.append(f"[{idx}] place: missing params.item — skipped")
            return
        variant = int(params.get("variant") or 0)
        _safe(client.place, str(item), int(cell[0]), int(cell[1]), variant, log,
              f"[{idx}] place {item} at ({cell[0]},{cell[1]})")
        return

    if verb == "sell":
        if subject is None:
            log.append(f"[{idx}] sell: missing building id in selector — skipped")
            return
        _safe(client.sell, subject, log, f"[{idx}] sell building#{subject}")
        return

    if verb == "repair":
        if subject is None:
            log.append(f"[{idx}] repair: missing building id in selector — skipped")
            return
        _safe(client.repair, subject, log, f"[{idx}] repair toggle building#{subject}")
        return

    if verb == "harvest":
        if not unit_ids:
            log.append(f"[{idx}] harvest: missing harvester id in selector — skipped")
            return
        cell = target.get("cell")
        if not cell or len(cell) < 2:
            log.append(f"[{idx}] harvest: missing target.cell (ore patch) — skipped")
            return
        queued = bool(params.get("queued", False))
        for hid in unit_ids:
            _safe(client.harvest, hid, int(cell[0]), int(cell[1]), queued, log,
                  f"[{idx}] harvest #{hid} -> ({cell[0]},{cell[1]})")
        return

    if verb == "deploy":
        # {"verb":"deploy","selector":{"unit_ids":[<mcvId>]}}
        # Generic DeployTransform — MCV becomes ConYard, etc.
        if not unit_ids:
            log.append(f"[{idx}] deploy: missing actor id in selector — skipped")
            return
        queued = bool(params.get("queued", False))
        for aid in unit_ids:
            _safe(client.deploy, aid, queued, log, f"[{idx}] deploy #{aid}")
        return


def _safe(fn, *args_and_log_msg, **_) -> None:
    """Call fn(*args), append a log line. Last arg is a list (log) and msg."""
    *args, log, msg = args_and_log_msg
    try:
        r = fn(*args)
        if r.get("ok"):
            log.append(f"  OK   {msg}")
        else:
            log.append(f"  FAIL {msg}: {r.get('error')}")
    except Exception as e:
        log.append(f"  ERR  {msg}: {e}")


# -------------------------------------------------------------------------------
# Driver
# -------------------------------------------------------------------------------

def process_utterance(client: OpenRAClient, utterance: str) -> dict:
    """Run one full turn: snapshot -> LLM -> execute. Returns a structured result.

    Shape:
        {
          "ok": bool,                     # False only on hard errors (no llm output / low conf)
          "snapshot": {"tick": int, "mine": int, "enemies": int},
          "llm": {"model": str, "latency_sec": float,
                  "intent": str, "confidence": float,
                  "summary": str | None,    # short rationale if the model gave one
                  "error": str | None,
                  "raw": str | None},
          "actions": [...],               # raw actions list from the LLM
          "exec_logs": [str, ...],        # one line per action result
          "skipped_reason": str | None,   # set when we chose not to execute
          "total_sec": float,
        }
    Designed to be called from a GUI / daemon loop without parsing stdout.
    """
    t0 = time.time()
    out: dict = {
        "ok": True, "snapshot": None, "llm": {}, "actions": [],
        "exec_logs": [], "skipped_reason": None, "total_sec": 0.0,
    }

    snap = client.snapshot()
    state = snapshot_to_lean_state(snap)
    state_json = json.dumps(state, ensure_ascii=False)
    out["snapshot"] = {"tick": snap.tick, "mine": len(snap.mine()),
                       "enemies": len(snap.enemies())}

    result = translate(utterance, state_json)
    out["llm"] = {
        "model":        result.get("_model"),
        "latency_sec":  result.get("_latency_sec"),
        "intent":       result.get("intent"),
        "confidence":   result.get("confidence"),
        "summary":      result.get("summary") or result.get("rationale"),
        "error":        result.get("_error"),
        "raw":          (result.get("_raw") or "")[:600] if result.get("_error") else None,
    }

    if "_error" in result:
        out["ok"] = False
        out["total_sec"] = time.time() - t0
        return out

    actions = result.get("actions") or []
    out["actions"] = actions
    if not actions:
        out["skipped_reason"] = "no actions returned by LLM"
        out["total_sec"] = time.time() - t0
        return out

    conf = result.get("confidence") or 0
    if conf < 0.5:
        out["skipped_reason"] = f"confidence too low ({conf})"
        out["total_sec"] = time.time() - t0
        return out

    out["exec_logs"] = execute_actions(client, actions, state)
    out["total_sec"] = time.time() - t0
    return out


def handle_one(client: OpenRAClient, utterance: str, *, verbose: bool = True) -> None:
    """Thin CLI wrapper around process_utterance — preserves old print-style output."""
    res = process_utterance(client, utterance)
    if verbose:
        snap = res["snapshot"]
        print(f"[snapshot] tick={snap['tick']} mine={snap['mine']} enemies={snap['enemies']}")
        llm = res["llm"]
        print(f"[llm] model={llm['model']} latency={llm['latency_sec']}s "
              f"intent={llm['intent']} confidence={llm['confidence']}")
        if llm.get("error"):
            print(f"[llm] ERROR: {llm['error']}")
            print(f"[llm] raw: {llm.get('raw','')}")
            return
    if res["skipped_reason"]:
        print(f"[exec] {res['skipped_reason']}; not executing.")
        return
    for line in res["exec_logs"]:
        print(line)
    print(f"[done] total={res['total_sec']:.2f}s")


def main() -> None:
    from . import config
    host = config.OPENRA_HOST
    port = config.OPENRA_PORT
    print(f"Connecting to OpenRA ExternalControl at {host}:{port} ...")
    with OpenRAClient(host=host, port=port, timeout=10.0) as client:
        if not client.ping():
            print("ping failed; is the game running with the trait enabled?")
            sys.exit(2)
        print("connected.")

        if len(sys.argv) > 1:
            handle_one(client, " ".join(sys.argv[1:]))
            return

        print("Type a Chinese tactical command, blank line to quit.")
        while True:
            try:
                line = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not line:
                break
            try:
                handle_one(client, line)
            except Exception as e:
                print(f"[err] {e}")


if __name__ == "__main__":
    main()
