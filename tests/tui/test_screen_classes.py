"""P4 follow-up: the screen's layout classes (zen, reading, layout-*) restyle only what the CSS
keys on them, not the ~1,900 widgets of a long paper (~0.7 s per z or esc before)."""

import importlib
import itertools
import pkgutil
from pathlib import Path
from typing import get_args

import pytest
from textual.app import App
from textual.css.model import CombinatorType, Selector, SelectorType
from textual.css.stylesheet import Stylesheet
from textual.dom import DOMNode

import augury.tui
from augury.core.db.items_repo import ItemsRepo
from augury.tui import app as app_module
from augury.tui.layout import Layout
from augury.tui.widgets.filter_chips import Chip
from augury.tui.widgets.reader_pane import ReaderPane
from tests.tui.conftest import until
from tests.tui.test_reader import LONG_MD, cache, open_first, seed

THEME = Path(app_module.__file__).with_name("theme.tcss")
# A table and a code block too, so their widgets' own CSS is in the comparison.
MD = LONG_MD + "\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n```python\nx = 1\n```\n\n- one\n- two\n"


def augury_css() -> list[tuple[str, str]]:
    """theme.tcss, and the DEFAULT_CSS of every class in augury.tui (widgets and screens)."""
    sources = [(str(THEME), THEME.read_text(encoding="utf-8"))]
    for info in pkgutil.walk_packages(augury.tui.__path__, "augury.tui."):
        module = importlib.import_module(info.name)
        for obj in vars(module).values():
            own = isinstance(obj, type) and obj.__module__ == module.__name__
            if own and "DEFAULT_CSS" in vars(obj):
                sources.append((f"{module.__name__}.{obj.__qualname__}", obj.DEFAULT_CSS))
    return sources


def _simple(selector: Selector) -> str:
    """A selector as the helper would query it: pseudo classes (:focus, ...) left out."""
    prefix = {SelectorType.ID: "#", SelectorType.CLASS: "."}.get(selector.type, "")
    return "*" if selector.type is SelectorType.UNIVERSAL else f"{prefix}{selector.name}"


def _keyed(selectors: list[Selector]) -> bool:
    screen_classes = app_module.SCREEN_CLASSES
    return any(s.type is SelectorType.CLASS and s.name in screen_classes for s in selectors)


def screen_class_targets(sources: list[tuple[str, str]]) -> dict[str, str]:
    """{what the rule styles: the rule} for every rule keyed on a screen class. A rule whose
    last compound selector carries the class itself (`Screen.zen { ... }`) styles the screen."""
    sheet = Stylesheet(variables=App().get_css_variables())
    for origin, css in sources:
        sheet.add_source(css, read_from=(origin, ""))
    sheet.parse()
    found: dict[str, str] = {}
    for rule in sheet.rules:
        for selector_set in rule.selector_set:
            selectors = selector_set.selectors
            if not _keyed(selectors):
                continue
            start = len(selectors) - 1  # the target: the last compound selector
            while start > 0 and selectors[start].combinator is CombinatorType.SAME:
                start -= 1
            target = selectors[start:]
            key = "Screen" if _keyed(target) else "".join(_simple(s) for s in target)
            found[key] = selector_set.css
    return found


def test_every_rule_keyed_on_a_screen_class_styles_a_node_the_helper_restyles():
    found = screen_class_targets(augury_css())
    assert set(found) >= {"#items-pane", "#reader", "#chip-theme"}, found  # theme.tcss was read
    missing = {t: rule for t, rule in found.items() if t != "Screen"}
    missing = {t: rule for t, rule in missing.items() if t not in app_module.SCREEN_CLASS_TARGETS}
    assert not missing, f"add these to SCREEN_CLASS_TARGETS in tui/app.py: {missing}"


def test_the_guard_notices_a_rule_styling_anything_else():
    extra = ("new rule", "Screen.zen #health { height: 1; }\nScreen.reading.zen { padding: 1; }")
    found = screen_class_targets([*augury_css(), extra])
    assert (
        found["#health"] == "Screen.zen #health"
        and "#health" not in app_module.SCREEN_CLASS_TARGETS
    )
    assert found["Screen"] == "Screen.reading.zen"  # the screen is always restyled


def css_state(screen: DOMNode) -> dict[DOMNode, tuple[object, ...]]:
    """What the stylesheet gave each node (inline styles aside), component classes included."""
    return {
        node: (
            node.styles.base.get_rules(),
            {name: s.base.get_rules() for name, s in node._component_styles.items()},
        )
        for node in screen.walk_children(with_self=True)
    }


def restyled_by_a_full_update(app) -> list[str]:
    """The nodes a class change's old full restyle would still have changed (should be none)."""
    screen = app.query_one("#main").screen
    before = css_state(screen)
    app.stylesheet.update_nodes(screen.walk_children(with_self=True), animate=True)
    after = css_state(screen)
    return [f"{type(node).__name__}#{node.id}" for node in before if before[node] != after[node]]


# Every combination of the screen's classes: one layout, reading or not, zen or not.
STATES = [
    {**{f"layout-{name}": name == layout for name in get_args(Layout)}, "reading": r, "zen": z}
    for layout in get_args(Layout)
    for r in (False, True)
    for z in (False, True)
]


@pytest.mark.parametrize("focus", ["reader", "search"])  # `#search:focus` has a narrow rule
async def test_every_class_transition_styles_the_screen_as_a_full_restyle_would(make_app, focus):
    app = make_app()
    cache(app, seed(app)[0], MD)
    async with app.run_test(size=(180, 50)) as pilot:
        await open_first(pilot)
        if focus == "search":
            app.query_one("#search").focus()
            await pilot.pause()
        screen = app.query_one("#main").screen
        wrong = []
        for before, after in itertools.permutations(STATES, 2):  # all 132, compared at once
            app._set_screen_classes(before)
            app.stylesheet.update_nodes(screen.walk_children(with_self=True), animate=True)
            app._set_screen_classes(after)
            if restyled := restyled_by_a_full_update(app):
                wrong.append((before, after, restyled))
        assert wrong == []


async def test_every_action_styles_the_screen_as_a_full_restyle_would(make_app, monkeypatch):
    # Each compared straight after the action, before a refresh could restyle anything else.
    app = make_app()
    item_id = seed(app)[0]
    cache(app, item_id, MD)
    async with app.run_test(size=(180, 50)) as pilot:
        steps = []

        def check(step: str) -> None:
            steps.append((step, restyled_by_a_full_update(app)))

        for width in (180, 90, 130, 80):
            app.apply_layout(width)
            check(f"{width}: layout")
            app.open_item(item_id)
            check(f"{width}: open")
            app.action_toggle_zen()
            check(f"{width}: zen on")
            app.action_toggle_zen()
            check(f"{width}: zen off")
            app.action_toggle_zen()
            app.action_back_to_table()
            check(f"{width}: esc from zen")
            app.open_item(item_id)
            app.action_toggle_zen()
            with monkeypatch.context() as gone:
                gone.setattr(ItemsRepo, "get", lambda _self, _id: None)
                app.close_reader_if_item_gone()  # e.g. its source was removed
            check(f"{width}: item gone")
            # The second open cancelled the first's worker; let the rest finish.
            await until(pilot, lambda: all(worker.is_finished for worker in app.workers))
        assert [step for step in steps if step[1]] == []
        assert len(steps) == 24


async def test_the_helper_restyles_every_match_of_a_compound_target(make_app, monkeypatch):
    # Only a bare #id is looked up with query_one; anything else is a full query.
    monkeypatch.setattr(app_module, "SCREEN_CLASS_TARGETS", ("#reader", "#filters Chip"))
    app = make_app()
    async with app.run_test(size=(180, 50)):
        restyled: list[object] = []
        update_nodes = app.stylesheet.update_nodes

        def spy(nodes, animate=False):
            nodes = list(nodes)
            restyled.extend(nodes)
            update_nodes(nodes, animate=animate)

        monkeypatch.setattr(app.stylesheet, "update_nodes", spy)
        app._set_screen_classes({"zen": True})
        assert [node.id for node in restyled if isinstance(node, ReaderPane)] == ["reader"]
        assert len([node for node in restyled if isinstance(node, Chip)]) == 6


async def test_the_helper_only_sets_screen_classes(make_app):
    app = make_app()
    async with app.run_test(size=(180, 50)):
        with pytest.raises(AssertionError):
            app._set_screen_classes({"zen": True, "not-a-screen-class": True})


async def test_zen_esc_and_resizes_never_restyle_the_article(make_app, monkeypatch):
    app = make_app()
    cache(app, seed(app)[0], MD)
    async with app.run_test(size=(180, 50)) as pilot:
        await open_first(pilot)
        viewer = app.query_one(ReaderPane).viewer
        restyled: list[str] = []
        update_nodes = app.stylesheet.update_nodes

        def spy(nodes, animate=False):
            nodes = list(nodes)
            restyled.extend(type(node).__name__ for node in nodes)
            update_nodes(nodes, animate=animate)

        monkeypatch.setattr(app.stylesheet, "update_nodes", spy)
        for _ in range(2):
            before = viewer.virtual_size
            await pilot.press("z")
            await until(pilot, lambda b=before: viewer.virtual_size != b and app.reader_settled)
        await pilot.resize_terminal(90, 50)
        await until(pilot, lambda: app.screen.has_class("layout-narrow") and app.reader_settled)
        await pilot.press("escape")
        await pilot.pause()
        assert not app.screen.has_class("reading")
        assert "MarkdownParagraph" not in restyled
        assert {"ReaderPane", "Container"} <= set(restyled)  # #reader and #items-pane were
