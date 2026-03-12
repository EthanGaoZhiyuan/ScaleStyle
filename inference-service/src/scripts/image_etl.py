#!/usr/bin/env python3
"""
Image ETL with Ray Data

Scalable pipeline for processing H&M product images at scale.

Features:
- Reads H&M articles.csv metadata
- Maps to image file paths
- Generates FashionCLIP embeddings in parallel
- Writes to Parquet with proper schema

Usage:
    python image_etl.py \\
        --metadata_path ../data-pipeline/data/raw/articles.csv \\
        --image_dir ../data-pipeline/data/raw/images \\
        --output_path ../data-pipeline/data/processed/image_embeddings.parquet \\
        --batch_size 32 \\
        --num_workers 4
"""

import argparse
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

import pandas as pd
import torch
from PIL import Image
from transformers import CLIPModel, CLIPProcessor

# Optional: Ray Data for distributed processing
try:
    import ray

    RAY_AVAILABLE = True
except ImportError:
    RAY_AVAILABLE = False
    print("⚠️  Ray not available, using simple batch processing")


class ImageETL:
    """ETL pipeline for H&M image embeddings"""

    def __init__(
        self,
        metadata_path: Path,
        image_dir: Path,
        model: str = "fashion_clip",
        device: Optional[str] = None,
        batch_size: int = 32,
    ):
        """
        Args:
            metadata_path: Path to articles.csv
            image_dir: Directory containing image files
            model: "st_clip" or "fashion_clip"
            device: "cuda", "mps", "cpu" (auto-detect if None)
            batch_size: Batch size for encoding
        """
        self.metadata_path = metadata_path
        self.image_dir = image_dir
        self.model_name = model
        self.batch_size = batch_size

        # Auto-detect device
        if device is None:
            if torch.cuda.is_available():
                device = "cuda"
            elif torch.backends.mps.is_available():
                device = "mps"
            else:
                device = "cpu"
        self.device = device

        # Model will be loaded lazily in workers
        self._model = None
        self._processor = None

    def load_model(self):
        """Lazy load model (called in worker processes)"""
        if self._model is not None:
            return self._model, self._processor

        model_configs = {
            "st_clip": "openai/clip-vit-base-patch32",
            "fashion_clip": "patrickjohncyh/fashion-clip",
        }

        print(f"Loading {self.model_name} on {self.device}...")
        model_id = model_configs[self.model_name]
        self._model = CLIPModel.from_pretrained(model_id).to(self.device)
        self._processor = CLIPProcessor.from_pretrained(model_id)
        self._model.eval()  # Set to evaluation mode
        print(f"✅ Model loaded on worker (PID: {os.getpid()})")

        return self._model, self._processor

    def load_metadata(self) -> pd.DataFrame:
        """Load H&M articles.csv and prepare image paths"""
        print(f"\nLoading metadata from {self.metadata_path}...")
        df = pd.read_csv(self.metadata_path)

        print(f"✅ Loaded {len(df)} articles")

        # Create image paths
        # H&M format: images/0{article_id[:3]}/{article_id}.jpg
        # Example: 0108775015 → images/010/0108775015.jpg
        def get_image_path(article_id):
            article_id_str = str(article_id).zfill(10)
            subdir = article_id_str[:3]
            filename = f"{article_id_str}.jpg"
            return self.image_dir / "images" / subdir / filename

        df["image_path"] = df["article_id"].apply(get_image_path)

        # Filter to existing images
        df["exists"] = df["image_path"].apply(lambda p: p.exists())
        df_valid = df[df["exists"]].copy()

        print(f"✅ Found {len(df_valid)}/{len(df)} images on disk")

        return df_valid[["article_id", "image_path"]]

    def encode_image(self, row: Dict) -> Optional[Dict]:
        """
        Encode a single image (called in parallel by Ray/workers).

        Args:
            row: Dict with 'article_id' and 'image_path'

        Returns:
            Dict with embedding and metadata, or None if failed
        """
        try:
            # Load model lazily
            model, processor = self.load_model()

            # Load image
            img_path = Path(row["image_path"])
            img = Image.open(img_path).convert("RGB")
            width, height = img.size
            file_size = img_path.stat().st_size

            # Generate embedding
            inputs = processor(images=img, return_tensors="pt").to(self.device)
            with torch.no_grad():
                image_features = model.get_image_features(**inputs)
                # Normalize to unit vector (L2 normalization)
                embedding = image_features / image_features.norm(dim=-1, keepdim=True)

            # Convert to numpy and ensure (512,) shape
            embedding_np = embedding.detach().cpu().numpy().squeeze()

            return {
                "image_id": str(row["article_id"]),
                "embedding": embedding_np.tolist(),
                "image_path": str(img_path.relative_to(self.image_dir.parent)),
                "width": width,
                "height": height,
                "file_size": file_size,
                "processed_at": datetime.now(timezone.utc).isoformat(),
            }

        except Exception as e:
            print(f"⚠️  Error processing {row.get('article_id')}: {e}")
            return None

    def run_simple(self, output_path: Path, limit: Optional[int] = None):
        """
        Run ETL with simple batch processing (no Ray).

        Args:
            output_path: Output Parquet file path
            limit: Limit number of images (for testing)
        """
        # Load metadata
        df_metadata = self.load_metadata()

        if limit:
            df_metadata = df_metadata.head(limit)
            print(f"⚠️  Limited to {limit} images for testing")

        print(f"\nProcessing {len(df_metadata)} images...")
        print(f"Batch size: {self.batch_size}")
        print(f"Device: {self.device}\n")

        # Process in batches using ImageEncoder
        from vision.image_encoder import ImageEncoder

        encoder = ImageEncoder(
            model=self.model_name,
            device=self.device,
            batch_size=self.batch_size,
        )

        image_paths = df_metadata["image_path"].tolist()
        results = encoder.encode_batch(image_paths, show_progress=True)

        # Save to Parquet
        encoder.save_to_parquet(results, output_path)

    def run_ray(
        self,
        output_path: Path,
        num_workers: int = 4,
        limit: Optional[int] = None,
    ):
        """
        Run ETL with Ray Data for distributed processing.

        Args:
            output_path: Output Parquet file path
            num_workers: Number of parallel workers
            limit: Limit number of images (for testing)
        """
        if not RAY_AVAILABLE:
            raise RuntimeError("Ray is not installed. Use run_simple() instead.")

        # Initialize Ray
        if not ray.is_initialized():
            ray.init(ignore_reinit_error=True)

        # Load metadata
        df_metadata = self.load_metadata()

        if limit:
            df_metadata = df_metadata.head(limit)
            print(f"⚠️  Limited to {limit} images for testing")

        print(f"\nProcessing {len(df_metadata)} images with Ray...")
        print(f"Workers: {num_workers}")
        print(f"Device: {self.device}\n")

        # Create Ray Dataset
        ds = ray.data.from_pandas(df_metadata)

        # Map encoding function
        ds_encoded = ds.map(
            self.encode_image,
            num_cpus=1,  # 1 CPU per task
        )

        # Filter out None results (failed encodings)
        ds_valid = ds_encoded.filter(lambda x: x is not None)

        # Write to Parquet
        output_path.parent.mkdir(parents=True, exist_ok=True)
        ds_valid.write_parquet(str(output_path))

        print(f"\n✅ Saved embeddings to {output_path}")

        # Validate
        df_result = pd.read_parquet(output_path)
        print(f"✅ Wrote {len(df_result)} records")
        print(f"   Columns: {df_result.columns.tolist()}")

        sample_emb = df_result.iloc[0]["embedding"]
        print(f"   Embedding dim: {len(sample_emb)}")


def main():
    parser = argparse.ArgumentParser(
        description="ETL pipeline for H&M image embeddings"
    )
    parser.add_argument(
        "--metadata_path",
        type=str,
        required=True,
        help="Path to articles.csv",
    )
    parser.add_argument(
        "--image_dir",
        type=str,
        required=True,
        help="Base directory containing images/ subdirectory",
    )
    parser.add_argument(
        "--output_path",
        type=str,
        required=True,
        help="Output Parquet file path",
    )
    parser.add_argument(
        "--model",
        type=str,
        default="fashion_clip",
        choices=["st_clip", "fashion_clip"],
        help="CLIP model to use",
    )
    parser.add_argument(
        "--batch_size",
        type=int,
        default=32,
        help="Batch size for encoding (simple mode)",
    )
    parser.add_argument(
        "--num_workers",
        type=int,
        default=4,
        help="Number of Ray workers (ray mode)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limit number of images (for testing)",
    )
    parser.add_argument(
        "--use_ray",
        action="store_true",
        help="Use Ray Data (requires ray installed)",
    )

    args = parser.parse_args()

    # Create ETL pipeline
    etl = ImageETL(
        metadata_path=Path(args.metadata_path),
        image_dir=Path(args.image_dir),
        model=args.model,
        batch_size=args.batch_size,
    )

    output_path = Path(args.output_path)

    # Run ETL
    if args.use_ray:
        etl.run_ray(
            output_path=output_path,
            num_workers=args.num_workers,
            limit=args.limit,
        )
    else:
        etl.run_simple(
            output_path=output_path,
            limit=args.limit,
        )

    print("\nETL Complete!")


if __name__ == "__main__":
    main()
