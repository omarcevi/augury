"""P9/P10 settings: [tui] copy_on_select and reading_width."""

import pytest

from augury.core.config import ConfigError, TuiConfig, load_config


def test_defaults():
    tui = TuiConfig()
    assert tui.copy_on_select is True
    assert tui.reading_width == 88


def test_both_can_be_set_in_config_toml(paths):
    paths.config_file.write_text("[tui]\ncopy_on_select = false\nreading_width = 72\n")
    tui = load_config(paths).tui
    assert tui.copy_on_select is False and tui.reading_width == 72


@pytest.mark.parametrize("width", [39, 201, 0, -5])
def test_reading_width_is_validated(paths, width):
    paths.config_file.write_text(f"[tui]\nreading_width = {width}\n")
    with pytest.raises(ConfigError, match=r"tui\.reading_width"):
        load_config(paths)


@pytest.mark.parametrize("width", [40, 200])
def test_reading_width_bounds_are_inclusive(paths, width):
    paths.config_file.write_text(f"[tui]\nreading_width = {width}\n")
    assert load_config(paths).tui.reading_width == width
