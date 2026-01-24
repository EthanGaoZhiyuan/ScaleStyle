"""
Tests for the generation deployment.

This module contains tests for the recommendation explanation generation functionality.

Note: These tests are skipped by default because they require downloading and loading
the Qwen2-1.5B-Instruct model (~3GB). To run these tests, set RUN_SLOW_TESTS=1:
    RUN_SLOW_TESTS=1 pytest tests/test_generation.py
"""

import pytest
import asyncio
import os
from src.ray_serve.deployments.generation import GenerationDeployment

# Skip all tests in this module by default (require model download)
pytestmark = pytest.mark.skipif(
    os.getenv("RUN_SLOW_TESTS") != "1",
    reason="Requires Qwen2-1.5B-Instruct model (~3GB). Run with RUN_SLOW_TESTS=1 to enable.",
)


@pytest.fixture
def generation():
    """Create a generation deployment instance for testing."""
    # P1 Fix: Use func_or_class to get underlying class
    GenCls = GenerationDeployment.func_or_class
    return GenCls()


@pytest.mark.asyncio
async def test_generation_basic(generation):
    """Test basic generation functionality."""
    query = "red summer dress"
    item = {
        "title": "Floral Summer Dress",
        "dept": "Ladieswear",
        "color": "Red",
        "desc": "Light and breezy dress perfect for summer",
        "price": "49.99",
    }

    result = await generation.generate_reason(query, item)

    assert result["success"] is True
    assert "reason" in result
    assert len(result["reason"]) > 0
    assert result["reason"].endswith(".")
    assert "latency_ms" in result
    assert result["latency_ms"] >= 0


@pytest.mark.asyncio
async def test_generation_one_sentence(generation):
    """Test that generation returns exactly one sentence."""
    query = "blue jeans"
    item = {
        "title": "Classic Blue Jeans",
        "dept": "Menswear",
        "color": "Blue",
        "desc": "Comfortable denim jeans",
        "price": "59.99",
    }

    result = await generation.generate_reason(query, item)

    reason = result["reason"]

    # Should not contain newlines
    assert "\n" not in reason

    # Should be relatively short (single sentence)
    assert len(reason) < 500

    # Should end with period
    assert reason.endswith(".")


@pytest.mark.asyncio
async def test_generation_with_minimal_item(generation):
    """Test generation with minimal item metadata."""
    query = "shoes"
    item = {
        "title": "Sneakers",
    }

    result = await generation.generate_reason(query, item)

    assert result["success"] is True
    assert len(result["reason"]) > 0


@pytest.mark.asyncio
async def test_generation_with_empty_query(generation):
    """Test generation with empty query string."""
    query = ""
    item = {
        "title": "Product",
        "dept": "Accessories",
        "color": "Black",
    }

    result = await generation.generate_reason(query, item)

    assert result["success"] is True
    assert len(result["reason"]) > 0


@pytest.mark.asyncio
async def test_generation_includes_metadata(generation):
    """Test that generation includes item metadata in explanation."""
    query = "winter jacket"
    item = {
        "title": "Warm Winter Coat",
        "dept": "Outerwear",
        "color": "Navy",
        "desc": "Insulated winter coat with hood",
        "price": "129.99",
    }

    result = await generation.generate_reason(query, item)

    reason = result["reason"].lower()

    # Should reference query or item attributes
    assert any(
        word in reason for word in ["winter", "coat", "navy", "outerwear", "warm"]
    )


@pytest.mark.asyncio
async def test_generation_performance(generation):
    """Test that generation completes within reasonable time."""
    query = "running shoes"
    item = {
        "title": "Athletic Running Shoes",
        "dept": "Sport",
        "color": "Black",
    }

    result = await generation.generate_reason(query, item)

    # Should complete within 2 seconds (template mode is fast)
    assert result["latency_ms"] < 2000
    assert result["success"] is True


@pytest.mark.asyncio
async def test_generation_concurrent(generation):
    """Test concurrent generation requests."""
    queries = [
        ("red dress", {"title": "Red Dress", "dept": "Ladieswear"}),
        ("blue jeans", {"title": "Blue Jeans", "dept": "Menswear"}),
        ("sneakers", {"title": "Sneakers", "dept": "Sport"}),
    ]

    # Run concurrent generations
    tasks = [generation.generate_reason(query, item) for query, item in queries]
    results = await asyncio.gather(*tasks)

    # All should succeed
    assert all(r["success"] for r in results)
    assert all(len(r["reason"]) > 0 for r in results)


@pytest.mark.asyncio
async def test_generation_returns_metadata(generation):
    """Test that generation returns required metadata fields."""
    query = "test query"
    item = {"title": "Test Item"}

    result = await generation.generate_reason(query, item)

    # Check required fields
    assert "reason" in result
    assert "mode" in result
    assert "latency_ms" in result
    assert "success" in result

    # Check types
    assert isinstance(result["reason"], str)
    assert isinstance(result["mode"], str)
    assert isinstance(result["latency_ms"], (int, float))
    assert isinstance(result["success"], bool)


@pytest.mark.asyncio
async def test_generation_mode(generation):
    """Test that generation reports correct mode."""
    query = "test"
    item = {"title": "Item"}

    result = await generation.generate_reason(query, item)

    # Should be template mode (default for test environment)
    assert result["mode"] == "template"
