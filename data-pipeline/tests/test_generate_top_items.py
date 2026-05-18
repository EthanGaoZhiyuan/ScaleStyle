"""
Unit tests for generate_top_items.py.

No file I/O required — tests operate on in-memory DataFrames.
"""

import pandas as pd
import pytest

from src.generate_top_items import count_purchases, validate_top_items


def _make_transactions(rows):
    """Build a minimal transactions DataFrame from a list of article_ids."""
    return pd.DataFrame({"article_id": rows})


def test_top_article_has_highest_purchase_count():
    df = _make_transactions(["AAA", "BBB", "AAA", "CCC", "AAA", "BBB"])
    result = count_purchases(df, topn=10)
    assert result.iloc[0]["article_id"] == "0000000AAA"
    assert result.iloc[0]["purchase_count"] == 3
    assert result.iloc[1]["purchase_count"] == 2
    assert result.iloc[2]["purchase_count"] == 1


def test_topn_limits_output_rows():
    df = _make_transactions(["A", "B", "C", "D", "E"])
    result = count_purchases(df, topn=3)
    assert len(result) == 3


def test_ranks_are_consecutive_from_one():
    df = _make_transactions(["X", "Y", "Z", "X", "X", "Y"])
    result = count_purchases(df, topn=10)
    assert result["rank"].tolist() == list(range(1, len(result) + 1))


def test_popularity_score_is_normalised_to_one_for_top():
    df = _make_transactions(["A", "A", "A", "B", "B", "C"])
    result = count_purchases(df, topn=10)
    assert result.iloc[0]["popularity_score"] == pytest.approx(1.0)
    assert result.iloc[1]["popularity_score"] == pytest.approx(2 / 3)
    assert result.iloc[2]["popularity_score"] == pytest.approx(1 / 3)


def test_tie_breaking_is_deterministic_by_article_id():
    # A, B, C all have 2 purchases — alphabetical ascending tie-break
    df = _make_transactions(["C", "C", "A", "A", "B", "B"])
    result = count_purchases(df, topn=10)
    # All padded to 10 chars; '0000000000A' < '0000000000B' < '0000000000C'
    article_ids = result["article_id"].tolist()
    assert article_ids[0] < article_ids[1] < article_ids[2]


def test_article_id_is_zero_padded_to_ten():
    df = _make_transactions(["123", "123"])
    result = count_purchases(df, topn=10)
    assert result.iloc[0]["article_id"] == "0000000123"


def test_validate_top_items_passes_valid_df():
    df = pd.DataFrame(
        [
            {"article_id": "0000000001", "purchase_count": 10, "popularity_score": 1.0, "rank": 1},
            {"article_id": "0000000002", "purchase_count": 5, "popularity_score": 0.5, "rank": 2},
        ]
    )
    validate_top_items(df)  # should not raise


def test_validate_top_items_raises_on_empty():
    with pytest.raises(ValueError, match="empty"):
        validate_top_items(pd.DataFrame(columns=["article_id", "purchase_count", "popularity_score", "rank"]))


def test_validate_top_items_raises_on_missing_column():
    df = pd.DataFrame([{"article_id": "0000000001", "purchase_count": 5, "rank": 1}])
    with pytest.raises(ValueError, match="Missing columns"):
        validate_top_items(df)


def test_validate_top_items_raises_on_zero_purchase_count():
    df = pd.DataFrame(
        [{"article_id": "0000000001", "purchase_count": 0, "popularity_score": 0.0, "rank": 1}]
    )
    with pytest.raises(ValueError, match="purchase_count"):
        validate_top_items(df)


def test_validate_top_items_raises_on_non_consecutive_ranks():
    df = pd.DataFrame(
        [
            {"article_id": "0000000001", "purchase_count": 10, "popularity_score": 1.0, "rank": 1},
            {"article_id": "0000000002", "purchase_count": 5, "popularity_score": 0.5, "rank": 3},
        ]
    )
    with pytest.raises(ValueError, match="ranks"):
        validate_top_items(df)
