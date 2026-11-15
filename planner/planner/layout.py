"""Layout engine: a brief (planner.brief/1) plus a floor model -> a checked layout.

Placement is by calculation from wall faces, door swings and stated
clearances. The engine tries candidate positions in a fixed order and keeps
the first that satisfies every rule, so the same brief always gives the same
layout. If something cannot be placed it says what and why; it never returns
a layout that breaks a rule.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

from shapely.geometry import LineString, Point, Polygon, box
from shapely.ops import unary_union
from shapely.prepared import prep

from . import model

ROOT = model.ROOT
RULES = json.loads((ROOT / "rules" / "furniture.json").read_text())
CL = RULES["clearance"]
SCHEMA = "planner.layout/1"
STEP = 0.05
FACADE_DEPTH_M = 0.35   # furniture stays this far inside the outer face where the facade is glass


class CannotPlace(Exception):
    pass


# ---------------------------------------------------------------- geometry

@dataclass
class Frame:
    """Local (u, v) axes placed in the drawing frame."""
    o: tuple
    ux: tuple
    uy: tuple

    def pt(self, u, v):
        return (self.o[0] + u * self.ux[0] + v * self.uy[0], self.o[1] + u * self.ux[1] + v * self.uy[1])

    def vec(self, u, v):
        return (u * self.ux[0] + v * self.uy[0], u * self.ux[1] + v * self.uy[1])

    def rect(self, u0, v0, u1, v1):
        return Polygon([self.pt(u0, v0), self.pt(u1, v0), self.pt(u1, v1), self.pt(u0, v1)])


@dataclass
class Piece:
    kind: str
    poly: Polygon
    zone: str
    solid: bool = True
    facing: tuple | None = None    # unit vector the sitter looks along
    target: Polygon | None = None  # the desk or table the sitter should face
    seat: bool = False
    clear: Polygon | None = None   # floor that must stay free of other solids
    workstation: bool = False


@dataclass
class Partition:
    a: tuple
    b: tuple
    wall: str
    zone: str

    @property
    def poly(self):
        return LineString([self.a, self.b]).buffer(CL["partition"] / 2, cap_style=2)


@dataclass
class Door:
    a: tuple
    b: tuple
    kind: str
    zone: str
    swing: Polygon | None = None


@dataclass
class Room:
    zone: str
    code: str
    rect: Polygon


@dataclass
class Context:
    unit: Polygon
    free: Polygon                 # floor not occupied by structure
    band: Polygon                 # strip along the unit perimeter (facade, core walls)
    swings: list
    entries: list
    pieces: list = field(default_factory=list)
    approaches: list = field(default_factory=list)   # floor in front of new doors
    aisles: list = field(default_factory=list)       # floor kept clear at the ends of desk blocks
    partitions: list = field(default_factory=list)
    doors: list = field(default_factory=list)
    rooms: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    edge_cache: dict = field(default_factory=dict)

    def open_floor(self):
        taken = [r.rect.buffer(CL["partition"], join_style=2) for r in self.rooms]
        return self.free.difference(unary_union(taken)) if taken else self.free


def sector(cx, cy, r, a0, a1):
    if a1 < a0:
        a1 += 360
    n = max(4, int((a1 - a0) / 6))
    arc = [(cx + r * math.cos(math.radians(a0 + (a1 - a0) * i / n)),
            cy + r * math.sin(math.radians(a0 + (a1 - a0) * i / n))) for i in range(n + 1)]
    return Polygon([(cx, cy)] + arc)


def chair(frame, u, v, look, size, zone, target=None, clear=None, workstation=False):
    """A chair centred at local (u, v); look is the local direction the sitter faces."""
    h = size / 2
    return Piece("chair", frame.rect(u - h, v - h, u + h, v + h), zone, solid=False, seat=True,
                 facing=frame.vec(*look), target=target, clear=clear, workstation=workstation)


def context(floor_model: dict, unit_id: str) -> Context:
    u = next(x for x in floor_model["units"] if x["id"] == unit_id)
    unit = Polygon(u["polygon"]["outer"])
    structure = unary_union([Polygon(w["outer"], w["holes"]) for w in floor_model["walls"]]
                            + [Polygon(c["polygon"]["outer"]) for c in floor_model["columns"]])
    fp = Polygon(floor_model["footprint"]["outer"])
    # Glazing has no wall hatch, so keep furniture off the facade by its depth.
    facade = fp.exterior.buffer(FACADE_DEPTH_M, cap_style=2, join_style=2)
    free = unit.difference(structure.buffer(0.001)).difference(facade).simplify(0.01)
    free = max(getattr(free, "geoms", [free]), key=lambda g: g.area)
    swings, entries = [], []
    for cx, cy, r, a0, a1 in floor_model["door_swings"]:
        if not 0.5 <= r <= 1.3 or unit.exterior.distance(Point(cx, cy)) > 0.6:
            continue
        s = sector(cx, cy, r, a0, a1).intersection(unit)
        if s.area < 0.05:
            continue
        swings.append(s)
        if fp.exterior.distance(Point(cx, cy)) > 1.0:   # a door in the core wall, not in the facade
            entries.append(s)
    return Context(unit=unit, free=free, band=unit.difference(unit.buffer(-0.5, join_style=2)),
                   swings=swings, entries=entries)


def edge_kinds(ctx: Context, a, b, outward):
    """Classify what lies just outside a room edge, sampled every 5 cm."""
    n = max(2, int(math.dist(a, b) / STEP))
    out = []
    for i in range(n):
        t = (i + 0.5) / n
        x, y = a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t
        near = Point(x + outward[0] * 0.15, y + outward[1] * 0.15)
        far = Point(x + outward[0] * 0.45, y + outward[1] * 0.45)   # past a wall's thickness
        out.append("solid" if not ctx.free.contains(near) else "facade" if not ctx.unit.contains(far) else "open")
    return out


def largest_rect(region) -> tuple | None:
    """Largest axis-aligned rectangle inside a region (exhaustive over its own coordinates)."""
    polys = [g for g in getattr(region, "geoms", [region]) if g.geom_type == "Polygon" and g.area > 1]
    best = None
    for poly in polys:
        poly = poly.simplify(0.02)
        rings = [poly.exterior] + list(poly.interiors)
        xs = sorted({round(x, 3) for r in rings for x, _ in r.coords})
        ys = sorted({round(y, 3) for r in rings for _, y in r.coords})
        inner = prep(poly.buffer(0.005))
        cands = sorted((((x1 - x0) * (y1 - y0), x0, y0, x1, y1)
                        for i, x0 in enumerate(xs) for x1 in xs[i + 1:]
                        for j, y0 in enumerate(ys) for y1 in ys[j + 1:]), reverse=True)
        for area, x0, y0, x1, y1 in cands:
            if best and area <= best[0]:
                break
            if inner.covers(box(x0, y0, x1, y1)):
                best = (area, x0, y0, x1, y1)
                break
    return best[1:] if best else None


# ------------------------------------------------------------------ fitting

def conflicts(ctx: Context, new: list, floor) -> str | None:
    """Why these pieces cannot go here, or None if they can."""
    inside = prep(floor.buffer(0.01))
    old_bodies = [p.poly for p in ctx.pieces]
    old_solids = [p.poly for p in ctx.pieces if p.solid]
    old_clear = [p.clear for p in ctx.pieces if p.clear is not None]
    for p in new:
        if not inside.covers(p.poly):
            return "outside the free floor"
        if any(p.poly.intersection(s).area > 1e-4 for s in ctx.swings + [d.swing for d in ctx.doors if d.swing]):
            return "inside a door swing"
        if any(p.poly.intersection(s).area > 1e-4 for s in ctx.approaches):
            return "in front of a door"
        if p.solid and any(p.poly.intersection(s).area > 1e-4 for s in ctx.aisles):
            return "in an aisle"
        if any(p.poly.intersection(o).area > 1e-4 for o in old_bodies):
            return "overlaps another piece"
        if p.solid and any(p.poly.intersection(c).area > 1e-4 for c in old_clear):
            return "takes another piece's clearance"
        if p.clear is not None:
            if not inside.covers(p.clear):
                return "clearance reaches outside the free floor"
            if any(p.clear.intersection(o).area > 1e-4 for o in old_solids):
                return "clearance is blocked"
    return None


# ------------------------------------------------------------------- rooms

def room_frames(x0, y0, x1, y1):
    """The eight ways to lay local axes over a rectangle; W is along u, D along v."""
    w, d = x1 - x0, y1 - y0
    return [
        (Frame((x0, y0), (1, 0), (0, 1)), w, d), (Frame((x1, y0), (-1, 0), (0, 1)), w, d),
        (Frame((x0, y1), (1, 0), (0, -1)), w, d), (Frame((x1, y1), (-1, 0), (0, -1)), w, d),
        (Frame((x0, y0), (0, 1), (1, 0)), d, w), (Frame((x0, y1), (0, -1), (1, 0)), d, w),
        (Frame((x1, y0), (0, 1), (-1, 0)), d, w), (Frame((x1, y1), (0, -1), (-1, 0)), d, w),
    ]


def room_edges(ctx, f: Frame, W, D):
    """Edge name -> (a, b, share solid, share open) in the frame's own terms."""
    spec = {"u0": ((0, 0), (0, D), (-1, 0)), "u1": ((W, 0), (W, D), (1, 0)),
            "v0": ((0, 0), (W, 0), (0, -1)), "v1": ((0, D), (W, D), (0, 1))}
    out = {}
    for name, (a, b, n) in spec.items():
        A, B = f.pt(*a), f.pt(*b)
        ck = (round(A[0], 3), round(A[1], 3), round(B[0], 3), round(B[1], 3))
        if ck not in ctx.edge_cache:
            ctx.edge_cache[ck] = edge_kinds(ctx, A, B, f.vec(*n))
        kinds = ctx.edge_cache[ck]
        out[name] = (A, B, kinds, kinds.count("solid") / len(kinds), kinds.count("open") / len(kinds))
    return out


def build_partitions(edges, wall_for, zone, door_edge=None, door_span=None, door_kind=None, frame=None, inward=None):
    parts, doors = [], []
    t = CL["partition"] / 2
    for name, (A, B, kinds, _, _) in edges.items():
        length = math.dist(A, B)
        ux, uy = (B[0] - A[0]) / length, (B[1] - A[1]) / length
        n = len(kinds)
        runs, start = [], None
        for i, k in enumerate(kinds + ["end"]):
            if k == "open" and start is None:
                start = i
            elif k != "open" and start is not None:
                runs.append((start * length / n, i * length / n))
                start = None
        if name == door_edge:
            d0, d1 = door_span
            cut = []
            for r0, r1 in runs:
                if d0 > r0 + 0.05:
                    cut.append((r0, min(r1, d0)))
                if d1 < r1 - 0.05:
                    cut.append((max(r0, d1), r1))
            runs = [r for r in cut if r[1] - r[0] > 0.05]
        # Partitions sit just outside the room edge and overlap at corners.
        ox, oy = wall_for["normals"][name]
        door = door_span if name == door_edge else (-1, -1)
        for r0, r1 in runs:
            e0 = 0 if abs(r0 - door[1]) < 0.06 else 2 * t     # run on into the wall or corner it meets
            e1 = 0 if abs(r1 - door[0]) < 0.06 else 2 * t
            a = (A[0] + ux * (r0 - e0) + ox * t, A[1] + uy * (r0 - e0) + oy * t)
            b = (A[0] + ux * (r1 + e1) + ox * t, A[1] + uy * (r1 + e1) + oy * t)
            parts.append(Partition(a, b, wall_for.get(name, "PW"), zone))
        if name == door_edge:
            d0, d1 = door_span
            a = (A[0] + ux * d0 + ox * t, A[1] + uy * d0 + oy * t)
            b = (A[0] + ux * d1 + ox * t, A[1] + uy * d1 + oy * t)
            swing = None
            if door_kind != "SD":
                w = d1 - d0
                hinge, tip = (A[0] + ux * d1, A[1] + uy * d1), (A[0] + ux * d0, A[1] + uy * d0)
                arc = [hinge] + [(hinge[0] + (tip[0] - hinge[0]) * math.cos(th) + inward[0] * w * math.sin(th),
                                  hinge[1] + (tip[1] - hinge[1]) * math.cos(th) + inward[1] * w * math.sin(th))
                                 for th in [math.pi / 2 * i / 10 for i in range(11)]]
                swing = Polygon(arc)
            doors.append(Door(a, b, door_kind, zone, swing))
    return parts, doors


def normals(f: Frame):
    return {"u0": f.vec(-1, 0), "u1": f.vec(1, 0), "v0": f.vec(0, -1), "v1": f.vec(0, 1)}


def side_chairs(f, zone, u0, u1, v_table0, v_table1, per_side, heads, target):
    s, out = RULES["side_chair"], []
    for i in range(per_side):
        u = u0 + (u1 - u0) * (i + 0.5) / per_side
        out.append(chair(f, u, v_table0 - 0.33, (0, 1), s, zone, target))
        out.append(chair(f, u, v_table1 + 0.33, (0, -1), s, zone, target))
    vm = (v_table0 + v_table1) / 2
    if heads >= 1:
        out.append(chair(f, u1 + 0.33, vm, (-1, 0), s, zone, target))
    if heads >= 2:
        out.append(chair(f, u0 - 0.33, vm, (1, 0), s, zone, target))
    return out


def mod_of2(f, W, D, zone, e, enc):
    """Manager office: desk with its back to the u=W wall, meeting table along the v=D wall, door in the u=0 wall."""
    if e["u0"][4] < 0.5:
        return None
    dv0, dv1 = D - 4.6, D - 2.8                      # 0.6 chairs, 0.7 table, 0.6 chairs, 0.9 passage, 1.8 desk
    desk = f.rect(W - 1.8, dv0, W - 1.0, dv1)
    dc = desk
    pieces = [Piece("desk", desk, zone),
              chair(f, W - 0.5, (dv0 + dv1) / 2, (-1, 0), RULES["task_chair"], zone, dc,
                    clear=f.rect(W - 1.0, dv0, W, dv1)),
              chair(f, W - 2.25, dv0 + 0.45, (1, 0), RULES["side_chair"], zone, dc),
              chair(f, W - 2.25, dv1 - 0.45, (1, 0), RULES["side_chair"], zone, dc)]
    t0, t1 = W - 2.8, W - 0.6
    table = f.rect(t0, D - 1.3, t1, D - 0.6)
    pieces.append(Piece("table", table, zone))
    pieces += side_chairs(f, zone, t0, t1, D - 1.3, D - 0.6, 3, 1, table)
    if dv0 >= 0.3:
        pieces.append(Piece("pedestal", f.rect(W - 0.58, 0.02, W - 0.02, min(0.62, dv0 + 0.25)), zone))
    width = RULES["door"][enc["door"]]
    d0 = max(0.1, D - 2.8 - width - 0.2)             # in front of the desk, clear of the meeting chairs
    score = 2 * e["u1"][3] + e["v1"][4] + e["v1"][3]
    return pieces, ("u0", (d0, d0 + width)), {"u0": enc["wall"], "u1": enc["wall"], "v0": enc["wall"], "v1": enc["wall"]}, score


def mod_of1(f, W, D, zone, e, enc):
    if e["u0"][4] < 0.5:
        return None
    dc = f.rect(W - 1.8, D / 2 - 0.8, W - 1.0, D / 2 + 0.8)
    pieces = [Piece("desk", dc, zone),
              chair(f, W - 0.5, D / 2, (-1, 0), RULES["task_chair"], zone, dc,
                    clear=f.rect(W - 1.0, D / 2 - 0.8, W, D / 2 + 0.8)),
              chair(f, W - 2.25, D / 2 - 0.4, (1, 0), RULES["side_chair"], zone, dc),
              chair(f, W - 2.25, D / 2 + 0.4, (1, 0), RULES["side_chair"], zone, dc)]
    width = RULES["door"][enc["door"]]
    return pieces, ("u0", (0.15, 0.15 + width)), {k: enc["wall"] for k in ("u0", "u1", "v0", "v1")}, 2 * e["u1"][3]


def mod_meeting(code):
    spec = RULES["rooms"][code]

    def build(f, W, D, zone, e, enc):
        if e["u0"][4] < 0.5:
            return None
        tl, td = spec["table"]
        t0, v0 = (W - td) / 2, (D - tl) / 2          # table runs along v
        g = Frame(f.pt(0, 0), f.uy, f.ux)             # swap axes so side_chairs can be reused
        table = g.rect(v0, t0, v0 + tl, t0 + td)
        pieces = [Piece("table", table, zone)]
        pieces += side_chairs(g, zone, v0, v0 + tl, t0, t0 + td, spec["side"], spec["heads"], table)
        width = RULES["door"][enc["door"]]
        return pieces, ("u0", (0.15, 0.15 + width)), {k: enc["wall"] for k in ("u0", "u1", "v0", "v1")}, e["u1"][3] + e["v0"][3] + e["v1"][3]
    return build


def mod_kitchen(seats):
    k = RULES["rooms"]["K"]
    per_side = max(1, math.ceil((seats - 2) / 2)) if seats > 2 else 1
    heads = min(2, max(0, seats - 2 * per_side))
    length = per_side * k["seat_pitch"] + 0.05

    def build(f, W, D, zone, e, enc, var=0):
        """Glass front at v=0 with a sliding door, counter run along the u=W wall, tall units at the back."""
        if e["v0"][4] < 0.5 or e["u1"][3] < 0.6:
            return None
        shift = (0.1, 0.0, -0.1, 0.2, -0.2)[var]     # the table may move this far to clear a door swing
        c = k["counter"]
        pieces = [Piece("counter", f.rect(W - c, 0.05, W, D - k["tall_run"]), zone),
                  Piece("tall", f.rect(W - c, D - k["tall_run"], W, D), zone)]
        t1 = W - c - 1.0 - (0.6 if heads >= 1 else 0)       # 1.0 m between the head chair and the counter
        t0 = t1 - length
        if t0 < (0.7 if heads >= 2 else 0.2):
            return None
        vm = D / 2 + shift
        table = f.rect(t0, vm - k["table_depth"] / 2, t1, vm + k["table_depth"] / 2)
        pieces.append(Piece("table", table, zone))
        pieces += side_chairs(f, zone, t0, t1, vm - k["table_depth"] / 2, vm + k["table_depth"] / 2, per_side, heads, table)
        width = RULES["door"][enc["door"]]
        mid = (W - c) / 2
        walls = {"v0": enc["wall"], "u0": "PW", "u1": "PW", "v1": "PW"}   # only the front follows the brief
        return pieces, ("v0", (mid - width / 2, mid + width / 2)), walls, 2 * e["u1"][3] + 2 * e["v1"][3] + e["u0"][3] - 0.01 * var

    build.variants = 5
    width_pref = k["counter"] + 1.0 + (0.6 if heads >= 1 else 0) + length + (0.6 if heads >= 2 else 0) + 0.7
    return build, (width_pref, k["depth_pref"]), (width_pref - 0.4, k["depth_min"])


def mod_store(f, W, D, zone, e, enc):
    if e["u0"][4] < 0.5:
        return None
    width = RULES["door"]["HD"]
    return [Piece("shelf", f.rect(W - 0.4, 0.05, W, D - 0.05), zone)], ("u0", (0.15, 0.15 + width)), \
        {k: "PW" for k in ("u0", "u1", "v0", "v1")}, e["u1"][3] + e["v0"][3] + e["v1"][3]


def candidate_rects(region, sketched: bool, sizes):
    """Rectangles a room could occupy: tucked into each corner of the available floor."""
    polys = [g for g in getattr(region, "geoms", [region]) if g.geom_type == "Polygon" and g.area > 1]
    inside = prep(region.buffer(0.005))
    seen, out = set(), []

    def add(x0, y0, w, d):
        key = (round(x0, 2), round(y0, 2), round(w, 2), round(d, 2))
        if key not in seen and inside.covers(box(x0, y0, x0 + w, y0 + d)):
            seen.add(key)
            out.append((x0, y0, x0 + w, y0 + d))

    lir = largest_rect(region) if sketched else None
    for w0, d0 in sizes:
        if lir:
            X0, Y0, X1, Y1 = lir
            w, d = min(w0, X1 - X0), min(d0, Y1 - Y0)
            for cx, cy in ((X0, Y0), (X1 - w, Y0), (X0, Y1 - d), (X1 - w, Y1 - d)):
                add(cx, cy, w, d)
        for poly in polys:
            for x, y in poly.simplify(0.03).exterior.coords[:-1]:
                for cx, cy in ((x, y), (x - w0, y), (x, y - d0), (x - w0, y - d0)):
                    add(cx, cy, w0, d0)
    return out, lir


def place_room(ctx: Context, zone: dict, code: str, builder, pref, minimum, enc):
    region = ctx.open_floor()
    sketched = bool(zone.get("rect"))
    if sketched:
        x0, y0, x1, y1 = zone["rect"]
        region = region.intersection(box(x0 - 0.05, y0 - 0.05, x1 + 0.05, y1 + 0.05))
    if region.area < 1:
        raise CannotPlace(f"{zone['label']}: no clear floor in the sketched area")
    rects, lir = candidate_rects(region, sketched, (tuple(pref), tuple(pref[::-1])))
    best = None
    for X0, Y0, X1, Y1 in rects:
        for f, W, D in room_frames(X0, Y0, X1, Y1):
            if W < minimum[0] - 1e-6 or D < minimum[1] - 1e-6:
                continue
            edges = room_edges(ctx, f, W, D)
            for var in range(getattr(builder, "variants", 1)):
                built = builder(f, W, D, zone["key"], edges, enc, var) if var else builder(f, W, D, zone["key"], edges, enc)
                if not built:
                    continue
                pieces, (door_edge, door_span), walls, score = built
                walls["normals"] = normals(f)
                out = walls["normals"][door_edge]
                parts, doors = build_partitions(edges, walls, zone["key"], door_edge, door_span, enc["door"], f, (-out[0], -out[1]))
                rect = box(X0, Y0, X1, Y1)
                da, db = doors[0].a, doors[0].b
                approach = Polygon([da, db, (db[0] + out[0], db[1] + out[1]), (da[0] + out[0], da[1] + out[1])])
                trial = Context(ctx.unit, rect, ctx.band, ctx.swings + [x.swing for x in doors if x.swing], [], list(ctx.pieces))
                if conflicts(trial, pieces, rect):
                    continue
                if not ctx.free.buffer(0.01).covers(approach) or any(approach.intersection(q.poly).area > 1e-4 for q in ctx.pieces):
                    continue                                  # the door must open onto clear floor
                # Closed sides score higher: a room in a corner needs fewer new walls.
                closed = sum(1 - edges[n][4] for n in edges)
                key = (round(score + closed, 3), round((X1 - X0) * (Y1 - Y0), 2))
                if best is None or key > best[0]:
                    best = (key, pieces, parts, doors, rect, approach)
    if best is None:
        offer = f"; the sketched area offers {lir[2] - lir[0]:.1f} x {lir[3] - lir[1]:.1f} m" if lir else ""
        raise CannotPlace(f"{zone['label']}: needs at least {minimum[0]:.1f} x {minimum[1]:.1f} m of clear floor "
                          f"with one side open to the office{offer}")
    _, pieces, parts, doors, rect, approach = best
    ctx.approaches.append(approach)
    ctx.pieces += pieces
    ctx.partitions += parts
    ctx.doors += doors
    ctx.rooms.append(Room(zone["key"], code, rect))


# ------------------------------------------------------- open-plan modules

def wall_faces(ctx: Context, floor, rect=None):
    """Wall lines bounding the floor: (start, direction, inward normal, length, length of real wall), most wall first."""
    faces = []
    polys = [g for g in getattr(floor, "geoms", [floor]) if g.geom_type == "Polygon"]
    for poly in polys:
        ring = list(poly.exterior.coords)
        if not poly.exterior.is_ccw:
            ring.reverse()
        for a, b in zip(ring, ring[1:]):
            length = math.dist(a, b)
            if length < 1.0 or (abs(a[0] - b[0]) > 0.01 and abs(a[1] - b[1]) > 0.01):
                continue
            mid = Point((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
            if ctx.unit.exterior.distance(mid) < 0.05:
                continue                                  # a door or window opening, not a wall
            if rect is not None and not box(*rect).buffer(0.3).intersects(LineString([a, b])):
                continue
            if abs(a[0] - b[0]) > 0.01:
                a, b = (a[0], round(a[1], 3)), (b[0], round(a[1], 3))
            else:
                a, b = (round(a[0], 3), a[1]), (round(a[0], 3), b[1])
            d = (round((b[0] - a[0]) / length), round((b[1] - a[1]) / length))
            faces.append((a, b, d))
    # Join faces on the same line across door and window openings: furniture may
    # stand in front of an opening as long as it is clear of the swing.
    lines = {}
    for a, b, d in faces:
        along = 0 if d[0] else 1
        key = (d, round(a[1 - along] * 20))
        lines.setdefault(key, []).append((a, b))
    out = []
    for (d, _), segs in lines.items():
        along = 0 if d[0] else 1
        sign = d[along]
        pts = sorted((p for seg in segs for p in seg), key=lambda p: sign * p[along])
        start, end = pts[0], pts[-1]
        solid = sum(math.dist(a, b) for a, b in segs)
        out.append((start, d, (-d[1], d[0]), abs(end[along] - start[along]), solid))
    return sorted(out, key=lambda f: (-round(f[4], 2), -round(f[3], 2), f[0]))


def desk_module(kind, codes, zone):
    """Local pieces for a workstation group standing against a wall (v = 0). Returns (length, builder)."""
    cz, tc = CL["chair_zone"], RULES["task_chair"]
    dims = [RULES["desk"][c] for c in codes]

    def ws(f, rect, seat_uv, look, clear):
        desk = f.rect(*rect)
        return [Piece("desk", desk, zone, workstation=True),
                chair(f, seat_uv[0], seat_uv[1], look, tc, zone, desk, clear=f.rect(*clear), workstation=True)]

    if kind == "T":          # head desk with its user's back to the wall, two desks face to face as the stem
        (hl, hd), (sl, sd), (sl2, _) = dims
        L = max(hl, 2 * sd + 2 * cz)
        m = L / 2

        def build(f, var=0):
            off = (0.0, 0.3, -0.3)[var]              # the head user may sit off-centre to clear a door swing
            out = ws(f, (m - hl / 2, cz, m + hl / 2, cz + hd), (m + off, cz - 0.45), (0, 1), (m - hl / 2, 0, m + hl / 2, cz))
            out += ws(f, (m - sd, cz + hd, m, cz + hd + sl), (m - sd - 0.45, cz + hd + sl / 2), (1, 0), (m - sd - cz, cz + hd, m - sd, cz + hd + sl))
            out += ws(f, (m, cz + hd, m + sd, cz + hd + sl2), (m + sd + 0.45, cz + hd + sl2 / 2), (-1, 0), (m + sd, cz + hd, m + sd + cz, cz + hd + sl2))
            return out
        build.variants = 3
        return L, build, codes
    if kind == "pair":       # two desks face to face, end on to the wall
        (l1, d1), (l2, d2) = dims
        L = d1 + d2 + 2 * cz

        def build(f):
            out = ws(f, (cz, 0.05, cz + d1, 0.05 + l1), (cz - 0.45, 0.05 + l1 / 2), (1, 0), (0, 0.05, cz, 0.05 + l1))
            out += ws(f, (cz + d1, 0.05, cz + d1 + d2, 0.05 + l2), (cz + d1 + d2 + 0.45, 0.05 + l2 / 2), (-1, 0), (cz + d1 + d2, 0.05, L, 0.05 + l2))
            return out
        return L, build, codes
    (l1, d1), = dims           # single desk, user's back to the wall

    def build(f):
        return ws(f, (0.1, cz, 0.1 + l1, cz + d1), (0.1 + l1 / 2, cz - 0.45), (0, 1), (0.1, 0, 0.1 + l1, cz))
    return l1 + 0.2, build, codes


def lounge_module(zone):
    aw, ad = RULES["armchair"]

    def build(f):
        tc = f.pt(1.7, 0.42)
        mk = lambda u: Piece("armchair", f.rect(u - aw / 2, 0.06, u + aw / 2, 0.06 + ad), zone, solid=False, seat=True,
                             facing=f.vec(0, 1), target=None)
        table = Point(tc).buffer(0.3, 16)
        return [mk(0.85), mk(2.55), Piece("round_table", table, zone)]
    return 3.4, build


def box_module(kind, length, depth, zone, seat=None):
    def build(f):
        out = [Piece(kind, f.rect(0.05, 0.02, length - 0.05, depth), zone)]
        if seat:
            out += seat(f)
        return out
    return length, build


def place_on_wall(ctx: Context, modules: list, rect, label: str, building_walls_first=False, allow_rest=False):
    """Place modules along the wall faces of the open floor, spread evenly on the longest face that takes them."""
    floor = ctx.open_floor()
    faces = wall_faces(ctx, floor, rect)
    if building_walls_first:
        def on_partition(face):
            start, d, _, length, _ = face
            mid = Point(start[0] + d[0] * length / 2, start[1] + d[1] * length / 2)
            return any(r.rect.exterior.distance(mid) < 0.2 for r in ctx.rooms)
        faces = sorted(faces, key=on_partition)            # stable: keeps the most-wall-first order within each group
    remaining = list(modules)
    for start, d, n, length, _ in faces:
        if not remaining:
            break
        fit = [m for m in remaining]
        k = len(fit)
        placed_here = []
        for i, mod in enumerate(fit):
            L, build = mod[0], mod[1]
            if L > length + 1e-6:
                continue
            target = min(max(length * (i + 0.5) / k - L / 2, 0), length - L)
            steps = int((length - L) / STEP) + 1
            order = sorted(range(steps), key=lambda s: (abs(s * STEP - target), s))
            for s in order:
                t = s * STEP
                f = Frame((start[0] + d[0] * t, start[1] + d[1] * t), d, n)
                done = False
                for var in range(getattr(build, "variants", 1)):
                    pieces = build(f, var) if var else build(f)
                    if rect is not None and not all(box(*rect).buffer(0.3).contains(p.poly.centroid) for p in pieces):
                        continue
                    if conflicts(ctx, pieces, floor) is None:
                        ctx.pieces += pieces
                        placed_here.append(id(mod))
                        done = True
                        break
                if done:
                    break
        remaining = [m for m in remaining if id(m) not in placed_here]
    if remaining and not allow_rest:
        where = "the sketched area" if rect is not None else "the unit"
        raise CannotPlace(f"{label}: {len(remaining)} of {len(modules)} do not fit along the walls of {where} "
                          f"with the required clearances")
    return remaining


def place_islands(ctx: Context, codes: list, rect, label: str, total: int):
    """Free-standing bench blocks: desks face to face, up to four pairs long, 1.2 m aisle between blocks."""
    cz, tc = CL["chair_zone"], RULES["task_chair"]
    floor = ctx.open_floor()
    if rect is not None:
        floor = floor.intersection(box(*rect))
    x0, y0, x1, y1 = floor.bounds
    rows_along_y = (x1 - x0) >= (y1 - y0)            # rows run across the shorter dimension
    codes = list(codes)
    while codes:
        placed = False
        taken = [q.poly for q in ctx.pieces] + [q.clear for q in ctx.pieces if q.clear is not None] \
            + ctx.swings + ctx.approaches + ctx.aisles + [d.swing for d in ctx.doors if d.swing]
        avail = prep(floor.difference(unary_union(taken)).buffer(0.01) if taken else floor.buffer(0.01))
        for pairs in range(min(4, len(codes) // 2), 0, -1) if len(codes) > 1 else [0]:
            chunk = codes[:2 * pairs] if pairs else codes[:1]
            lens = [RULES["desk"][c][0] for c in chunk]
            depth = RULES["desk"][chunk[0]][1]
            run = sum(lens[0::2])
            across = 2 * cz + (2 * depth if pairs else depth)
            for along_y in (rows_along_y, not rows_along_y):
                w, h = (across, run) if along_y else (run, across)
                xs = [x0 + 0.2 * i for i in range(int((x1 - x0 - w) / 0.2) + 1)]
                ys = [y1 - h - 0.2 * j for j in range(int((y1 - y0 - h) / 0.2) + 1)]
                for x in xs:
                    for y in ys:
                        if not avail.covers(box(x, y, x + w, y + h)):
                            continue                          # quick reject before building the pieces
                        f = Frame((x, y), (1, 0), (0, 1)) if along_y else Frame((x, y + h), (0, -1), (1, 0))
                        pieces, t = [], 0.0
                        for i in range(0, len(chunk), 2):
                            L = RULES["desk"][chunk[i]][0]
                            da = f.rect(cz, t, cz + depth, t + L)
                            pieces += [Piece("desk", da, "WS", workstation=True),
                                       chair(f, cz - 0.45, t + L / 2, (1, 0), tc, "WS", da, clear=f.rect(0, t, cz, t + L), workstation=True)]
                            if i + 1 < len(chunk):
                                db = f.rect(cz + depth, t, cz + 2 * depth, t + L)
                                pieces += [Piece("desk", db, "WS", workstation=True),
                                           chair(f, cz + 2 * depth + 0.45, t + L / 2, (-1, 0), tc, "WS", db,
                                                 clear=f.rect(cz + 2 * depth, t, 2 * cz + 2 * depth, t + L), workstation=True)]
                            t += L
                        aisle = Piece("aisle", f.rect(cz, -0.6, across - cz, run + 0.6), "WS", solid=False)
                        if conflicts(ctx, pieces, floor) is None and not any(
                                aisle.poly.intersection(q.poly).area > 1e-4 for q in ctx.pieces if q.solid):
                            ctx.pieces += pieces
                            ctx.aisles.append(aisle.poly)
                            codes = codes[len(chunk):]
                            placed = True
                            break
                    if placed:
                        break
                if placed:
                    break
            if placed:
                break
        if not placed:
            done = total - len(codes)
            raise CannotPlace(f"{label}: {done} of {total} fit with the required clearances; "
                              f"{len(codes)} more do not. Reduce the number, free up floor, or use a larger unit")


def _desk_codes(brief):
    return [c for c in ("WS3", "WS2", "WS1") for _ in range(brief["workstations"].get(c, 0))]


def workstation_modules(brief: dict, zone_key: str):
    """Groups that stand against a wall. Standard fit-out uses free-standing benches instead."""
    if brief["fit_out_level"] not in ("L2", "L3"):
        return []
    codes = _desk_codes(brief)
    return [desk_module("T", codes[i:i + 3], zone_key) for i in range(0, len(codes) - len(codes) % 3, 3)]


def loose_desks(brief: dict):
    codes = _desk_codes(brief)
    if brief["fit_out_level"] not in ("L2", "L3"):
        return codes
    return codes[len(codes) - len(codes) % 3:]


# -------------------------------------------------------------------- main

def run(brief: dict, floor_model: dict) -> Context:
    if brief.get("schema") != "planner.brief/1":
        raise CannotPlace("not a planner.brief/1 brief")
    if brief["floor_drawing_sha256"] != floor_model["source"]["sha256"]:
        raise CannotPlace("the brief was written for a different version of the floor drawing; refill the form")
    ctx = context(floor_model, brief["unit"])
    zones = {z["key"]: z for z in brief["zones"]}
    enc = brief.get("enclosures", {})
    default = {"wall": "PW", "door": "HD"}
    sketched_first = sorted(zones.values(), key=lambda z: (z["rect"] is None, z["key"]))

    for z in sketched_first:                       # 1. rooms, sketched ones first
        code = z["key"].split("-")[0]
        if code == "OF2":
            place_room(ctx, z, code, mod_of2, RULES["rooms"]["OF2"]["pref"], RULES["rooms"]["OF2"]["min"], enc.get("office", default))
        elif code == "OF1":
            place_room(ctx, z, code, mod_of1, RULES["rooms"]["OF1"]["pref"], RULES["rooms"]["OF1"]["min"], enc.get("office", default))
        elif code in ("M4", "M6", "M10"):
            place_room(ctx, z, code, mod_meeting(code), RULES["rooms"][code]["pref"], RULES["rooms"][code]["min"], enc.get("meeting", default))
        elif code == "K" and brief["kitchen"]["type"] == "K2":
            builder, pref, minimum = mod_kitchen(brief["kitchen"]["seats"])
            place_room(ctx, z, code, builder, pref, minimum, enc.get("kitchen", {"wall": "GW", "door": "SD"}))
        elif code in ("ST", "SR"):
            place_room(ctx, z, code, mod_store, RULES["rooms"][code]["pref"], RULES["rooms"][code]["min"], default)

    for z in sketched_first:                       # 2. everything that stands against a wall
        code = z["key"].split("-")[0]
        if code == "WS":
            total = sum(brief["workstations"].values())
            mods = workstation_modules(brief, "WS")
            rest = place_on_wall(ctx, mods, z["rect"], z["label"], building_walls_first=True, allow_rest=True) if mods else []
            codes = [c for m in rest for c in m[2]] + loose_desks(brief)
            if codes:
                place_islands(ctx, codes, z["rect"], z["label"], total)
        elif code == "PB":
            place_on_wall(ctx, [box_module("booth", 1.1, 1.1, "PB") for _ in range(brief["rooms"]["PB"])], z["rect"], z["label"])
        elif code == "K" and brief["kitchen"]["type"] == "K1":
            place_on_wall(ctx, [box_module("counter", 2.4, 0.6, "K")], z["rect"], z["label"])
        elif code == "RC":
            seat = lambda f: [chair(f, 0.9, 1.25, (0, -1), RULES["task_chair"], "RC", f.rect(0.05, 0.02, 1.75, 0.8))]
            place_on_wall(ctx, [box_module("desk", 1.8, 0.8, "RC", seat)], z["rect"], z["label"])
        elif code == "LS":
            place_on_wall(ctx, [lounge_module("LS")], z["rect"], z["label"])
    if brief.get("terrace"):
        ctx.notes.append("Terrace furniture is not placed: the terrace outline is not in the floor model yet.")
    return ctx


def to_json(brief: dict, ctx: Context, report: dict) -> dict:
    r = lambda poly: [[round(x, 3), round(y, 3)] for x, y in poly.exterior.coords[:-1]]
    return {
        "schema": SCHEMA, "reference": brief.get("reference"), "building": brief["building"],
        "floor": brief["floor"], "unit": brief["unit"], "status": "ok" if report["ok"] else "refused",
        "checks": report, "notes": ctx.notes,
        "rooms": [{"zone": x.zone, "type": x.code, "rect": [round(v, 3) for v in x.rect.bounds]} for x in ctx.rooms],
        "partitions": [{"a": [round(v, 3) for v in p.a], "b": [round(v, 3) for v in p.b], "wall": p.wall, "zone": p.zone} for p in ctx.partitions],
        "doors": [{"a": [round(v, 3) for v in d.a], "b": [round(v, 3) for v in d.b], "type": d.kind, "zone": d.zone,
                   "swing": r(d.swing) if d.swing else None} for d in ctx.doors],
        "pieces": [{"kind": p.kind, "zone": p.zone, "polygon": r(p.poly),
                    "facing": [round(v, 3) for v in p.facing] if p.facing else None} for p in ctx.pieces],
    }
