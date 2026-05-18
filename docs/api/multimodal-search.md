# Multimodal Search Path

## Current Architecture

The repository does not use a single multimodal architecture everywhere.

- Text search on `/search` is a BGE-style text embedding path: router -> embedding -> Milvus retrieval -> Redis enrichment -> optional rerank.
- Vision search on `/search/image` was previously a separate CLIP path.
- The old `VisionDeployment.search_multimodal()` implementation used weighted embedding fusion inside CLIP space, then a single image-collection lookup.

That means the old multimodal path was primarily embedding-level fusion, not true dual-recall merge-rerank.

## Implemented Path

For explicit `mode=multimodal` image requests, the inference ingress now runs a bounded dual-recall merge-rerank path:

1. Text recall path
   - query -> `EmbeddingDeployment` -> `RetrievalDeployment.search()`
   - uses the existing text collection and query embedding path

2. Image recall path
   - reference image -> `VisionDeployment` in `mode=image`
   - uses the existing CLIP image collection path

3. Candidate merge
   - candidates are merged explicitly with weighted reciprocal-rank fusion
   - dedupe happens by `article_id`
   - the merged list records contributing sources for each item

4. Rerank stage
   - merged candidates are enriched once from Redis in a batch
   - if reranking is enabled, the merged candidate set is reranked once using the text query

## Latency and Cost Controls

- text recall and image recall run in parallel
- per-branch candidate count is bounded by `min(RECALL_K, max(k * 3, min(RERANKER_MAX_DOCS, 50)))`
- merged candidates are capped before enrichment and reranking
- metadata enrichment is a single batched Redis pipeline call, not per-item lookup in the merge path
- reranking still runs once over the merged candidate set, preserving a single rerank stage on the hot path

## Notes

- Pure `text_to_image` and `image_to_image` requests remain on the existing vision path.
- `image_to_image` is now treated as an alias for the vision deployment's historical `image` mode so gateway and inference naming stay aligned.
- This is intentionally a minimal extension, not a full rewrite of all image-serving paths.
