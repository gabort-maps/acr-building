"""Generate the brief form: one self-contained HTML page per building.

The form replaces a free-text prompt. It shows each unit with its bays and
rows, lets the user paint rough zones, collects the programme and fit-out,
checks that the programme can fit, and emits a structured brief.
"""
from __future__ import annotations

import json

from shapely.geometry import Polygon

from . import model, navigate

ROOT = model.ROOT


def _floor_payload(building: dict, fm: dict) -> dict:
    cfg = next(f for f in building["floors"] if f["id"] == fm["floor"])
    columns = [c["centre"] for c in fm["columns"]]
    centre_x = sum(Polygon(u["polygon"]["outer"]).centroid.x for u in fm["units"]) / len(fm["units"])
    units = []
    for u in fm["units"]:
        poly = Polygon(u["polygon"]["outer"])
        ucfg = next(c for c in cfg["units"] if c["id"] == u["id"])
        end_wall = "low_x" if poly.centroid.x < centre_x else "high_x"
        units.append({
            "id": u["id"], "name": u["name"], "area_m2": u["area_m2"], "area_label_m2": u["area_label_m2"],
            "polygon": u["polygon"]["outer"], "terrace": bool(ucfg.get("terrace")),
            "grid": navigate.unit_cells(poly, columns, end_wall),
        })
    fp = Polygon(fm["footprint"]["outer"])
    return {
        "id": fm["floor"], "name": fm["name"], "source_sha256": fm["source"]["sha256"],
        "bbox": [round(v, 2) for v in fp.bounds],
        "walls": [[[round(x, 2), round(y, 2)] for x, y in w["outer"]] for w in fm["walls"]],
        "columns": [[[round(x, 2), round(y, 2)] for x, y in c["polygon"]["outer"]] for c in fm["columns"]],
        "units": units,
    }


def build(building_id: str):
    building = model.load_building(building_id)
    out_dir = ROOT / "buildings" / building_id / "out"
    floors = []
    for f in building["floors"]:
        path = out_dir / f"floor_{f['id']}.json"
        if not path.exists():
            raise model.ModelError(f"run 'extract {building_id}' first: {path.name} is missing")
        floors.append(_floor_payload(building, json.loads(path.read_text())))
    example_path = ROOT / "buildings" / building_id / "example_brief.json"
    data = {
        "building": {"id": building["id"], "name": building["name"], "address": building["address"],
                     "north_angle_deg": building["north_angle_deg"]},
        "rules": json.loads((ROOT / "rules" / "programme.json").read_text()),
        "floors": floors,
        "example": json.loads(example_path.read_text()) if example_path.exists() else None,
    }
    template = (ROOT / "planner" / "templates" / "brief_form.html").read_text()
    html = template.replace("/*__DATA__*/null", json.dumps(data, separators=(",", ":"), ensure_ascii=False))
    out = out_dir / "brief_form.html"
    out.write_text(html)
    return out
