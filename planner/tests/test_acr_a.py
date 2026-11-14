"""Regression: Building A areas must match the figures on the issued offer sheets."""
import pytest

from planner import model

BUILDING = model.load_building("acr_a")
SOURCE = model.ROOT / "buildings" / "acr_a" / "source"
CASES = [(f, u) for f in BUILDING["floors"] for u in f["units"]]

pytestmark = pytest.mark.skipif(not any(SOURCE.glob("*.DWG")), reason="source drawings are not in the repository")


@pytest.fixture(scope="module")
def floors():
    return {f["id"]: model.build_floor(BUILDING, f) for f in BUILDING["floors"]}


@pytest.mark.parametrize("floor,unit", CASES, ids=[f"floor{f['id']}-{u['id']}" for f, u in CASES])
def test_area_matches_offer_sheet(floors, floor, unit):
    got = next(u for u in floors[floor["id"]]["units"] if u["id"] == unit["id"])
    assert got["area_label_m2"] == unit["expect_m2"]


@pytest.mark.parametrize("floor,unit", CASES, ids=[f"floor{f['id']}-{u['id']}" for f, u in CASES])
def test_outline_agrees_with_drawing(floors, floor, unit):
    check = next(u for u in floors[floor["id"]]["units"] if u["id"] == unit["id"])["check"]
    assert check["on_drawn_walls"] >= 0.70          # the rest is openings and core partitions
    assert check["inside_proposed_footprint"] >= 0.985  # never outside the building


def test_same_input_same_output(floors):
    again = model.build_floor(BUILDING, BUILDING["floors"][4])
    assert again == floors["4"]
