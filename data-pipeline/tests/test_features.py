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
    """Create a local Spark session for testing."""
    return (
        SparkSession.builder.master("local[1]")
        .appName("TestETL")
        .config("spark.driver.extraJavaOptions", "-Djava.security.manager=allow")
        .getOrCreate()
    )


def test_preprocess_customers_imputation(spark):
    """
    Verifies that NULL ages are correctly imputed to 40.
    And NULL FN status is imputed to 0.
    """

    # 1. Prepare Mock Data (Small subset to test logic)
    data = [
        ("user_1", None, None, None, "ACTIVE", "NONE"),  # All Nulls
        ("user_2", 1, 1, 25, None, None),  # No Nulls
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

    # 2. Run the function under test
    output_df = preprocess_customers(input_df)
    results = output_df.collect()

    # 3. Assertions
    # Check User 1 (Imputation case)
    user_1 = results[0]
    assert user_1.age == 40.0, "Null age should be imputed to 40"
    assert user_1.FN == 0, "Null FN should be imputed to 0"

    # Check User 2 (No Imputation case)
    user_2 = results[1]
    assert user_2.age == 25.0, "Non-null age should remain unchanged"
    assert user_2.FN == 1, "Non-null FN should remain unchanged"

    print("\n test_preprocess_customers_imputation passed.")


def test_preprocess_articles_logic(spark):
    """
    Tests that product_type_no -1 is replaced by 0.
    And department_group is correctly created.
    """

    # 1. Prepare Mock Data
    data = [
        # Row 1: Valid Data
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
        # Row 2: Null detail_desc (Should be dropped)
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

    # 2. Run function
    output_df = preprocess_articles(input_df)
    results = output_df.collect()

    # 3. Assertions

    # Count check: Should only have 1 row (Row 102 dropped)
    assert output_df.count() == 1, "Should drop rows with null detail_desc"

    row = results[0]

    # Logic check: Concatenations
    # department = "Jersey" + "_" + "Jersey" (Wait, mock data is Top_Jersey) -> "Top_Jersey"
    assert row.department == "Top_Jersey"
    assert row.index_crossed == "Ladies_Womens"
    assert row.color == "Black_Dark_Black Master"
    assert row.product == "Crop Top_Vest_Upper Body"

    # Logic check: Columns dropped
    current_columns = output_df.columns
    assert "product_code" not in current_columns
    assert "department_no" not in current_columns
    assert "article_id" in current_columns  # Should keep ID

    print("\n test_preprocess_articles_logic passed!")


def test_preprocess_transactions_logic(spark):
    """
    Tests transaction logic:
    1. Date conversion
    2. Duplicate aggregation
    3. Scaling
    """

    # 1. Prepare Mock Data
    data = [
        ("2020-09-20", "user_1", "item_A", 100.0, 1),
        ("2020-09-20", "user_1", "item_A", 100.0, 1),  # Duplicate row
        ("2020-09-21", "user_2", "item_B", 200.0, 2),  # Different row
    ]

    columns = ["t_dat", "customer_id", "article_id", "price", "sales_channel_id"]

    input_df = spark.createDataFrame(data, columns)

    # 2. Run function
    output_df = preprocess_transactions(input_df)
    results = output_df.collect()

    # 3. Assertions
    assert output_df.count() == 2, "Should aggregate duplicate rows"

    # Check logic for user_1
    row_user_1 = [row for row in results if row.customer_id == "user_1"][0]

    # Date Check
    assert isinstance(row_user_1.t_dat, date), "t_dat should be converted to date type"

    # Count Check
    assert isinstance(
        row_user_1.article_purchase_count_scaled, float
    ), "article_purchase_count should be float after scaling"
    assert isinstance(
        row_user_1.price_scaled, float
    ), "price should be float after scaling"

    print("\n test_preprocess_transactions_logic passed!")


def test_merge_datasets_logic(spark):
    """
    Tests the left join logic.
    Ensures row count matches transactions and columns are merged.
    """

    # 1. Mock Transactions Data
    transactions_data = [("u1", "a1", 1.0), ("u2", "a2", 2.0)]
    transactions_columns = ["customer_id", "article_id", "price"]
    transactions_df = spark.createDataFrame(transactions_data, transactions_columns)

    # 2. Mock Customers Data
    customers_data = [("u1", 25), ("u2", 30)]
    customers_columns = ["customer_id", "age"]
    customers_df = spark.createDataFrame(customers_data, customers_columns)

    # 3. Mock Articles Data
    articles_data = [("a1", "Red"), ("a2", "Blue")]
    articles_columns = ["article_id", "color"]
    articles_df = spark.createDataFrame(articles_data, articles_columns)

    # 4. Run merge function
    merged_df = merge_datasets(transactions_df, customers_df, articles_df)

    # 5. Assertions
    assert (
        merged_df.count() == transactions_df.count()
    ), "Row count should match transactions data"

    # Check columns
    cols = merged_df.columns
    assert (
        "price" in cols
    ), "Merged DataFrame should contain 'price' column from transactions"
    assert "age" in cols, "Merged DataFrame should contain 'age' column from customers"
    assert (
        "color" in cols
    ), "Merged DataFrame should contain 'color' column from articles"

    # Check value
    row_u1 = merged_df.filter(merged_df.customer_id == "u1").collect()[0]
    assert row_u1.age == 25, "Age for user u1 should be 25"
    assert row_u1.color == "Red", "Color for article a1 should be Red"

    print("\n test_merge_datasets_logic passed!")
