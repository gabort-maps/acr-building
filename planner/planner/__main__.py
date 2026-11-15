"""Command line:
    python -m planner extract <building_id>   floor models and area check
    python -m planner form <building_id>      brief form (HTML)
    python -m planner layout <brief.json>     checked layout (JSON, SVG, PNG); add --debug to draw a refused one
"""
import sys

import json
import re
from pathlib import Path

from . import checks, draw, form, layout, model


def run_layout(brief_path: Path, debug: bool) -> int:
    brief = json.loads(brief_path.read_text())
    out_dir = model.ROOT / "buildings" / brief["building"] / "out"
    fm = json.loads((out_dir / f"floor_{brief['floor']}.json").read_text())
    name = re.sub(r"[^a-z0-9]+", "_", (brief.get("reference") or brief_path.stem).lower()).strip("_")
    print(f"{brief.get('reference') or brief_path.name}: floor {brief['floor']}, unit {brief['unit']}")
    try:
        ctx = layout.run(brief, fm)
    except layout.CannotPlace as e:
        print(f"  REFUSED  {e}")
        return 1
    report = checks.validate(brief, ctx)
    for c in report["checks"]:
        print(f"  {'pass' if c['ok'] else 'FAIL'}  {c['check']}")
        for p in c["problems"]:
            print(f"          - {p}")
    for n in ctx.notes:
        print(f"  note  {n}")
    target = out_dir / "layouts"
    target.mkdir(exist_ok=True)
    (target / f"{name}.json").write_text(json.dumps(layout.to_json(brief, ctx, report), indent=1))
    if report["ok"] or debug:
        unit = next(u for u in fm["units"] if u["id"] == brief["unit"])
        svg = target / f"{name}.svg"
        svg.write_text(draw.svg(fm, ctx, f"{fm['name']} · {unit['name']} · {brief.get('reference') or ''}", report))
        draw.to_png(svg, target / f"{name}.png")
        print(f"  drawing: {target / (name + '.png')}")
    print("  LAYOUT OK" if report["ok"] else "  LAYOUT REFUSED: no sheet is issued")
    return 0 if report["ok"] else 1


def main(argv):
    if len(argv) >= 3 and argv[1] == "layout":
        return run_layout(Path(argv[2]), "--debug" in argv)
    if len(argv) != 3 or argv[1] not in ("extract", "form"):
        print(__doc__)
        return 2
    if argv[1] == "form":
        print(form.build(argv[2]))
        return 0
    building = model.load_building(argv[2])
    expect = {(f["id"], u["id"]): u.get("expect_m2") for f in building["floors"] for u in f["units"]}
    failed = False
    print(f"{building['name']}")
    for fm in model.build_all(argv[2]):
        for u in fm["units"]:
            want = expect[(fm["floor"], u["id"])]
            ok = want is None or want == u["area_label_m2"]
            failed |= not ok
            c = u["check"]
            print(f"  floor {fm['floor']:<2} {u['name']:<11} {u['area_m2']:>8.2f} m²  -> {u['area_label_m2']:>4} m²"
                  f"   expected {want}   {'OK' if ok else 'MISMATCH'}"
                  f"   on drawn walls {c['on_drawn_walls']:.0%}, inside proposal {c['inside_proposed_footprint']:.1%}")
            gaps = c["unsupported_stretches"]
            if gaps:
                longest = max(gaps, key=lambda g: g["length_m"])
                print(f"             {len(gaps)} stretches without a drawn wall (openings, core partitions);"
                      f" longest {longest['length_m']} m at {longest['from']}")
        print(f"           walls {len(fm['walls'])}, columns {len(fm['columns'])}, windows {len(fm['windows'])},"
              f" doors {len(fm['doors'])}, swings {len(fm['door_swings'])}")
    for skipped in building.get("not_supported", []):
        print(f"  REFUSED {skipped['source']}: {skipped['reason']}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
