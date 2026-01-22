import logging
import asyncio
import time
from typing import List, Dict, Any, Optional

from ray import serve
from pymilvus import MilvusClient

from src.config import MilvusConfig

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
        """
        # Initialize ready state to False until Milvus is fully loaded
        self.ready = False

        # Load Milvus connection parameters from centralized config
        self.milvus_host = MilvusConfig.HOST
        self.milvus_port = MilvusConfig.PORT
        self.collection_name = MilvusConfig.COLLECTION

        # Connect to Milvus
        uri = f"http://{self.milvus_host}:{self.milvus_port}"
        self.client = MilvusClient(uri=uri)

        # Check if the Milvus collection exists
        if not self.client.has_collection(self.collection_name):
            logger.warning(
                "Milvus collection '%s' not found. Retrieval will be DISABLED until init is done. "
                "Run: python data-pipeline/src/scripts/milvus_init.py",
                self.collection_name,
            )
            self.ready = False
            return

        # Initialize lock for thread safety during concurrent requests
        self._lock = asyncio.Lock()

        # Load the collection into memory for search operations
        try:
            self.client.load_collection(self.collection_name)
            self.ready = True
            logger.info("Milvus ready uri=%s collection=%s", uri, self.collection_name)
        except Exception as e:
            logger.exception("Milvus load_collection failed: %s", e)
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
        top_k: int = 10,
        candidate_k: int = 200,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Synchronous Milvus search implementation (I/O intensive).

        This method contains the actual Milvus search operation and will be run
        in a thread pool to avoid blocking the event loop.

        Args:
            vector: Query embedding vector for similarity search.
            top_k: Number of top results to return (not currently used in return).
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
        top_k: int = 10,
        candidate_k: int = 200,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Perform vector similarity search in Milvus (async wrapper).

        Args:
            vector: Query embedding vector for similarity search.
            top_k: Number of top results to return (not currently used in return).
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
                self._search_impl, vector, top_k, candidate_k, filters
            )
