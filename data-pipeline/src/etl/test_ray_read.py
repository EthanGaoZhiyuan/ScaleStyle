import pyarrow.fs
import ray


def test_ray_read_minio():
    # 1. Initialize Ray
    if not ray.is_initialized():
        ray.init()

    print("Ray initialized.")

    # 2. Configure S3/MinIO connection for PyArrow
    s3_fs = pyarrow.fs.S3FileSystem(
        access_key="minioadmin",
        secret_key="minioadmin",
        endpoint_override="http://127.0.0.1:9000",
        scheme="http",
    )

    # 3. Define the S3 path
    s3_uri = "recsys-data/processed/train_data_parquet"

    print(f"Reading data from MinIO: {s3_uri}")

    try:
        # 4. Read Parquet using Ray Data
        ds = ray.data.read_parquet(s3_uri, filesystem=s3_fs)

        # 5. Show results
        print(f"Successfully read dataset with {ds.count()} records.")
        print("Preview of the first 5 records:")
        ds.show(limit=5)
    except Exception as e:
        print(f"Error reading dataset: {e}")
    finally:
        # 6. Shutdown Ray
        if ray.is_initialized():
            ray.shutdown()
            print("Ray shutdown.")


if __name__ == "__main__":
    test_ray_read_minio()
