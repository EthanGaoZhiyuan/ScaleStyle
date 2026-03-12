from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_inference_base_manifest_requires_image_override_and_persistent_cache():
    manifest = (REPO_ROOT / "infrastructure/k8s/base/inference.yaml").read_text()

    assert "scalestyle-inference:__MUST_BE_OVERRIDDEN_BY_KUSTOMIZE__" in manifest
    assert "claimName: inference-model-cache" in manifest
    assert "path: /readyz" in manifest
    assert "emptyDir:" not in manifest


def test_elasticache_production_defaults_defer_stateful_changes_to_window():
    elasticache_tf = (REPO_ROOT / "infrastructure/terraform/elasticache.tf").read_text()
    variables_tf = (REPO_ROOT / "infrastructure/terraform/variables.tf").read_text()

    assert "apply_immediately = var.redis_apply_immediately" in elasticache_tf
    assert "preferred_maintenance_window = var.redis_preferred_maintenance_window" in elasticache_tf
    assert "snapshot_window            = var.redis_snapshot_window" in elasticache_tf
    assert 'variable "redis_apply_immediately"' in variables_tf
    assert "default = false" in variables_tf
