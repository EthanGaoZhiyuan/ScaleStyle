#!/usr/bin/env python3
"""
Generates BGE-small text embeddings for the online serving retrieval path.

Reads articles.csv, optionally enriches price from transactions_train.csv,
and writes article_embeddings_bge_small_v1_5_detail.parquet — the artifact
consumed by validate_embeddings.py and bootstrap_data.py (Milvus load).

Model: BAAI/bge-small-en-v1.5  (384-dim, CLS pooling, L2-normalised)

Usage:
    # Smoke test — embed first 100 articles, write to default output path
    python data-pipeline/src/generate_embeddings.py --limit 100 --overwrite

    # Full generation with defaults
    python data-pipeline/src/generate_embeddings.py --overwrite

    # Custom paths
    python data-pipeline/src/generate_embeddings.py \\
        --input  data-pipeline/data/raw/articles.csv \\
        --transactions data-pipeline/data/raw/transactions_train.csv \\
        --output data-pipeline/data/processed/article_embeddings_bge_small_v1_5_detail.parquet \\
        --overwrite

Environment:
    EMBEDDING_MODEL        override default model name
    EXPECTED_EMBEDDING_DIM override expected output dimension
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import torch
import torch.nn.functional as F
from tqdm import tqdm
from transformers import AutoModel, AutoTokenizer

# ──────────────────────────── constants ────────────────────────────

TEXT_FORMAT_VERSION = "product_text_v1"

DEFAULT_MODEL_NAME = "BAAI/bge-small-en-v1.5"
DEFAULT_EXPECTED_DIM = 384
DEFAULT_BATCH_SIZE = 128
DEFAULT_MAX_LENGTH = 512

# Canonical embedding column name — must match bootstrap_data.py
EMBEDDING_COL = "bge_embedding"

# Paths are anchored to the data-pipeline project root (two levels up from this file)
_PIPELINE_ROOT = Path(__file__).parent.parent.resolve()
DEFAULT_INPUT = str(_PIPELINE_ROOT / "data" / "raw" / "articles.csv")
DEFAULT_TRANSACTIONS = str(_PIPELINE_ROOT / "data" / "raw" / "transactions_train.csv")
DEFAULT_OUTPUT = str(
    _PIPELINE_ROOT
    / "data"
    / "processed"
    / "article_embeddings_bge_small_v1_5_detail.parquet"
)

# Metadata columns forwarded from articles to the output parquet when present.
# bootstrap_data.py / redis_metadata.py both consume these.
_OPTIONAL_METADATA_COLS = [
    "price",
    "prod_name",
    "product_type_name",
    "product_group_name",
    "graphical_appearance_name",
    "colour_group_name",
    "perceived_colour_value_name",
    "department_name",
    "index_name",
    "section_name",
    "garment_group_name",
    "detail_desc",
]

# ──────────────────────────── logging ────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("scalestyle.generate_embeddings")

# ──────────────────────────── product text construction ────────────────────────────


def build_product_text(row: dict) -> str:
    """
    Build a dense semantic text string from H&M article fields.

    This is the canonical document-side text for embedding — used at generation
    time, not at query time.  Missing optional fields become empty strings and
    are silently omitted from the output.

    Args:
        row: dict-like with article fields from articles.csv or articles parquet.

    Returns:
        str: Concatenated product description text.
    """

    def _s(field: str) -> str:
        val = row.get(field, "") or ""
        return str(val).strip()

    parts = []

    prod_name = _s("prod_name") or "Unknown Product"
    parts.append(f"Product: {prod_name}.")

    product_type = _s("product_type_name")
    product_group = _s("product_group_name")
    if product_type or product_group:
        type_str = " / ".join(filter(None, [product_type, product_group]))
        parts.append(f"Type: {type_str}.")

    colour = _s("colour_group_name")
    perceived = _s("perceived_colour_value_name")
    if colour or perceived:
        colour_str = " / ".join(filter(None, [colour, perceived]))
        parts.append(f"Colour: {colour_str}.")

    appearance = _s("graphical_appearance_name")
    if appearance:
        parts.append(f"Appearance: {appearance}.")

    department = _s("department_name")
    section = _s("section_name")
    garment = _s("garment_group_name")
    index_name = _s("index_name")
    location_str = ", ".join(filter(None, [department, section, garment, index_name]))
    if location_str:
        parts.append(f"Category: {location_str}.")

    detail_desc = _s("detail_desc")
    if detail_desc:
        parts.append(f"Description: {detail_desc}")

    return " ".join(parts)


# ──────────────────────────── data loading ────────────────────────────


def _read_flexible(path: str, columns: list[str] | None = None) -> pd.DataFrame:
    """Read a CSV file, a single parquet file, or a directory of parquet shards."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Input not found: {path}")

    if p.is_dir():
        shards = sorted(p.glob("*.parquet"))
        if not shards:
            raise ValueError(f"No .parquet files found in directory: {path}")
        dfs = [pd.read_parquet(str(f), columns=columns) for f in shards]
        logger.info("Read %d parquet shards from %s", len(dfs), path)
        return pd.concat(dfs, ignore_index=True)

    suffix = p.suffix.lower()
    if suffix == ".csv":
        return pd.read_csv(str(path), usecols=columns, low_memory=False)
    if suffix == ".parquet":
        return pd.read_parquet(str(path), columns=columns)
    raise ValueError(f"Unsupported file type '{suffix}'. Expected .csv or .parquet.")


def load_articles(input_path: str) -> pd.DataFrame:
    """Load article data, validate article_id presence, and deduplicate."""
    logger.info("Loading articles from: %s", input_path)
    df = _read_flexible(input_path)

    if "article_id" not in df.columns:
        raise ValueError(
            f"'article_id' column is missing from input. "
            f"Available columns: {df.columns.tolist()}"
        )

    n_before = len(df)
    df = df.drop_duplicates(subset=["article_id"], keep="first").reset_index(drop=True)
    n_dropped = n_before - len(df)
    if n_dropped > 0:
        logger.warning(
            "Dropped %d duplicate article_id rows (kept first occurrence)", n_dropped
        )

    logger.info("Loaded %d articles, %d columns", len(df), len(df.columns))
    return df


def load_price_enrichment(transactions_path: str) -> pd.DataFrame | None:
    """
    Compute mean price per article from transaction data.

    Returns a DataFrame with columns [article_id, price], or None if the
    transactions file is absent or unreadable (non-fatal — caller logs warning).
    """
    if not Path(transactions_path).exists():
        logger.warning(
            "Transactions file not found: %s — price column will be null",
            transactions_path,
        )
        return None

    try:
        logger.info("Loading transactions for price enrichment from: %s", transactions_path)
        df_tx = _read_flexible(transactions_path, columns=["article_id", "price"])
        df_price = df_tx.groupby("article_id", as_index=False)["price"].mean()
        logger.info("Computed mean price for %d articles", len(df_price))
        return df_price
    except Exception as exc:
        logger.warning("Price enrichment failed (%s) — price column will be null", exc)
        return None


# ──────────────────────────── embedding model ────────────────────────────


class EmbeddingModel:
    """Thin wrapper around AutoTokenizer + AutoModel for batch embedding generation."""

    def __init__(
        self,
        model_name: str = DEFAULT_MODEL_NAME,
        max_length: int = DEFAULT_MAX_LENGTH,
        device: str = "auto",
    ):
        self.model_name = model_name
        self.max_length = max_length

        if device == "auto":
            if torch.cuda.is_available():
                self.device = "cuda"
            elif torch.backends.mps.is_available():
                self.device = "mps"
            else:
                self.device = "cpu"
        else:
            self.device = device

        # float16 on CUDA (faster + less VRAM); float32 on MPS/CPU (MPS float16 less stable)
        self.dtype = torch.float16 if self.device == "cuda" else torch.float32

        logger.info("Loading tokenizer: %s", model_name)
        self.tokenizer = AutoTokenizer.from_pretrained(model_name)

        logger.info("Loading model (device=%s, dtype=%s) …", self.device, self.dtype)
        self.model = AutoModel.from_pretrained(model_name, torch_dtype=self.dtype)
        self.model.to(self.device).eval()
        logger.info("Model ready")

    def embed_batch(self, texts: list[str]) -> np.ndarray:
        """
        Embed a list of texts.

        Uses CLS pooling (last_hidden_state[:, 0]) and L2 normalisation.
        Returns a float32 numpy array of shape (N, dim).
        """
        inputs = self.tokenizer(
            texts,
            max_length=self.max_length,
            padding=True,
            truncation=True,
            return_tensors="pt",
        )
        inputs = {k: v.to(self.device) for k, v in inputs.items()}

        with torch.no_grad():
            outputs = self.model(**inputs)
            embeddings = outputs.last_hidden_state[:, 0]
            embeddings = F.normalize(embeddings, p=2, dim=1)
            return embeddings.float().cpu().numpy()


def generate_embeddings(
    df: pd.DataFrame,
    model: EmbeddingModel,
    batch_size: int = DEFAULT_BATCH_SIZE,
    text_format_version: str = TEXT_FORMAT_VERSION,
) -> list[list[float]]:
    """
    Generate one embedding vector per row in df.

    Returns a list of float lists (one per article row).
    """
    logger.info(
        "Generating embeddings for %d articles (batch_size=%d, text_format=%s)",
        len(df),
        batch_size,
        text_format_version,
    )

    texts = [build_product_text(row) for row in df.to_dict("records")]

    all_chunks: list[np.ndarray] = []
    for i in tqdm(range(0, len(texts), batch_size), desc="Embedding batches", unit="batch"):
        chunk_embs = model.embed_batch(texts[i : i + batch_size])
        all_chunks.append(chunk_embs)

    final = np.concatenate(all_chunks, axis=0)
    return [vec.tolist() for vec in final]


# ──────────────────────────── validation ────────────────────────────


def validate_embeddings_array(
    embeddings: list[list[float]],
    expected_dim: int,
) -> None:
    """
    Fail fast if the generated embedding array has any structural problem.

    Raises AssertionError with a descriptive message on the first failure.
    """
    assert len(embeddings) > 0, "No embeddings were generated"

    dims = {len(e) for e in embeddings}
    assert len(dims) == 1, f"Inconsistent embedding dimensions across rows: {dims}"

    actual_dim = dims.pop()
    assert actual_dim == expected_dim, (
        f"Dimension mismatch: generated {actual_dim}-dim vectors, "
        f"expected {expected_dim}. "
        f"Verify --model-name and --expected-dim match (BGE-small=384, BGE-large=1024)."
    )

    null_count = sum(1 for e in embeddings if e is None)
    assert null_count == 0, f"Found {null_count} null embeddings"


# ──────────────────────────── output construction ────────────────────────────


def build_output_df(df: pd.DataFrame, embeddings: list[list[float]]) -> pd.DataFrame:
    """
    Build the output DataFrame.

    Columns: article_id, bge_embedding, then any metadata columns from
    _OPTIONAL_METADATA_COLS that are present in df.
    """
    out = pd.DataFrame()
    out["article_id"] = df["article_id"].values
    out[EMBEDDING_COL] = embeddings

    for col in _OPTIONAL_METADATA_COLS:
        if col in df.columns:
            out[col] = df[col].values

    return out


def write_sidecar_metadata(
    sidecar_path: str,
    *,
    model_name: str,
    embedding_dim: int,
    batch_size: int,
    max_length: int,
    text_format_version: str,
    row_count: int,
    source_input: str,
    transactions_input: str,
    output_path: str,
    device: str,
    dtype: str,
) -> None:
    """Write a JSON sidecar file capturing artifact lineage and model parameters."""
    meta = {
        "model_name": model_name,
        "embedding_dim": embedding_dim,
        "embedding_column": EMBEDDING_COL,
        "pooling": "cls",
        "normalized": True,
        "max_length": max_length,
        "batch_size": batch_size,
        "text_format_version": text_format_version,
        "row_count": row_count,
        "source_input": source_input,
        "transactions_input": transactions_input,
        "output_path": output_path,
        "device": device,
        "dtype": dtype,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    with open(sidecar_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)


# ──────────────────────────── CLI ────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate BGE embeddings for H&M fashion articles.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--input",
        default=DEFAULT_INPUT,
        help="Articles CSV or parquet path, or parquet directory (default: data/raw/articles.csv)",
    )
    parser.add_argument(
        "--transactions",
        default=DEFAULT_TRANSACTIONS,
        help="Transactions CSV/parquet for price enrichment (optional; missing = null price)",
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help="Output parquet path",
    )
    parser.add_argument(
        "--model-name",
        default=DEFAULT_MODEL_NAME,
        help=f"HuggingFace model name (default: {DEFAULT_MODEL_NAME})",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"Embedding batch size (default: {DEFAULT_BATCH_SIZE})",
    )
    parser.add_argument(
        "--max-length",
        type=int,
        default=DEFAULT_MAX_LENGTH,
        help=f"Tokenizer max token length (default: {DEFAULT_MAX_LENGTH})",
    )
    parser.add_argument(
        "--expected-dim",
        type=int,
        default=DEFAULT_EXPECTED_DIM,
        help=f"Expected output embedding dimension (default: {DEFAULT_EXPECTED_DIM})",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process only the first N articles — useful for smoke tests",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite output file if it already exists",
    )
    parser.add_argument(
        "--device",
        default="auto",
        choices=["auto", "cuda", "mps", "cpu"],
        help="Torch device (default: auto — CUDA → MPS → CPU)",
    )
    parser.add_argument(
        "--metadata-json",
        default=None,
        help="Sidecar metadata JSON path (default: <output>.meta.json)",
    )
    parser.add_argument(
        "--text-format-version",
        default=TEXT_FORMAT_VERSION,
        help=f"Text format version tag written to sidecar (default: {TEXT_FORMAT_VERSION})",
    )
    return parser


def main() -> None:
    args = _build_parser().parse_args()

    output_path = Path(args.output)

    # ── Guard: refuse to clobber existing output unless --overwrite ──
    if output_path.exists() and not args.overwrite:
        print(
            f"Output already exists: {args.output}\n"
            "   Use --overwrite to replace it."
        )
        sys.exit(1)

    # ── Load articles ──
    df = load_articles(args.input)

    if args.limit is not None:
        df = df.head(args.limit).reset_index(drop=True)
        logger.info("--limit %d applied: processing first %d articles", args.limit, len(df))

    # ── Price enrichment (optional) ──
    df_price = load_price_enrichment(args.transactions)
    if df_price is not None:
        df = df.merge(df_price, on="article_id", how="left")
        null_price = int(df["price"].isna().sum())
        if null_price:
            logger.info("Price merged; %d articles have no transaction data (price=NaN)", null_price)
    elif "price" not in df.columns:
        df["price"] = None
        logger.warning("No price data available; 'price' column will be null")

    # ── Load model ──
    model = EmbeddingModel(
        model_name=args.model_name,
        max_length=args.max_length,
        device=args.device,
    )

    # ── Generate embeddings ──
    embeddings = generate_embeddings(
        df,
        model,
        batch_size=args.batch_size,
        text_format_version=args.text_format_version,
    )

    # ── Validate ──
    validate_embeddings_array(embeddings, expected_dim=args.expected_dim)
    logger.info(
        "Validation passed: %d embeddings, dim=%d, no nulls",
        len(embeddings),
        args.expected_dim,
    )

    # ── Build output DataFrame ──
    output_df = build_output_df(df, embeddings)

    # ── Write parquet ──
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_df.to_parquet(str(output_path), index=False)

    # ── Write sidecar metadata ──
    sidecar_path = args.metadata_json or str(output_path.with_suffix(".meta.json"))
    write_sidecar_metadata(
        sidecar_path,
        model_name=args.model_name,
        embedding_dim=args.expected_dim,
        batch_size=args.batch_size,
        max_length=args.max_length,
        text_format_version=args.text_format_version,
        row_count=len(output_df),
        source_input=args.input,
        transactions_input=args.transactions,
        output_path=str(output_path),
        device=model.device,
        dtype=str(model.dtype),
    )

    # ── Summary ──
    sample_vec = output_df[EMBEDDING_COL].iloc[0]
    print("\n" + "=" * 60)
    print("Embedding generation complete")
    print("=" * 60)
    print(f"  Model:      {args.model_name}")
    print(f"  Device:     {model.device}  (dtype={model.dtype})")
    print(f"  Rows:       {len(output_df):,}")
    print(f"  Dim:        {len(sample_vec)}")
    print(f"  Col:        {EMBEDDING_COL}")
    print(f"  Output:     {output_path}")
    print(f"  Sidecar:    {sidecar_path}")
    print(f"  Columns:    {output_df.columns.tolist()}")
    print(f"  Sample[0]:  [{sample_vec[0]:.6f}, {sample_vec[1]:.6f}, ...]")
    print("=" * 60)
    print("\nNext step:")
    print(f"  python data-pipeline/src/bootstrap_data.py --parquet {output_path} --drop-existing")


if __name__ == "__main__":
    main()
