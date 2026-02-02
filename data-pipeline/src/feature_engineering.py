# ==============================================================================
# FEATURE ENGINEERING MODULE (STATIC FEATURES)
# ------------------------------------------------------------------------------
# Assisted by: Google Gemini (AI) for PySpark syntax and production hardening.
# Core Data Science Logic Source:
#   - Kaggle Notebook: 'Part 1 - EDA, Data Cleaning & Feature Engineering' (by palash97)
#   - Kaggle Notebook: 'Recommend Items Purchased Together' (by cdeotte)
#
# Production Rationale:
#   - Re-implemented Pandas logic using PySpark for distributed scalability (31M+ rows).
#   - Used PySpark MLlib for robust numerical scaling (StandardScaler).
#   - Added explicit schema handling for data quality safety.
# ==============================================================================
# Production Logic Updated: Phase 2 (Time Decay & Negative Sampling)
# ==============================================================================

import random

from pyspark.sql import DataFrame, SparkSession
from pyspark.sql import functions as F
from pyspark.sql.functions import col, isnull, lit, when
from pyspark.sql.types import ArrayType, IntegerType


def get_spark_session(app_name: str = "ScaleStyle_Static_Feature_ETL") -> SparkSession:
    """
    Initialize a Spark session with memory configs tuned for large scale ETL.
    """
    return (
        SparkSession.builder.appName(app_name)
        # Configs can be tuned based on Colab/AWS instance size
        .config("spark.driver.memory", "4g")
        .config("spark.executor.memory", "4g")
        .getOrCreate()
    )


def preprocess_customers(customers_df: DataFrame) -> DataFrame:
    """
    Cleans and generates static features for Customers.

    Logic derived from EDA:
    1. 'FN' and 'Active' are sparsely populated (approx 65% null). Null implies False/0.
    2. 'age' has ~1% nulls. Imputed with 40 (median/robust estimate).
    3. 'club_member_status' nulls imputed as 'ACTIVE' (majority class).
    """

    # 1. Boolean/Flag imputation
    customers_fe = customers_df.withColumn(
        "FN", when(isnull(col("FN")), 0).otherwise(col("FN"))
    ).withColumn("Active", when(isnull(col("Active")), 0).otherwise(col("Active")))

    # 2. Age imputation & Binning
    # Note: pyspark.sql.functions.lit(col) Creates a Column of literal value.
    #
    # Ref: https://spark.apache.org/docs/latest/api/python/reference/pyspark.sql/api/pyspark.sql.functions.lit.html
    customers_fe = customers_fe.withColumn(
        "age", when(isnull(col("age")), lit(40)).otherwise(col("age"))
    ).withColumn(
        "age_bucket",
        when(col("age") < 25, lit(1))
        .when((col("age") >= 25) & (col("age") < 40), lit(2))
        .when((col("age") >= 40) & (col("age") < 60), lit(3))
        .otherwise(lit(4)),
    )

    # 3. Categorical Imputation
    customers_fe = customers_fe.withColumn(
        "club_member_status",
        when(isnull(col("club_member_status")), lit("ACTIVE")).otherwise(
            col("club_member_status")
        ),
    ).withColumn(
        "fashion_news_frequency",
        when(isnull(col("fashion_news_frequency")), lit("NONE"))
        .when(col("fashion_news_frequency") == "None", lit("NONE"))
        .otherwise(col("fashion_news_frequency")),
    )

    # Select final feature set
    return customers_fe.select(
        "customer_id",
        "FN",
        "Active",
        "age",
        "age_bucket",
        "club_member_status",
        "fashion_news_frequency",
    )


def preprocess_articles(articles_df: DataFrame) -> DataFrame:
    """
    Clean and generate static features for Articles.

    Logic derived from EDA:
    1. 'product_type_no' contains -1 for missing values.
    2. 'department_name' and 'garment_group_name' are highly correlated,
        creating a crossed feature 'department_group'.
    """

    # .na() Returns a DataFrameNaFunctions for handling missing values.
    # Ref: https://spark.apache.org/docs/latest/api/python/reference/pyspark.sql/api/pyspark.sql.DataFrame.na.html
    # .drop() Returns a new DataFrame omitting rows with null or NaN values. DataFrame.dropna() and DataFrameNaFunctions.drop() are aliases of each other.
    # Ref: https://spark.apache.org/docs/latest/api/python/reference/pyspark.sql/api/pyspark.sql.DataFrameNaFunctions.drop.html#pyspark.sql.DataFrameNaFunctions.drop
    #
    # @dispatch_df_method
    # def drop(
    #     self,
    #     how: str = "any",
    #     thresh: Optional[int] = None,
    #     subset: Optional[Union[str, Tuple[str, ...], List[str]]] = None,
    # ) -> DataFrame:
    #
    # 1. Handle missing product_type_no
    df = articles_df.na.drop(subset=["detail_desc"])

    # 2. Feature Crossing
    # Department = department_name + '_' + garment_group_name
    df = df.withColumn(
        "department",
        F.concat_ws("_", F.col("department_name"), F.col("garment_group_name")),
    )

    # index_crossed = index_name + '_' + index_group_name
    df = df.withColumn(
        "index_crossed",
        F.concat_ws("_", F.col("index_name"), F.col("index_group_name")),
    )

    # color = color_group_name + '_' + perceived_color_value_name + '_' + perceived_color_master_name
    df = df.withColumn(
        "color",
        F.concat_ws(
            "_",
            F.col("colour_group_name"),
            F.col("perceived_colour_value_name"),
            F.col("perceived_colour_master_name"),
        ),
    )

    # Product = prod_name + '_' + product_type_name + '_' + product_group_name
    df = df.withColumn(
        "product",
        F.concat_ws(
            "_",
            F.col("prod_name"),
            F.col("product_type_name"),
            F.col("product_group_name"),
        ),
    )

    # 3. Drop uninteresting columns
    drop_cols = [
        "product_code",
        "product_type_no",
        "graphical_appearance_no",
        "colour_group_code",
        "perceived_colour_master_id",
        "department_no",
        "index_code",
        "index_group_no",
        "section_no",
        "garment_group_no",
    ]
    df = df.drop(*drop_cols)

    return df


def preprocess_transactions(transactions_df: DataFrame) -> DataFrame:
    """
    Cleans and generates static features for Transactions.

    Logic derived from EDA:
    1. Convert 't_dat' to datatype
    2. Aggregate to duplicate rows to create 'article_purchase_count', which reduces the dataset size and captures quantity.
    3. Scale 'price' and 'article_purchase_count' using StandardScaler. Matches sklearn defaults: withMean=True, withStd=True.
    """

    # 1. Data Conversion
    df = transactions_df.withColumn("t_dat", F.to_date(F.col("t_dat")))

    # 2. Time Decay Weighting
    max_date = df.agg(F.max("t_dat")).collect()[0][0]

    # F.datediff(end, start) Returns the number of days between two dates.
    # Ref: https://spark.apache.org/docs/latest/api/python/reference/pyspark.sql/api/pyspark.sql.functions.datediff.html
    # 3. Days Difference Calculation
    df = df.withColumn("days_diff", F.datediff(F.lit(max_date), F.col("t_dat")))

    # 4. Decay Weight Calculation
    decay_rate = 0.005
    df = df.withColumn("decay_weight", 1.0 / (1.0 + decay_rate * F.col("days_diff")))

    # 5. Aggregation
    # Group by customer_id and article_id to aggregate features
    df_grouped = df.groupBy("customer_id", "article_id").agg(
        F.sum("decay_weight").alias("total_decay_weight"),
        F.count("article_id").alias("article_purchase_count"),
        F.max("t_dat").alias("last_purchase_date"),
        F.mean("price").alias("price"),
        F.last("sales_channel_id").alias("sales_channel_id"),
    )

    return df_grouped


def generate_negative_samples(df_interactions: DataFrame, ratio: int = 4) -> DataFrame:
    """
    Phase 2 Upgrade: Popularity-based negative sampling

    Generates negative samples for implicit feedback data using popularity-based sampling.

    Why:
    Recommendation models need to know what users do not like.
    Since we only have purcahse data (implicit feedback), we need to generate negative samples.
    We assume that popular items not purchased by a user are likely negative samples.
    """
    # 1. Positive Interactions
    positives = df_interactions.withColumn("label", F.lit(1))

    # 2. Popular Items Calculation
    top_items = (
        df_interactions.groupBy("article_id")
        .count()
        .orderBy(F.desc("count"))
        .limit(1000)
    )

    popular_items = [row["article_id"] for row in top_items.collect()]

    # 3. Negative Sampling Logic (Pure Python)
    # This function defines the logic for sampling negative items.
    # It runs on the worker nodes (executors) and operates on standard Python lists.
    def get_negative_samples(purchased_items):
        if not purchased_items:
            return []

        negatives = set()
        num_negatives_needed = ratio * len(purchased_items)

        attemps = 0
        max_attempts = num_negatives_needed * 3

        while len(negatives) < num_negatives_needed and attemps < max_attempts:
            candidate = random.choice(popular_items)
            if candidate not in purchased_items:
                negatives.add(candidate)
            attemps += 1

        return list(negatives)

    # .udf() Creates a user-defined function (UDF) that can be used in DataFrame operations.
    # Ref: https://spark.apache.org/docs/latest/api/python/reference/pyspark.sql/api/pyspark.sql.functions.udf.html
    # IntegerType() The data type representing integer values.
    # Ref: https://spark.apache.org/docs/latest/api/python/reference/pyspark.sql/api/pyspark.sql.types.IntegerType.html
    # ArrayType() The data type representing arrays of a specified element type.
    # Ref: https://spark.apache.org/docs/latest/api/python/reference/pyspark.sql/api/pyspark.sql.types.ArrayType.html
    # Why F.udf()?
    # Register as Spark UDF
    # We wrap the Python function with F.udf to make it usable in Spark distributed operations.
    # - Serialization: Spark pickles the function to send it to worker nodes.
    # - Schema Enforcement: ArrayType(IntegerType()) tells Spark the return type is List[int].
    #   Without this, Spark cannot infer the schema for the new column.
    #
    # Ref: https://spark.apache.org/docs/latest/api/python/reference/pyspark.sql/api/pyspark.sql.functions.udf.html
    get_negatives_udf = F.udf(get_negative_samples, ArrayType(IntegerType()))

    # 4. Generate Negative Samples
    user_history = df_interactions.groupBy("customer_id").agg(
        F.collect_set("article_id").alias("purchased_items")
    )

    # 5. Create Negative Samples DataFrame
    user_with_negatives = user_history.withColumn(
        "negative_samples", get_negatives_udf(F.col("purchased_items"))
    )

    # 6. Explode Negative Samples
    negatives = (
        user_with_negatives.select(
            F.col("customer_id"),
            F.explode(F.col("negative_samples")).alias("article_id"),
        )
        .withColumn("label", F.lit(0))
        .withColumn("total_decay_weight", F.lit(0.0))
        .withColumn("article_purchase_count", F.lit(0))
        .withColumn("price", F.lit(0.0))
        .withColumn("sales_channel_id", F.lit(2))
    )

    # 7. Combine Positives and Negatives
    common_cols = [
        "customer_id",
        "article_id",
        "label",
        "total_decay_weight",
        "price",
        "sales_channel_id",
    ]

    # Merge positive (purchased) and negative (non-purchased) samples into one training set.
    # unionByName ensures columns are aligned correctly by name, creating the final dataset for binary classification.
    final_df = positives.select(*common_cols).unionByName(
        negatives.select(*common_cols)
    )

    return final_df


def merge_datasets(
    transactions_df: DataFrame, customers_df: DataFrame, articles_df: DataFrame
) -> DataFrame:
    """
    Merges Transactions (Fact Table) with Customers and Articles (Dimension Tables).

    Strategy:
    - Left join Transactions with Customers on 'customer_id'.
    - Left join the result with Articles on 'article_id'.
    - Results is a wide table containing interaction history + user features + item features.
    """

    # 1. Join Transactions with Customers
    df_merged = transactions_df.join(customers_df, on="customer_id", how="left")

    # 2. Join with Articles
    df_merged = df_merged.join(articles_df, on="article_id", how="left")

    return df_merged


if __name__ == "__main__":
    print("Feature Engineering Module Loaded Successfully.")
    print(
        "Phase 2 Upgrades: Time Decay Weighting and Popularity-based Negative Sampling included."
    )
