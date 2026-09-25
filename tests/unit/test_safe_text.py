from rich.text import Text

from augury.tui.safe_text import markup, text

HOSTILE = "Evil [link=https://x.y]click[/link] [bold red]RED[/] \x1b[31mansi"


def test_markup_escapes_values_but_keeps_literal_markup():
    out = markup(t"[b]{HOSTILE}[/b]")
    assert out.startswith("[b]") and out.endswith("[/b]")
    assert r"\[link=" in out and "\x1b" not in out


def test_escaped_markup_renders_the_brackets_literally():
    rendered = Text.from_markup(markup(t"{HOSTILE}")).plain
    assert rendered == "Evil [link=https://x.y]click[/link] [bold red]RED[/] ansi"


def test_markup_honours_format_specs_and_conversions():
    value, name = 3.14159, "x"
    assert markup(t"{value:.2f} {name!r}") == "3.14 'x'"


def test_text_is_literal_and_stripped():
    t = text(HOSTILE, one_line=True)
    assert "[link=" in t.plain and "\x1b" not in t.plain and t.no_wrap
