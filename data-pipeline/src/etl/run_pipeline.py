import argparse
import logging
import os
import sys
from datetime import datetime

# Add the project root directory to the Python path to ensure module under src/ can be imported
sys.path.append(os.getcwd())

import pyarrow.fs
import ray

from src.utils.s3_client import S3Client

# --- Logging Configuration ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

logger = logging.getLogger(__name__)


def run_spark_etl(start_date, end_date):
    """
    TODO: this is where the pyspark logic from the notebook will be refactored.
    For now, this is a placeholder to demonstrate the pipeline structure.
    """
    logger.info(f"[ETL] Starting Spark ETL from {start_date} to {end_date}")
    logger.info("[ETL] Spark ETL completed successfully.")

    logger.info("[ETL] Data processing complete (Mocked).")


def run_s3_upload(local_path, bucket_name, s3_prefix):
    """
    Call S3Client to upload processed data to MinIO.
    """
    logger.info(
        f"[S3 Upload] Uploading data from {local_path} to bucket: {bucket_name}, prefix: {s3_prefix}"
    )

    if not os.path.exists(local_path):
        logger.error(f"[S3 Upload] Local path not found: {local_path}")
        return False

    try:
        client = S3Client()
        # Note: assume S3Client has hardcoded MinIO connection info.
        # Modify S3Client to accept dynamic params if needed.
        client.upload_folder(local_path, s3_prefix)
        logger.info("[S3 Upload] Upload completed successfully.")
        return True
    except Exception as e:
        logger.error(f"[S3 Upload] Upload failed: {e}")
        return False


def run_data_validation(bucket_name, s3_prefix):
    """
    Use Ray to read uploaded data from MinIO for integrity validation.
    """
    s3_uri = f"{bucket_name}/{s3_prefix}"
    logger.info(
        f"[Data Validation] Validating data in bucket: {bucket_name}, prefix: {s3_prefix}"
    )

    try:
        # Initialize Ray if not already running
        if not ray.is_initialized():
            ray.init()
            logger.info("[Data Validation] Ray initialized.")

        # Configure S3/MinIO connection for PyArrow
        s3_fs = pyarrow.fs.S3FileSystem(
            access_key="minioadmin",
            secret_key="minioadmin",
            endpoint_override="http://127.0.0.1:9000",
            scheme="http",
        )

        # Read dataset
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
    # --- 1. Argument Parsing ---
    parser = argparse.ArgumentParser(
        description="ScaleStyle Data Pipeline Orchestrator"
    )

    # Mode selection: Determines which steps to run
    parser.add_argument(
        "--stage",
        type=str,
        choices=["etl", "upload", "validate", "all"],
        default="all",
        help="Pipeline stage to run",
    )

    # Date parameters (for future incremental processing)
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

    # Path parameters
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

    # --- 2. Workflow control ---
    logger.info("=" * 50)
    logger.info(f"Starting Data Pipeline with stage: {args.stage.upper()}")
    logger.info("=" * 50)

    # Step 1: ETL (Currently Mocked)
    if args.stage in ["etl", "all"]:
        run_spark_etl(args.start_date, args.end_date)

    # Step 2: Upload to S3/MinIO
    if args.stage in ["upload", "all"]:
        upload_success = run_s3_upload(
            args.local_data_path, args.bucket_name, args.s3_prefix
        )
        if not upload_success:
            logger.error("Upload step failed. Exiting pipeline.")
            sys.exit(1)

    # Step 3: Data Validation
    if args.stage in ["validate", "all"]:
        run_data_validation(args.bucket_name, args.s3_prefix)

    logger.info("=" * 50)
    logger.info("Data Pipeline completed.")
    logger.info("=" * 50)


if __name__ == "__main__":
    main()
