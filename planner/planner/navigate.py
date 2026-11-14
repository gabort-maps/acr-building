"""Shared vocabulary for naming a place on a floor.

A unit is cut into bays (along the building, counted from the end wall) and
rows (across it, from the courtyard side to the back side), following the
column lines. Each bay-row cell is halved both ways so a rough sketch can be
painted at about 2.5 m resolution. The same cells are used by the brief form
and by the layout engine, so "bay 2, courtyard side" means one rectangle.
"""
from __future__ import annotations

import math

from shapely.geometry import Polygon, box

MAX_SPAN_M = 6.5     # a span wider than this is split evenly
EDGE_CLEAR_M = 1.5   # column lines this close to the unit edge do not start a bay
CLUSTER_M = 0.8      # columns within this distance share a line
MIN_CELL_M2 = 0.5

ROW_NAMES = {
    1: ["full depth"],
    2: ["courtyard side", "back side"],
    3: ["courtyard side", "centre", "back side"],
    4: ["courtyard side", "centre, courtyard half", "centre, back half", "back side"],
}


def _lines(values, lo, hi):
    inner = sorted(v for v in values if lo + EDGE_CLEAR_M < v < hi - EDGE_CLEAR_M)
    clusters = []
    for v in inner:
        if clusters and v - clusters[-1][-1] <= CLUSTER_M:
            clusters[-1].append(v)
        else:
            clusters.append([v])
    cuts = [lo] + [sum(c) / len(c) for c in clusters] + [hi]
    out = [cuts[0]]
    for a, b in zip(cuts, cuts[1:]):
        parts = max(1, math.ceil((b - a) / MAX_SPAN_M - 1e-9))
        out += [a + (b - a) * k / parts for k in range(1, parts + 1)]
    return out


def unit_cells(unit_polygon: Polygon, column_centres, end_wall: str) -> dict:
    """end_wall is 'low_x' or 'high_x': the side bays are counted from."""
    x0, y0, x1, y1 = unit_polygon.bounds
    # Only free-standing columns set the grid; columns in or near a wall do not.
    inside = [c for c in column_centres
              if unit_polygon.contains(_pt(c)) and unit_polygon.exterior.distance(_pt(c)) > EDGE_CLEAR_M]
    xs = _lines([c[0] for c in inside], x0, x1)
    ys = _lines([c[1] for c in inside], y0, y1)
    n_bays, n_rows = len(xs) - 1, len(ys) - 1
    names = ROW_NAMES.get(n_rows) or [f"row {i + 1} from the courtyard" for i in range(n_rows)]
    cells = []
    for i in range(n_bays):
        bay = i + 1 if end_wall == "low_x" else n_bays - i
        for j in range(n_rows):
            xm, ym = (xs[i] + xs[i + 1]) / 2, (ys[j] + ys[j + 1]) / 2
            for sub, (a, b, c, d) in enumerate(((xs[i], ys[j], xm, ym), (xm, ys[j], xs[i + 1], ym),
                                                (xs[i], ym, xm, ys[j + 1]), (xm, ym, xs[i + 1], ys[j + 1]))):
                piece = unit_polygon.intersection(box(a, b, c, d))
                if piece.area < MIN_CELL_M2:
                    continue
                piece = max(getattr(piece, "geoms", [piece]), key=lambda g: g.area)
                cells.append({
                    "id": f"b{bay}r{j + 1}{'abcd'[sub]}", "bay": bay, "row": j + 1, "row_name": names[j],
                    "polygon": [[round(x, 3), round(y, 3)] for x, y in piece.exterior.coords[:-1]],
                    "area_m2": round(piece.area, 2),
                    "bbox": [round(v, 3) for v in piece.bounds],
                })
    return {"bays": n_bays, "rows": n_rows, "row_names": names, "end_wall": end_wall,
            "bay_lines": [round(v, 3) for v in xs], "row_lines": [round(v, 3) for v in ys], "cells": cells}


def _pt(c):
    from shapely.geometry import Point
    return Point(c[0], c[1])
