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
| 3b. Layout engine that reads the brief | Working: rooms, workstations, kitchen, lounge, reception, booths. Terrace furniture not yet |
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
    python -m planner layout buildings/acr_a/briefs/example_4_S.json   # checked layout: JSON, SVG, PNG
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

## The layout engine

`planner/layout.py` places by calculation from wall faces, door swings and the
clearances in `rules/furniture.json`. It tries candidate positions in a fixed
order and keeps the first that satisfies every rule, so one brief always gives
one layout. `planner/checks.py` then re-checks the result independently:
counts, free floor, overlaps, door swings, chair space, chairs facing their
table, walls meeting walls, and a 0.9 m route from the entrance to every room,
desk and seat. A layout that fails any check is refused and no drawing is
issued (`--debug` draws it anyway, marked as refused).

Sketched zones are honoured; unsketched parts are placed by the engine. Rooms
are tucked into corners. With middle-management or executive fit-out, desks
form T-clusters against the building's walls; otherwise, and for any that do
not fit on a wall, they form free-standing bench blocks of up to eight.

## Adding a floor or a building

1. Put the DWG in `buildings/<id>/source/`.
2. If the drawing uses different layer names, add a profile.
3. Add the floor to `building.json` with its unit outlines. `model.footprint()`
   proposes the outer face from the wall geometry; the outline is confirmed
   once by a person and then re-checked against the drawing on every run.

## Known limits

- `E_01_pince.DWG` (basement) is not in metres and is refused.
- Terrace furniture is not placed: terrace outlines are not in the floor model yet.
- A brief that cannot fit takes up to a minute to be refused; one that fits takes seconds.
- The footprint proposal is exact on floors 0 to 2 and within about 1% on
  floors 3 and 4, where terrace doors and inset windows confuse it. That is
  why outlines are confirmed data and not taken from the proposal.
