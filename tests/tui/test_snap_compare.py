"""Failure paths of the in-repo `snap_compare` fixture, exercised directly against its
comparison/write helper -- no need to render a real Textual app for these.
"""

import asyncio

import pytest
from textual.app import App, ComposeResult
from textual.pilot import Pilot
from textual.widgets import Static

from tests.tui.conftest import _capture_svg, compare_svg_to_baseline


def test_missing_baseline_without_update_flag_fails(tmp_path):
    baseline = tmp_path / "__snapshots__" / "mod" / "test_thing.svg"

    with pytest.raises(AssertionError, match=r"No snapshot baseline.*--snapshot-update"):
        compare_svg_to_baseline("<svg>new</svg>", baseline, update=False, actual_dir=tmp_path)

    assert not baseline.exists()


def test_update_flag_writes_the_baseline_and_passes(tmp_path):
    baseline = tmp_path / "__snapshots__" / "mod" / "test_thing.svg"

    result = compare_svg_to_baseline("<svg>new</svg>", baseline, update=True, actual_dir=tmp_path)

    assert result is True
    assert baseline.read_text(encoding="utf-8") == "<svg>new</svg>"


def test_matching_baseline_passes_without_writing_an_actual_file(tmp_path):
    baseline = tmp_path / "__snapshots__" / "mod" / "test_thing.svg"
    baseline.parent.mkdir(parents=True)
    baseline.write_text("<svg>same</svg>", encoding="utf-8")

    result = compare_svg_to_baseline("<svg>same</svg>", baseline, update=False, actual_dir=tmp_path)

    assert result is True
    assert list(tmp_path.glob("test_thing.svg")) == []


def test_mismatch_fails_and_writes_the_actual_svg(tmp_path):
    baseline = tmp_path / "__snapshots__" / "mod" / "test_thing.svg"
    baseline.parent.mkdir(parents=True)
    baseline.write_text("<svg>old</svg>", encoding="utf-8")
    actual_dir = tmp_path / "actual"
    actual_dir.mkdir()

    with pytest.raises(AssertionError, match="Snapshot mismatch") as excinfo:
        compare_svg_to_baseline("<svg>new</svg>", baseline, update=False, actual_dir=actual_dir)

    assert str(baseline) in str(excinfo.value)
    actual_path = actual_dir / "test_thing.svg"
    assert str(actual_path) in str(excinfo.value)
    assert actual_path.read_text(encoding="utf-8") == "<svg>new</svg>"
    # The baseline itself is left untouched by a failed comparison.
    assert baseline.read_text(encoding="utf-8") == "<svg>old</svg>"


async def test_calling_snap_compare_from_an_async_test_raises_clearly(make_app, snap_compare):
    # pytest-asyncio already has a loop running for this test, so `snap_compare`'s
    # `asyncio.run` would raise a confusing error; it should instead fail fast with a
    # message telling the reader what to do differently, before touching the app at all.
    with pytest.raises(RuntimeError, match="snap_compare must be called from a sync test"):
        snap_compare(make_app())


class _LabelApp(App[None]):
    """A minimal app, unrelated to AuguryApp, just to exercise `run_before` in isolation."""

    def compose(self) -> ComposeResult:
        yield Static("before", id="label")


async def _change_the_label(pilot: Pilot[None]) -> None:
    pilot.app.query_one("#label", Static).update("after")


def test_run_before_is_applied_before_the_screenshot_is_taken():
    # Exercises `_capture_svg` (what `snap_compare` calls) directly with and without
    # `run_before`, and asserts on the exported SVG's own content -- no baseline file
    # needed, per the review's guidance for this test.
    plain_svg = asyncio.run(_capture_svg(_LabelApp(), (40, 10), None))
    changed_svg = asyncio.run(_capture_svg(_LabelApp(), (40, 10), _change_the_label))

    assert "before" in plain_svg
    assert "after" not in plain_svg
    assert "after" in changed_svg
    assert "before" not in changed_svg
