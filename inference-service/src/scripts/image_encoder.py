#!/usr/bin/env python3
"""
Batch Image Encoder

Processes multiple images efficiently with FashionCLIP, outputting 512-d embeddings.

Usage:
    from vision.image_encoder import ImageEncoder
    
    encoder = ImageEncoder(model="fashion_clip", device="cuda", batch_size=32)
    results = encoder.encode_batch(image_paths)
    encoder.save_to_parquet(results, "embeddings.parquet")

CLI Usage:
    python image_encoder.py \\
        --model fashion_clip \\
        --image_dir ../data/images \\
        --output_path embeddings.parquet \\
        --batch_size 16
"""

import argparse
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
import torch
from PIL import Image
from transformers import CLIPModel, CLIPProcessor
from tqdm import tqdm


class ImageEncoder:
    """Batch image encoder with FashionCLIP"""

    def __init__(
        self,
        model: str = "fashion_clip",
        device: Optional[str] = None,
        batch_size: int = 32,
    ):
        """
        Args:
            model: "st_clip" or "fashion_clip"
            device: "cuda", "mps", "cpu" (auto-detect if None)
            batch_size: Images per batch
        """
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

        # Load model
        print(f"\n{'='*60}")
        print(f"Loading {model} on {device}...")
        print(f"{'='*60}")

        model_configs = {
            "st_clip": "openai/clip-vit-base-patch32",
            "fashion_clip": "patrickjohncyh/fashion-clip",
        }

        if model not in model_configs:
            raise ValueError(f"Unknown model: {model}")

        start_time = time.time()
        self.model_name = model
        model_id = model_configs[model]
        
        # Load CLIP model and processor
        self.model = CLIPModel.from_pretrained(model_id).to(device)
        self.processor = CLIPProcessor.from_pretrained(model_id)
        self.model.eval()  # Set to evaluation mode
        
        load_time = time.time() - start_time

        print(f"✅ Model loaded in {load_time:.2f}s\n")

        # Enable mixed precision for faster inference on A100/H100
        if device == "cuda" and torch.cuda.get_device_capability()[0] >= 8:
            torch.set_float32_matmul_precision("high")
            print("✅ Mixed precision enabled (A100/H100 optimization)\n")

    def encode_single(self, image_path: Path) -> Optional[Dict]:
        """
        Encode a single image.

        Returns:
            Dict with keys: image_id, embedding, width, height, file_size, path
            None if image cannot be loaded
        """
        try:
            # Load image
            img = Image.open(image_path).convert("RGB")
            width, height = img.size
            file_size = image_path.stat().st_size

            # Preprocess and generate embedding
            inputs = self.processor(images=img, return_tensors="pt").to(self.device)
            
            with torch.no_grad():
                image_features = self.model.get_image_features(**inputs)
                # Normalize to unit vector (L2 normalization)
                embedding = image_features / image_features.norm(dim=-1, keepdim=True)

            # Convert to numpy and ensure (512,) shape
            embedding_np = embedding.detach().cpu().numpy().squeeze()

            return {
                "image_id": image_path.stem,  # Filename without extension
                "embedding": embedding_np.tolist(),
                "image_path": str(image_path),
                "width": width,
                "height": height,
                "file_size": file_size,
                "processed_at": datetime.now(timezone.utc).isoformat(),
            }

        except Exception as e:
            print(f"⚠️  Error processing {image_path}: {e}")
            return None

    def encode_batch(
        self,
        image_paths: List[Path],
        show_progress: bool = True,
    ) -> List[Dict]:
        """
        Encode multiple images in batches.

        Args:
            image_paths: List of Path objects to images
            show_progress: Show progress bar

        Returns:
            List of dicts with embeddings and metadata
        """
        results = []
        num_batches = (len(image_paths) + self.batch_size - 1) // self.batch_size

        iterator = range(0, len(image_paths), self.batch_size)
        if show_progress:
            iterator = tqdm(iterator, total=num_batches, desc="Encoding batches")

        total_time = 0

        for i in iterator:
            batch_paths = image_paths[i : i + self.batch_size]

            # Load images
            images = []
            valid_indices = []
            metadata = []

            for idx, img_path in enumerate(batch_paths):
                try:
                    img = Image.open(img_path).convert("RGB")
                    images.append(img)
                    valid_indices.append(idx)
                    metadata.append(
                        {
                            "image_id": img_path.stem,
                            "image_path": str(img_path),
                            "width": img.size[0],
                            "height": img.size[1],
                            "file_size": img_path.stat().st_size,
                        }
                    )
                except Exception as e:
                    print(f"\n⚠️  Skipping {img_path}: {e}")
                    continue

            if not images:
                continue

            # Encode batch
            batch_start = time.time()
            with torch.no_grad():
                inputs = self.processor(images=images, return_tensors="pt", padding=True).to(self.device)
                image_features = self.model.get_image_features(**inputs)
                # Normalize embeddings (L2 normalization)
                embeddings = image_features / image_features.norm(dim=-1, keepdim=True)
            batch_time = time.time() - batch_start
            total_time += batch_time

            # Convert to numpy
            embeddings_np = embeddings.detach().cpu().numpy()

            # Combine with metadata
            for j, emb in enumerate(embeddings_np):
                result = metadata[j].copy()
                result["embedding"] = emb.squeeze().tolist()
                result["processed_at"] = datetime.now(timezone.utc).isoformat()
                results.append(result)

        # Print summary
        if results:
            avg_time_per_image = total_time / len(results) * 1000  # ms
            throughput = len(results) / total_time if total_time > 0 else 0
            print(f"\n{'='*60}")
            print(f"✅ Processed {len(results)}/{len(image_paths)} images")
            print(f"   Total time: {total_time:.2f}s")
            print(f"   Throughput: {throughput:.1f} img/s")
            print(f"   Avg per image: {avg_time_per_image:.1f}ms")
            print(f"{'='*60}\n")

        return results

    def save_to_parquet(
        self,
        results: List[Dict],
        output_path: Path,
        compression: str = "snappy",
    ):
        """
        Save embeddings to Parquet file.

        Args:
            results: List of dicts from encode_batch()
            output_path: Output Parquet file path
            compression: "snappy", "gzip", or None
        """
        if not results:
            print("⚠️  No results to save")
            return

        # Convert to DataFrame
        df = pd.DataFrame(results)

        # Validate embeddings
        sample_emb = df.iloc[0]["embedding"]
        assert len(sample_emb) == 512, f"Expected 512-d, got {len(sample_emb)}-d"

        # Save
        output_path.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(output_path, compression=compression, index=False)

        file_size_mb = output_path.stat().st_size / 1024 / 1024
        print(f"✅ Saved {len(results)} embeddings to {output_path}")
        print(f"   File size: {file_size_mb:.2f} MB")


def main():
    parser = argparse.ArgumentParser(description="Batch encode images with FashionCLIP")
    parser.add_argument(
        "--model",
        type=str,
        default="fashion_clip",
        choices=["st_clip", "fashion_clip"],
        help="CLIP model to use",
    )
    parser.add_argument(
        "--image_dir",
        type=str,
        required=True,
        help="Directory containing images",
    )
    parser.add_argument(
        "--output_path",
        type=str,
        required=True,
        help="Output Parquet file path",
    )
    parser.add_argument(
        "--batch_size",
        type=int,
        default=32,
        help="Batch size for encoding",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limit number of images to process (for testing)",
    )
    parser.add_argument(
        "--pattern",
        type=str,
        default="*.jpg",
        help="File pattern to match (default: *.jpg)",
    )

    args = parser.parse_args()

    # Find images
    image_dir = Path(args.image_dir)
    if not image_dir.exists():
        raise FileNotFoundError(f"Directory not found: {image_dir}")

    image_paths = sorted(image_dir.rglob(args.pattern))

    if args.limit:
        image_paths = image_paths[: args.limit]

    print(f"\nFound {len(image_paths)} images in {image_dir}")

    if not image_paths:
        print("⚠️  No images found!")
        return

    # Encode
    encoder = ImageEncoder(
        model=args.model,
        batch_size=args.batch_size,
    )

    results = encoder.encode_batch(image_paths)

    # Save
    output_path = Path(args.output_path)
    encoder.save_to_parquet(results, output_path)

    print("\nImage ETL Complete!")


if __name__ == "__main__":
    main()
