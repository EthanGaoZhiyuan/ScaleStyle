import os
import sys

from pyspark.sql import SparkSession
from pyspark.sql import functions as F

# Set Java options to allow security manager
os.environ["JDK_JAVA_OPTIONS"] = "-Djava.security.manager=allow"


def generate_top_items():
    print("Starting Top-100 popular items generation...")
    # Initialize Spark session
    spark = (
        SparkSession.builder.appName("Generate Top Items")
        .config("spark.driver.memory", "4g")
        .config("spark.driver.bindAddress", "127.0.0.1")
        .master("local[*]")
        .getOrCreate()
    )

    # Determine the base directory
    base_dir = os.path.dirname(os.path.abspath(__file__))

    input_path = os.path.join(base_dir, "../data/processed/train_data_parquet")
    output_path = os.path.join(base_dir, "../data/processed/top_items_parquet")

    if not os.path.exists(input_path):
        print(f"Input path {input_path} does not exist.", file=sys.stderr)
        sys.exit(1)

    # Read the input data
    df = spark.read.parquet(input_path)
    print("Data loaded. Row count:", df.count())

    print("Computing Top-100 popular items...")

    top_items = df.groupBy("article_id").count().orderBy(F.desc("count")).limit(100)

    # Keep only the article_id column and cast it to string
    top_items_final = top_items.select(F.col("article_id").cast("string"))

    # Save the result
    print(f"Saving Top-100 popular items to {output_path}...")
    top_items_final.coalesce(1).write.mode("overwrite").parquet(output_path)
    print("Top-100 popular items saved successfully.")

    # Show a sample of the saved data
    spark.read.parquet(output_path).show(10)


if __name__ == "__main__":
    generate_top_items()
