"""P15: interests.yaml's about, topics and avoid, edited from the config page (view 3)."""

import pytest
import yaml
from textual.widgets import Input

from augury.agents.scout import ScoutDeps, ScoutReport
from augury.core.config import Interests
from augury.tui import app as app_module
from augury.tui.widgets.config_view import ConfigView
from augury.tui.widgets.input_modal import InputModal
from tests.tui.conftest import until
from tests.tui.test_settings_edit import SIZE, notes, open_config, row, select, source_cell, table


def on_disk(app) -> dict:
    return yaml.safe_load(app.paths.interests_file.read_text(encoding="utf-8"))


async def type_in(pilot, value: str) -> None:
    pilot.app.screen.query_one(Input).value = value
    await pilot.press("enter")
    await pilot.pause()


async def test_the_interests_follow_the_config_toml_settings(make_app, paths):
    paths.interests_file.write_text("about: ML engineer\ntopics: [agents, rag]\n")
    app = make_app(interests=Interests(about="ML engineer", topics=["agents", "rag"]))
    async with app.run_test(size=SIZE) as pilot:
        await open_config(pilot)
        names = [f"{r.section}.{r.field}" for r in table(app).settings]
        assert names[-4:] == ["pricing.*", "interests.about", "interests.topics", "interests.avoid"]
        assert str(table(app).get_row("interests.topics")[1]) == "agents, rag"
        assert source_cell(app, "interests.about") == "interests.yaml"
        assert source_cell(app, "interests.avoid") == "default"
        assert "interests.topics = agents, rag  (interests.yaml)" in (
            app.query_one(ConfigView).text_content
        )


async def test_about_is_edited_in_the_input_modal(make_app, paths):
    paths.interests_file.write_text("about: student\ntopics: [agents]\navoid: [crypto]\n")
    app = make_app(interests=Interests(about="student", topics=["agents"], avoid=["crypto"]))
    async with app.run_test(size=SIZE) as pilot:
        await open_config(pilot)
        await select(pilot, "interests.about")
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, InputModal)
        assert app.screen.query_one(Input).value == "student"
        assert "e.g. ML engineer building RAG apps" in str(
            app.screen.query_one("#input-hint").render()
        )
        await type_in(pilot, "ML engineer building RAG apps")
        assert on_disk(app) == {
            "about": "ML engineer building RAG apps",
            "topics": ["agents"],
            "avoid": ["crypto"],
        }
        assert app.interests.about == "ML engineer building RAG apps"
        assert row(app, "interests.about").value == "ML engineer building RAG apps"
        assert "Saved interests.about · applies from the next scout" in notes(app)


async def test_topics_are_typed_as_a_comma_separated_list(make_app):
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await open_config(pilot)
        assert source_cell(app, "interests.topics") == "default"
        await select(pilot, "interests.topics")
        await pilot.press("enter")
        await pilot.pause()
        field = app.screen.query_one(Input)
        assert field.value == ""
        hint = str(app.screen.query_one("#input-hint").render())
        assert hint.startswith("comma-separated, e.g. LLM agents, RAG")
        await type_in(pilot, " LLM agents, ,RAG , robotics,")
        assert on_disk(app) == {
            "about": "",
            "topics": ["LLM agents", "RAG", "robotics"],
            "avoid": [],
        }
        assert app.interests == Interests(topics=["LLM agents", "RAG", "robotics"])
        assert source_cell(app, "interests.topics") == "interests.yaml"
        assert str(table(app).get_row("interests.topics")[1]) == "LLM agents, RAG, robotics"
        await pilot.press("enter")  # again: the list comes back comma-joined
        await pilot.pause()
        assert app.screen.query_one(Input).value == "LLM agents, RAG, robotics"


async def test_backspace_clears_and_an_empty_row_stays_as_it_is(make_app, paths):
    paths.interests_file.write_text("about: me\ntopics: [a, b]\navoid: []\n")
    app = make_app(interests=Interests(about="me", topics=["a", "b"]))
    async with app.run_test(size=SIZE) as pilot:
        await open_config(pilot)
        await select(pilot, "interests.topics")
        await pilot.press("backspace")
        await pilot.pause()
        assert on_disk(app) == {"about": "me", "topics": [], "avoid": []}
        assert app.interests == Interests(about="me")
        assert source_cell(app, "interests.topics") == "default"
        assert "Cleared interests.topics · applies from the next scout" in notes(app)
        before = app.paths.interests_file.read_bytes()
        await select(pilot, "interests.avoid")
        await pilot.press("delete")
        await pilot.pause()
        assert app.paths.interests_file.read_bytes() == before
        assert "interests.avoid is already empty" in notes(app)


@pytest.mark.parametrize(
    ("body", "problem"),
    [("audience: ML engineers\n", "audience"), ("about: [unclosed\n", "not valid YAML")],
)
async def test_an_old_or_malformed_file_refuses_editing_and_is_untouched(
    make_app, paths, body, problem
):
    paths.interests_file.write_text(body)
    before = paths.interests_file.read_bytes()
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await open_config(pilot)
        for name, key in (("interests.about", "enter"), ("interests.topics", "backspace")):
            await select(pilot, name)
            await pilot.press(key)
            await pilot.pause()
            assert not isinstance(app.screen, InputModal)
        assert paths.interests_file.read_bytes() == before
        refused = [n for n in app._notifications if problem in n.message]
        assert len(refused) == 2
        assert all("augury init" in n.message and n.severity == "error" for n in refused)
        assert all(not n.markup for n in refused)


async def test_the_next_scout_triages_with_the_new_interests(make_app, monkeypatch):
    seen: list[Interests] = []

    async def fake_scout(deps: ScoutDeps) -> ScoutReport:
        seen.append(deps.interests)
        return ScoutReport(run_id="fake", status="ok", sources={}, new_items=0)

    monkeypatch.setattr(app_module, "run_scout", fake_scout)
    app = make_app(interests=Interests(topics=["old"]))
    async with app.run_test(size=SIZE) as pilot:
        await pilot.press("r")
        await until(pilot, lambda: len(seen) == 1 and not app.scouting)
        await open_config(pilot)
        await select(pilot, "interests.topics")
        await pilot.press("enter")
        await pilot.pause()
        await type_in(pilot, "agents, robotics")
        await pilot.press("r")
        await until(pilot, lambda: len(seen) == 2 and not app.scouting)
        assert seen[0].topics == ["old"]
        assert seen[1] == Interests(topics=["agents", "robotics"])


async def test_the_interest_keys_do_nothing_outside_the_config_view(make_app, paths):
    paths.interests_file.write_text("about: me\ntopics: [a]\n")
    before = paths.interests_file.read_bytes()
    app = make_app(interests=Interests(about="me", topics=["a"]))
    async with app.run_test(size=SIZE) as pilot:
        await pilot.press("3")
        await select(pilot, "interests.topics")
        await pilot.press("1")  # back to the items, the table's cursor still on topics
        await pilot.pause()
        assert app.check_action("reset_setting", ()) is False
        assert app.check_action("edit_setting", ()) is False
        await pilot.press("backspace", "delete", "space", "enter")
        await pilot.pause()
        assert paths.interests_file.read_bytes() == before
        assert app.interests == Interests(about="me", topics=["a"])
