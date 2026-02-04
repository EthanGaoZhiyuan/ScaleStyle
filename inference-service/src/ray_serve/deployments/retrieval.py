import logging
import asyncio
import os
import time
from typing import List, Dict, Any, Optional

from ray import serve

from src.ray_serve.utils.milvus_client import create_milvus_client

logger = logging.getLogger("scalestyle.retrieval")


@serve.deployment
class RetrievalDeployment:
    """
    Ray Serve deployment for vector-based retrieval using Milvus.

    This deployment handles semantic search queries by connecting to a Milvus
    vector database and performing similarity searches with optional filtering.
    """

    def __init__(self):
        """
        Initialize the retrieval deployment and connect to Milvus.

        Establishes connection to Milvus vector database and loads the collection.
        If the collection is not found, the deployment enters a not-ready state.

        Uses exponential backoff retry for production resilience.
        """
        # Initialize ready state to False until Milvus is fully loaded
        self.ready = False
        self.collection_name = os.getenv("MILVUS_COLLECTION", "scale_style_bge_v2")

        logger.info(
            f"🚀 Initializing RetrievalDeployment for collection: {self.collection_name}"
        )

        try:
            # Connect to Milvus using utility function with automatic retry logic
            # Reads MILVUS_HOST and MILVUS_PORT from environment at runtime
            logger.info("Connecting to Milvus with retry logic...")
            self.client = create_milvus_client(max_retries=10, retry_delay=3.0)
            logger.info("✅ Milvus client created successfully")

            # Check if the Milvus collection exists
            logger.info(f"Checking if collection '{self.collection_name}' exists...")
            if not self.client.has_collection(self.collection_name):
                logger.error(
                    f"❌ Milvus collection '{self.collection_name}' not found. "
                    "Retrieval will be DISABLED until init is done. "
                    "Run: python data-pipeline/src/scripts/milvus_init.py"
                )
                self.ready = False
                return

            logger.info(f"✅ Collection '{self.collection_name}' found")

            # Initialize lock for thread safety during concurrent requests
            self._lock = asyncio.Lock()

            # Load the collection into memory for search operations
            logger.info(f"Loading collection '{self.collection_name}' into memory...")
            try:
                self.client.load_collection(self.collection_name)
                self.ready = True

                # Get collection statistics
                stats = self.client.get_collection_stats(self.collection_name)
                row_count = stats.get("row_count", "unknown")
                logger.info(
                    f"✅ Milvus retrieval ready! Collection: {self.collection_name}, "
                    f"Rows: {row_count}"
                )
            except Exception as e:
                logger.error(
                    f"❌ Failed to load collection '{self.collection_name}': {e}"
                )
                self.ready = False

        except Exception as e:
            logger.error(
                f"❌ Failed to initialize RetrievalDeployment: {type(e).__name__}: {e}",
                exc_info=True,
            )
            self.ready = False

    async def is_ready(self) -> bool:
        """
        Check if the retrieval service is ready to handle requests.

        Returns:
            bool: True if Milvus collection is loaded and ready, False otherwise.
        """
        return bool(self.ready)

    def _build_filter_expr(self, filters: Dict[str, Any]) -> Optional[str]:
        """
        Build a Milvus filter expression from filter criteria.

        Args:
            filters: Dictionary containing filter criteria such as:
                - colour_group_name: Filter by color group
                - price_lt: Filter items with price less than this value

        Returns:
            Optional[str]: Filter expression string for Milvus search, or None if no filters.
        """
        exprs = []
        # Extract filter criteria
        # todo: expand with more filters as needed
        colour = filters.get("colour_group_name")
        price_lt = filters.get("price_lt")

        # Build filter expressions
        if colour:
            exprs.append(f'colour_group_name == "{colour}"')
        if price_lt is not None:
            exprs.append(f"price < {float(price_lt)}")

        # Combine multiple filters with AND logic
        return " and ".join(exprs) if exprs else None

    def _search_impl(
        self,
        vector: List[float],
        candidate_k: int = 200,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Synchronous Milvus search implementation (I/O intensive).

        This method contains the actual Milvus search operation and will be run
        in a thread pool to avoid blocking the event loop.

        Args:
            vector: Query embedding vector for similarity search.
            candidate_k: Maximum number of candidates to retrieve from Milvus.
            filters: Optional filter criteria (color, price, etc.).

        Returns:
            List[Dict[str, Any]]: List of search results with article_id and similarity score.
        """

        t0 = time.time()
        # Configure search parameters: COSINE similarity with nprobe=10
        search_params = {"metric_type": "COSINE", "params": {"nprobe": 10}}

        # Build filter expression if filters are provided
        expr = self._build_filter_expr(filters or {})

        # Prepare filter kwargs for newer Milvus API
        kwargs = {}
        if expr:
            kwargs["filter"] = expr

        try:
            # Attempt search with newer API (filter parameter)
            results = self.client.search(
                collection_name=self.collection_name,
                data=[vector],
                limit=candidate_k,
                output_fields=["article_id"],
                search_params=search_params,
                **kwargs,
            )
        except TypeError:
            # Fallback for older Milvus API (expr parameter)
            kwargs = {}
            if expr:
                kwargs["expr"] = expr
            results = self.client.search(
                collection_name=self.collection_name,
                data=[vector],
                limit=candidate_k,
                output_fields=["article_id"],
                search_params=search_params,
                **kwargs,
            )

        # Extract search results (first query result)
        hits = results[0]
        out = []

        # Process each hit and extract article_id and similarity score
        for h in hits:
            ent = h.get("entity", {}) or {}
            article_id = ent.get("article_id", h.get("id"))
            out.append({"article_id": article_id, "score": h.get("distance")})

        # Log search performance
        logger.info(
            "search candidate_k=%d expr=%s took=%.2fms",
            candidate_k,
            expr,
            (time.time() - t0) * 1000,
        )
        # Return results limited to candidate_k
        return out[:candidate_k]

    async def search(
        self,
        vector: List[float],
        candidate_k: int = 200,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Perform vector similarity search in Milvus (async wrapper).

        Args:
            vector: Query embedding vector for similarity search.
            candidate_k: Maximum number of candidates to retrieve from Milvus.
            filters: Optional filter criteria (color, price, etc.).

        Returns:
            List[Dict[str, Any]]: List of search results with article_id and similarity score.

        Raises:
            RuntimeError: If the retrieval service is not ready.
        """
        # Ensure the service is ready before searching
        if not self.ready:
            raise RuntimeError(
                "Retrieval not ready (Milvus collection missing or not loaded)"
            )

        # Run I/O-intensive Milvus search in thread pool with lock for safety
        async with self._lock:
            return await asyncio.to_thread(
                self._search_impl, vector, candidate_k, filters
            )
