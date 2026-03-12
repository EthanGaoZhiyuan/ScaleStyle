import argparse
import logging
import os
import sys
from datetime import datetime

sys.path.append(os.getcwd())

import pyarrow.fs
import ray

from src.utils.s3_client import S3Client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

logger = logging.getLogger(__name__)


def run_spark_etl(start_date, end_date):
    """Run Spark ETL to process raw data (placeholder for future implementation)."""
    logger.info(f"[ETL] Starting Spark ETL from {start_date} to {end_date}")
    logger.info("[ETL] Data processing complete (placeholder).")


def run_s3_upload(local_path, bucket_name, s3_prefix):
    """Upload processed data to MinIO/S3."""
    logger.info(
        f"[S3 Upload] Uploading data from {local_path} to bucket: {bucket_name}, prefix: {s3_prefix}"
    )

    if not os.path.exists(local_path):
        logger.error(f"[S3 Upload] Local path not found: {local_path}")
        return False

    try:
        client = S3Client()
        client.upload_folder(local_path, s3_prefix)
        logger.info("[S3 Upload] Upload completed successfully.")
        return True
    except Exception as e:
        logger.error(f"[S3 Upload] Upload failed: {e}")
        return False


def run_data_validation(bucket_name, s3_prefix):
    """Validate uploaded data integrity using Ray Data."""
    s3_uri = f"{bucket_name}/{s3_prefix}"
    logger.info(
        f"[Data Validation] Validating data in bucket: {bucket_name}, prefix: {s3_prefix}"
    )

    try:
        if not ray.is_initialized():
            ray.init()
            logger.info("[Data Validation] Ray initialized.")

        s3_fs = pyarrow.fs.S3FileSystem(
            access_key=os.getenv("MINIO_ACCESS_KEY", "minioadmin"),
            secret_key=os.getenv("MINIO_SECRET_KEY", "minioadmin"),
            endpoint_override=os.getenv("MINIO_ENDPOINT", "http://127.0.0.1:9000"),
            scheme="http",
        )

        ds = ray.data.read_parquet(s3_uri, filesystem=s3_fs)
        count = ds.count()

        logger.info(
            f"[Data Validation] Successfully read dataset with {count} records."
        )
        if count > 0:
            logger.info("[Data Validation] Data validation passed.")
            return True
        else:
            logger.error("[Data Validation] Data validation failed: No records found.")
            return False
    except Exception as e:
        logger.error(f"[Data Validation] Data validation failed: {e}")
        return False
    finally:
        if ray.is_initialized():
            ray.shutdown()
            logger.info("[Data Validation] Ray shutdown.")


def main():
    parser = argparse.ArgumentParser(
        description="ScaleStyle Data Pipeline Orchestrator"
    )

    parser.add_argument(
        "--stage",
        type=str,
        choices=["etl", "upload", "validate", "all"],
        default="all",
        help="Pipeline stage to run",
    )
    parser.add_argument(
        "--start_date",
        type=str,
        help="Start date for data processing (YYYY-MM-DD)",
        default="2018-01-01",
    )
    parser.add_argument(
        "--end_date",
        type=str,
        help="End date for data processing (YYYY-MM-DD)",
        default=datetime.today().strftime("%Y-%m-%d"),
    )
    parser.add_argument(
        "--local_data_path",
        type=str,
        help="Local path to processed data",
        required=True,
    )
    parser.add_argument(
        "--bucket_name", type=str, help="S3/MinIO bucket name", default="recsys-data"
    )
    parser.add_argument(
        "--s3_prefix",
        type=str,
        help="Target folder in S3/MinIO",
        default="processed/train_data_parquet",
    )

    args = parser.parse_args()

    logger.info("=" * 50)
    logger.info(f"Starting Data Pipeline with stage: {args.stage.upper()}")
    logger.info("=" * 50)

    if args.stage in ["etl", "all"]:
        run_spark_etl(args.start_date, args.end_date)

    if args.stage in ["upload", "all"]:
        upload_success = run_s3_upload(
            args.local_data_path, args.bucket_name, args.s3_prefix
        )
        if not upload_success:
            logger.error("Upload step failed. Exiting pipeline.")
            sys.exit(1)

    if args.stage in ["validate", "all"]:
        validation_success = run_data_validation(args.bucket_name, args.s3_prefix)
        if not validation_success:
            logger.error("Data validation failed. Exiting pipeline.")
            sys.exit(1)

    logger.info("=" * 50)
    logger.info("Data Pipeline completed.")
    logger.info("=" * 50)


if __name__ == "__main__":
    main()
