"""P14: one setting changed in config.toml, and nothing else about the file."""

import tomllib

import pytest

from augury.core.config import ConfigError
from augury.core.config_edit import (
    MalformedConfig,
    bounds,
    check_value,
    choices,
    default_of,
    editor_kind,
    reset_value,
    set_value,
)


def toml(paths) -> dict:
    return tomllib.loads(paths.config_file.read_text(encoding="utf-8"))


def test_a_missing_config_toml_is_created_with_just_that_key(paths):
    assert not paths.config_file.exists()
    config = set_value(paths, "tui", "tldr", "collapsed")
    assert config.tui.tldr == "collapsed"
    assert toml(paths) == {"tui": {"tldr": "collapsed"}}


def test_an_edit_keeps_comments_order_and_every_other_key(paths):
    original = (
        "# my settings\n"
        "[export]\n"
        'path = ""  # off for now\n'
        "\n"
        "[scout]\n"
        "# twice a day\n"
        "auto_after_hours = 12\n"
    )
    paths.config_file.write_text(original, encoding="utf-8")
    set_value(paths, "scout", "prefetch_top_n", "5")
    after = paths.config_file.read_text(encoding="utf-8")
    assert after.startswith(original)  # every byte of it, the new key after the last one
    assert toml(paths)["scout"] == {"auto_after_hours": 12, "prefetch_top_n": 5}


def test_a_value_is_written_as_its_own_type(paths):
    set_value(paths, "tui", "reading_width", "100")
    set_value(paths, "scout", "auto_after_hours", "6")
    set_value(paths, "tui", "copy_on_select", False)
    assert toml(paths) == {
        "tui": {"reading_width": 100, "copy_on_select": False},
        "scout": {"auto_after_hours": 6.0},
    }
    assert isinstance(toml(paths)["scout"]["auto_after_hours"], float)


@pytest.mark.parametrize(
    ("section", "field", "value", "problem"),
    [
        ("tui", "reading_width", "20", "greater than or equal to 40"),
        ("tui", "reading_width", "wide", "valid integer"),
        ("scout", "auto_after_hours", "-1", "greater than or equal to 0"),
        ("tui", "tldr", "sometimes", "'shown' or 'collapsed'"),
        ("models", "fast", "just-a-name", "provider/model"),
    ],
)
def test_an_invalid_value_is_refused_and_nothing_is_written(paths, section, field, value, problem):
    paths.config_file.write_text("[tui]\nreading_width = 90\n", encoding="utf-8")
    before = paths.config_file.read_bytes()
    with pytest.raises(ConfigError, match=problem):
        set_value(paths, section, field, value)
    with pytest.raises(ConfigError, match=problem):
        check_value(paths, section, field, value)
    assert paths.config_file.read_bytes() == before


def test_check_value_never_writes(paths):
    config = check_value(paths, "tui", "reading_width", "120")
    assert config.tui.reading_width == 120
    assert not paths.config_file.exists()


def test_a_valid_model_spec_is_saved(paths):
    assert set_value(paths, "models", "fast", " openai/gpt-x ").models.fast == "openai/gpt-x"


def test_a_problem_elsewhere_in_the_file_blocks_the_edit_and_is_named(paths):
    paths.config_file.write_text("[scout]\nauto_after_hour = 6\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=r"scout\.auto_after_hour"):
        set_value(paths, "tui", "tldr", "collapsed")
    assert "tldr" not in paths.config_file.read_text(encoding="utf-8")


def test_a_malformed_config_toml_refuses_every_edit_and_stays_untouched(paths):
    paths.config_file.write_text("[scout\nauto_after_hours = 6\n", encoding="utf-8")
    before = paths.config_file.read_bytes()
    with pytest.raises(MalformedConfig, match="press e"):
        set_value(paths, "tui", "tldr", "collapsed")
    with pytest.raises(MalformedConfig):
        reset_value(paths, "scout", "auto_after_hours")
    assert paths.config_file.read_bytes() == before


def test_export_path_expands_the_home_folder(paths, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "Notes").mkdir()
    config = set_value(paths, "export", "path", "~/Notes/Augury")  # a new leaf folder is fine
    assert config.export.path == str(tmp_path / "Notes" / "Augury")
    assert toml(paths)["export"]["path"] == str(tmp_path / "Notes" / "Augury")


def test_export_path_refuses_a_folder_whose_parent_does_not_exist(paths, tmp_path):
    target = tmp_path / "no" / "such" / "place"
    with pytest.raises(ConfigError, match="neither is its parent"):
        set_value(paths, "export", "path", str(target))
    assert not paths.config_file.exists()


def test_a_blank_export_path_turns_the_export_off(paths):
    paths.config_file.write_text('[export]\npath = "/somewhere"\n', encoding="utf-8")
    assert set_value(paths, "export", "path", "  ").export.path == ""


def test_reset_removes_the_key_and_a_section_it_empties(paths):
    paths.config_file.write_text(
        '# mine\n[export]\npath = ""\n\n[tui]\ntldr = "collapsed"\n', encoding="utf-8"
    )
    config = reset_value(paths, "tui", "tldr")
    assert config is not None and config.tui.tldr == "shown"
    assert toml(paths) == {"export": {"path": ""}}
    assert paths.config_file.read_text(encoding="utf-8").startswith('# mine\n[export]\npath = ""')


def test_reset_keeps_the_rest_of_the_section(paths):
    paths.config_file.write_text("[tui]\nreading_width = 100\ntldr = 'collapsed'\n")
    reset_value(paths, "tui", "tldr")
    assert toml(paths) == {"tui": {"reading_width": 100}}


def test_reset_of_a_key_the_file_does_not_have_changes_nothing(paths):
    paths.config_file.write_text("[tui]\nreading_width = 100\n", encoding="utf-8")
    before = paths.config_file.read_bytes()
    assert reset_value(paths, "tui", "tldr") is None
    assert reset_value(paths, "scout", "auto_after_hours") is None
    assert paths.config_file.read_bytes() == before
    assert reset_value(paths, "tui", "theme") is None  # nor is a missing file created
    paths.config_file.unlink()
    assert reset_value(paths, "tui", "theme") is None and not paths.config_file.exists()


def test_the_write_is_atomic_and_keeps_the_files_mode(paths):
    paths.config_file.write_text("[tui]\nreading_width = 100\n", encoding="utf-8")
    paths.config_file.chmod(0o640)
    set_value(paths, "tui", "tldr", "collapsed")
    assert paths.config_file.stat().st_mode & 0o777 == 0o640
    leftovers = [p.name for p in paths.config_dir.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_a_symlinked_config_toml_stays_a_symlink(paths, tmp_path):
    real = tmp_path / "dotfiles" / "augury.toml"
    real.parent.mkdir()
    real.write_text("[tui]\nreading_width = 100\n", encoding="utf-8")
    paths.config_file.symlink_to(real)
    set_value(paths, "tui", "tldr", "collapsed")
    assert paths.config_file.is_symlink()
    assert tomllib.loads(real.read_text(encoding="utf-8"))["tui"]["tldr"] == "collapsed"


@pytest.mark.parametrize(
    ("section", "field", "kind"),
    [
        ("tui", "remember_state", "bool"),
        ("tui", "tldr", "choice"),
        ("tui", "reading_width", "number"),
        ("scout", "auto_after_hours", "number"),
        ("tui", "theme", "text"),
        ("models", "fast", "text"),
        ("export", "path", "text"),
        ("models", "overrides", "file"),  # a dict
        ("pricing", '"openai/x"', "file"),  # not a section with fields
        ("pricing", "*", "file"),
        ("nope", "nothing", "file"),
        ("google", "api_key", "secret"),  # T22's tripwire: whatever the section
        ("budget", "token", "secret"),
    ],
)
def test_the_editor_is_chosen_by_the_fields_type(section, field, kind):
    assert editor_kind(section, field) == kind


def test_choices_bounds_and_defaults_come_from_the_model():
    assert choices("tui", "tldr") == ("shown", "collapsed")
    assert bounds("tui", "reading_width") == "40\u2013200"  # an en dash
    assert bounds("scout", "auto_after_hours") == "≥ 0"
    assert bounds("http", "timeout_s") == "> 0"
    assert bounds("tui", "theme") == ""
    assert default_of("tui", "reading_width") == 88
    assert default_of("models", "overrides") == {}
