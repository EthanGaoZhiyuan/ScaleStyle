def test_contract_normalize_stable_fields():
    """
    Test that contract normalization produces stable output even with missing fields
    Boundary condition: Input data missing multiple fields
    """
    from src.ray_serve.deployments.ingress import _contract_normalize

    # Input data missing many fields
    raw = [
        {"article_id": "1", "meta": {"price": "0.1", "colour_group_name": "Red"}},
        {"article_id": "2", "meta": {"detail_desc": "hello"}},
    ]

    out, dbg = _contract_normalize(raw, limit=2)

    # Verify output count
    assert len(out) == 2

    # Verify each result has a stable field set
    for item in out:
        assert "article_id" in item
        assert "meta" in item
        meta = item["meta"]

        # Core fields should always exist (even if original data doesn't have them, should have defaults)
        assert "image_url" in meta
        assert "price" in meta

    # Verify debug info records missing fields
    assert "missing_total" in dbg or "missing_fields_count" in dbg

    # Since input data is missing fields, missing count should be > 0
    missing_count = dbg.get("missing_total", dbg.get("missing_fields_count", 0))
    assert missing_count > 0

    print(f" Contract normalization successful, missing fields: {missing_count}")
    print(f" Missing field details: {dbg.get('missing_by_field', {})}")


def test_contract_normalize_empty_input():
    """
    Test handling of empty input
    Boundary condition: Empty list input
    """
    from src.ray_serve.deployments.ingress import _contract_normalize

    raw = []
    out, dbg = _contract_normalize(raw, limit=10)

    assert len(out) == 0
    assert "missing_total" in dbg or "missing_fields_count" in dbg
    print(" Empty input handling successful")
