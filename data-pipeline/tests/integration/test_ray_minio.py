"""Integration test for Ray Data reading from MinIO."""

import os

import pyarrow.fs
import pytest
import ray


@pytest.fixture(scope="module")
def minio_credentials():
    """Get MinIO credentials from environment variables."""
    return {
        "access_key": os.getenv("MINIO_ACCESS_KEY", "minioadmin"),
        "secret_key": os.getenv("MINIO_SECRET_KEY", "minioadmin"),
        "endpoint": os.getenv("MINIO_ENDPOINT", "http://127.0.0.1:9000"),
    }


@pytest.fixture(scope="module")
def ray_context():
    """Initialize and shutdown Ray for tests."""
    if not ray.is_initialized():
        ray.init()
    yield
    if ray.is_initialized():
        ray.shutdown()


def test_ray_read_parquet_from_minio(minio_credentials, ray_context):
    """Test reading Parquet data from MinIO using Ray Data."""
    s3_fs = pyarrow.fs.S3FileSystem(
        access_key=minio_credentials["access_key"],
        secret_key=minio_credentials["secret_key"],
        endpoint_override=minio_credentials["endpoint"],
        scheme="http",
    )

    s3_uri = "recsys-data/processed/train_data_parquet"

    ds = ray.data.read_parquet(s3_uri, filesystem=s3_fs)
    count = ds.count()

    assert count > 0, "Dataset should contain records"
    print(f"Successfully read {count} records from MinIO")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
