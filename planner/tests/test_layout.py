"""Regression: the layout engine on Building A briefs."""
import json

import pytest

from planner import checks, layout, model

OUT = model.ROOT / "buildings" / "acr_a" / "out"
BRIEFS = model.ROOT / "buildings" / "acr_a" / "briefs"

pytestmark = pytest.mark.skipif(not (OUT / "floor_4.json").exists(), reason="run 'python -m planner extract acr_a' first")


def load(name):
    brief = json.loads((BRIEFS / name).read_text())
    return brief, json.loads((OUT / f"floor_{brief['floor']}.json").read_text())


@pytest.mark.parametrize("name", ["example_4_S.json", "test_a_1_S.json"])
def test_brief_gives_a_layout_that_passes_every_check(name):
    brief, fm = load(name)
    ctx = layout.run(brief, fm)
    report = checks.validate(brief, ctx)
    assert report["ok"], [c for c in report["checks"] if not c["ok"]]


def test_same_brief_same_layout():
    brief, fm = load("example_4_S.json")
    a = layout.to_json(brief, layout.run(brief, fm), {"ok": True})
    b = layout.to_json(brief, layout.run(brief, fm), {"ok": True})
    assert a == b


def test_example_matches_the_built_layout():
    """Six desks in two T-clusters on the west wall, office on the courtyard side, kitchen at the back."""
    brief, fm = load("example_4_S.json")
    ctx = layout.run(brief, fm)
    rooms = {r.zone: r.rect.bounds for r in ctx.rooms}
    assert rooms["OF2-1"][1] < 8.5 and rooms["OF2-1"][2] > 223.5      # against the south facade and the core wall
    assert rooms["K"][3] > 18.9                                        # against the north facade
    desks = [p for p in ctx.pieces if p.kind == "desk" and p.workstation]
    assert len(desks) == 6 and all(p.poly.centroid.x < 216.5 for p in desks)


def test_overfull_brief_is_refused_with_a_reason():
    brief, fm = load("test_b_1_N.json")
    brief["workstations"] = {"WS2": 90}
    with pytest.raises(layout.CannotPlace) as e:
        layout.run(brief, fm)
    assert "more do not" in str(e.value)


def test_brief_for_another_drawing_version_is_refused():
    brief, fm = load("example_4_S.json")
    brief["floor_drawing_sha256"] = "0" * 64
    with pytest.raises(layout.CannotPlace):
        layout.run(brief, fm)
