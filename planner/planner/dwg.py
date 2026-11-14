"""Read a DWG into plain geometry, using a drawing profile for layer names."""
from __future__ import annotations

import hashlib
import json
import math
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import ezdwg
from ezdxf import path as ezpath
from ezdxf import recover
from shapely.geometry import Polygon

ROOT = Path(__file__).resolve().parent.parent


def load_profile(name: str) -> dict:
    return json.loads((ROOT / "profiles" / f"{name}.json").read_text())


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass
class Drawing:
    """Geometry of one drawing, grouped by profile role (wall, window, ...)."""

    source: str
    sha256: str
    lines: dict = field(default_factory=dict)    # role -> [((x1,y1),(x2,y2))]
    arcs: dict = field(default_factory=dict)     # role -> [(cx,cy,r,a0,a1)]
    hatches: dict = field(default_factory=dict)  # role -> [Polygon]
    texts: dict = field(default_factory=dict)    # role -> [(x,y,text)]
    skipped_hatches: int = 0


def _hatch_polygons(entity) -> list[Polygon]:
    out = []
    for boundary in entity.paths:
        pts = [(v.x, v.y) for v in ezpath.from_hatch_boundary_path(boundary).flattening(0.005)]
        if len(pts) < 3:
            continue
        poly = Polygon(pts)
        if not poly.is_valid:
            poly = poly.buffer(0)
        if poly.area > 1e-4:
            out.append(poly)
    return out


def read(dwg_path: Path, profile: dict) -> Drawing:
    dwg_path = Path(dwg_path)
    with tempfile.TemporaryDirectory() as tmp:
        dxf = Path(tmp) / "in.dxf"
        ezdwg.read(str(dwg_path)).export_dxf(str(dxf))
        doc, _ = recover.readfile(str(dxf))

    prefix = re.compile(profile["layer_prefix_regex"])
    role_of = {}
    for role, layers in profile["layers"].items():
        for layer in layers:
            role_of[layer] = role
    scale = 1.0 / profile["units_per_metre"]

    d = Drawing(source=dwg_path.name, sha256=sha256(dwg_path))
    # The floor sits in whichever block holds the most entities (model space
    # or a DRAWING_n block, it varies per file).
    block = max(doc.blocks, key=len)
    for e in block:
        role = role_of.get(prefix.sub("", e.dxf.layer))
        if role is None or role == "ignore":
            continue
        kind = e.dxftype()
        if kind == "LINE":
            a, b = e.dxf.start, e.dxf.end
            d.lines.setdefault(role, []).append(((a.x * scale, a.y * scale), (b.x * scale, b.y * scale)))
        elif kind == "ARC":
            c = e.dxf.center
            d.arcs.setdefault(role, []).append(
                (c.x * scale, c.y * scale, e.dxf.radius * scale, e.dxf.start_angle, e.dxf.end_angle))
        elif kind == "HATCH":
            try:
                polys = _hatch_polygons(e)
            except Exception:
                d.skipped_hatches += 1
                continue
            d.hatches.setdefault(role, []).extend(polys)
        elif kind in ("MTEXT", "TEXT"):
            p = e.dxf.insert
            text = e.plain_text() if kind == "MTEXT" else e.dxf.text
            d.texts.setdefault(role, []).append((p.x * scale, p.y * scale, text.strip()))
    return d


def is_construction_line(seg, min_len: float) -> bool:
    """Long diagonal lines are drafting aids, not building geometry."""
    (x1, y1), (x2, y2) = seg
    dx, dy = abs(x2 - x1), abs(y2 - y1)
    return math.hypot(dx, dy) > min_len and min(dx, dy) > 0.05
