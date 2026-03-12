import os
from pathlib import Path

os.environ.setdefault("PROMETHEUS_MULTIPROC_DIR", "/tmp/prometheus_multiproc")

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    multiprocess,
)

_REGISTRY: dict = {}
_MULTIPROC_DIR_ENV = "PROMETHEUS_MULTIPROC_DIR"


def _multiprocess_dir() -> Path | None:
    raw = os.getenv(_MULTIPROC_DIR_ENV, "").strip()
    if not raw:
        return None
    return Path(raw)


def _is_multiprocess_enabled() -> bool:
    return _multiprocess_dir() is not None


def bootstrap_metrics_storage(clear_existing: bool = False) -> Path | None:
    metrics_dir = _multiprocess_dir()
    if metrics_dir is None:
        return None

    metrics_dir.mkdir(parents=True, exist_ok=True)
    if clear_existing:
        for path in metrics_dir.iterdir():
            if path.is_file() and path.suffix == ".db":
                path.unlink()
    return metrics_dir


bootstrap_metrics_storage(clear_existing=False)


def counter(name: str, doc: str, labels) -> Counter:
    if name not in _REGISTRY:
        _REGISTRY[name] = Counter(name, doc, labels)
    return _REGISTRY[name]


def histogram(name: str, doc: str, labels, buckets) -> Histogram:
    if name not in _REGISTRY:
        _REGISTRY[name] = Histogram(name, doc, labels, buckets=buckets)
    return _REGISTRY[name]


def gauge(name: str, doc: str, labels=None) -> Gauge:
    labels = labels or []
    if name not in _REGISTRY:
        kwargs = {"multiprocess_mode": "livesum"} if _is_multiprocess_enabled() else {}
        _REGISTRY[name] = Gauge(name, doc, labels, **kwargs)
    return _REGISTRY[name]


def generate_latest_metrics() -> bytes:
    if _is_multiprocess_enabled():
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        return generate_latest(registry)
    return generate_latest()


metrics_content_type = CONTENT_TYPE_LATEST
