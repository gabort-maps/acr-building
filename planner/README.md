# planner

Turns construction drawings (DWG) into offer sheets, furnished layouts and 3D
views without manual drafting. Nothing is generated: every number in an output
is read from the drawing or from a config file and recomputed on every run.

## Status

| Stage | State |
| --- | --- |
| 1. Floor model from DWG | Working for Building A floors 0 to 4 |
| 2. Offer sheets | Not started |
| 3a. Brief form (programme, fit-out, rough sketch) | Working for Building A floors 0 to 4 |
| 3b. Layout engine that reads the brief | Not started |
| 4. 3D and renders | Not started |
| 5. Packaging | Not started |

## What is general and what is per building

- `planner/` is the engine. It contains no building-specific numbers.
- `profiles/<name>.json` describes one drawing convention: layer names, units.
- `buildings/<id>/building.json` describes one building: floors, source files,
  unit outlines, area rules.
- `buildings/<id>/source/` holds the DWGs. It is not committed.

## Run

    pip install -r requirements.txt
    python -m planner extract acr_a      # writes buildings/acr_a/out/floor_<n>.json
    python -m planner form acr_a         # writes buildings/acr_a/out/brief_form.html
    python -m pytest tests

`extract` prints each unit's measured area, the figure it would print on a
sheet, and how much of the outline lies on drawn walls. It exits non-zero if a
figure differs from the `expect_m2` recorded in the building config.

## Naming a place

`planner/navigate.py` cuts each unit into bays (counted from the end wall) and
rows (courtyard side to back side) along the free-standing column lines. The
brief form and the layout engine share these cells, so "bay 2, courtyard side"
is one rectangle for both.

## The brief

The form replaces a free-text prompt. It emits `planner.brief/1`: the unit,
the programme, walls and doors, the rough sketch as cells and a rectangle per
zone, and a capacity check against `rules/programme.json`. It issues no brief
for a programme that cannot fit.

## Adding a floor or a building

1. Put the DWG in `buildings/<id>/source/`.
2. If the drawing uses different layer names, add a profile.
3. Add the floor to `building.json` with its unit outlines. `model.footprint()`
   proposes the outer face from the wall geometry; the outline is confirmed
   once by a person and then re-checked against the drawing on every run.

## Known limits

- `E_01_pince.DWG` (basement) is not in metres and is refused.
- The footprint proposal is exact on floors 0 to 2 and within about 1% on
  floors 3 and 4, where terrace doors and inset windows confuse it. That is
  why outlines are confirmed data and not taken from the proposal.
