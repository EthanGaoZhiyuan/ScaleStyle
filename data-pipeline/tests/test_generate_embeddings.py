"""
Unit tests for generate_embeddings.py and validate_embeddings.py.

These tests cover pure functions and file I/O only — no model loading,
no GPU, no network access required.
"""

import json
import tempfile
from pathlib import Path

import pandas as pd
import pytest

from src.generate_embeddings import (
    EMBEDDING_COL,
    TEXT_FORMAT_VERSION,
    build_output_df,
    build_product_text,
    load_articles,
    validate_embeddings_array,
    write_sidecar_metadata,
)
from src.validate_embeddings import validate


# ──────────────────────────── build_product_text ────────────────────────────


class TestBuildProductText:
    def test_includes_prod_name(self):
        text = build_product_text({"prod_name": "Slim Jacket"})
        assert "Slim Jacket" in text

    def test_includes_all_supplied_fields(self):
        row = {
            "prod_name": "Slim Jacket",
            "product_type_name": "Jacket",
            "product_group_name": "Outerwear",
            "graphical_appearance_name": "Solid",
            "colour_group_name": "Black",
            "perceived_colour_value_name": "Dark",
            "department_name": "Sportswear",
            "section_name": "Mens Sport",
            "garment_group_name": "Jacket Group",
            "index_name": "Menswear",
            "detail_desc": "Water-resistant coating.",
        }
        text = build_product_text(row)
        assert "Slim Jacket" in text
        assert "Jacket" in text
        assert "Black" in text
        assert "Dark" in text
        assert "Solid" in text
        assert "Sportswear" in text
        assert "Water-resistant" in text

    def test_handles_empty_dict(self):
        text = build_product_text({})
        assert "Unknown Product" in text
        assert len(text) > 0

    def test_handles_missing_optional_fields(self):
        text = build_product_text({"prod_name": "Basic Tee"})
        assert "Basic Tee" in text
        # Absent optional fields must not produce literal None/nan in output
        assert "None" not in text
        assert "nan" not in text.lower()

    def test_handles_none_values(self):
        row = {
            "prod_name": "Dress",
            "colour_group_name": None,
            "detail_desc": None,
        }
        text = build_product_text(row)
        assert "Dress" in text
        assert "None" not in text

    def test_handles_numeric_values_gracefully(self):
        row = {"prod_name": "Hat", "article_id": 123456}
        text = build_product_text(row)
        assert "Hat" in text

    def test_returns_string(self):
        assert isinstance(build_product_text({}), str)


# ──────────────────────────── validate_embeddings_array ────────────────────────────


class TestValidateEmbeddingsArray:
    def test_passes_for_valid_embeddings(self):
        embeddings = [[0.1] * 384 for _ in range(10)]
        validate_embeddings_array(embeddings, expected_dim=384)  # must not raise

    def test_fails_on_wrong_dimension(self):
        embeddings = [[0.1] * 1024 for _ in range(5)]
        with pytest.raises(AssertionError, match="Dimension mismatch"):
            validate_embeddings_array(embeddings, expected_dim=384)

    def test_fails_on_empty_list(self):
        with pytest.raises(AssertionError):
            validate_embeddings_array([], expected_dim=384)

    def test_fails_on_inconsistent_dimensions(self):
        embeddings = [[0.1] * 384, [0.1] * 128]
        with pytest.raises(AssertionError):
            validate_embeddings_array(embeddings, expected_dim=384)

    def test_fails_on_null_embedding(self):
        embeddings = [[0.1] * 384, None, [0.2] * 384]
        with pytest.raises((AssertionError, TypeError)):
            validate_embeddings_array(embeddings, expected_dim=384)


# ──────────────────────────── build_output_df ────────────────────────────


class TestBuildOutputDf:
    def test_required_columns_always_present(self):
        df = pd.DataFrame({"article_id": [1, 2], "prod_name": ["A", "B"]})
        out = build_output_df(df, [[0.1] * 384, [0.2] * 384])
        assert "article_id" in out.columns
        assert EMBEDDING_COL in out.columns

    def test_embedding_column_is_canonical(self):
        df = pd.DataFrame({"article_id": [1]})
        out = build_output_df(df, [[0.1] * 384])
        assert EMBEDDING_COL in out.columns
        # No stray 'embedding' column unless that IS the canonical name
        if EMBEDDING_COL != "embedding":
            assert "embedding" not in out.columns

    def test_optional_metadata_columns_forwarded(self):
        df = pd.DataFrame({
            "article_id": [1],
            "price": [9.99],
            "prod_name": ["Tee"],
            "colour_group_name": ["Black"],
            "detail_desc": ["A fine shirt"],
        })
        out = build_output_df(df, [[0.1] * 384])
        assert "price" in out.columns
        assert "prod_name" in out.columns
        assert "colour_group_name" in out.columns
        assert "detail_desc" in out.columns

    def test_optional_columns_absent_when_not_in_source(self):
        df = pd.DataFrame({"article_id": [1]})
        out = build_output_df(df, [[0.1] * 384])
        assert "price" not in out.columns

    def test_row_count_preserved(self):
        df = pd.DataFrame({"article_id": range(50)})
        embeddings = [[0.1] * 384] * 50
        out = build_output_df(df, embeddings)
        assert len(out) == 50


# ──────────────────────────── write_sidecar_metadata ────────────────────────────


class TestWriteSidecarMetadata:
    def test_creates_valid_json_with_required_keys(self, tmp_path):
        sidecar_path = str(tmp_path / "output.meta.json")
        write_sidecar_metadata(
            sidecar_path,
            model_name="BAAI/bge-small-en-v1.5",
            embedding_dim=384,
            batch_size=128,
            max_length=512,
            text_format_version=TEXT_FORMAT_VERSION,
            row_count=100,
            source_input="/data/articles.csv",
            transactions_input="/data/transactions_train.csv",
            output_path="/data/output.parquet",
            device="cpu",
            dtype="torch.float32",
        )

        with open(sidecar_path) as f:
            meta = json.load(f)

        assert meta["model_name"] == "BAAI/bge-small-en-v1.5"
        assert meta["embedding_dim"] == 384
        assert meta["embedding_column"] == EMBEDDING_COL
        assert meta["pooling"] == "cls"
        assert meta["normalized"] is True
        assert meta["row_count"] == 100
        assert "created_at" in meta
        assert "Z" in meta["created_at"] or "+" in meta["created_at"]

    def test_text_format_version_recorded(self, tmp_path):
        sidecar_path = str(tmp_path / "meta.json")
        write_sidecar_metadata(
            sidecar_path,
            model_name="BAAI/bge-small-en-v1.5",
            embedding_dim=384,
            batch_size=32,
            max_length=256,
            text_format_version="product_text_v2",
            row_count=5,
            source_input="in",
            transactions_input="tx",
            output_path="out",
            device="cpu",
            dtype="float32",
        )
        with open(sidecar_path) as f:
            meta = json.load(f)
        assert meta["text_format_version"] == "product_text_v2"


# ──────────────────────────── load_articles ────────────────────────────


class TestLoadArticles:
    def test_loads_from_csv(self, tmp_path):
        csv_path = tmp_path / "articles.csv"
        pd.DataFrame({
            "article_id": [1, 2, 3],
            "prod_name": ["A", "B", "C"],
        }).to_csv(str(csv_path), index=False)

        df = load_articles(str(csv_path))
        assert len(df) == 3
        assert "article_id" in df.columns

    def test_loads_from_parquet(self, tmp_path):
        pq_path = tmp_path / "articles.parquet"
        pd.DataFrame({
            "article_id": [10, 20],
            "prod_name": ["X", "Y"],
        }).to_parquet(str(pq_path))

        df = load_articles(str(pq_path))
        assert len(df) == 2

    def test_deduplicates_article_id_keeps_first(self, tmp_path):
        csv_path = tmp_path / "articles.csv"
        pd.DataFrame({
            "article_id": [1, 1, 2],
            "prod_name": ["First", "Duplicate", "Other"],
        }).to_csv(str(csv_path), index=False)

        df = load_articles(str(csv_path))
        assert len(df) == 2
        assert df[df["article_id"] == 1].iloc[0]["prod_name"] == "First"

    def test_raises_on_missing_article_id_column(self, tmp_path):
        csv_path = tmp_path / "bad.csv"
        pd.DataFrame({"prod_name": ["A", "B"]}).to_csv(str(csv_path), index=False)

        with pytest.raises(ValueError, match="article_id"):
            load_articles(str(csv_path))

    def test_raises_on_missing_file(self):
        with pytest.raises(FileNotFoundError):
            load_articles("/nonexistent/path/articles.csv")


# ──────────────────────────── validate_embeddings.py ────────────────────────────


class TestValidateEmbeddings:
    def _make_parquet(self, tmp_path, dim: int = 384, n: int = 10) -> Path:
        """Write a minimal valid embedding parquet to tmp_path."""
        p = tmp_path / "embeddings.parquet"
        pd.DataFrame({
            "article_id": range(n),
            "bge_embedding": [[0.1] * dim] * n,
        }).to_parquet(str(p))
        return p

    def test_passes_for_valid_parquet(self, tmp_path):
        p = self._make_parquet(tmp_path, dim=384)
        assert validate(str(p), expected_dim=384) is True

    def test_fails_on_missing_file(self, tmp_path):
        assert validate(str(tmp_path / "missing.parquet"), expected_dim=384) is False

    def test_fails_on_wrong_dimension(self, tmp_path):
        p = self._make_parquet(tmp_path, dim=1024)
        assert validate(str(p), expected_dim=384) is False

    def test_fails_on_empty_parquet(self, tmp_path):
        p = tmp_path / "empty.parquet"
        pd.DataFrame({"article_id": [], "bge_embedding": []}).to_parquet(str(p))
        assert validate(str(p), expected_dim=384) is False

    def test_fails_on_missing_embedding_column(self, tmp_path):
        p = tmp_path / "no_emb.parquet"
        pd.DataFrame({"article_id": [1, 2]}).to_parquet(str(p))
        assert validate(str(p), expected_dim=384) is False

    def test_warns_on_duplicate_article_id(self, tmp_path, capsys):
        p = tmp_path / "dups.parquet"
        pd.DataFrame({
            "article_id": [1, 1, 2],
            "bge_embedding": [[0.1] * 384] * 3,
        }).to_parquet(str(p))
        # Should still pass (duplicates are a warning, not an error)
        result = validate(str(p), expected_dim=384)
        captured = capsys.readouterr()
        assert "duplicate" in captured.out.lower()
        assert result is True

    def test_sidecar_metadata_checked_when_present(self, tmp_path):
        p = self._make_parquet(tmp_path, dim=384)
        sidecar = p.with_suffix(".meta.json")
        sidecar.write_text(
            json.dumps({
                "model_name": "BAAI/bge-small-en-v1.5",
                "embedding_dim": 384,
                "embedding_column": "bge_embedding",
            })
        )
        assert validate(str(p), expected_dim=384) is True

    def test_sidecar_dim_mismatch_produces_warning(self, tmp_path, capsys):
        p = self._make_parquet(tmp_path, dim=384)
        sidecar = p.with_suffix(".meta.json")
        # Sidecar claims 1024-dim but actual parquet is 384-dim
        sidecar.write_text(json.dumps({"embedding_dim": 1024}))
        result = validate(str(p), expected_dim=384)
        captured = capsys.readouterr()
        assert "Sidecar" in captured.out
        assert result is True  # sidecar mismatch is a warning, not a hard error
