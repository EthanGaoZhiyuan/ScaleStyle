import json
import os
import random

import pandas as pd


def export_metadata():
    # Determine the base directory
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # Define input and output paths
    input_path = os.path.join(base_dir, "data/raw/articles.csv")
    output_path = os.path.join(base_dir, "data/processed/product_metadata.json")

    # Check if input file exists
    if not os.path.exists(input_path):
        print(f"Input path {input_path} does not exist.")
        return

    # Read the CSV file
    use_cols = ["article_id", "prod_name", "product_type_name", "detail_desc"]

    # Load data with specified columns and dtypes
    df = pd.read_csv(input_path, usecols=use_cols, dtype={"article_id": str})

    # Handle missing values
    df["detail_desc"] = df["detail_desc"].fillna("No description available.")
    df["prod_name"] = df["prod_name"].fillna("Unknown Product")

    print(f"Loaded {len(df)} records from articles.csv")

    # Create metadata mapping
    metadata_map = {}

    def get_mock_price():
        return round(random.uniform(19.99, 99.99), 2)

    # Populate metadata map
    for _, row in df.iterrows():
        # Format article_id to be zero-padded to 10 digits
        article_id = str(row["article_id"]).zfill(10)

        # Construct image URL
        # Rule: Use first three digits of article_id for folder structure
        folder = article_id[:3]
        img_url = (
            f"https://lp2.hm.com/hmgoepprod?set=source[/{folder}/{article_id}.jpg],"
            "origin[dam],category[],type[LOOKBOOK],res[m],hmver[1]&call=url[file:/product/main]"
        )

        metadata_map[article_id] = {
            "itemId": article_id,
            "name": row["prod_name"],
            "category": row["product_type_name"],
            "description": str(row["detail_desc"])[:200],
            "price": get_mock_price(),
            "imgUrl": img_url,
        }

    # Save metadata to JSON file
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(metadata_map, f, ensure_ascii=False)

    print(f"Exported metadata for {len(metadata_map)} items to {output_path}")


if __name__ == "__main__":
    export_metadata()
