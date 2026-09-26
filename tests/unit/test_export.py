from datetime import UTC, datetime, timedelta

from augury.agents.normalize import store_items
from augury.agents.rank import build_digest
from augury.core.clock import local_day
from augury.core.config import Config, ExportConfig, RankingConfig
from augury.core.db.open import open_db
from augury.core.db.triage_repo import TriageRepo
from augury.core.models import RawItem, Signals, TriageResult
from augury.export.markdown import daily_path, export_daily, md_text, md_url, render_daily

NOW = datetime(2026, 9, 25, 9, 0, tzinfo=UTC)
DAY = local_day(NOW)
HOSTILE = "[click](javascript:alert(1)) <img src=x onerror=alert(1)> **bold** #tag $x$"


def seed(paths):
    conn = open_db(paths, now=NOW)
    raws = [
        RawItem(
            source_id="hf-papers",
            kind="paper",
            arxiv_id="2609.00001",
            url="https://huggingface.co/papers/2609.00001",
            title="Qwen3-30B-A3B beats GRPO",
            signals=Signals(upvotes=145, github_stars=2100),
        ),
        RawItem(source_id="hf-blog", url="https://x/launch", title="Our product launch"),
        RawItem(source_id="hf-community", url="javascript:alert(1)", title=HOSTILE),
    ]
    ids = store_items(conn, raws, now=NOW).new_ids
    TriageRepo(conn).save_all(
        [
            TriageResult(
                item_id=ids[0],
                relevance=9,
                tags=["reasoning", "rl"],
                why_read="A cheaper route to *strong* reasoning.",
            ),
            TriageResult(item_id=ids[1], relevance=4, flags=["promo"]),
            TriageResult(item_id=ids[2], relevance=2, why_read="[x](https://e.vil)"),
        ],
        run_id="scout-1",
        model="m",
        prompt_version=1,
    )
    build_digest(conn, DAY, now=NOW, config=RankingConfig(), ai=True)
    return conn


def test_md_text_is_one_inert_line():
    assert md_text("a\nb  *c* [d]") == r"a b \*c\* \[d\]"


def test_frontmatter_sections_and_the_hidden_list(paths):
    text = render_daily(seed(paths), DAY, run_id="scout-1")
    assert text.startswith(
        f"---\ntype: augury-daily\ndate: {DAY.isoformat()}\nrun_id: scout-1\n---\n"
    )
    assert "## 1. Qwen3-30B-A3B beats GRPO" in text
    assert r"**Why read:** A cheaper route to \*strong\* reasoning." in text
    assert "- Tags: reasoning, rl" in text
    assert "145 upvotes" in text and "2100 GitHub stars" in text
    assert "[arXiv](https://arxiv.org/abs/2609.00001)" in text
    assert "## Hidden by triage (1)" in text and "- Our product launch (promo)" in text


def test_nothing_obsidian_only(paths):
    text = render_daily(seed(paths), DAY, run_id="scout-1")
    assert "[[" not in text and "> [!" not in text


def test_hostile_titles_and_links_are_inert(paths):
    text = render_daily(seed(paths), DAY, run_id="scout-1")
    assert r"\[click\](javascript:alert(1)) \<img src=x onerror=alert(1)\>" in text
    assert r"\*\*bold\*\* \#tag \$x\$" in text  # no bold, no tag, no math
    assert r"\[x\](https://e.vil)" in text  # the model's why_read can't make a link either
    assert "[Open](javascript" not in text  # only http(s) links are written


def test_a_link_cannot_break_out_of_its_line_or_its_parentheses():
    assert md_url("JavaScript:alert(1)") is None
    assert md_url("data:text/html,<b>x</b>") is None
    url = md_url("https://x.com/a b(c)<d>\n\n[r]: javascript:alert(1)\\")
    assert url == "https://x.com/a%20b%28c%29%3Cd%3E%0A%0A[r]:%20javascript:alert%281%29%5C"
    assert (
        md_url("https://x.com/{{ site.secret }}") == "https://x.com/%7B%7B%20site.secret%20%7D%7D"
    )
    assert md_url("https://例え.jp/パス?q=1#top") == "https://例え.jp/パス?q=1#top"


def test_a_lone_surrogate_is_replaced_not_fatal():
    # Such text can't be UTF-8 encoded, so writing it would raise; it becomes "?" instead.
    assert md_text("a\ud800b") == "a?b"
    assert md_url("https://x.com/a\udfffb") == "https://x.com/a%3Fb"
    assert md_text("naïve 例え") == "naïve 例え"  # real non-ASCII text is untouched


def test_template_tags_are_broken_up():
    # Jekyll's Liquid and Hugo's shortcodes run before Markdown and ignore its escapes;
    # escaping each brace leaves no "{{" or "{%" for them to find.
    text = md_text("{{ site.secret }} {% include x %} {{< param y >}} {{% z %}}")
    assert "{{" not in text and "{%" not in text
    assert md_text("50%% off") == r"50\%\% off"  # nor an Obsidian comment hiding the rest


def test_a_strange_arxiv_id_gets_no_link(paths):
    conn = open_db(paths, now=NOW)
    raw = RawItem(
        source_id="hf-papers",
        kind="paper",
        arxiv_id="1) [x](javascript:alert(1)",
        url="https://huggingface.co/papers/odd",
        title="Odd paper",
    )
    store_items(conn, [raw], now=NOW)
    build_digest(conn, DAY, now=NOW, config=RankingConfig(), ai=False)
    text = render_daily(conn, DAY, run_id="r")
    assert "## 1. Odd paper" in text and "[arXiv]" not in text and "javascript" not in text


def test_an_empty_day_says_so(paths):
    text = render_daily(open_db(paths, now=NOW), DAY, run_id=None)
    assert "run_id: none" in text and "No digest for this day yet" in text


def test_the_previous_day_link_is_relative(paths, tmp_path):
    conn = seed(paths)
    root = tmp_path / "vault"
    before = DAY - timedelta(days=1)
    daily_path(root, before).parent.mkdir(parents=True)
    daily_path(root, before).write_text("old")
    text = render_daily(conn, DAY, run_id="r", root=root)
    assert f"Previous: [{before.isoformat()}]({before.isoformat()}.md)" in text


def test_export_writes_under_daily_and_creates_the_folders(paths, tmp_path):
    conn = seed(paths)
    config = Config(export=ExportConfig(path=str(tmp_path / "vault" / "Augury")))
    path = export_daily(conn, config, DAY, run_id="r")
    assert path == tmp_path / "vault" / "Augury" / "Daily" / f"{DAY.isoformat()}.md"
    assert path is not None and path.read_text(encoding="utf-8").startswith("---\ntype: augury")
    assert [p.name for p in path.parent.iterdir()] == [path.name]  # no temporary file left


def test_an_empty_export_path_does_nothing(paths):
    assert export_daily(seed(paths), Config(), DAY, run_id="r") is None
