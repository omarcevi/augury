"""P15: interests.yaml's three keys, edited one at a time from the config page."""

import pytest
import yaml

from augury.core.config import Interests
from augury.core.config_edit import (
    MalformedConfig,
    check_interest,
    load_raw_interests,
    reset_interest,
    set_interest,
    split_list,
)
from augury.tui.widgets.config_view import interest_settings


def on_disk(paths) -> dict:
    return yaml.safe_load(paths.interests_file.read_text(encoding="utf-8"))


def test_a_missing_file_is_created_with_all_three_keys_in_order(paths):
    assert not paths.interests_file.exists()
    interests = set_interest(paths, "about", "  I build RAG systems  ")
    assert interests == Interests(about="I build RAG systems")
    text = paths.interests_file.read_text(encoding="utf-8")
    assert [line.split(":")[0] for line in text.splitlines()] == ["about", "topics", "avoid"]


def test_an_edit_keeps_the_other_two_keys(paths):
    paths.interests_file.write_text(
        "about: ML engineer\ntopics: [agents, rag]\navoid: [crypto]\n", encoding="utf-8"
    )
    set_interest(paths, "topics", "LLM agents, , RAG ,robotics,")
    assert on_disk(paths) == {
        "about": "ML engineer",
        "topics": ["LLM agents", "RAG", "robotics"],
        "avoid": ["crypto"],
    }


def test_unicode_is_written_as_itself(paths):
    set_interest(paths, "avoid", "Kryptowährung, 暗号")
    text = paths.interests_file.read_text(encoding="utf-8")
    assert "Kryptowährung" in text and "暗号" in text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("a, b ,c", ["a", "b", "c"]),
        ("  ,, ", []),
        ("", []),
        ("RAG, rag, RAG", ["RAG", "rag", "RAG"]),  # the order, as typed
    ],
)
def test_split_list(text, expected):
    assert split_list(text) == expected


def test_check_interest_never_writes(paths):
    assert check_interest(paths, "topics", "a, b").topics == ["a", "b"]
    assert not paths.interests_file.exists()


def test_reset_clears_one_key_and_keeps_the_rest(paths):
    paths.interests_file.write_text("about: me\ntopics: [a, b]\navoid: [c]\n", encoding="utf-8")
    assert reset_interest(paths, "topics") == Interests(about="me", avoid=["c"])
    assert on_disk(paths) == {"about": "me", "topics": [], "avoid": ["c"]}
    assert reset_interest(paths, "about") == Interests(avoid=["c"])


def test_reset_of_an_empty_key_changes_nothing(paths):
    paths.interests_file.write_text("about: me\n", encoding="utf-8")
    before = paths.interests_file.read_bytes()
    assert reset_interest(paths, "topics") is None
    assert paths.interests_file.read_bytes() == before
    paths.interests_file.unlink()
    assert reset_interest(paths, "about") is None and not paths.interests_file.exists()


@pytest.mark.parametrize(
    ("body", "problem"),
    [
        ("audience: ML engineers\n", "augury init"),  # the key P13 renamed to about
        ("about: [unclosed\n", "not valid YAML"),
        ("- just\n- a list\n", "augury init"),
        ("topics: 5\n", "augury init"),
    ],
)
def test_an_unusable_file_refuses_every_edit_and_stays_untouched(paths, body, problem):
    paths.interests_file.write_text(body, encoding="utf-8")
    before = paths.interests_file.read_bytes()
    for edit in (
        lambda: set_interest(paths, "about", "x"),
        lambda: check_interest(paths, "about", "x"),
        lambda: reset_interest(paths, "topics"),
    ):
        with pytest.raises(MalformedConfig, match=problem):
            edit()
    assert paths.interests_file.read_bytes() == before


def test_the_write_is_atomic(paths):
    paths.interests_file.write_text("about: me\n", encoding="utf-8")
    paths.interests_file.chmod(0o640)
    set_interest(paths, "topics", "a")
    assert paths.interests_file.stat().st_mode & 0o777 == 0o640
    assert [p.name for p in paths.config_dir.iterdir() if p.name.endswith(".tmp")] == []


def test_the_rows_say_where_each_value_comes_from(paths):
    paths.interests_file.write_text("about: me\ntopics: []\n", encoding="utf-8")
    raw = load_raw_interests(paths)
    rows = {r.field: r for r in interest_settings(Interests(about="me", avoid=["x", "y"]), raw)}
    assert list(rows) == ["about", "topics", "avoid"]
    assert (rows["about"].value, rows["about"].source) == ("me", "interests.yaml")
    assert (rows["topics"].value, rows["topics"].source) == ("", "default")  # present but empty
    assert (rows["avoid"].value, rows["avoid"].source) == ("x, y", "default")  # not in the file


def test_load_raw_interests_is_empty_for_a_missing_or_broken_file(paths):
    assert load_raw_interests(paths) == {}
    paths.interests_file.write_text("about: [unclosed\n", encoding="utf-8")
    assert load_raw_interests(paths) == {}
    paths.interests_file.write_text("- a list\n", encoding="utf-8")
    assert load_raw_interests(paths) == {}
