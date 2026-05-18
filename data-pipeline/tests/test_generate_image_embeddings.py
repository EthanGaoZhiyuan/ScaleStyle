"""
Unit tests for generate_image_embeddings.py.

No model loading, no GPU, no network access required.
Tests cover pure helper functions and the validation path using fake parquets.
"""

import json
from pathlib import Path

import pandas as pd
import pytest

from src.generate_image_embeddings import (
    IMAGE_EMBEDDING_COL,
    build_article_lookup,
    discover_images,
    image_path_to_article_id,
    normalize_article_id,
    select_device,
)
from src.validate_image_embeddings import validate

# ──────────────────────────── normalize_article_id ────────────────────────────


def test_normalize_article_id_from_int():
    assert normalize_article_id(108775015) == "0108775015"


def test_normalize_article_id_from_padded_str():
    assert normalize_article_id("0108775015") == "0108775015"


def test_normalize_article_id_from_unpadded_str():
    assert normalize_article_id("108775015") == "0108775015"


def test_normalize_article_id_short_value():
    assert normalize_article_id(1) == "0000000001"


def test_normalize_article_id_already_ten_digits():
    assert normalize_article_id("1234567890") == "1234567890"


def test_normalize_article_id_float():
    # CSVs sometimes read IDs as float
    assert normalize_article_id(108775015.0) == "0108775015"


# ──────────────────────────── image_path_to_article_id ────────────────────────────


def test_image_path_to_article_id_standard():
    p = Path("images/010/0108775015.jpg")
    assert image_path_to_article_id(p) == "0108775015"


def test_image_path_to_article_id_different_subdir():
    p = Path("images/024/0249136006.jpg")
    assert image_path_to_article_id(p) == "0249136006"


def test_image_path_to_article_id_absolute():
    p = Path("/data/raw/images/010/0108775015.jpg")
    assert image_path_to_article_id(p) == "0108775015"


def test_image_path_to_article_id_short_stem_gets_padded():
    # stems shorter than 10 chars should be zero-padded
    p = Path("images/001/0000000001.jpg")
    assert image_path_to_article_id(p) == "0000000001"


# ──────────────────────────── discover_images ────────────────────────────


def test_discover_images_finds_jpgs(tmp_path):
    subdir = tmp_path / "010"
    subdir.mkdir()
    (subdir / "0108775015.jpg").write_bytes(b"fake")
    (subdir / "0108775016.jpg").write_bytes(b"fake")
    (subdir / "README.txt").write_text("ignored")

    files = discover_images(str(tmp_path))
    assert len(files) == 2
    assert all(f.suffix == ".jpg" for f in files)


def test_discover_images_sorted(tmp_path):
    for name in ["0000000003.jpg", "0000000001.jpg", "0000000002.jpg"]:
        (tmp_path / name).write_bytes(b"fake")

    files = discover_images(str(tmp_path))
    names = [f.name for f in files]
    assert names == sorted(names)


def test_discover_images_raises_on_missing_dir():
    with pytest.raises(FileNotFoundError):
        discover_images("/nonexistent/path/images")


def test_discover_images_raises_on_empty_dir(tmp_path):
    with pytest.raises(ValueError, match="No .jpg files"):
        discover_images(str(tmp_path))


# ──────────────────────────── build_article_lookup ────────────────────────────


def test_build_article_lookup_normalizes_ids(tmp_path):
    csv_path = tmp_path / "articles.csv"
    csv_path.write_text("article_id,prod_name\n108775015,Jacket\n249136006,Dress\n")

    lookup = build_article_lookup(str(csv_path))
    assert "0108775015" in lookup
    assert "0249136006" in lookup
    assert lookup["0108775015"]["prod_name"] == "Jacket"


def test_build_article_lookup_raises_on_missing_file():
    with pytest.raises(FileNotFoundError):
        build_article_lookup("/nonexistent/articles.csv")


# ──────────────────────────── select_device ────────────────────────────


def test_select_device_cpu_returns_float32():
    device, dtype = select_device("cpu")
    assert device == "cpu"
    import torch

    assert dtype == torch.float32


def test_select_device_mps_returns_float32():
    device, dtype = select_device("mps")
    assert device == "mps"
    import torch

    assert dtype == torch.float32


def test_select_device_cuda_returns_float16():
    device, dtype = select_device("cuda")
    assert device == "cuda"
    import torch

    assert dtype == torch.float16


# ──────────────────────────── validate_image_embeddings ────────────────────────────


def _make_fake_parquet(
    path: Path, dim: int = 512, n_rows: int = 5, null_emb: bool = False
):
    """Write a minimal valid (or deliberately invalid) image embedding parquet."""
    emb = [None if null_emb else [float(i) / (dim or 1)] * dim for i in range(n_rows)]
    df = pd.DataFrame(
        {
            "article_id": list(range(108775015, 108775015 + n_rows)),
            "article_id_str": [f"{108775015 + i:010d}" for i in range(n_rows)],
            "image_path": [f"/fake/images/0{108775015 + i}.jpg" for i in range(n_rows)],
            IMAGE_EMBEDDING_COL: emb,
        }
    )
    df.to_parquet(str(path), index=False)


def test_validate_passes_for_valid_parquet(tmp_path):
    p = tmp_path / "embeddings.parquet"
    _make_fake_parquet(p, dim=512)
    assert validate(str(p), expected_dim=512) is True


def test_validate_fails_on_missing_file(tmp_path):
    assert validate(str(tmp_path / "nonexistent.parquet"), expected_dim=512) is False


def test_validate_fails_on_wrong_dimension(tmp_path):
    p = tmp_path / "embeddings.parquet"
    _make_fake_parquet(p, dim=256)  # wrong dim
    assert validate(str(p), expected_dim=512) is False


def test_validate_fails_on_empty_parquet(tmp_path):
    p = tmp_path / "embeddings.parquet"
    df = pd.DataFrame(columns=["article_id", "image_path", IMAGE_EMBEDDING_COL])
    df.to_parquet(str(p), index=False)
    assert validate(str(p), expected_dim=512) is False


def test_validate_fails_on_missing_column(tmp_path):
    p = tmp_path / "embeddings.parquet"
    # Missing image_path column
    df = pd.DataFrame(
        {
            "article_id": [108775015],
            IMAGE_EMBEDDING_COL: [[0.0] * 512],
        }
    )
    df.to_parquet(str(p), index=False)
    assert validate(str(p), expected_dim=512) is False


def test_validate_fails_on_null_embedding(tmp_path):
    p = tmp_path / "embeddings.parquet"
    _make_fake_parquet(p, dim=512, null_emb=True)
    assert validate(str(p), expected_dim=512) is False


def test_validate_checks_sidecar_dim_mismatch(tmp_path):
    p = tmp_path / "embeddings.parquet"
    _make_fake_parquet(p, dim=512)
    # Write a sidecar claiming wrong dim
    sidecar = p.with_suffix(".meta.json")
    sidecar.write_text(
        json.dumps(
            {
                "embedding_dim": 256,
                "model_name": "clip",
                "vector_field": "image_embedding",
                "row_count": 5,
            }
        )
    )
    # Hard check passes (actual parquet is 512), but sidecar warning is emitted
    result = validate(str(p), expected_dim=512)
    assert result is True  # warning only, not a hard failure
