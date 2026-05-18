"""
Unit tests for bootstrap_image_collection.py.

Covers validate_parquet (the only pure function) — no Milvus connection required.
"""

from pathlib import Path

import pandas as pd
import pytest

from src.bootstrap_image_collection import (
    DEFAULT_VECTOR_FIELD,
    validate_parquet,
)

# ──────────────────────────── helpers ────────────────────────────


def _write_parquet(
    path: Path,
    n_rows: int = 5,
    dim: int = 512,
    vector_field: str = DEFAULT_VECTOR_FIELD,
    include_image_path: bool = True,
    include_article_id: bool = True,
    null_embeddings: bool = False,
) -> None:
    data: dict = {}
    if include_article_id:
        data["article_id"] = list(range(108775015, 108775015 + n_rows))
        data["article_id_str"] = [f"{108775015 + i:010d}" for i in range(n_rows)]
    if include_image_path:
        data["image_path"] = [f"/fake/{108775015 + i}.jpg" for i in range(n_rows)]
    if null_embeddings:
        data[vector_field] = [None] * n_rows
    else:
        data[vector_field] = [[float(j) / max(dim, 1)] * dim for j in range(n_rows)]
    pd.DataFrame(data).to_parquet(str(path), index=False)


# ──────────────────────────── passing cases ────────────────────────────


def test_validate_parquet_valid(tmp_path):
    p = tmp_path / "emb.parquet"
    _write_parquet(p, n_rows=10, dim=512)
    df = validate_parquet(str(p), expected_dim=512, vector_field=DEFAULT_VECTOR_FIELD)
    assert len(df) == 10


def test_validate_parquet_returns_dataframe(tmp_path):
    p = tmp_path / "emb.parquet"
    _write_parquet(p, n_rows=3)
    result = validate_parquet(str(p))
    assert isinstance(result, pd.DataFrame)


# ──────────────────────────── file errors ────────────────────────────


def test_validate_parquet_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        validate_parquet(str(tmp_path / "nonexistent.parquet"))


# ──────────────────────────── schema errors ────────────────────────────


def test_validate_parquet_missing_image_path_column(tmp_path):
    p = tmp_path / "emb.parquet"
    _write_parquet(p, include_image_path=False)
    with pytest.raises(ValueError, match="Missing required columns"):
        validate_parquet(str(p))


def test_validate_parquet_missing_article_id_column(tmp_path):
    p = tmp_path / "emb.parquet"
    _write_parquet(p, include_article_id=False)
    with pytest.raises(ValueError, match="Missing required columns"):
        validate_parquet(str(p))


def test_validate_parquet_missing_vector_column(tmp_path):
    p = tmp_path / "emb.parquet"
    # Write parquet without the vector field entirely
    df = pd.DataFrame(
        {
            "article_id": [1, 2],
            "image_path": ["/a.jpg", "/b.jpg"],
        }
    )
    df.to_parquet(str(p), index=False)
    with pytest.raises(ValueError, match="Missing required columns"):
        validate_parquet(str(p))


# ──────────────────────────── dimension errors ────────────────────────────


def test_validate_parquet_wrong_dimension(tmp_path):
    p = tmp_path / "emb.parquet"
    _write_parquet(p, dim=256)
    with pytest.raises(ValueError, match="Dimension mismatch"):
        validate_parquet(str(p), expected_dim=512)


def test_validate_parquet_correct_dimension_passes(tmp_path):
    p = tmp_path / "emb.parquet"
    _write_parquet(p, dim=512)
    df = validate_parquet(str(p), expected_dim=512)
    assert len(df) > 0


# ──────────────────────────── data quality errors ────────────────────────────


def test_validate_parquet_empty_file(tmp_path):
    p = tmp_path / "emb.parquet"
    pd.DataFrame(columns=["article_id", "image_path", DEFAULT_VECTOR_FIELD]).to_parquet(
        str(p), index=False
    )
    with pytest.raises(ValueError, match="empty"):
        validate_parquet(str(p))


def test_validate_parquet_null_embeddings(tmp_path):
    p = tmp_path / "emb.parquet"
    _write_parquet(p, null_embeddings=True)
    with pytest.raises(ValueError, match="null"):
        validate_parquet(str(p))


def test_validate_parquet_inconsistent_dimensions(tmp_path):
    p = tmp_path / "emb.parquet"
    df = pd.DataFrame(
        {
            "article_id": [1, 2, 3],
            "image_path": ["/a.jpg", "/b.jpg", "/c.jpg"],
            DEFAULT_VECTOR_FIELD: [
                [0.0] * 512,
                [0.0] * 256,  # wrong dim
                [0.0] * 512,
            ],
        }
    )
    df.to_parquet(str(p), index=False)
    with pytest.raises(ValueError, match="[Ii]nconsistent"):
        validate_parquet(str(p))


# ──────────────────────────── custom vector field ────────────────────────────


def test_validate_parquet_custom_vector_field(tmp_path):
    p = tmp_path / "emb.parquet"
    _write_parquet(p, vector_field="clip_vec", dim=512)
    df = validate_parquet(str(p), expected_dim=512, vector_field="clip_vec")
    assert len(df) > 0


def test_validate_parquet_custom_vector_field_missing(tmp_path):
    p = tmp_path / "emb.parquet"
    _write_parquet(p, vector_field="image_embedding", dim=512)
    with pytest.raises(ValueError, match="Missing required columns"):
        validate_parquet(str(p), expected_dim=512, vector_field="clip_vec")
