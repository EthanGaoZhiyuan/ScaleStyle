def test_contract_normalize_stable_fields():
    """
    Test that contract normalization produces stable output even with missing fields
    Boundary condition: Input data missing multiple fields
    """
    from src.utils.contract import _contract_normalize

    # Input data missing many fields
    raw = [
        {"article_id": "1", "meta": {"price": "0.1", "color": "Red"}},
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


def test_contract_normalize_preserves_canonical_article_id():
    """
    contract_normalize must output article_id as a canonical 10-digit zero-padded
    string.  No downstream zfill compensation should be needed.
    """
    from src.utils.contract import _contract_normalize

    # Canonical 10-digit string passes through unchanged
    raw = [{"article_id": "0108775015", "score": 0.99, "meta": {}}]
    results, _ = _contract_normalize(raw, limit=1)
    assert results[0]["article_id"] == "0108775015"
    assert isinstance(results[0]["article_id"], str)

    # 9-digit string (missing leading zero) gets padded
    raw2 = [{"article_id": "108775015", "score": 0.99, "meta": {}}]
    results2, _ = _contract_normalize(raw2, limit=1)
    assert results2[0]["article_id"] == "0108775015"

    # Integer input gets converted to canonical padded string
    raw3 = [{"article_id": 108775015, "score": 0.99, "meta": {}}]
    results3, _ = _contract_normalize(raw3, limit=1)
    assert results3[0]["article_id"] == "0108775015"
    assert isinstance(results3[0]["article_id"], str)

    # Short numeric id gets padded to 10 digits
    raw4 = [{"article_id": "123", "score": 0.5, "meta": {}}]
    results4, _ = _contract_normalize(raw4, limit=1)
    assert results4[0]["article_id"] == "0000000123"
    assert len(results4[0]["article_id"]) == 10


def test_contract_normalize_article_id_edge_cases():
    """article_id edge cases: empty, None, and non-numeric must not crash."""
    from src.utils.contract import _contract_normalize

    # Missing article_id key — falls back to empty string, stays empty
    raw_no_id = [{"score": 0.5, "meta": {}}]
    results, _ = _contract_normalize(raw_no_id, limit=1)
    assert results[0]["article_id"] == ""

    # Explicit None — same fallback
    raw_none = [{"article_id": None, "score": 0.5, "meta": {}}]
    results2, _ = _contract_normalize(raw_none, limit=1)
    assert results2[0]["article_id"] == ""

    # Non-numeric string — must not raise, returns zfill-padded string
    raw_str = [{"article_id": "ABC-1", "score": 0.5, "meta": {}}]
    results3, _ = _contract_normalize(raw_str, limit=1)
    assert results3[0]["article_id"] == "00000ABC-1"
    assert isinstance(results3[0]["article_id"], str)


def test_contract_normalize_empty_input():
    """
    Test handling of empty input
    Boundary condition: Empty list input
    """
    from src.utils.contract import _contract_normalize

    raw = []
    out, dbg = _contract_normalize(raw, limit=10)

    assert len(out) == 0
    assert "missing_total" in dbg or "missing_fields_count" in dbg
    print(" Empty input handling successful")
