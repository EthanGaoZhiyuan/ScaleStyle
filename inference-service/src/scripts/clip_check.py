#!/usr/bin/env python3
"""
CLIP Embedding Sanity Check

Verifies that image embeddings can be generated stably with both:
- LAION CLIP (general-purpose baseline)
- FashionCLIP (domain-adapted for fashion)

Usage:
    python clip_check.py --model laion_clip --image_path ./data/sample.jpg
    python clip_check.py --model fashion_clip --image_path ./data/sample.jpg

Environment variables:
    VISION_BACKBONE: laion_clip|fashion_clip (default: laion_clip)
"""

import argparse
import os
import time
from pathlib import Path

import numpy as np
import torch
from PIL import Image
from sentence_transformers import SentenceTransformer

# Model configurations
MODEL_CONFIGS = {
    "st_clip": {
        "model_name": "clip-ViT-B-32",
        "description": "Sentence-Transformers CLIP ViT-B/32 (baseline)",
        "expected_dim": 512,
    },
    "fashion_clip": {
        "model_name": "patrickjohncyh/fashion-clip",
        "description": "FashionCLIP 2.0 (domain-adapted for fashion, PRODUCTION)",
        "expected_dim": 512,
    },
}


def load_model(model_key: str, device: str = "cpu"):
    """Load CLIP model with specified backend"""
    if model_key not in MODEL_CONFIGS:
        raise ValueError(
            f"Unknown model: {model_key}. Choose from {list(MODEL_CONFIGS.keys())}"
        )

    config = MODEL_CONFIGS[model_key]
    print(f"\n{'='*60}")
    print(f"Loading: {config['description']}")
    print(f"Model ID: {config['model_name']}")
    print(f"Device: {device}")
    print(f"{'='*60}\n")

    start_time = time.time()
    model = SentenceTransformer(config["model_name"], device=device)
    load_time = time.time() - start_time

    print(f"✅ Model loaded in {load_time:.2f}s")
    return model, config


def embed_image(model, image_path: Path, normalize: bool = True):
    """Generate embedding for a single image"""
    # Load image
    image = Image.open(image_path).convert("RGB")
    print(f"\n📸 Image: {image_path.name}")
    print(f"   Size: {image.size}")

    # Generate embedding
    start_time = time.time()
    with torch.no_grad():
        embedding = model.encode(
            image,
            convert_to_tensor=True,
            normalize_embeddings=normalize,
        )
    embed_time = time.time() - start_time

    # Convert to numpy and ensure 1D shape (512,) not (1, 512)
    embedding_np = embedding.detach().cpu().numpy()
    embedding_np = np.asarray(embedding_np).squeeze()

    print(f"\n⚡ Embedding generated in {embed_time*1000:.2f}ms")
    return embedding_np


def analyze_embedding(embedding: np.ndarray, config: dict):
    """Analyze and print embedding statistics"""
    print(f"\n{'='*60}")
    print("EMBEDDING ANALYSIS")
    print(f"{'='*60}")

    print(f"\n📊 Shape: {embedding.shape}")
    print(f"   Expected dimension: {config['expected_dim']}")

    if embedding.shape[0] != config["expected_dim"]:
        print("   ⚠️  WARNING: Dimension mismatch!")
    else:
        print("   ✅ Dimension matches expected")

    print(f"\n🔢 Dtype: {embedding.dtype}")

    # L2 norm (should be ~1.0 if normalized)
    l2_norm = np.linalg.norm(embedding)
    print(f"\n📏 L2 Norm: {l2_norm:.6f}")
    if 0.99 <= l2_norm <= 1.01:
        print("   ✅ Embedding is normalized")
    else:
        print("   ⚠️  Embedding may not be normalized")

    # Statistics
    print("\n📈 Statistics:")
    print(f"   Min: {embedding.min():.6f}")
    print(f"   Max: {embedding.max():.6f}")
    print(f"   Mean: {embedding.mean():.6f}")
    print(f"   Std: {embedding.std():.6f}")

    # First 5 values (for reproducibility check)
    print("\n🔍 First 5 values (for reproducibility):")
    for i, val in enumerate(embedding[:5]):
        print(f"   [{i}]: {val:.8f}")

    print(f"\n{'='*60}\n")


def compute_similarity(model, image_paths: list[Path]):
    """Compute pairwise cosine similarities (sanity check)"""
    if len(image_paths) < 2:
        return

    print(f"\n{'='*60}")
    print("SIMILARITY SANITY CHECK")
    print(f"{'='*60}\n")

    embeddings = []
    for img_path in image_paths:
        emb = embed_image(model, img_path, normalize=True)
        embeddings.append(emb)

    # Compute pairwise cosine similarities
    print("\n📊 Cosine Similarities:")
    for i in range(len(embeddings)):
        for j in range(i + 1, len(embeddings)):
            sim = np.dot(embeddings[i], embeddings[j])
            print(f"   {image_paths[i].name} ↔ {image_paths[j].name}: {sim:.4f}")

    print(f"\n{'='*60}\n")


def main():
    parser = argparse.ArgumentParser(
        description="CLIP Embedding Sanity Check"
    )
    parser.add_argument(
        "--model",
        type=str,
        default=os.getenv("VISION_BACKBONE", "fashion_clip"),
        choices=list(MODEL_CONFIGS.keys()),
        help="CLIP model backend to use (default: fashion_clip for production)",
    )
    parser.add_argument(
        "--image_path",
        type=str,
        required=True,
        help="Path to image file (or comma-separated paths for similarity check)",
    )
    parser.add_argument(
        "--device",
        type=str,
        default="cuda" if torch.cuda.is_available() else "cpu",
        choices=["cpu", "cuda", "mps"],
        help="Device to run model on",
    )

    args = parser.parse_args()

    # Parse image paths
    image_paths = [Path(p.strip()) for p in args.image_path.split(",")]

    # Validate image paths
    for img_path in image_paths:
        if not img_path.exists():
            raise FileNotFoundError(f"Image not found: {img_path}")

    # Load model
    model, config = load_model(args.model, args.device)

    # Generate and analyze embeddings
    if len(image_paths) == 1:
        embedding = embed_image(model, image_paths[0])
        analyze_embedding(embedding, config)
    else:
        # Multiple images: do similarity check
        for img_path in image_paths:
            embedding = embed_image(model, img_path)
            analyze_embedding(embedding, config)
        compute_similarity(model, image_paths)

    print("\nCheck Complete!")


if __name__ == "__main__":
    main()
