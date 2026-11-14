"""Command line:
    python -m planner extract <building_id>   floor models and area check
    python -m planner form <building_id>      brief form (HTML)
"""
import sys

from . import form, model


def main(argv):
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
