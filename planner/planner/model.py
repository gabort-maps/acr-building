"""Build the floor model: one JSON per floor that every later stage reads."""
from __future__ import annotations

import json
import math
from pathlib import Path

from shapely.geometry import LineString, Polygon
from shapely.ops import unary_union

from . import dwg

ROOT = dwg.ROOT
SCHEMA = "planner.floor/1"
FACE_BRIDGE_M = 6.0  # half the widest facade opening the proposal will bridge


class ModelError(Exception):
    """Raised when the drawing and the config disagree. Never papered over."""


def load_building(building_id: str) -> dict:
    return json.loads((ROOT / "buildings" / building_id / "building.json").read_text())


def _polygons(geom) -> list:
    if geom.geom_type == "Polygon":
        return [] if geom.is_empty else [geom]
    return [p for g in getattr(geom, "geoms", []) for p in _polygons(g)]


def _largest(geom) -> Polygon:
    polys = _polygons(geom)
    return max(polys, key=lambda g: g.area) if polys else Polygon()


def _filled_outline(geom) -> Polygon:
    """The component that encloses the most ground, with its holes filled."""
    return max((Polygon(p.exterior) for p in _polygons(geom)), key=lambda p: p.area)


def footprint(drawing: dwg.Drawing, profile: dict) -> Polygon:
    """Proposed outer face of the building, computed from the wall geometry.

    Used to draft outlines for a new floor and as a cross-check. The outlines
    that areas are measured from are config data, confirmed once per floor.
    """
    min_len = profile["construction_line_min_length_m"]
    eps = 0.002  # lines get a hair of width so they can be unioned

    def strokes(roles):
        return [LineString(seg).buffer(eps, cap_style=2)
                for role in roles for seg in drawing.lines.get(role, [])
                if not dwg.is_construction_line(seg, min_len)]

    def enclose(geoms, r):
        # Grow, fill the inside, shrink back. Filling before shrinking keeps
        # hair-thin lines from vanishing.
        return _largest(_filled_outline(unary_union(geoms).buffer(r, join_style=2)).buffer(-r, join_style=2))

    def open_(geom, r):
        return geom.buffer(-r, join_style=2).buffer(r, join_style=2)

    solid = list(drawing.hatches.get("wall", [])) + list(drawing.hatches.get("column_hatch", []))
    if not solid:
        raise ModelError(f"{drawing.source}: no wall geometry found with this profile")
    # 1. Shape: walls plus door and window lines close every opening, so the
    #    outline has the right steps and notches, but door frames, sills and
    #    window tick marks make it slightly too fat.
    shape = _largest(open_(enclose(solid + strokes(("wall", "window", "door")), 0.75), 0.3))
    # 2. Face: the walls alone (without door mullions and other specks),
    #    bridged across every opening. Its edge is the true outer face of the
    #    facade, but bridging that far also fills courtyards and notches.
    lo = profile["min_wall_hatch_m2"]
    structure = [p for p in solid if p.area >= lo]
    face = enclose(structure + strokes(("wall",)), FACE_BRIDGE_M)
    # The proposal has the shape of (1) and the face of (2).
    return _largest(shape.intersection(face))


def wall_evidence(drawing: dwg.Drawing, profile: dict):
    """Everything in the drawing that can justify an outline edge."""
    min_len = profile["construction_line_min_length_m"]
    lines = [LineString(seg) for role in ("wall", "window", "door") for seg in drawing.lines.get(role, [])
             if not dwg.is_construction_line(seg, min_len)]
    return unary_union(list(drawing.hatches.get("wall", [])) + list(drawing.hatches.get("column_hatch", [])) + lines)


def outline_support(outline: Polygon, evidence, tol: float = 0.03, step: float = 0.05):
    """Share of the outline that lies on drawn wall geometry, and the gaps.

    The outline is config data. This is the check that it still agrees with
    the drawing: an edge floating in empty space is reported, never accepted
    silently.
    """
    ring = outline.exterior
    n = max(1, int(ring.length / step))
    hits = [evidence.distance(ring.interpolate(i * ring.length / n)) <= tol for i in range(n)]
    gaps, start = [], None
    for i, ok in enumerate(hits + [True]):
        if not ok and start is None:
            start = i
        elif ok and start is not None:
            length = (i - start) * ring.length / n
            if length >= 0.5:
                a, b = ring.interpolate(start * ring.length / n), ring.interpolate(i * ring.length / n)
                gaps.append({"from": [round(a.x, 2), round(a.y, 2)], "to": [round(b.x, 2), round(b.y, 2)],
                             "length_m": round(length, 2)})
            start = None
    return sum(hits) / n, gaps


def _round(coords, nd=3):
    return [[round(x, nd), round(y, nd)] for x, y in coords]


def _poly_json(poly: Polygon) -> dict:
    return {"outer": _round(poly.exterior.coords[:-1]),
            "holes": [_round(r.coords[:-1]) for r in poly.interiors]}


def round_area(area: float, rule: str) -> int:
    if rule == "ceil":
        return math.ceil(area - 1e-9)
    raise ModelError(f"unknown rounding rule {rule!r}")


def build_floor(building: dict, floor: dict) -> dict:
    profile = dwg.load_profile(building["profile"])
    src = ROOT / "buildings" / building["id"] / "source" / floor["source"]
    if not src.exists():
        raise ModelError(f"missing source drawing {src}")
    drawing = dwg.read(src, profile)
    fp = footprint(drawing, profile)

    lo, hi = profile["column_area_m2"]
    columns = [p for p in drawing.hatches.get("column_hatch", []) if lo <= p.area <= hi]
    walls = [p for p in drawing.hatches.get("wall", []) if p.intersects(fp.buffer(0.6))]

    evidence = wall_evidence(drawing, profile)
    units = []
    for u in floor["units"]:
        outline = building["outlines"][u["outline"]]
        poly = Polygon(outline["polygon"])
        if not poly.is_valid or poly.is_empty:
            raise ModelError(f"floor {floor['id']} unit {u['id']}: outline is not a valid polygon")
        support, gaps = outline_support(poly, evidence)
        overlap = poly.intersection(fp).area / poly.area
        units.append({
            "id": u["id"], "name": u["name"],
            "polygon": _poly_json(poly),
            "area_m2": round(poly.area, 3),
            "area_label_m2": round_area(poly.area, building["area_rules"]["rounding"]),
            "outline_source": outline["source"],
            "check": {"on_drawn_walls": round(support, 3), "unsupported_stretches": gaps,
                      "inside_proposed_footprint": round(overlap, 4)},
        })

    min_len = profile["construction_line_min_length_m"]
    def segs(role):
        return [[list(map(lambda v: round(v, 3), a)), list(map(lambda v: round(v, 3), b))]
                for a, b in drawing.lines.get(role, []) if not dwg.is_construction_line((a, b), min_len)]

    return {
        "schema": SCHEMA,
        "building": building["id"],
        "floor": floor["id"],
        "name": floor["name"],
        "source": {"file": drawing.source, "sha256": drawing.sha256, "profile": profile["name"],
                   "skipped_hatches": drawing.skipped_hatches},
        "frame": {"units": "m", "north_angle_deg": building["north_angle_deg"]},
        "footprint": _poly_json(fp),
        "units": units,
        "walls": [_poly_json(p) for p in walls],
        "columns": [{"centre": [round(p.centroid.x, 3), round(p.centroid.y, 3)],
                     "polygon": _poly_json(p)} for p in columns],
        "windows": segs("window"),
        "doors": segs("door"),
        "door_swings": [[round(v, 3) for v in a] for a in drawing.arcs.get("door", [])],
        "core_lines": segs("core_lines"),
        "levels": sorted({t for _, _, t in drawing.texts.get("levels", []) if t}),
    }


def build_all(building_id: str) -> list[dict]:
    building = load_building(building_id)
    out_dir = ROOT / "buildings" / building_id / "out"
    out_dir.mkdir(exist_ok=True)
    floors = []
    for floor in building["floors"]:
        model = build_floor(building, floor)
        (out_dir / f"floor_{floor['id']}.json").write_text(json.dumps(model, separators=(",", ":")))
        floors.append(model)
    return floors
