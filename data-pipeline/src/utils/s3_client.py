import os

import boto3


class S3Client:
    def __init__(self, aws_access_key_id=None, aws_secret_access_key=None, region_name=None):
        self.s3 = boto3.client(
            "s3",
            endpoint_url="http://127.0.0.1:9000",
            aws_access_key_id="minioadmin",
            aws_secret_access_key="minioadmin",
        )
        self.bucket_name = "recsys-data"

    def upload_folder(self, local_folder, s3_folder_prefix):
        """
        Recursively upload all files in a folder.
        :param local_folder: Path to the local folder
        :param s3_folder_prefix: Target folder name in MinIO
        """

        if not os.path.exists(local_folder):
            print(f"Error: Local path not found: {local_folder}")
            return

        print(f"Starting upload: {local_folder} -> bucket: {self.bucket_name}")

        # Traverse the directory
        for root, _dirs, files in os.walk(local_folder):
            for filename in files:
                # Ignore hidden files
                if filename.startswith("."):
                    continue

                local_path = os.path.join(root, filename)

                # Calculate relative path to maintain folder structure
                relative_path = os.path.relpath(local_path, local_folder)
                s3_path = os.path.join(s3_folder_prefix, relative_path)

                try:
                    self.s3.upload_file(local_path, self.bucket_name, s3_path)
                    print(f"Uploaded Successfully: {s3_path}")
                except Exception as e:
                    print(f"Upload Failed: {filename}: {e}")


if __name__ == "__main__":
    s3_client = S3Client()
    s3_client.upload_folder("./data/output/", "output/")

    local_data_path = (
        "/Users/Ethan/Develop/ScaleStyle/data-pipeline/data/processed/train_data_parquet"
    )
    # Execute upload
    s3_client.upload_folder(local_data_path, "processed/train_data_parquet")
