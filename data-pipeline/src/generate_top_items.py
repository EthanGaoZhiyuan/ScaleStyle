#!/usr/bin/env python3
"""
Builds transaction-based popularity artifact for cold-start fallback.

Reads transactions_train.csv, counts purchases per article_id, and writes
data/processed/top_items.parquet (columns: article_id, purchase_count,
popularity_score, rank).  This artifact is loaded into Redis global:popular
by bootstrap_data.py to serve best-sellers when the inference path is unavailable.

Usage:
    python data-pipeline/src/generate_top_items.py
    python data-pipeline/src/generate_top_items.py --transactions data-pipeline/data/raw/transactions_train.csv
    python data-pipeline/src/generate_top_items.py --topn 500 --output data-pipeline/data/processed/top_items.parquet
"""

import argparse
import sys
from pathlib import Path

import pandas as pd

from src.config import POPULARITY_CANDIDATE_TOPN

# Run from project root or from data-pipeline/
_SRC_DIR = Path(__file__).parent
_DATA_DIR = _SRC_DIR.parent / "data"

DEFAULT_TRANSACTIONS_PATHS = [
    str(_DATA_DIR / "raw" / "transactions_train.csv"),
    "data-pipeline/data/raw/transactions_train.csv",
    "data/raw/transactions_train.csv",
]
DEFAULT_OUTPUT_PATH = str(_DATA_DIR / "processed" / "top_items.parquet")


def count_purchases(
    df: pd.DataFrame, topn: int = POPULARITY_CANDIDATE_TOPN
) -> pd.DataFrame:
    """
    Group transaction rows by article_id and rank by purchase frequency.

    Input df must have an 'article_id' column (string or int).
    Returns columns: article_id (str, zero-padded to 10), purchase_count (int),
    popularity_score (float 0-1, 1.0 = most popular), rank (int from 1).
    Ties broken deterministically by article_id ascending.
    """
    counts = (
        df.assign(article_id=df["article_id"].astype(str))
        .groupby("article_id", sort=False)
        .size()
        .rename("purchase_count")
        .reset_index()
    )

    counts = (
        counts.sort_values(["purchase_count", "article_id"], ascending=[False, True])
        .head(topn)
        .reset_index(drop=True)
    )

    counts["article_id"] = counts["article_id"].str.zfill(10)
    counts["rank"] = counts.index + 1
    max_count = int(counts["purchase_count"].iloc[0])
    counts["popularity_score"] = counts["purchase_count"] / max_count

    return counts[["article_id", "purchase_count", "popularity_score", "rank"]]


def compute_top_items(
    csv_path: str, topn: int = POPULARITY_CANDIDATE_TOPN
) -> pd.DataFrame:
    """Read transactions CSV and return a ranked popularity DataFrame."""
    print(f"Reading transactions from {csv_path}...")
    df = pd.read_csv(csv_path, usecols=["article_id"], dtype={"article_id": str})
    print(f"  {len(df):,} transaction rows loaded")
    result = count_purchases(df, topn)
    print(f"  {len(result):,} unique articles ranked")
    return result


def validate_top_items(df: pd.DataFrame) -> None:
    """Raise ValueError if df fails basic integrity checks."""
    if len(df) == 0:
        raise ValueError("top_items is empty")
    required = {"article_id", "purchase_count", "popularity_score", "rank"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Missing columns: {missing}")
    if df["purchase_count"].le(0).any():
        raise ValueError("purchase_count must be > 0 for all rows")
    expected_ranks = list(range(1, len(df) + 1))
    if df["rank"].tolist() != expected_ranks:
        raise ValueError("ranks must be consecutive integers starting from 1")
    if df["article_id"].str.len().ne(10).any():
        raise ValueError("article_id must be zero-padded to 10 characters")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate top_items.parquet from transactions"
    )
    parser.add_argument(
        "--transactions", default=None, help="Path to transactions_train.csv"
    )
    parser.add_argument(
        "--output", default=DEFAULT_OUTPUT_PATH, help="Output parquet path"
    )
    parser.add_argument(
        "--topn",
        type=int,
        default=POPULARITY_CANDIDATE_TOPN,
        help="Number of top items to keep",
    )
    args = parser.parse_args()

    tx_path = args.transactions
    if not tx_path:
        for candidate in DEFAULT_TRANSACTIONS_PATHS:
            if Path(candidate).exists():
                tx_path = candidate
                break

    if not tx_path or not Path(tx_path).exists():
        print("transactions_train.csv not found. Tried:")
        for p in DEFAULT_TRANSACTIONS_PATHS:
            print(f"  - {p}")
        print("Use --transactions PATH to specify location.")
        sys.exit(1)

    df = compute_top_items(tx_path, topn=args.topn)

    validate_top_items(df)
    print("Validation passed")

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(output_path, index=False)
    print(f"Saved {len(df):,} rows to {output_path}")

    print("\nTop 10 articles by purchase count:")
    print(df.head(10).to_string(index=False))


if __name__ == "__main__":
    main()
