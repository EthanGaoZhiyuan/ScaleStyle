"""
Reranker deployment for semantic reordering of search results.

This module provides reranking functionality to improve result relevance by
scoring query-document pairs. Supports both CrossEncoder models and a lightweight
stub scoring fallback for development/testing.
"""

import os
import re
import time
import logging
from typing import List, Dict, Any

from ray import serve

logger = logging.getLogger("scalestyle.reranker")


def _env(*names: str, default: str | None = None) -> str | None:
    """
    Read environment variable with fallback to multiple names.
    
    Supports backward compatibility by checking multiple environment variable names.
    Returns the first non-empty value found, or the default.
    
    Args:
        *names: Variable names to check in order of priority.
        default: Default value if none of the variables are set.
    
    Returns:
        str | None: First non-empty value found, or default.
    """
    for n in names:
        v = os.getenv(n)
        if v is not None and str(v).strip() != "":
            return v
    return default


def _env_bool(name: str, default: bool = False) -> bool:
    """
    Read a boolean value from environment variable.

    Accepts various truthy values: "1", "true", "yes", "y", "on" (case-insensitive).

    Args:
        name: Environment variable name to read.
        default: Default value if variable is not set.

    Returns:
        bool: Parsed boolean value or default.
    """
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "y", "on")


def _simple_tokenize(text: str) -> List[str]:
    """
    Simple tokenization for stub scoring.

    Extracts lowercase alphanumeric tokens using regex. Used for the
    lightweight stub scoring fallback.

    Args:
        text: Input text to tokenize.

    Returns:
        List[str]: List of lowercase alphanumeric tokens.
    """
    return re.findall(r"[a-z0-9]+", (text or "").lower())


def _stub_score(query: str, doc: str) -> float:
    """
    Lightweight stub scoring based on term overlap.

    Computes a simple relevance score using:
    - Token overlap ratio (Jaccard-like similarity)
    - Length penalty to favor concise documents

    Used as a fallback when CrossEncoder model is unavailable.

    Args:
        query: Query string.
        doc: Document string to score.

    Returns:
        float: Relevance score between 0.0 and 1.0.
    """
    # Tokenize and convert to sets for overlap calculation
    q = set(_simple_tokenize(query))
    d = set(_simple_tokenize(doc))
    if not q or not d:
        return 0.0
    # Calculate token overlap ratio
    overlap = len(q & d) / max(1, len(q))

    # Apply length penalty to favor shorter, more concise documents
    length_penalty = 1.0 / (1.0 + (len(doc or "") / 500.0))
    return float(overlap * length_penalty)


@serve.deployment
class RerankerDeployment:
    """
    Ray Serve deployment for reranking search results.

    Provides semantic reranking using either:
    1. CrossEncoder models (e.g., ms-marco-MiniLM) for production-quality scoring
    2. Stub scoring (token overlap) as a lightweight fallback

    Configurable via environment variables for model selection and batch processing.
    """

    def __init__(self):
        """
        Initialize the reranker deployment.

        Loads CrossEncoder model if specified and available, otherwise falls back
        to stub scoring. Configuration is controlled via environment variables:
        - RERANKER_ENABLED: Enable/disable reranking
        - RERANKER_MODEL / RERANK_MODEL: Model name or "stub" for fallback
        - RERANKER_BATCH_SIZE / RERANK_BATCH_SIZE: Batch size for model inference
        - RERANKER_MAX_DOCS / RERANK_MAX_DOCS: Maximum documents to rerank
        - RERANKER_DEVICE / RERANK_DEVICE: Device to use (cpu/cuda)
        - RERANKER_MODE / RERANK_MODE: Reranker mode
        
        Note: Supports both RERANKER_* and RERANK_* prefixes for backward compatibility.
        """
        # Load configuration from environment (prioritize RERANKER_* names)
        self.enabled: bool = _env_bool("RERANKER_ENABLED", True)
        self.model_name: str = (_env("RERANKER_MODEL", "RERANK_MODEL", default="stub") or "stub").strip()
        self.batch_size: int = int(_env("RERANKER_BATCH_SIZE", "RERANK_BATCH_SIZE", default="16") or "16")
        self.max_docs: int = int(_env("RERANKER_MAX_DOCS", "RERANK_MAX_DOCS", default="100") or "100")
        self.device: str = (_env("RERANKER_DEVICE", "RERANK_DEVICE", default="cpu") or "cpu").strip()
        self.mode: str = (_env("RERANKER_MODE", "RERANK_MODE", default="auto") or "auto").strip()

        # Initialize model state
        self._mode: str = "stub"  # Default to stub mode
        self._model = None
        self._tokenizer = None

        # Early exit if reranker is disabled
        if not self.enabled:
            logger.info("Reranker is disabled via environment variable.")
            return

        # Use stub mode if explicitly specified
        if self.model_name.lower() in {"stub", "dummy"}:
            self._mode = "stub"
            logger.info("Reranker using stub scoring model.")
            return

        # Try to import sentence_transformers for CrossEncoder models
        try:
            from sentence_transformers import CrossEncoder
        except Exception:
            logger.warning(
                "sentence_transformers not installed. Falling back to stub model."
            )
            self._mode = "stub"
            return

        # Attempt to load the specified CrossEncoder model
        try:
            self._model = CrossEncoder(self.model_name)
            self._mode = "cross-encoder"
            logger.info(f"Reranker using model: {self.model_name}")
        except Exception as e:
            logger.error(
                f"Failed to load model {self.model_name}. Falling back to stub model. Error: {e}"
            )
            self._mode = "stub"
            
        # Warmup the model if enabled (using consistent env var name)
        warmup = os.getenv("RERANKER_WARMUP", "1") == "1"
        if warmup and self._mode == "cross-encoder":
            try:
                t0 = time.time()
                _ = self._model.predict([("warmup query", "warmup doc")])
                logger.info("reranker warmup ok %.1fms", (time.time() - t0) * 1000)
            except Exception as e:
                logger.warning("reranker warmup failed: %s", e)

    def is_enabled(self) -> bool:
        """
        Check if the reranker is enabled.

        Returns:
            bool: True if reranker is enabled, False otherwise.
        """
        return bool(self.enabled)

    def get_mode(self) -> str:
        """
        Get the current scoring mode.

        Returns:
            str: "stub" for fallback scoring, "cross-encoder" for model-based scoring.
        """
        return self._mode

    def score(self, query: str, docs: List[str]) -> Dict[str, Any]:
        """
        Score documents against a query for reranking.

        Computes relevance scores for each document using either CrossEncoder model
        or stub scoring. Documents exceeding max_docs limit are scored with -1e9.

        Args:
            query: Search query string.
            docs: List of document strings to score (typically metadata concatenations).

        Returns:
            Dict[str, Any]: Dictionary containing:
                - scores (List[float]): Relevance scores for each document
                - rerank_ms (float): Reranking time in milliseconds
                - mode (str): Scoring mode used ("stub" or "cross-encoder")
                - enabled (bool): Whether reranker is enabled
        """
        t0 = time.perf_counter()

        # Return zero scores if disabled or no documents
        if not self.enabled or not docs:
            return {
                "scores": [0.0] * len(docs),
                "rerank_ms": (time.perf_counter() - t0) * 1000,
                "mode": self._mode,
                "enabled": self.enabled,
            }

        # Limit documents to max_docs for performance
        docs_in = docs[: self.max_docs]

        # Compute scores based on available mode
        if self._mode == "stub" or self._model is None:
            # Use simple token overlap scoring
            scores = [_stub_score(query, doc) for doc in docs_in]
        else:
            # Use CrossEncoder model for semantic scoring
            # CrossEncoder.predict expects a list of (query, doc) pairs
            pairs = [(query, doc) for doc in docs_in]
            try:
                scores = self._model.predict(pairs, batch_size=self.batch_size)
            except Exception as e:
                # Fallback to stub scoring on model failure
                logger.exception(
                    f"Error during model prediction: {e}. Falling back to stub scoring."
                )
                scores = [_stub_score(query, doc) for doc in docs_in]

        # Pad scores with very low values for documents beyond max_docs limit
        if len(docs_in) < len(docs):
            pad = [-1e9] * (len(docs) - len(docs_in))
            scores = list(scores) + pad

        # Return scores with metadata
        return {
            "scores": [float(s) for s in scores],
            "rerank_ms": (time.perf_counter() - t0) * 1000.0,
            "mode": self._mode,
            "enabled": True,
        }
