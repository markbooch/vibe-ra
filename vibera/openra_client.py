"""
OpenRA External Control client.

Talks newline-delimited JSON over TCP to the OpenRA.Mods.External trait
(127.0.0.1:7778 with patches/openra-port.patch applied; 7777 upstream).
Used as the "hands" of the voice commander —
the LLM produces high-level actions, this module turns them into
concrete unit orders.

Server protocol (all messages are JSON terminated by '\n'):

  -> {"cmd": "ping"}                              <- {"ok": true}
  -> {"cmd": "snapshot"}                          <- {"ok": true, "tick": ..., "localPlayer": "...", "actors": [...]}
  -> {"cmd": "move",       "actor": id, "x": cx, "y": cy, "queued": false}
  -> {"cmd": "attackmove", "actor": id, "x": cx, "y": cy, "queued": false}
  -> {"cmd": "attack",     "actor": id, "target": id, "queued": false}
  -> {"cmd": "guard",      "actor": id, "target": id}
  -> {"cmd": "stop",       "actor": id}
  -> {"cmd": "stance",     "actor": id, "stance": "Defend|HoldFire|ReturnFire|AttackAnything"}
  -> {"cmd": "produce",    "actor": factoryId, "item": "e1", "count": 5, "queued": true}
  -> {"cmd": "place",      "actor": factoryId, "item": "powr", "x": cx, "y": cy, "variant": 0}
  -> {"cmd": "auto_place", "item": "tent", "variant": 0, "max_radius": 20}    <- {"ok": true, "x": cx, "y": cy}
  -> {"cmd": "sell",       "actor": buildingId}
  -> {"cmd": "repair",     "actor": buildingId}    # toggle
  -> {"cmd": "harvest",    "actor": harvId, "x": cx, "y": cy, "queued": false}

Each command replies with {"ok": true} or {"ok": false, "error": "..."}.
"""
from __future__ import annotations

import json
import socket
import threading
from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class QueueItem:
    item: str
    done: bool
    paused: bool
    remaining_time: int

    @classmethod
    def from_dict(cls, d: dict) -> "QueueItem":
        return cls(
            item=d.get("item", ""),
            done=d.get("done", False),
            paused=d.get("paused", False),
            remaining_time=d.get("remainingTime", 0),
        )


@dataclass
class Queue:
    """A production queue (Building/Defense/Infantry/Vehicle/Aircraft/Ship)."""
    type: str
    host_actor: int            # ActorID of the actor that owns this trait. In RA1 it's the PlayerActor.
    host_type: str             # actor type name, e.g. "player" or "fact"
    buildable: list[str]
    current: Optional[QueueItem]
    queued: list[QueueItem]

    @classmethod
    def from_dict(cls, d: dict) -> "Queue":
        return cls(
            type=d.get("type", ""),
            host_actor=d.get("hostActor", 0),
            host_type=d.get("hostType", ""),
            buildable=list(d.get("buildable") or []),
            current=QueueItem.from_dict(d["current"]) if d.get("current") else None,
            queued=[QueueItem.from_dict(x) for x in (d.get("queued") or [])],
        )


@dataclass
class Actor:
    id: int
    type: str
    owner: Optional[str]
    mine: bool
    x: int
    y: int
    hp: int
    max_hp: int
    idle: bool
    stance: Optional[str]
    queues: list[Queue]   # filled when the snapshot embeds per-actor queues. In RA1
                          # snapshot puts queues on `self.queues` instead, so this is empty.

    @classmethod
    def from_dict(cls, d: dict) -> "Actor":
        return cls(
            id=d["id"],
            type=d["type"],
            owner=d.get("owner"),
            mine=d.get("mine", False),
            x=d["x"],
            y=d["y"],
            hp=d.get("hp", 0),
            max_hp=d.get("maxHp", 0),
            idle=d.get("idle", False),
            stance=d.get("stance"),
            queues=[Queue.from_dict(q) for q in (d.get("queue") or [])],
        )


@dataclass
class SelfState:
    """Player-level economy + power. None when no local player (replays etc.)."""
    cash: int
    resources: int
    resource_cap: int
    spendable: int
    power_provided: int
    power_drained: int
    power_excess: int
    power_state: Optional[str]   # "Normal" | "Low" | "Critical"
    queues: list[Queue]          # all enabled production queues this player owns

    @classmethod
    def from_dict(cls, d: dict) -> "SelfState":
        return cls(
            cash=d.get("cash", 0),
            resources=d.get("resources", 0),
            resource_cap=d.get("resourceCap", 0),
            spendable=d.get("spendable", 0),
            power_provided=d.get("powerProvided", 0),
            power_drained=d.get("powerDrained", 0),
            power_excess=d.get("powerExcess", 0),
            power_state=d.get("powerState"),
            queues=[Queue.from_dict(q) for q in (d.get("queues") or [])],
        )


@dataclass
class MapInfo:
    width: int
    height: int
    tileset: str
    base_center: Optional[tuple[int, int]]   # (cx, cy) in cell coords; None if no base yet

    @classmethod
    def from_dict(cls, d: dict) -> "MapInfo":
        bc = d.get("baseCenter")
        return cls(
            width=d.get("width", 0),
            height=d.get("height", 0),
            tileset=d.get("tileset", ""),
            base_center=(int(bc[0]), int(bc[1])) if bc and len(bc) >= 2 else None,
        )


@dataclass
class Snapshot:
    tick: int
    local_player: Optional[str]
    self_state: Optional[SelfState]
    actors: list[Actor]
    map: Optional[MapInfo] = None

    def mine(self) -> list[Actor]:
        return [a for a in self.actors if a.mine]

    def factories(self) -> list[Actor]:
        """All friendly actors that expose at least one production queue."""
        return [a for a in self.actors if a.mine and a.queues]

    def enemies(self) -> list[Actor]:
        return [
            a for a in self.actors
            if not a.mine
            and a.owner not in (None, "Neutral", "Creeps")
            and not a.type.startswith(("crate", "mpspawn"))
        ]


class OpenRAClient:
    def __init__(self, host: str = "127.0.0.1", port: int = 7778, timeout: float = 5.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.sock: Optional[socket.socket] = None
        self.rfile = None
        self.wfile = None
        # The pump thread owns connect/snapshot; daemon / build_order /
        # adviser / reactors all dispatch commands on their own threads.
        # The server protocol is strictly request/response, so concurrent
        # writers would interleave bytes and lose responses. Lock around
        # the whole write+read pair.
        self._call_lock = threading.Lock()

    def connect(self) -> None:
        s = socket.create_connection((self.host, self.port), timeout=self.timeout)
        s.settimeout(self.timeout)
        self.sock = s
        self.rfile = s.makefile("r", encoding="utf-8", newline="\n")
        self.wfile = s.makefile("w", encoding="utf-8", newline="\n")

    def close(self) -> None:
        for f in (self.rfile, self.wfile):
            try:
                if f:
                    f.close()
            except Exception:
                pass
        try:
            if self.sock:
                self.sock.close()
        finally:
            self.sock = None
            self.rfile = None
            self.wfile = None

    def __enter__(self) -> "OpenRAClient":
        self.connect()
        return self

    def __exit__(self, *a) -> None:
        self.close()

    def call(self, **kwargs: Any) -> dict:
        if not self.wfile or not self.rfile:
            raise RuntimeError("not connected")
        with self._call_lock:
            self.wfile.write(json.dumps(kwargs) + "\n")
            self.wfile.flush()
            line = self.rfile.readline()
        if not line:
            raise ConnectionError("server closed connection")
        return json.loads(line)

    # --- High-level helpers ---------------------------------------------------

    def ping(self) -> bool:
        return self.call(cmd="ping").get("ok", False)

    def snapshot(self) -> Snapshot:
        r = self.call(cmd="snapshot")
        if not r.get("ok"):
            raise RuntimeError(f"snapshot failed: {r}")
        self_dict = r.get("self")
        map_dict = r.get("map")
        return Snapshot(
            tick=r.get("tick", 0),
            local_player=r.get("localPlayer"),
            self_state=SelfState.from_dict(self_dict) if self_dict else None,
            actors=[Actor.from_dict(a) for a in r.get("actors", [])],
            map=MapInfo.from_dict(map_dict) if map_dict else None,
        )

    def move(self, actor_id: int, x: int, y: int, queued: bool = False) -> dict:
        return self.call(cmd="move", actor=actor_id, x=int(x), y=int(y), queued=queued)

    def attack_move(self, actor_id: int, x: int, y: int, queued: bool = False) -> dict:
        return self.call(cmd="attackmove", actor=actor_id, x=int(x), y=int(y), queued=queued)

    def attack(self, actor_id: int, target_id: int, queued: bool = False) -> dict:
        return self.call(cmd="attack", actor=actor_id, target=int(target_id), queued=queued)

    def guard(self, actor_id: int, target_id: int) -> dict:
        return self.call(cmd="guard", actor=actor_id, target=int(target_id))

    def stop(self, actor_id: int) -> dict:
        return self.call(cmd="stop", actor=actor_id)

    def stance(self, actor_id: int, stance: str) -> dict:
        return self.call(cmd="stance", actor=actor_id, stance=stance)

    def produce(self, item: str, count: int = 1, queued: bool = True,
                factory_id: Optional[int] = None) -> dict:
        """Queue `count` of `item` (rules name like 'e1', '2tnk', 'powr').
        The trait auto-finds a queue actor that can build `item`. Pass
        `factory_id` only if you want to force a specific factory."""
        kw: dict[str, Any] = {"cmd": "produce", "item": item, "count": int(count), "queued": queued}
        if factory_id is not None:
            kw["actor"] = int(factory_id)
        return self.call(**kw)

    def place(self, item: str, x: int, y: int, variant: int = 0,
              factory_id: Optional[int] = None) -> dict:
        """Place a finished building. (x,y) = top-left of footprint. Trait
        auto-finds the queue actor with a Done item; pass `factory_id` to
        force one in case of ambiguity."""
        kw: dict[str, Any] = {"cmd": "place", "item": item, "x": int(x), "y": int(y), "variant": int(variant)}
        if factory_id is not None:
            kw["actor"] = int(factory_id)
        return self.call(**kw)

    def auto_place(self, item: str, variant: int = 0, max_radius: int = 20) -> dict:
        """Server picks the closest legal cell to base center using the
        engine's CanPlaceBuilding check (same logic as the AI bot). Returns
        {ok:true, x, y} on success, {ok:false, error} otherwise.
        Requires the item to already be Done in some queue."""
        return self.call(cmd="auto_place", item=item, variant=int(variant), max_radius=int(max_radius))

    def sell(self, building_id: int) -> dict:
        return self.call(cmd="sell", actor=building_id)

    def repair(self, building_id: int) -> dict:
        """Toggle repair on/off for a friendly building."""
        return self.call(cmd="repair", actor=building_id)

    def harvest(self, harvester_id: int, x: int, y: int, queued: bool = False) -> dict:
        return self.call(cmd="harvest", actor=harvester_id, x=int(x), y=int(y), queued=queued)

    def deploy(self, actor_id: int, queued: bool = False) -> dict:
        """DeployTransform — generic 'press D'. MCV -> ConYard, etc."""
        return self.call(cmd="deploy", actor=actor_id, queued=queued)


if __name__ == "__main__":
    # Smoke test: connect, ping, snapshot, print summary.
    import sys
    with OpenRAClient() as c:
        if not c.ping():
            print("ping failed", file=sys.stderr)
            sys.exit(1)
        snap = c.snapshot()
        mine = snap.mine()
        en = snap.enemies()
        print(f"tick={snap.tick} player={snap.local_player} mine={len(mine)} enemies={len(en)}")
        if snap.map:
            print(f"  [map] {snap.map.width}x{snap.map.height} tileset={snap.map.tileset} baseCenter={snap.map.base_center}")
        if snap.self_state:
            s = snap.self_state
            print(f"  cash=${s.cash} ore={s.resources}/{s.resource_cap} "
                  f"power={s.power_provided}-{s.power_drained}={s.power_excess} ({s.power_state})")
        for a in mine[:10]:
            print(f"  [me] #{a.id} {a.type} @ ({a.x},{a.y}) hp={a.hp}/{a.max_hp} idle={a.idle}")
        for f in snap.factories():
            for q in f.queues:
                cur = f"{q.current.item}({q.current.remaining_time})" if q.current else "-"
                print(f"  [queue] {f.type}#{f.id} type={q.type} cur={cur} buildable={len(q.buildable)} queued={len(q.queued)}")
        for a in en[:10]:
            print(f"  [en] #{a.id} {a.type} @ ({a.x},{a.y}) owner={a.owner}")
