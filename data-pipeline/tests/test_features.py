import pytest
from pyspark.sql import SparkSession
from src.feature_engineering import (
    preprocess_customers,
    preprocess_articles,
    preprocess_transactions,
    merge_datasets,
)
from datetime import date


@pytest.fixture(scope="session")
def spark():
    """Local Spark session for unit tests."""
    return (
        SparkSession.builder.master("local[1]")
        .appName("TestETL")
        .config("spark.driver.extraJavaOptions", "-Djava.security.manager=allow")
        .getOrCreate()
    )


def test_preprocess_customers_imputation(spark):
    """Test NULL age imputed to 40 and NULL FN imputed to 0."""
    data = [
        ("user_1", None, None, None, "ACTIVE", "NONE"),
        ("user_2", 1, 1, 25, None, None),
    ]
    columns = [
        "customer_id",
        "FN",
        "Active",
        "age",
        "club_member_status",
        "fashion_news_frequency",
    ]

    input_df = spark.createDataFrame(data, columns)
    output_df = preprocess_customers(input_df)
    results = output_df.collect()

    user_1 = results[0]
    assert user_1.age == 40.0, "Null age should be imputed to 40"
    assert user_1.FN == 0, "Null FN should be imputed to 0"

    user_2 = results[1]
    assert user_2.age == 25.0, "Non-null age should remain unchanged"
    assert user_2.FN == 1, "Non-null FN should remain unchanged"


def test_preprocess_articles_logic(spark):
    """Test article preprocessing: null detail_desc dropped, concatenations work correctly."""
    data = [
        (
            "101",
            "Top",
            "Jersey",
            "Ladies",
            "Womens",
            "Black",
            "Dark",
            "Black Master",
            "Crop Top",
            "Vest",
            "Upper Body",
            "Nice top",
            1,
            1,
            1,
            1,
            1,
            1,
            1,
            "A",
            1,
            1,
            1,
        ),
        (
            "102",
            "Top",
            "Jersey",
            "Ladies",
            "Womens",
            "White",
            "Light",
            "White Master",
            "Tee",
            "Vest",
            "Upper Body",
            None,
            1,
            1,
            1,
            1,
            1,
            1,
            1,
            "A",
            1,
            1,
            1,
        ),
    ]
    columns = [
        "article_id",
        "department_name",
        "garment_group_name",
        "index_name",
        "index_group_name",
        "colour_group_name",
        "perceived_colour_value_name",
        "perceived_colour_master_name",
        "prod_name",
        "product_type_name",
        "product_group_name",
        "detail_desc",
        "product_code",
        "product_type_no",
        "graphical_appearance_no",
        "colour_group_code",
        "perceived_colour_value_id",
        "perceived_colour_master_id",
        "department_no",
        "index_code",
        "index_group_no",
        "section_no",
        "garment_group_no",
    ]

    input_df = spark.createDataFrame(data, columns)
    output_df = preprocess_articles(input_df)
    results = output_df.collect()

    assert output_df.count() == 1, "Should drop rows with null detail_desc"

    row = results[0]
    assert row.department == "Top_Jersey"
    assert row.index_crossed == "Ladies_Womens"
    assert row.color == "Black_Dark_Black Master"
    assert row.product == "Crop Top_Vest_Upper Body"

    current_columns = output_df.columns
    assert "product_code" not in current_columns
    assert "department_no" not in current_columns
    assert "article_id" in current_columns


def test_preprocess_transactions_logic(spark):
    """Test transaction date conversion, duplicate aggregation, and scaling."""
    data = [
        ("2020-09-20", "user_1", "item_A", 100.0, 1),
        ("2020-09-20", "user_1", "item_A", 100.0, 1),
        ("2020-09-21", "user_2", "item_B", 200.0, 2),
    ]
    columns = ["t_dat", "customer_id", "article_id", "price", "sales_channel_id"]

    input_df = spark.createDataFrame(data, columns)
    output_df = preprocess_transactions(input_df)
    results = output_df.collect()

    assert output_df.count() == 2, "Should aggregate duplicate rows"

    row_user_1 = [row for row in results if row.customer_id == "user_1"][0]
    assert isinstance(row_user_1.t_dat, date), "t_dat should be converted to date type"
    assert isinstance(
        row_user_1.article_purchase_count_scaled, float
    ), "article_purchase_count should be float after scaling"
    assert isinstance(
        row_user_1.price_scaled, float
    ), "price should be float after scaling"


def test_merge_datasets_logic(spark):
    """Test left join preserves transaction row count and merges customer/article columns."""
    transactions_data = [("u1", "a1", 1.0), ("u2", "a2", 2.0)]
    transactions_columns = ["customer_id", "article_id", "price"]
    transactions_df = spark.createDataFrame(transactions_data, transactions_columns)

    customers_data = [("u1", 25), ("u2", 30)]
    customers_columns = ["customer_id", "age"]
    customers_df = spark.createDataFrame(customers_data, customers_columns)

    articles_data = [("a1", "Red"), ("a2", "Blue")]
    articles_columns = ["article_id", "color"]
    articles_df = spark.createDataFrame(articles_data, articles_columns)

    merged_df = merge_datasets(transactions_df, customers_df, articles_df)

    assert (
        merged_df.count() == transactions_df.count()
    ), "Row count should match transactions data"

    cols = merged_df.columns
    assert "price" in cols
    assert "age" in cols
    assert "color" in cols

    row_u1 = merged_df.filter(merged_df.customer_id == "u1").collect()[0]
    assert row_u1.age == 25
    assert row_u1.color == "Red"
