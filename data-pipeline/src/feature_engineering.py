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

from pyspark.ml.feature import StandardScaler, VectorAssembler
from pyspark.ml.functions import vector_to_array
from pyspark.sql import DataFrame, SparkSession
from pyspark.sql import functions as F
from pyspark.sql.functions import col, isnull, lit, when


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
        when(isnull(col("club_member_status")), lit("ACTIVE")).otherwise(col("club_member_status")),
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

    # 1. Handle missing product_type_no
    df = articles_df.na.drop(subset=["detail_desc"])

    # 2. Feature Crossing
    # Department = department_name + '_' + garment_group_name
    df = df.withColumn(
        "department", F.concat_ws("_", F.col("department_name"), F.col("garment_group_name"))
    )

    # index_crossed = index_name + '_' + index_group_name
    df = df.withColumn(
        "index_crossed", F.concat_ws("_", F.col("index_name"), F.col("index_group_name"))
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
            "_", F.col("prod_name"), F.col("product_type_name"), F.col("product_group_name")
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

    # 2. Handle Duplicates & Create 'article_purchase_count'
    group_cols = ["t_dat", "customer_id", "article_id", "price", "sales_channel_id"]
    df = df.groupBy(group_cols).count().withColumnRenamed("count", "article_purchase_count")

    # 3. Scaling Numerical Features
    features_to_scale = ["price", "article_purchase_count"]

    for feature_name in features_to_scale:
        # a. Vector Assembler: Convert scalar to vector
        vec_col_name = f"{feature_name}_vec"
        assembler = VectorAssembler(inputCols=[feature_name], outputCol=vec_col_name)
        df = assembler.transform(df)

        # b. StandardScaler: Fit and transform
        scaled_vec_col_name = f"{feature_name}_scaled_vec"
        scaler = StandardScaler(
            inputCol=vec_col_name, outputCol=scaled_vec_col_name, withMean=True, withStd=True
        )
        scaler_model = scaler.fit(df)
        df = scaler_model.transform(df)

        # c. Extract scaled scalar from vector
        final_col_name = f"{feature_name}_scaled"
        df = df.withColumn(final_col_name, vector_to_array(F.col(scaled_vec_col_name)).getItem(0))

        # Cleanup temp columns
        df = df.drop(vec_col_name, scaled_vec_col_name)

    return df.select(
        "t_dat",
        "customer_id",
        "article_id",
        "price_scaled",
        "article_purchase_count_scaled",
        "sales_channel_id",
    )


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
    print("This module is intended to be imported by the ETL Runner Notebook.")
