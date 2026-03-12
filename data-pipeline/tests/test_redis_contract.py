import pandas as pd

from src import bootstrap_data


class _FakePipeline:
    def __init__(self):
        self.hset_calls = []
        self.executed = False

    def hset(self, key, mapping):
        self.hset_calls.append((key, mapping))
        return self

    def execute(self):
        self.executed = True
        return []


class _FakeRedis:
    def __init__(self):
        self.pipeline_instance = _FakePipeline()
        self.deleted_keys = []
        self.zadd_calls = []

    def ping(self):
        return True

    def pipeline(self):
        return self.pipeline_instance

    def delete(self, key):
        self.deleted_keys.append(key)

    def zadd(self, key, mapping):
        self.zadd_calls.append((key, mapping))

    def type(self, key):
        return "zset"

    def zcard(self, key):
        return len(self.zadd_calls[0][1]) if self.zadd_calls else 0


def test_load_redis_data_writes_canonical_item_and_meta_keys(monkeypatch):
    fake_redis = _FakeRedis()
    monkeypatch.setattr(bootstrap_data.redis, "Redis", lambda **kwargs: fake_redis)

    df = pd.DataFrame(
        [
            {
                "article_id": 123456,
                "prod_name": "Green Jacket",
                "colour_group_name": "Green",
                "product_type_name": "outerwear",
                "department_name": "Outerwear",
                "detail_desc": "Padded metadata contract",
                "price": 59.99,
                "image_url": "https://example.test/jacket.jpg",
            }
        ]
    )

    bootstrap_data.load_redis_data(df)

    expected_key = "item:0000123456"
    expected_meta_key = "item:0000123456:meta"
    assert fake_redis.pipeline_instance.executed is True
    assert len(fake_redis.pipeline_instance.hset_calls) == 2
    assert fake_redis.pipeline_instance.hset_calls[0][0] == expected_key
    assert fake_redis.pipeline_instance.hset_calls[1][0] == expected_meta_key
    assert (
        fake_redis.pipeline_instance.hset_calls[0][1]
        == fake_redis.pipeline_instance.hset_calls[1][1]
    )
    assert fake_redis.pipeline_instance.hset_calls[0][1]["article_id"] == "0000123456"
    assert fake_redis.pipeline_instance.hset_calls[0][1]["prod_name"] == "Green Jacket"
    assert fake_redis.pipeline_instance.hset_calls[0][1]["category"] == "outerwear"
    assert fake_redis.zadd_calls[0][0] == bootstrap_data.POPULARITY_KEY
    assert "0000123456" in fake_redis.zadd_calls[0][1]
