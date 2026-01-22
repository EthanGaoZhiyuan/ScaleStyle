import pytest
from src.ray_serve.deployments.router import RouterDeployment


@pytest.mark.asyncio
async def test_route_browse_intent():
    """
    Test that trending/hot queries are correctly routed to BROWSE intent
    Boundary condition: BROWSE intent recognition
    """
    r = RouterDeployment()
    out = await r.route("what's trending?")

    assert out["intent"] == "BROWSE"
    assert "filters" in out
    print(" BROWSE intent recognition successful")


@pytest.mark.asyncio
async def test_route_search_with_filters():
    """
    Test that search queries can extract filters
    Boundary condition: Filter extraction (color, price, etc.)
    """
    r = RouterDeployment()
    out = await r.route("red dress under 50")

    assert out["intent"] == "SEARCH"
    filters = out.get("filters", {})
    # Should extract some filter conditions
    assert len(filters) > 0
    print(f" Filter extraction successful: {filters}")


@pytest.mark.asyncio
async def test_route_special_chars():
    """
    Test that special characters don't cause crashes
    Boundary condition: emoji, quotes, special spaces
    """
    r = RouterDeployment()

    # Contains emoji, quotes, extra spaces
    out = await r.route('  "red" dress under $50  ')

    assert out["intent"] in ("SEARCH", "BROWSE")
    assert "filters" in out
    # Success if no exception is thrown
    print(" Special character handling successful")


@pytest.mark.asyncio
async def test_route_empty_query():
    """
    Test handling of empty queries
    Boundary condition: Empty string or only spaces
    """
    r = RouterDeployment()

    # Empty query should have default behavior
    out = await r.route("   ")

    assert "intent" in out
    assert "filters" in out
    print(" Empty query handling successful")
