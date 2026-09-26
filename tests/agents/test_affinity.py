from datetime import UTC, datetime, timedelta

from augury.agents.affinity import like_affinity, liked_terms, terms_query
from augury.agents.normalize import store_items
from augury.core.db.open import open_db
from augury.core.db.state_repo import StateRepo
from augury.core.models import RawItem

NOW = datetime(2026, 9, 25, 9, 0, tzinfo=UTC)
EARLIER = NOW - timedelta(days=2)


def add(conn, titles: list[str], *, source: str = "hf-blog", now: datetime = NOW) -> list[str]:
    raws = [
        RawItem(source_id=source, url=f"https://x/{now:%d}/{source}/{i}", title=t)
        for i, t in enumerate(titles)
    ]
    return store_items(conn, raws, now=now).new_ids


def like(conn, *ids: str) -> None:
    for item_id in ids:
        StateRepo(conn).toggle(item_id, "liked", now=NOW)


def test_nothing_liked_means_no_affinity(paths):
    conn = open_db(paths, now=NOW)
    today = add(conn, ["Diffusion at scale"])
    assert like_affinity(conn, today) is None


def test_liked_terms_skip_stopwords_and_count_each_item_once(paths):
    conn = open_db(paths, now=NOW)
    like(
        conn,
        *add(
            conn,
            ["Diffusion for the win", "Diffusion diffusion models", "RL in the loop"],
            now=EARLIER,
        ),
    )
    terms = liked_terms(conn, limit=30)
    assert terms[0] == "diffusion" and "rl" in terms
    assert not {"the", "for", "in"} & set(terms)


def test_ties_go_to_terms_from_liked_titles(paths):
    conn = open_db(paths, now=NOW)
    raw = RawItem(
        source_id="hf-blog", url="https://x/t", title="Sparse attention", summary="We study kernels"
    )
    like(conn, *store_items(conn, [raw], now=EARLIER).new_ids)
    assert liked_terms(conn, limit=2) == ["attention", "sparse"]  # not "kernels", "study"


def test_items_like_your_likes_score_higher(paths):
    conn = open_db(paths, now=NOW)
    like(conn, *add(conn, ["Latent diffusion tricks", "Diffusion transformers"], now=EARLIER))
    today = add(conn, ["A diffusion primer", "A tokenizer primer"])
    affinity = like_affinity(conn, today)
    assert affinity is not None
    assert affinity.topic[today[0]] == 1.0 and affinity.topic[today[1]] == 0.0
    assert affinity.matched[today[0]] == ["diffusion"] and affinity.matched[today[1]] == []


def test_a_source_you_like_scores_higher(paths):
    conn = open_db(paths, now=NOW)
    like(conn, *add(conn, ["One", "Two"], source="hf-papers", now=EARLIER))
    add(conn, ["Three"], source="hf-blog", now=EARLIER)
    affinity = like_affinity(conn, add(conn, ["Four"]))
    assert affinity is not None
    assert affinity.source["hf-papers"] == 1.0 > affinity.source["hf-blog"]


def test_hostile_liked_titles_cannot_break_the_query(paths):
    conn = open_db(paths, now=NOW)
    like(conn, *add(conn, ['He said "NEAR(a b)" *star* AND OR NOT', "x*^:(-)"], now=EARLIER))
    query = terms_query(liked_terms(conn, limit=30))
    assert query is not None and query.count('"') % 2 == 0 and "NEAR(" not in query
    affinity = like_affinity(conn, add(conn, ["near the star"]))  # MATCH must not raise
    assert affinity is not None


def test_a_hidden_liked_item_is_not_a_signal(paths):
    conn = open_db(paths, now=NOW)
    [gone] = add(conn, ["Quantum widgets"], now=EARLIER)
    like(conn, gone)
    StateRepo(conn).toggle(gone, "hidden", now=NOW)
    like(conn, *add(conn, ["Diffusion notes"], now=EARLIER))
    assert "quantum" not in liked_terms(conn, limit=30)


def test_matched_terms_are_only_shown_for_a_topic_score(paths):
    # FTS keeps "_diffusion" whole (tokenchars '-._'), so the query doesn't match it.
    conn = open_db(paths, now=NOW)
    like(conn, *add(conn, ["Diffusion notes"], now=EARLIER))
    [odd] = add(conn, ["_diffusion internals"])
    affinity = like_affinity(conn, [odd])
    assert affinity is not None
    assert (affinity.topic[odd], affinity.matched[odd]) == (0.0, [])


def test_overlapping_terms_are_listed_once(paths):
    conn = open_db(paths, now=NOW)
    like(conn, *add(conn, ["Agents and agent tools", "Agents at work"], now=EARLIER))
    [item] = add(conn, ["Agents for tools"])
    affinity = like_affinity(conn, [item])
    assert affinity is not None and affinity.matched[item] == ["agents", "tools"]
