import pytest

from augury.core.config import Config, ConfigError, load_config, load_interests


def test_missing_files_give_defaults(paths):
    config = load_config(paths)
    assert config == Config()
    assert config.scout.auto_after_hours == 12
    assert config.http.min_interval_s == 1.0
    assert load_interests(paths).topics == []


def test_valid_file_overrides_defaults(paths):
    paths.config_file.write_text('[scout]\nauto_after_hours = 6\n[tui]\ntheme = "nord"\n')
    config = load_config(paths)
    assert config.scout.auto_after_hours == 6
    assert config.tui.theme == "nord"
    assert config.http.timeout_s == 20.0


def test_unknown_key_is_reported_with_its_location(paths):
    paths.config_file.write_text("[scout]\nauto_after_hour = 6\n")
    with pytest.raises(ConfigError) as err:
        load_config(paths)
    assert "scout.auto_after_hour" in str(err.value)
    assert "Extra inputs are not permitted" in str(err.value)


def test_invalid_toml_names_the_file(paths):
    paths.config_file.write_text("[scout\n")
    with pytest.raises(ConfigError, match="not valid TOML"):
        load_config(paths)


def test_export_path_is_optional(paths):
    assert load_config(paths).export.path == ""
    paths.config_file.write_text('[export]\npath = "~/Notes/Augury"\n')
    assert load_config(paths).export.path == "~/Notes/Augury"


def test_interests_yaml_is_loaded(paths):
    paths.interests_file.write_text("audience: ML engineers\ntopics: [agents, rag]\n")
    interests = load_interests(paths)
    assert interests.audience == "ML engineers"
    assert interests.topics == ["agents", "rag"]


def test_empty_interests_file_is_fine(paths):
    paths.interests_file.write_text("")
    assert load_interests(paths).avoid == []


def test_remember_state_is_on_and_reopening_the_last_article_is_off_by_default(paths):
    tui = load_config(paths).tui
    assert tui.remember_state is True and tui.reopen_last_article is False
    paths.config_file.write_text("[tui]\nremember_state = false\nreopen_last_article = true\n")
    tui = load_config(paths).tui
    assert tui.remember_state is False and tui.reopen_last_article is True


def test_the_tldr_box_is_shown_by_default_and_can_start_collapsed(paths):
    assert load_config(paths).tui.tldr == "shown"
    paths.config_file.write_text('[tui]\ntldr = "collapsed"\n')
    assert load_config(paths).tui.tldr == "collapsed"
    paths.config_file.write_text('[tui]\ntldr = "hidden"\n')
    with pytest.raises(ConfigError, match=r"tui\.tldr: Input should be 'shown' or 'collapsed'"):
        load_config(paths)


def test_remember_state_must_be_a_boolean(paths):
    paths.config_file.write_text('[tui]\nremember_state = "sometimes"\n')
    with pytest.raises(ConfigError, match=r"tui\.remember_state: Input should be a valid boolean"):
        load_config(paths)
