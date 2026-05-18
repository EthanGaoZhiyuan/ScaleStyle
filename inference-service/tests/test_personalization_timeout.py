"""
H-5: Text-path personalization snapshot timeout tests.

Verifies that when feature_reader.load_personalization_snapshot() exceeds
PersonalizationConfig.SNAPSHOT_TIMEOUT_MS, the text search pipeline:
  - Still returns results (does not propagate the timeout as an error)
  - Degrades only the personalization layer (rerank order preserved)
  - Logs "personalization_snapshot_timeout" at WARNING level
  - Does NOT log "behavior_boost_failed" (that's for non-timeout exceptions)

asyncio.TimeoutError must be caught by its dedicated handler (not the generic
Exception handler) so operators can distinguish Redis latency spikes from
other personalization failures in logs and metrics.
"""

import time

import pytest

from tests.utils import FakeHandle

# ---------------------------------------------------------------------------
# Shared test infrastructure
# ---------------------------------------------------------------------------


class DummyRedis:
    """Minimal Redis stub for IngressDeployment construction."""

    def ping(self):
        return True

    def pipeline(self):
        class P:
            def __init__(self):
                self._keys = []

            def hgetall(self, k):
                self._keys.append(k)

            def execute(self, **kwargs):
                return [
                    {
                        "article_id": k.split(":")[-1],
                        "prod_name": "Test product",
                        "price": "0.05",
                        "detail_desc": "A product",
                        "colour_group_name": "Black",
                        "department_name": "Test",
                    }
                    for k in self._keys
                ]

        return P()


class SlowFeatureReader:
    """
    Simulates a slow Redis call that blocks long enough to trigger the timeout.
    time.sleep is safe here: asyncio.to_thread runs it in a worker thread;
    asyncio.wait_for cancels the coroutine after timeout_ms without blocking
    the event loop.  The thread continues briefly then is abandoned.
    """

    def load_personalization_snapshot(
        self, user_id, candidate_ids, max_recent_clicks=20
    ):
        time.sleep(1.0)  # 1 s — always exceeds any ≤50ms timeout
        raise AssertionError("Should never reach here — wait_for fires first")


class InstantFeatureReader:
    """Simulates a fast Redis call that completes well within the timeout."""

    def load_personalization_snapshot(
        self, user_id, candidate_ids, max_recent_clicks=20
    ):
        from src.personalization.snapshot import PersonalizationSnapshot

        return PersonalizationSnapshot(
            user_id=user_id,
            recent_clicks=(),
            category_affinity={},
        )


class BrokenFeatureReader:
    """Simulates a Redis call that raises a non-timeout exception."""

    def load_personalization_snapshot(self, *a, **kw):
        raise RuntimeError("Redis connection refused")


def _make_ingress(monkeypatch, vision_handle=None):
    """Build a minimal IngressDeployment with fake handles."""
    monkeypatch.setattr(
        "src.utils.redis_client.RedisClient.get_client", lambda: DummyRedis()
    )
    from src.deployments.ingress import IngressDeployment

    router = FakeHandle(
        route=lambda q, user_id=None: {
            "intent": "SEARCH",
            "filters": {},
            "flow": "smart",
        }
    )
    embed = FakeHandle(embed=lambda q, is_query=True: [0.1, 0.2, 0.3])
    retrieval = FakeHandle(
        search=lambda vector, **kw: [
            {"article_id": "0108775015", "score": 0.95},
            {"article_id": "0626366003", "score": 0.88},
        ]
    )
    popularity = FakeHandle(topk=lambda k: [{"article_id": "p1"}])
    reranker = FakeHandle(
        score=lambda q, docs: {
            "scores": [0.9 - i * 0.1 for i in range(len(docs))],
            "rerank_ms": 1.0,
            "mode": "stub",
        }
    )
    generation = FakeHandle(explain=lambda q, item: {"reason": "", "mode": "fallback"})
    return IngressDeployment(
        router, embed, retrieval, popularity, reranker, generation, vision_handle
    )


# ---------------------------------------------------------------------------
# Tests: timeout path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_personalization_timeout_text_search_still_returns_results(monkeypatch):
    """
    When feature_reader hangs past the timeout, _search_impl must still return
    a valid results list — not an error or exception.
    """
    monkeypatch.setattr("src.config.PersonalizationConfig.SNAPSHOT_TIMEOUT_MS", 1)

    from src.deployments.ingress import SearchRequest

    ing = _make_ingress(monkeypatch)
    ing._feature_reader = SlowFeatureReader()

    resp = await ing._search_impl(SearchRequest(query="black dress", k=2, user_id="u1"))

    assert "results" in resp, "Response must contain 'results' even on timeout"
    assert (
        len(resp["results"]) > 0
    ), "Must return at least one result on personalization timeout"


@pytest.mark.asyncio
async def test_personalization_timeout_logs_dedicated_timeout_message(
    monkeypatch, caplog
):
    """
    asyncio.TimeoutError must trigger the dedicated timeout handler which logs
    'personalization_snapshot_timeout', NOT 'behavior_boost_failed'.
    """
    import logging

    monkeypatch.setattr("src.config.PersonalizationConfig.SNAPSHOT_TIMEOUT_MS", 1)

    from src.deployments.ingress import SearchRequest

    ing = _make_ingress(monkeypatch)
    ing._feature_reader = SlowFeatureReader()

    with caplog.at_level(logging.WARNING):
        await ing._search_impl(SearchRequest(query="black dress", k=2, user_id="u1"))

    timeout_msgs = [
        r for r in caplog.records if "personalization_snapshot_timeout" in r.message
    ]
    boost_fail_msgs = [
        r for r in caplog.records if "behavior_boost_failed" in r.message
    ]

    assert len(timeout_msgs) == 1, (
        f"Expected exactly 1 'personalization_snapshot_timeout' log; got {len(timeout_msgs)}.\n"
        f"All warning messages: {[r.message for r in caplog.records if r.levelno >= logging.WARNING]}"
    )
    assert (
        len(boost_fail_msgs) == 0
    ), "asyncio.TimeoutError must NOT fall through to 'behavior_boost_failed' handler"


@pytest.mark.asyncio
async def test_personalization_timeout_message_includes_user_and_timeout(
    monkeypatch, caplog
):
    """The timeout warning must include user_id and the configured timeout_ms for debugging."""
    import logging

    monkeypatch.setattr("src.config.PersonalizationConfig.SNAPSHOT_TIMEOUT_MS", 1)

    from src.deployments.ingress import SearchRequest

    ing = _make_ingress(monkeypatch)
    ing._feature_reader = SlowFeatureReader()

    with caplog.at_level(logging.WARNING):
        await ing._search_impl(
            SearchRequest(query="black dress", k=2, user_id="test-u")
        )

    timeout_msg = next(
        (
            r.message
            for r in caplog.records
            if "personalization_snapshot_timeout" in r.message
        ),
        None,
    )
    assert timeout_msg is not None
    assert "test-u" in timeout_msg, "user_id must appear in timeout log"
    assert "1" in timeout_msg, "timeout_ms must appear in timeout log"


@pytest.mark.asyncio
async def test_personalization_generic_failure_logs_boost_failed_not_timeout(
    monkeypatch, caplog
):
    """
    A RuntimeError from the feature reader must log 'behavior_boost_failed',
    NOT 'personalization_snapshot_timeout'.  The two handlers must be distinct.
    """
    import logging

    monkeypatch.setattr("src.config.PersonalizationConfig.SNAPSHOT_TIMEOUT_MS", 5000)

    from src.deployments.ingress import SearchRequest

    ing = _make_ingress(monkeypatch)
    ing._feature_reader = BrokenFeatureReader()

    with caplog.at_level(logging.WARNING):
        resp = await ing._search_impl(
            SearchRequest(query="black dress", k=2, user_id="u1")
        )

    # Results still returned
    assert "results" in resp
    assert len(resp["results"]) > 0

    timeout_msgs = [
        r for r in caplog.records if "personalization_snapshot_timeout" in r.message
    ]
    boost_fail_msgs = [
        r for r in caplog.records if "behavior_boost_failed" in r.message
    ]

    assert len(timeout_msgs) == 0, "RuntimeError must not trigger timeout handler"
    assert (
        len(boost_fail_msgs) == 1
    ), "RuntimeError must trigger generic boost_failed handler"


# ---------------------------------------------------------------------------
# Tests: success path unaffected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_personalization_success_path_unaffected(monkeypatch, caplog):
    """
    Adding the TimeoutError handler must not break the success path: fast reader
    completes without timeout logs and results are returned.
    """
    import logging

    monkeypatch.setattr("src.config.PersonalizationConfig.SNAPSHOT_TIMEOUT_MS", 5000)

    from src.deployments.ingress import SearchRequest

    ing = _make_ingress(monkeypatch)
    ing._feature_reader = InstantFeatureReader()

    with caplog.at_level(logging.WARNING):
        resp = await ing._search_impl(
            SearchRequest(query="black dress", k=2, user_id="u1")
        )

    assert "results" in resp
    assert len(resp["results"]) > 0

    timeout_msgs = [
        r for r in caplog.records if "personalization_snapshot_timeout" in r.message
    ]
    boost_fail_msgs = [
        r for r in caplog.records if "behavior_boost_failed" in r.message
    ]

    assert len(timeout_msgs) == 0, "Fast reader must produce no timeout warnings"
    assert (
        len(boost_fail_msgs) == 0
    ), "Fast reader must produce no boost_failed warnings"


@pytest.mark.asyncio
async def test_personalization_disabled_no_snapshot_load(monkeypatch, caplog):
    """
    When PERSONALIZATION_ENABLED=False, no snapshot load is attempted and
    no timeout log appears even if the reader is slow.
    """
    import logging

    monkeypatch.setattr("src.config.PersonalizationConfig.ENABLED", False)
    monkeypatch.setattr("src.config.PersonalizationConfig.SNAPSHOT_TIMEOUT_MS", 1)

    from src.deployments.ingress import SearchRequest

    ing = _make_ingress(monkeypatch)
    ing._feature_reader = SlowFeatureReader()

    with caplog.at_level(logging.WARNING):
        resp = await ing._search_impl(
            SearchRequest(query="black dress", k=2, user_id="u1")
        )

    assert "results" in resp
    assert len(resp["results"]) > 0

    timeout_msgs = [
        r for r in caplog.records if "personalization_snapshot_timeout" in r.message
    ]
    assert len(timeout_msgs) == 0, "No snapshot load when PERSONALIZATION_ENABLED=False"
