#!/usr/bin/env python3
"""
Phase 3 Day 2: Test Image ETL Pipeline

Quick validation script for image_encoder.py and image_etl.py

Usage:
    cd inference-service
    python tests/test_image_etl.py
"""

import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import numpy as np
import pandas as pd


def test_model_loading():
    """Test: Model loads successfully"""
    print("\n" + "=" * 60)
    print("TEST 1: Model Loading")
    print("=" * 60)

    from vision.image_encoder import ImageEncoder

    try:
        _encoder = ImageEncoder(model="fashion_clip", batch_size=8)
        print("✅ FashionCLIP model loaded successfully")
        return True
    except Exception as e:
        print(f"❌ Model loading failed: {e}")
        return False


def test_single_image_encoding():
    """Test: Single image encoding produces valid embedding"""
    print("\n" + "=" * 60)
    print("TEST 2: Single Image Encoding")
    print("=" * 60)

    from vision.image_encoder import ImageEncoder
    from PIL import Image
    import tempfile

    # Create a dummy test image
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
        test_img_path = Path(f.name)
        img = Image.new("RGB", (256, 256), color="red")
        img.save(test_img_path)

    try:
        encoder = ImageEncoder(model="fashion_clip", batch_size=8)
        result = encoder.encode_single(test_img_path)

        # Validate result
        assert result is not None, "Result is None"
        assert "embedding" in result, "Missing 'embedding' key"
        assert "image_id" in result, "Missing 'image_id' key"
        assert "width" in result, "Missing 'width' key"
        assert "height" in result, "Missing 'height' key"

        embedding = result["embedding"]
        assert len(embedding) == 512, f"Expected 512-d, got {len(embedding)}-d"

        # Check L2 norm (should be ~1.0 if normalized)
        embedding_np = np.array(embedding)
        l2_norm = np.linalg.norm(embedding_np)
        assert 0.99 <= l2_norm <= 1.01, f"L2 norm {l2_norm:.6f} not normalized"

        print("✅ Single image encoded successfully")
        print(f"   Embedding shape: {len(embedding)}")
        print(f"   L2 norm: {l2_norm:.6f}")
        print(f"   Image size: {result['width']}x{result['height']}")

        # Cleanup
        test_img_path.unlink()

        return True

    except Exception as e:
        print(f"❌ Single image encoding failed: {e}")
        test_img_path.unlink(missing_ok=True)
        return False


def test_batch_encoding():
    """Test: Batch encoding processes multiple images"""
    print("\n" + "=" * 60)
    print("TEST 3: Batch Encoding")
    print("=" * 60)

    from vision.image_encoder import ImageEncoder
    from PIL import Image
    import tempfile

    # Create multiple test images
    test_images = []
    num_images = 5

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        for i in range(num_images):
            img_path = tmpdir / f"test_{i}.jpg"
            # Create images with different colors
            color = (i * 50, (i * 30) % 255, (i * 70) % 255)
            img = Image.new("RGB", (256, 256), color=color)
            img.save(img_path)
            test_images.append(img_path)

        try:
            encoder = ImageEncoder(model="fashion_clip", batch_size=2)
            results = encoder.encode_batch(test_images, show_progress=False)

            # Validate results
            assert (
                len(results) == num_images
            ), f"Expected {num_images}, got {len(results)}"

            for result in results:
                assert len(result["embedding"]) == 512, "Invalid embedding dimension"
                embedding_np = np.array(result["embedding"])
                l2_norm = np.linalg.norm(embedding_np)
                assert 0.99 <= l2_norm <= 1.01, f"L2 norm {l2_norm:.6f} not normalized"

            print("✅ Batch encoding successful")
            print(f"   Processed {len(results)} images")
            print("   All embeddings are 512-d and normalized")

            return True

        except Exception as e:
            print(f"❌ Batch encoding failed: {e}")
            return False


def test_parquet_save():
    """Test: Parquet save and load work correctly"""
    print("\n" + "=" * 60)
    print("TEST 4: Parquet Save/Load")
    print("=" * 60)

    from vision.image_encoder import ImageEncoder
    from PIL import Image
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        # Create test image
        img_path = tmpdir / "test.jpg"
        img = Image.new("RGB", (256, 256), color="blue")
        img.save(img_path)

        try:
            encoder = ImageEncoder(model="fashion_clip", batch_size=1)
            results = encoder.encode_batch([img_path], show_progress=False)

            # Save to Parquet
            output_path = tmpdir / "embeddings.parquet"
            encoder.save_to_parquet(results, output_path)

            # Load and validate
            df = pd.read_parquet(output_path)

            assert len(df) == 1, f"Expected 1 row, got {len(df)}"
            assert "embedding" in df.columns, "Missing 'embedding' column"
            assert "image_id" in df.columns, "Missing 'image_id' column"

            embedding = df.iloc[0]["embedding"]
            assert len(embedding) == 512, f"Expected 512-d, got {len(embedding)}-d"

            print("✅ Parquet save/load successful")
            print(f"   File size: {output_path.stat().st_size / 1024:.2f} KB")
            print(f"   Columns: {df.columns.tolist()}")

            return True

        except Exception as e:
            print(f"❌ Parquet save/load failed: {e}")
            return False


def test_error_handling():
    """Test: Corrupted images are handled gracefully"""
    print("\n" + "=" * 60)
    print("TEST 5: Error Handling")
    print("=" * 60)

    from vision.image_encoder import ImageEncoder
    from PIL import Image
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        # Create one valid and one corrupted image
        valid_path = tmpdir / "valid.jpg"
        corrupted_path = tmpdir / "corrupted.jpg"

        img = Image.new("RGB", (256, 256), color="green")
        img.save(valid_path)

        # Create corrupted file (not a valid image)
        with open(corrupted_path, "w") as f:
            f.write("This is not an image")

        try:
            encoder = ImageEncoder(model="fashion_clip", batch_size=2)
            results = encoder.encode_batch(
                [valid_path, corrupted_path], show_progress=False
            )

            # Should get 1 result (valid image), corrupted one skipped
            assert len(results) == 1, f"Expected 1 result, got {len(results)}"
            assert results[0]["image_id"] == "valid", "Wrong image processed"

            print("✅ Error handling successful")
            print("   Processed 1/2 images (1 corrupted, skipped)")

            return True

        except Exception as e:
            print(f"❌ Error handling failed: {e}")
            return False


def run_all_tests():
    """Run all tests and report summary"""
    print("\n" + "=" * 60)
    print("PHASE 3 DAY 2: IMAGE ETL TESTS")
    print("=" * 60)

    # Check dependencies
    print("\nChecking dependencies...")
    try:
        import importlib.util

        required_modules = ["torch", "PIL", "sentence_transformers", "pandas"]
        missing = [
            mod for mod in required_modules if importlib.util.find_spec(mod) is None
        ]

        if missing:
            raise ImportError(f"Missing modules: {', '.join(missing)}")

        import torch  # noqa: F401

        print("✅ All dependencies available")
    except ImportError as e:
        print(f"❌ Missing dependency: {e}")
        print("\nInstall with:")
        print("  pip install torch pillow sentence-transformers pandas pyarrow")
        return

    # Check device
    if torch.cuda.is_available():
        device = "cuda"
    elif torch.backends.mps.is_available():
        device = "mps"
    else:
        device = "cpu"
    print(f"✅ Using device: {device}")

    # Run tests
    tests = [
        test_model_loading,
        test_single_image_encoding,
        test_batch_encoding,
        test_parquet_save,
        test_error_handling,
    ]

    results = []
    for test_func in tests:
        try:
            result = test_func()
            results.append(result)
        except Exception as e:
            print(f"\n❌ Test crashed: {e}")
            results.append(False)

    # Summary
    passed = sum(results)
    total = len(results)

    print("\n" + "=" * 60)
    print(f"TEST SUMMARY: {passed}/{total} passed")
    print("=" * 60)

    if passed == total:
        print("\n✅ All tests passed! Ready for Day 2 ETL.")
    else:
        print(f"\n⚠️  {total - passed} test(s) failed. Check errors above.")


if __name__ == "__main__":
    run_all_tests()
