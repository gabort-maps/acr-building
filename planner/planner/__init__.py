"""Deterministic floor-plan pipeline: DWG -> floor model -> sheets, layouts, 3D.

Nothing in this package generates geometry. Every number in an output is either
read from the source drawing or from a per-building config file, and is
recomputed on every run.
"""
