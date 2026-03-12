"""Focused tests for the real time-decay math used by the event consumer."""

import math
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("KAFKA_BOOTSTRAP_SERVERS", "test-kafka:9093")
os.environ.setdefault("CONSUMER_MODE", "primary")

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from consumer import apply_decay_update, decay_score, popularity_rank_score

HALF_LIFE_SECONDS = 10.0
DECAY_LAMBDA = math.log(2.0) / HALF_LIFE_SECONDS


def test_repeated_events_over_time_apply_real_decay():
    updated = apply_decay_update(
        previous_score=1.0,
        previous_timestamp=0.0,
        current_timestamp=10.0,
        decay_lambda=DECAY_LAMBDA,
    )

    # One half-life later: 1.0 decays to 0.5, then the new click adds 1.0.
    assert updated == pytest.approx(1.5, rel=1e-6)


def test_stale_features_decay_before_new_click_is_added():
    updated = apply_decay_update(
        previous_score=1.0,
        previous_timestamp=0.0,
        current_timestamp=50.0,
        decay_lambda=DECAY_LAMBDA,
    )

    # Five half-lives later: 1.0 -> 1/32, then +1.0 click.
    expected = 1.0 + (1.0 / 32.0)
    assert updated == pytest.approx(expected, rel=1e-6)
    assert updated < 2.0, "Old forever-growing behavior would have produced exactly 2.0"


def test_decay_with_missing_timestamp_preserves_legacy_value_until_first_touch():
    updated = apply_decay_update(
        previous_score=3.0,
        previous_timestamp=None,
        current_timestamp=100.0,
        decay_lambda=DECAY_LAMBDA,
    )

    # Migration behavior: a legacy value without timestamp is treated as current
    # on first touch, then future updates decay from that first timestamp onward.
    assert updated == pytest.approx(4.0, rel=1e-6)


def test_global_popularity_ranking_reflects_decay_not_all_time_counts():
    old_actual_now = decay_score(
        previous_score=1.0,
        previous_timestamp=0.0,
        current_timestamp=15.0,
        decay_lambda=DECAY_LAMBDA,
    )
    new_actual_now = 1.0

    old_rank = popularity_rank_score(
        actual_score=1.0, last_update_timestamp=0.0, decay_lambda=DECAY_LAMBDA
    )
    new_rank = popularity_rank_score(
        actual_score=1.0, last_update_timestamp=15.0, decay_lambda=DECAY_LAMBDA
    )

    assert old_actual_now == pytest.approx(math.exp(-DECAY_LAMBDA * 15.0), rel=1e-6)
    assert new_actual_now > old_actual_now
    assert (
        new_rank > old_rank
    ), "Popularity ranking surrogate should prefer the fresher item"
