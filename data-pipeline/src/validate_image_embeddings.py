#!/usr/bin/env python3
"""
Validates the generated image embedding artifact before Milvus bootstrap.

Checks row count, required columns (article_id, image_path, image_embedding),
embedding dimension (default 512), null counts, ID consistency, optional image
path existence on disk, and optional sidecar metadata consistency.
Exits 0 on success, 1 on any failure.

Usage:
    python data-pipeline/src/validate_image_embeddings.py \\
        --input data-pipeline/data/processed/article_image_embeddings_clip_vit_base_patch32.parquet

    python data-pipeline/src/validate_image_embeddings.py \\
        --input data-pipeline/data/processed/article_image_embeddings_clip_vit_base_patch32.parquet \\
        --expected-dim 512 \\
        --embedding-column image_embedding
"""

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

DEFAULT_EXPECTED_DIM = 512
DEFAULT_EMBEDDING_COL = "image_embedding"
DEFAULT_INPUT = str(
    Path(__file__).parent.parent
    / "data"
    / "processed"
    / "article_image_embeddings_clip_vit_base_patch32.parquet"
)


def validate(
    input_path: str,
    expected_dim: int = DEFAULT_EXPECTED_DIM,
    embedding_col: str = DEFAULT_EMBEDDING_COL,
    check_image_paths: bool = False,
) -> bool:
    """
    Run all validation checks against an image embedding parquet file.

    Prints a labelled result for each check (✓ / ⚠ / ❌).
    Returns True if all hard checks pass, False otherwise.
    Warnings do not cause a False return.
    """
    errors: list[str] = []
    warnings: list[str] = []

    p = Path(input_path)

    # 1. File exists
    if not p.exists():
        print(f"File not found: {input_path}")
        return False
    print(f"File exists: {input_path}")

    # 2. Readable parquet
    try:
        df = pd.read_parquet(str(p))
    except Exception as exc:
        print(f"Cannot read parquet: {exc}")
        return False
    print(f"Parquet loaded: {len(df):,} rows, {len(df.columns)} columns")

    # 3. Row count > 0
    if len(df) == 0:
        errors.append("Parquet is empty (0 rows)")

    # 4. Required columns present
    required = {"article_id", "image_path", embedding_col}
    missing = required - set(df.columns)
    if missing:
        errors.append(
            f"Missing required columns: {sorted(missing)}. "
            f"Available: {sorted(df.columns.tolist())}"
        )

    if errors:
        for e in errors:
            print(f"{e}")
        return False

    print(f"Required columns present: article_id, image_path, {embedding_col}")

    # 5. No null embeddings
    null_count = int(df[embedding_col].isna().sum())
    if null_count > 0:
        errors.append(f"{null_count} null values in '{embedding_col}' column")
    else:
        print(f"No null embeddings in '{embedding_col}'")

    # 6. Embedding dimension (sample first row)
    actual_dim: int | None = None
    sample_emb = df[embedding_col].iloc[0]
    try:
        actual_dim = len(sample_emb)
    except TypeError:
        errors.append(
            f"Embedding at row 0 is not iterable "
            f"(got {type(sample_emb).__name__}; expected list or array)"
        )

    if actual_dim is not None:
        if actual_dim != expected_dim:
            errors.append(
                f"Dimension mismatch: parquet has {actual_dim}-dim vectors, "
                f"expected {expected_dim}. "
                f"Is this a CLIP-base (512-dim) artifact?"
            )
        else:
            print(f"Embedding dimension: {actual_dim} (expected {expected_dim})")

    # 7. Consistent dimensions across all rows
    if actual_dim is not None and len(df) > 1 and not errors:
        try:
            all_dims = df[embedding_col].apply(len).unique()
            if len(all_dims) > 1:
                errors.append(
                    f"Inconsistent embedding dimensions across rows: {sorted(all_dims.tolist())}"
                )
            else:
                print(f"All embeddings have consistent dimension: {all_dims[0]}")
        except Exception as exc:
            warnings.append(f"Could not verify dimension consistency: {exc}")

    # 8. article_id / article_id_str consistency (non-blocking)
    if "article_id_str" in df.columns:
        def _normalize(v) -> str:
            return str(int(v)).zfill(10)

        try:
            computed = df["article_id"].apply(_normalize)
            mismatches = int((computed != df["article_id_str"]).sum())
            if mismatches > 0:
                warnings.append(
                    f"{mismatches} rows where article_id_str does not match "
                    f"normalize_article_id(article_id)"
                )
            else:
                print("article_id_str consistent with article_id")
        except Exception as exc:
            warnings.append(f"Could not verify article_id consistency: {exc}")

    # 9. image_path spot-check (optional — can be slow for large parquets)
    if check_image_paths:
        missing_paths = df["image_path"].apply(lambda p: not Path(p).exists())
        n_missing = int(missing_paths.sum())
        if n_missing > 0:
            warnings.append(f"{n_missing} image_path values do not exist on local filesystem")
        else:
            print(f"All {len(df):,} image_path values exist on disk")

    # 10. Sidecar metadata consistency (non-blocking)
    sidecar_path = p.with_suffix(".meta.json")
    if sidecar_path.exists():
        try:
            with open(str(sidecar_path), encoding="utf-8") as f:
                meta = json.load(f)
            meta_dim = meta.get("embedding_dim")
            meta_col = meta.get("vector_field")
            meta_model = meta.get("model_name", "unknown")
            meta_rows = meta.get("row_count")

            if meta_dim is not None and meta_dim != expected_dim:
                warnings.append(
                    f"Sidecar says embedding_dim={meta_dim} "
                    f"but --expected-dim={expected_dim}"
                )
            if meta_col is not None and meta_col != embedding_col:
                warnings.append(
                    f"Sidecar says vector_field='{meta_col}' "
                    f"but --embedding-column='{embedding_col}'"
                )
            if meta_rows is not None and meta_rows != len(df):
                warnings.append(
                    f"Sidecar says row_count={meta_rows} "
                    f"but parquet has {len(df)} rows"
                )
            print(
                f"Sidecar metadata: model={meta_model}, "
                f"dim={meta_dim}, rows={meta_rows}"
            )
        except Exception as exc:
            warnings.append(f"Could not read sidecar metadata: {exc}")
    else:
        warnings.append(f"No sidecar metadata found at {sidecar_path} (non-fatal)")

    # ── Print warnings and errors ──
    for w in warnings:
        print(f"{w}")
    for e in errors:
        print(f"{e}")

    if errors:
        return False

    print(
        f"\nValidation passed: {len(df):,} rows, "
        f"{actual_dim}-dim '{embedding_col}' vectors"
    )
    return True


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate a ScaleStyle image embedding parquet artifact.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--input",
        default=DEFAULT_INPUT,
        help="Path to the image embedding parquet file to validate",
    )
    parser.add_argument(
        "--expected-dim",
        type=int,
        default=DEFAULT_EXPECTED_DIM,
        help=f"Expected embedding dimension (default: {DEFAULT_EXPECTED_DIM})",
    )
    parser.add_argument(
        "--embedding-column",
        default=DEFAULT_EMBEDDING_COL,
        help=f"Embedding column name (default: {DEFAULT_EMBEDDING_COL})",
    )
    parser.add_argument(
        "--metadata-json",
        default=None,
        help="Path to sidecar JSON (default: <input>.meta.json)",
    )
    parser.add_argument(
        "--check-image-paths",
        action="store_true",
        help="Verify each image_path exists on local filesystem (slow for large parquets)",
    )
    args = parser.parse_args()

    print(f"\nValidating: {args.input}")
    print(f"Expected dim: {args.expected_dim}  |  Embedding col: {args.embedding_column}\n")

    ok = validate(
        args.input,
        args.expected_dim,
        args.embedding_column,
        check_image_paths=args.check_image_paths,
    )
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
