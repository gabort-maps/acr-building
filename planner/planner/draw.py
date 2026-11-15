"""Draw a layout as SVG (1 m = 40 px) and rasterise it with headless Chromium."""
from __future__ import annotations

import math
import subprocess
from pathlib import Path

from shapely.geometry import Point, Polygon

from .layout import Context

PX = 40
STYLE = """
.bg{fill:#fff}.wall{fill:#262d35}.other{fill:#c9cfd6}.win{stroke:#7f8a96;stroke-width:1}
.swing{fill:none;stroke:#8a949e;stroke-width:.8}.pw{stroke:#262d35;stroke-width:4}.aw{stroke:#262d35;stroke-width:6}
.gw{stroke:#5b9bd5;stroke-width:3}.door{fill:none;stroke:#262d35;stroke-width:1}.slide{stroke:#5b9bd5;stroke-width:5;stroke-linecap:round}
.desk,.table,.round_table{fill:#f4efe6;stroke:#3a424c;stroke-width:1}.counter,.shelf,.booth,.pedestal{fill:#d9dde2;stroke:#3a424c;stroke-width:1}
.tall{fill:#aab2bb;stroke:#3a424c;stroke-width:1}.chair,.armchair{fill:#e3e8ee;stroke:#3a424c;stroke-width:1}.back{stroke:#3a424c;stroke-width:3}
.lbl{font:500 12px Inter,Arial,sans-serif;fill:#6b7580}.ttl{font:600 15px Inter,Arial,sans-serif;fill:#262d35}
.bad{font:600 13px Inter,Arial,sans-serif;fill:#b8352a}
"""


def svg(floor_model: dict, ctx: Context, title: str, report: dict) -> str:
    x0, y0, x1, y1 = ctx.unit.bounds
    m = 1.6
    X = lambda x: round((x - x0 + m) * PX, 1)
    Y = lambda y: round((y1 - y + m) * PX, 1)
    pts = lambda coords: " ".join(f"{X(x)},{Y(y)}" for x, y in coords)
    w, h = (x1 - x0 + 2 * m) * PX, (y1 - y0 + 2 * m) * PX + 30
    view = ctx.unit.buffer(m)
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w:.0f}" height="{h:.0f}" viewBox="0 0 {w:.0f} {h:.0f}">',
         f"<style>{STYLE}</style>", f'<rect class="bg" width="{w:.0f}" height="{h:.0f}"/>']
    for a, b in floor_model["windows"]:
        if view.contains(Point(a)):
            o.append(f'<line class="win" x1="{X(a[0])}" y1="{Y(a[1])}" x2="{X(b[0])}" y2="{Y(b[1])}"/>')
    for wall in floor_model["walls"]:
        p = Polygon(wall["outer"])
        if p.intersects(view):
            o.append(f'<polygon class="{"wall" if p.intersects(ctx.unit.buffer(0.05)) else "other"}" points="{pts(wall["outer"])}"/>')
    for c in floor_model["columns"]:
        if Polygon(c["polygon"]["outer"]).intersects(view):
            o.append(f'<polygon class="wall" points="{pts(c["polygon"]["outer"])}"/>')
    for s in ctx.swings:
        o.append(f'<polygon class="swing" points="{pts(s.exterior.coords[:-1])}"/>')
    for p in ctx.partitions:
        o.append(f'<line class="{p.wall.lower()}" x1="{X(p.a[0])}" y1="{Y(p.a[1])}" x2="{X(p.b[0])}" y2="{Y(p.b[1])}"/>')
    for d in ctx.doors:
        if d.swing is not None:
            o.append(f'<polygon class="door" points="{pts(d.swing.exterior.coords[:-1])}"/>')
        else:
            dx, dy = d.b[0] - d.a[0], d.b[1] - d.a[1]
            o.append(f'<line class="slide" x1="{X(d.a[0] + dx * .5)}" y1="{Y(d.a[1] + dy * .5)}" x2="{X(d.b[0] + dx * .45)}" y2="{Y(d.b[1] + dy * .45)}"/>')
    order = {"chair": 0, "armchair": 0}
    for p in sorted((q for q in ctx.pieces if q.kind != 'aisle'), key=lambda q: order.get(q.kind, 1)):
        coords = list(p.poly.exterior.coords[:-1])
        o.append(f'<polygon class="{p.kind}" points="{pts(coords)}"/>')
        if p.facing and len(coords) == 4:
            c = p.poly.centroid
            half = math.dist(coords[0], coords[1]) / 2
            bx, by = c.x - p.facing[0] * half, c.y - p.facing[1] * half
            tx, ty = -p.facing[1] * half * .8, p.facing[0] * half * .8
            o.append(f'<line class="back" x1="{X(bx - tx)}" y1="{Y(by - ty)}" x2="{X(bx + tx)}" y2="{Y(by + ty)}"/>')
    o.append(f'<text class="ttl" x="{m * PX * .5:.0f}" y="22">{title}</text>')
    o.append(f'<text class="lbl" x="{X(x0) + 14}" y="{h - 12:.0f}">{ctx.unit.area:.2f} m² measured · indicative layout, not for construction</text>')
    if not report["ok"]:
        o.append(f'<text class="bad" x="{w - 250:.0f}" y="22">REFUSED: fails a rule (debug view)</text>')
    o.append("</svg>")
    return "\n".join(o)


def to_png(svg_path: Path, png_path: Path):
    script = (
        "const {chromium}=require('playwright');const fs=require('fs');(async()=>{"
        "const s=fs.readFileSync(process.argv[1],'utf8');const m=s.match(/width=\"(\\d+)\" height=\"(\\d+)\"/);"
        "const b=await chromium.launch();const p=await b.newPage({viewport:{width:+m[1],height:+m[2]},deviceScaleFactor:2});"
        "await p.setContent('<body style=\"margin:0\">'+s+'</body>');await p.screenshot({path:process.argv[2]});await b.close();})();"
    )
    subprocess.run(["node", "-e", script, str(svg_path), str(png_path)], check=True, cwd="/")
