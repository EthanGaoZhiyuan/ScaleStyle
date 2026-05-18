#!/usr/bin/env python3
"""
Generates CLIP image embeddings for the multimodal retrieval path.

Reads H&M product images, encodes them with the CLIP image encoder, and writes a
validated parquet artifact ready for future Milvus bootstrap (scale_style_clip_image_v1).

Model: openai/clip-vit-base-patch32  (512-dim, L2-normalised)

Usage:
    # Smoke test — embed first 100 images
    python data-pipeline/src/generate_image_embeddings.py --limit 100 --overwrite

    # Full generation
    python data-pipeline/src/generate_image_embeddings.py --overwrite

    # Custom paths
    python data-pipeline/src/generate_image_embeddings.py \\
        --images-dir data-pipeline/data/raw/images \\
        --articles   data-pipeline/data/raw/articles.csv \\
        --output     data-pipeline/data/processed/article_image_embeddings_clip_vit_base_patch32.parquet \\
        --overwrite

Environment:
    CLIP_MODEL             Override default model name
    EXPECTED_EMBEDDING_DIM Override expected output dimension
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
from PIL import Image, UnidentifiedImageError
from tqdm import tqdm
from transformers import CLIPModel, CLIPProcessor

# ──────────────────────────── constants ────────────────────────────

DEFAULT_MODEL_NAME = "openai/clip-vit-base-patch32"
DEFAULT_EXPECTED_DIM = 512
DEFAULT_BATCH_SIZE = 64

IMAGE_EMBEDDING_COL = "image_embedding"

_PIPELINE_ROOT = Path(__file__).parent.parent.resolve()
DEFAULT_IMAGES_DIR = str(_PIPELINE_ROOT / "data" / "raw" / "images")
DEFAULT_ARTICLES_PATH = str(_PIPELINE_ROOT / "data" / "raw" / "articles.csv")
DEFAULT_OUTPUT = str(
    _PIPELINE_ROOT
    / "data"
    / "processed"
    / "article_image_embeddings_clip_vit_base_patch32.parquet"
)

_OPTIONAL_METADATA_COLS = [
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
logger = logging.getLogger("scalestyle.generate_image_embeddings")

# ──────────────────────────── article ID helpers ────────────────────────────


def normalize_article_id(value) -> str:
    """
    Normalize article_id to a 10-digit zero-padded string.

    Accepts int, float, or any string representation.
    Examples:
        108775015   -> "0108775015"
        "108775015" -> "0108775015"
        "0108775015" -> "0108775015"
    """
    return str(int(value)).zfill(10)


def image_path_to_article_id(path: Path) -> str:
    """
    Derive the normalized article_id from an image file path.

    The H&M dataset uses 10-digit zero-padded stems as filenames:
        images/010/0108775015.jpg  ->  "0108775015"
    """
    return path.stem.zfill(10)


# ──────────────────────────── data loading ────────────────────────────


def discover_images(images_dir: str) -> list[Path]:
    """Return all .jpg files under images_dir, sorted for reproducibility."""
    root = Path(images_dir)
    if not root.exists():
        raise FileNotFoundError(f"Images directory not found: {images_dir}")
    files = sorted(root.rglob("*.jpg"))
    if not files:
        raise ValueError(f"No .jpg files found under: {images_dir}")
    return files


def build_article_lookup(articles_path: str) -> dict[str, dict]:
    """
    Load articles.csv and return {normalized_article_id_str: row_dict}.

    Only metadata columns useful for the output parquet are retained.
    """
    if not Path(articles_path).exists():
        raise FileNotFoundError(f"Articles CSV not found: {articles_path}")

    keep = ["article_id"] + _OPTIONAL_METADATA_COLS
    df = pd.read_csv(articles_path, usecols=lambda c: c in keep, low_memory=False)
    df["article_id_str"] = df["article_id"].apply(normalize_article_id)

    lookup: dict[str, dict] = {}
    for _, row in df.iterrows():
        lookup[row["article_id_str"]] = row.to_dict()
    logger.info("Loaded %d articles into lookup", len(lookup))
    return lookup


# ──────────────────────────── CLIP model wrapper ────────────────────────────


def select_device(device_arg: str) -> tuple[str, torch.dtype]:
    """
    Resolve device string and matching dtype.

    "auto" priority: CUDA (float16) → MPS (float32) → CPU (float32).
    Float16 is only used on CUDA where it is stable; MPS/CPU use float32.
    """
    if device_arg == "auto":
        if torch.cuda.is_available():
            return "cuda", torch.float16
        if torch.backends.mps.is_available():
            return "mps", torch.float32
        return "cpu", torch.float32

    dtype = torch.float16 if device_arg == "cuda" else torch.float32
    return device_arg, dtype


class CLIPImageEncoder:
    """Thin wrapper around CLIPModel for batch image encoding."""

    def __init__(self, model_name: str, device: str, dtype: torch.dtype):
        self.model_name = model_name
        self.device = device
        self.dtype = dtype

        logger.info("Loading CLIPProcessor: %s", model_name)
        self.processor = CLIPProcessor.from_pretrained(model_name)

        logger.info("Loading CLIPModel (device=%s, dtype=%s) …", device, dtype)
        self.model = CLIPModel.from_pretrained(model_name, torch_dtype=dtype)
        self.model.to(device).eval()
        logger.info("CLIP model ready")

    def encode_batch(self, images: list) -> np.ndarray:
        """
        Encode a list of PIL Images.

        Returns a float32 numpy array of shape (N, dim), L2-normalised.
        """
        inputs = self.processor(images=images, return_tensors="pt")
        # Cast pixel_values to model dtype before moving to device
        pixel_values = inputs["pixel_values"].to(dtype=self.dtype, device=self.device)

        with torch.no_grad():
            features = self.model.get_image_features(pixel_values=pixel_values)
            features = F.normalize(features, p=2, dim=1)
            return features.float().cpu().numpy()


# ──────────────────────────── core generation ────────────────────────────


def generate_image_embeddings(
    image_files: list[Path],
    article_lookup: dict[str, dict],
    encoder: CLIPImageEncoder,
    batch_size: int = DEFAULT_BATCH_SIZE,
    limit: int | None = None,
) -> tuple[list[dict], int, int]:
    """
    Encode matched images and return (rows, unmatched_count, skipped_corrupt_count).

    rows: list of dicts ready to become a DataFrame.
    unmatched_count: image files with no corresponding article in articles.csv.
    skipped_corrupt_count: image files that could not be opened by PIL.
    """
    rows: list[dict] = []
    unmatched_count = 0
    skipped_corrupt_count = 0

    # Filter to matched images first (avoids wasting encode time on unmatched)
    matched_files: list[tuple[Path, dict]] = []
    for img_path in image_files:
        aid = image_path_to_article_id(img_path)
        meta = article_lookup.get(aid)
        if meta is None:
            logger.warning(
                "No article metadata for image: %s (article_id=%s)", img_path.name, aid
            )
            unmatched_count += 1
        else:
            matched_files.append((img_path, meta))

    if limit is not None:
        matched_files = matched_files[:limit]

    logger.info(
        "Processing %d matched images (unmatched=%d, limit=%s)",
        len(matched_files),
        unmatched_count,
        limit,
    )

    # Process in batches
    pending: list[tuple[Image.Image, Path, dict]] = []

    def _flush(batch: list[tuple[Image.Image, Path, dict]]) -> None:
        if not batch:
            return
        pil_images = [item[0] for item in batch]
        embeddings = encoder.encode_batch(pil_images)
        for (_, img_path, meta), emb in zip(batch, embeddings, strict=False):
            row: dict = {
                "article_id": meta["article_id"],
                "article_id_str": normalize_article_id(meta["article_id"]),
                "image_path": str(img_path),
                IMAGE_EMBEDDING_COL: emb.tolist(),
            }
            for col in _OPTIONAL_METADATA_COLS:
                if col in meta:
                    row[col] = meta[col]
            rows.append(row)

    for img_path, meta in tqdm(matched_files, desc="Embedding images", unit="img"):
        try:
            pil_img = Image.open(img_path).convert("RGB")
        except (UnidentifiedImageError, OSError, Exception) as exc:
            logger.warning("Skipping corrupt image %s: %s", img_path.name, exc)
            skipped_corrupt_count += 1
            continue

        pending.append((pil_img, img_path, meta))

        if len(pending) >= batch_size:
            _flush(pending)
            pending = []

    _flush(pending)  # flush remainder

    return rows, unmatched_count, skipped_corrupt_count


# ──────────────────────────── sidecar metadata ────────────────────────────


def write_sidecar(
    sidecar_path: str,
    *,
    model_name: str,
    embedding_dim: int,
    row_count: int,
    images_dir: str,
    articles_path: str,
    output_path: str,
    device: str,
    dtype: str,
    batch_size: int,
    total_image_files_found: int,
    matched_image_count: int,
    unmatched_image_count: int,
    skipped_corrupt_image_count: int,
) -> None:
    meta = {
        "model_name": model_name,
        "embedding_dim": embedding_dim,
        "vector_field": IMAGE_EMBEDDING_COL,
        "normalized": True,
        "encoder": "CLIP image encoder",
        "pooling": "CLS / [CLS] token projection",
        "row_count": row_count,
        "images_dir": images_dir,
        "articles_path": articles_path,
        "output_path": output_path,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "device": device,
        "dtype": dtype,
        "batch_size": batch_size,
        "total_image_files_found": total_image_files_found,
        "matched_image_count": matched_image_count,
        "unmatched_image_count": unmatched_image_count,
        "skipped_corrupt_image_count": skipped_corrupt_image_count,
    }
    with open(sidecar_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
    logger.info("Sidecar written: %s", sidecar_path)


# ──────────────────────────── CLI ────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate CLIP image embeddings for H&M fashion articles.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--images-dir",
        default=DEFAULT_IMAGES_DIR,
        help="Root directory containing H&M images (default: data/raw/images)",
    )
    parser.add_argument(
        "--articles",
        default=DEFAULT_ARTICLES_PATH,
        help="Path to articles.csv (default: data/raw/articles.csv)",
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help="Output parquet path",
    )
    parser.add_argument(
        "--model-name",
        default=DEFAULT_MODEL_NAME,
        help=f"HuggingFace CLIP model name (default: {DEFAULT_MODEL_NAME})",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"Image batch size (default: {DEFAULT_BATCH_SIZE})",
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
        help="Process only the first N matched images — useful for smoke tests",
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
    return parser


def main() -> None:
    args = _build_parser().parse_args()

    output_path = Path(args.output)
    if output_path.exists() and not args.overwrite:
        print(
            f"Output already exists: {args.output}\n"
            "   Use --overwrite to replace it."
        )
        sys.exit(1)

    # ── Resolve device ──
    device, dtype = select_device(args.device)
    print(f"\nDevice: {device}  dtype: {dtype}")

    # ── Discover images ──
    image_files = discover_images(args.images_dir)
    print(f"Found {len(image_files):,} image files in {args.images_dir}")

    # ── Load article metadata ──
    article_lookup = build_article_lookup(args.articles)

    # ── Load CLIP model ──
    encoder = CLIPImageEncoder(
        model_name=args.model_name,
        device=device,
        dtype=dtype,
    )

    # ── Generate embeddings ──
    rows, unmatched_count, skipped_corrupt_count = generate_image_embeddings(
        image_files=image_files,
        article_lookup=article_lookup,
        encoder=encoder,
        batch_size=args.batch_size,
        limit=args.limit,
    )

    if not rows:
        print("No embeddings generated — check images-dir and articles paths.")
        sys.exit(1)

    # ── Build output DataFrame ──
    output_df = pd.DataFrame(rows)

    # ── Validate dimension ──
    sample_vec = output_df[IMAGE_EMBEDDING_COL].iloc[0]
    actual_dim = len(sample_vec)
    if actual_dim != args.expected_dim:
        print(
            f"Dimension mismatch: generated {actual_dim}-dim vectors, "
            f"expected {args.expected_dim}."
        )
        sys.exit(1)

    # ── Write parquet ──
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_df.to_parquet(str(output_path), index=False)

    # ── Write sidecar ──
    sidecar_path = args.metadata_json or str(output_path.with_suffix(".meta.json"))
    write_sidecar(
        sidecar_path,
        model_name=args.model_name,
        embedding_dim=actual_dim,
        row_count=len(output_df),
        images_dir=args.images_dir,
        articles_path=args.articles,
        output_path=str(output_path),
        device=device,
        dtype=str(dtype),
        batch_size=args.batch_size,
        total_image_files_found=len(image_files),
        matched_image_count=len(rows) + skipped_corrupt_count,
        unmatched_image_count=unmatched_count,
        skipped_corrupt_image_count=skipped_corrupt_count,
    )

    # ── Summary ──
    print("\n" + "=" * 60)
    print("Image embedding generation complete")
    print("=" * 60)
    print(f"  Model:          {args.model_name}")
    print(f"  Device:         {device}  (dtype={dtype})")
    print(f"  Rows:           {len(output_df):,}")
    print(f"  Dim:            {actual_dim}")
    print(f"  Col:            {IMAGE_EMBEDDING_COL}")
    print(f"  Output:         {output_path}")
    print(f"  Sidecar:        {sidecar_path}")
    print(f"  Total images:   {len(image_files):,}")
    print(f"  Matched:        {len(rows):,}")
    print(f"  Unmatched:      {unmatched_count:,}")
    print(f"  Skipped/corrupt:{skipped_corrupt_count:,}")
    print(f"  Sample[0]:      [{sample_vec[0]:.6f}, {sample_vec[1]:.6f}, ...]")
    print("=" * 60)
    print("\nNext step:")
    print("  python data-pipeline/src/validate_image_embeddings.py \\")
    print(f"    --input {output_path}")


if __name__ == "__main__":
    main()
