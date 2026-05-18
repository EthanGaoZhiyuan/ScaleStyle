# Local E2E Validation — ScaleStyle Multimodal Recommendation System

**Date:** 2026-05-16  
**Machine:** Local Apple Silicon (Mac M4 Max, 128 GB)  
**Stack:** docker-compose (gateway + inference + Redis + Milvus + etcd + MinIO)

---

## Data Store Verification

| Store | Key / Collection | Expected | Actual | Pass |
|---|---|---|---|---|
| Redis | `global:popular` type | `zset` | `zset` | ✓ |
| Redis | `global:popular` cardinality | 1000 | 1000 | ✓ |
| Milvus | `scale_style_bge_small_v1_5` rows | ~105,542 | 105,542 | ✓ |
| Milvus | `scale_style_bge_small_v1_5` dim | 384 | 384 | ✓ |
| Milvus | `scale_style_clip_image_v1` rows | ~105,100 | 105,100 | ✓ |
| Milvus | `scale_style_clip_image_v1` dim | 512 | 512 | ✓ |

Top-5 `global:popular` by purchase count:

| Rank | Article ID | Purchase Count |
|---|---|---|
| 1 | 0706016001 | 50,287 |
| 2 | 0706016002 | 35,043 |
| 3 | 0372860001 | 31,718 |
| 4 | 0610776002 | 30,199 |
| 5 | 0759871002 | 26,329 |

---

## Smoke Test Results

### smoke-text
- Endpoint: `GET /api/recommendation/search?query=black+dress&k=5`
- Result: **PASS** — 5 items, `degraded=false`, `source=ray`, `itemId=0827955002` (10-digit)
- Observed latency: ~130–150 ms

### smoke-image
- Endpoint: `POST /api/recommendation/search/image`
- Image: `data-pipeline/data/raw/images/010/0108775015.jpg` (CLIP image-to-image)
- Result: **PASS** — 5 items, `degraded=false`, `mode=image`, `source=ray`, `itemId=0108775015` (10-digit)
- Observed latency: ~140–145 ms

### smoke-hybrid
- Endpoint: `POST /api/recommendation/search/hybrid`
- Image: same as above, query: `"similar but black"`, weights: image=0.5 / text=0.4 / behavior=0.1
- Result: **PASS** — 5 items, `degraded=false`, `mode=hybrid`, `architecture=dual_recall_normalized_fusion`
- Top item: `0108775015` `finalScore=0.5` `candidateSources=["image"]`
- Observed latency: ~140–145 ms

### smoke-fallback (non-destructive)
- Pre-condition check: gateway `/api/recommendation/debug/cache-stats` → HTTP 200
- Redis `global:popular` ZCARD → 1000
- Result: **PASS** (pre-conditions only)

### smoke-fallback (destructive — `DESTRUCTIVE=1`)
- Action: `docker pause scalestyle-inference` → gateway call → `docker unpause`
- Result: **PASS** — `degraded=true`, `source=redis-cache`, `degradedReason=STALE_DATA_ALLOWED`, 5 items
- Recovery: inference healthy at ~20s after unpause
- Note: The gateway served stale Redis request-cache results (first resilience layer) before reaching `global:popular` (second layer). This is correct behavior.

---

## Stability Loop

| Endpoint | Run 1 | Run 2 | Run 3 |
|---|---|---|---|
| smoke-text | PASS | PASS | PASS |
| smoke-image | PASS | PASS | PASS |
| smoke-hybrid | PASS | PASS | PASS |

No intermittent `degraded=true`. No empty results across 9 runs.

---

## Known Limitations

- **Hybrid fusion is baseline:** min-max normalized weighted score fusion with fixed weights. No learned ranking, no user-specific reranking.
- **`behavior_score` is inert:** wired in the response schema and accepted in the API, but always returns `0.0` (Phase 7 placeholder).
- **No reranker on hybrid path:** the text-only and image-only paths run a reranker; the hybrid handler currently skips it.
- **No K8s / AWS validation:** all results are from local docker-compose. No SLA or production traffic claim.
- **Image URLs empty for text search results:** `imgUrl` is blank for text-only results when the gateway's static image serving is not populated for those items.
- **No auth layer:** endpoints are unauthenticated; suitable for local/demo use only.
