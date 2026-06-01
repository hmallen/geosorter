"""Tests for time-clustered neighbor-GPS inference (pure core).

``infer_locations`` borrows a coordinate for a no-coord-but-timestamped item from
the nearest-in-time item that *has* a coordinate, but only within ``max_gap``.
"""

from datetime import datetime, timedelta, timezone

from geosorter import inference
from geosorter.inference import InferenceResult, infer_locations

UTC = timezone.utc


def _t(minute: int) -> datetime:
    """A UTC instant at a fixed base offset by ``minute`` minutes."""
    return datetime(2024, 6, 1, 12, 0, tzinfo=UTC) + timedelta(minutes=minute)


def test_borrows_nearest_within_window():
    items = [
        (0, _t(0), None),               # target: no coord, has timestamp
        (1, _t(10), (40.0, -105.0)),    # source 10 min away, within 30-min window
    ]
    out = infer_locations(items, max_gap=timedelta(minutes=30))
    assert out == {0: InferenceResult(item_id=0, lat=40.0, lon=-105.0, source_id=1)}


def test_omits_when_gap_exceeded():
    items = [
        (0, _t(0), None),
        (1, _t(45), (40.0, -105.0)),    # 45 min away, beyond the 30-min window
    ]
    assert infer_locations(items, max_gap=timedelta(minutes=30)) == {}


def test_omits_when_no_timestamp():
    items = [
        (0, None, None),                # no timestamp -> cannot be clustered
        (1, _t(5), (40.0, -105.0)),
    ]
    assert infer_locations(items, max_gap=timedelta(minutes=30)) == {}


def test_picks_nearer_of_two_straddling_candidates():
    items = [
        (0, _t(-20), (10.0, 10.0)),     # 20 min before
        (1, _t(0), None),               # target
        (2, _t(5), (40.0, -105.0)),     # 5 min after -> nearer
    ]
    out = infer_locations(items, max_gap=timedelta(minutes=30))
    assert out == {1: InferenceResult(item_id=1, lat=40.0, lon=-105.0, source_id=2)}


def test_coordless_items_are_never_sources():
    items = [
        (0, _t(0), None),               # target
        (1, _t(2), None),               # close in time but no coord -> not a source
    ]
    assert infer_locations(items, max_gap=timedelta(minutes=30)) == {}


def test_items_with_coord_are_not_inference_targets():
    items = [
        (0, _t(0), (1.0, 2.0)),         # already has a coord -> left alone
        (1, _t(5), (40.0, -105.0)),
    ]
    assert infer_locations(items, max_gap=timedelta(minutes=30)) == {}
