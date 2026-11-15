"""Independent checks on a finished layout. They do not trust the placement code.

A layout that fails any check is refused: no drawing is issued for it.
"""
from __future__ import annotations

import math

from shapely.geometry import LineString, Point
from shapely.ops import unary_union

from .layout import CL, Context

WALK = CL["walkway"] / 2 - 0.03   # erosion that leaves only floor a 0.9 m walkway can use
SEAT = CL["seat_access"] / 2 - 0.03


def _parts(g):
    return [p for p in getattr(g, "geoms", [g]) if p.geom_type == "Polygon" and not p.is_empty]


def validate(brief: dict, ctx: Context) -> dict:
    out = []

    def add(name, problems):
        out.append({"check": name, "ok": not problems, "problems": problems[:12]})

    P = ctx.pieces
    desks = sum(1 for p in P if p.kind == "desk" and p.workstation)
    want = sum(brief.get("workstations", {}).values())
    problems = [] if desks == want else [f"{desks} workstations placed, {want} asked for"]
    if brief["kitchen"]["type"] == "K2":
        seats = sum(1 for p in P if p.seat and p.zone == "K")
        if seats != brief["kitchen"]["seats"]:
            problems.append(f"{seats} dining seats placed, {brief['kitchen']['seats']} asked for")
    placed_zones = {p.zone for p in P}
    problems += [f"{z['label']} was not placed" for z in brief["zones"] if z["key"] not in placed_zones]
    add("Everything in the brief is placed, in the numbers asked for", problems)

    floor = ctx.free.buffer(0.01)
    add("Every piece stands on free floor, clear of walls and columns",
        [f"{p.kind} in {p.zone}" for p in P if not floor.covers(p.poly)])

    add("No two pieces overlap",
        [f"{a.kind} and {b.kind} in {a.zone}" for i, a in enumerate(P) for b in P[i + 1:]
         if a.poly.intersection(b.poly).area > 1e-4])

    swings = ctx.swings + [d.swing for d in ctx.doors if d.swing]
    add("Nothing stands in a door swing or in front of a door",
        [f"{p.kind} in {p.zone}" for p in P
         if any(p.poly.intersection(s).area > 1e-4 for s in swings + ctx.approaches)])

    walls = [x.poly for x in ctx.partitions]
    problems = []
    for p in P:
        if p.clear is None:
            continue
        blockers = [q for q in P if q is not p and q.solid and q.poly.intersection(p.clear).area > 1e-4]
        if blockers or any(w.intersection(p.clear).area > 1e-4 for w in walls) or not floor.covers(p.clear):
            problems.append(f"{CL['chair_zone']:.1f} m behind a desk in {p.zone} is blocked")
    add(f"{CL['chair_zone']:.1f} m of chair space behind every desk", problems)

    problems = []
    for p in P:
        if p.facing and p.target is not None:
            c = p.poly.centroid
            sight = LineString([(c.x, c.y), (c.x + p.facing[0] * 0.9, c.y + p.facing[1] * 0.9)])
            if not sight.intersects(p.target):       # looking straight ahead, the sitter must meet their own desk or table
                problems.append(f"chair in {p.zone} at ({c.x:.2f}, {c.y:.2f})")
    add("Every chair faces its desk or table", problems)

    problems = []
    for w in ctx.partitions:
        for end in (w.a, w.b):
            if ctx.band.contains(Point(end)) and ctx.free.contains(Point(end).buffer(0.12)):
                problems.append(f"{w.zone} wall ends on glazing at ({end[0]:.2f}, {end[1]:.2f})")
    add("New walls meet a wall or a pillar, never a window", problems)

    # Reachability: walk from the entrance with a 0.9 m wide body, then reach each seat.
    solid = unary_union([p.poly for p in P if p.solid] + walls) if (P or walls) else None
    walkable = ctx.free.difference(solid) if solid else ctx.free
    problems = []
    if not ctx.entries:
        problems.append("no entrance door found for this unit in the drawing")
    else:
        seeds = [e.centroid for e in ctx.entries]
        wide = [g for g in _parts(walkable.buffer(-WALK)) if any(g.distance(s) < 0.8 for s in seeds)]
        if not wide:
            problems.append("the entrance is blocked")
        else:
            reach_wide = unary_union(wide)
            narrow = [g for g in _parts(walkable.buffer(-SEAT)) if g.intersects(reach_wide)]
            reach = unary_union(narrow)
            for r in ctx.rooms:
                if not r.rect.intersects(reach_wide):
                    problems.append(f"no {CL['walkway']:.1f} m route into {r.zone}")
            for p in P:
                if p.seat and p.poly.distance(reach) > SEAT + 0.1:
                    c = p.poly.centroid
                    problems.append(f"seat in {p.zone} at ({c.x:.2f}, {c.y:.2f}) cannot be reached")
            for p in P:
                if p.clear is not None and p.clear.distance(reach_wide) > WALK + 0.1:
                    problems.append(f"no {CL['walkway']:.1f} m route to a desk in {p.zone}")
    add(f"Every room, desk and seat can be reached from the entrance ({CL['walkway']:.1f} m walkways)", problems)

    return {"ok": all(c["ok"] for c in out), "checks": out}
