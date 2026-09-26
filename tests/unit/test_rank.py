import math

import pytest
from pydantic import ValidationError

from augury.agents.rank import ai_weights, combine, percentile, recency
from augury.core.config import RankingConfig, load_config

DEFAULT = ai_weights(RankingConfig())  # pre-flight: combine takes a weights mapping


def test_recency_decays_over_three_days():
    assert recency(0) == 1.0 and recency(3) == pytest.approx(math.exp(-1))
    assert recency(-2) == 1.0  # a date slightly in the future just counts as new


def test_percentile_is_mid_rank_within_the_population():
    assert percentile(4, [1, 2, 3, 4]) == 0.875
    assert percentile(1, [1, 2, 3, 4]) == 0.125
    assert percentile(5, [5, 5]) == 0.5
    assert percentile(7, [7]) == 0.5


def test_all_terms_use_the_configured_weights():
    b = combine({"rel": 0.8, "pop": 0.6, "rec": 0.9, "nov": 0.5}, DEFAULT)
    assert b.final == pytest.approx(0.5 * 0.8 + 0.25 * 0.6 + 0.15 * 0.9 + 0.10 * 0.5)
    assert b.missing == []


def test_missing_terms_hand_their_weight_to_the_others():
    b = combine({"rel": None, "pop": 0.6, "rec": 0.9, "nov": None}, DEFAULT)
    assert b.weights == pytest.approx({"pop": 0.625, "rec": 0.375})
    assert b.final == pytest.approx(0.625 * 0.6 + 0.375 * 0.9)
    assert b.missing == ["rel", "nov"]


def test_an_item_without_popularity_is_not_penalized():
    without = combine({"rel": 0.8, "pop": None, "rec": 0.8, "nov": None}, DEFAULT)
    with_it = combine({"rel": 0.8, "pop": 0.8, "rec": 0.8, "nov": None}, DEFAULT)
    assert without.final == pytest.approx(0.8)
    assert with_it.final == pytest.approx(0.8)


def test_zero_weights_on_every_available_term_score_zero():
    only_novelty = ai_weights(RankingConfig(w_rel=0, w_pop=0, w_rec=0, w_nov=1))
    b = combine({"rel": 0.9, "pop": None, "rec": 1.0, "nov": None}, only_novelty)
    assert (b.final, b.weights) == (0.0, {})


def test_ranking_needs_one_positive_weight():
    with pytest.raises(ValidationError, match="at least one"):
        RankingConfig(w_rel=0, w_pop=0, w_rec=0, w_nov=0)


def test_weights_come_from_config(paths):
    paths.config_file.write_text("[ranking]\nw_rel = 1.0\nw_pop = 0\nw_topic = 0.6\n")
    ranking = load_config(paths).ranking
    assert (ranking.w_rel, ranking.w_pop, ranking.w_rec) == (1.0, 0, 0.15)
    assert (ranking.w_topic, ranking.w_source, ranking.w_fresh) == (0.6, 0.20, 0.35)
