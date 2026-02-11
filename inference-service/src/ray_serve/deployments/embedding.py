import time
import logging
import asyncio

import torch
import torch.nn.functional as F
from transformers import AutoModel, AutoTokenizer
from ray import serve

from src.config import EmbeddingConfig

logger = logging.getLogger("scalestyle.embedding")


@serve.deployment(
    ray_actor_options={
        "num_cpus": EmbeddingConfig.NUM_CPUS,
        "num_gpus": EmbeddingConfig.NUM_GPUS,
    },
    # Single replica for resource-constrained minikube (production: scale with HPA)
    num_replicas=1,
    autoscaling_config=None,
)
class EmbeddingDeployment:
    """
    Ray Serve deployment for text embedding generation using transformer models.

    This deployment loads a pre-trained transformer model (default: BGE-large-en-v1.5)
    and provides embedding generation for both queries and documents. Supports GPU
    acceleration when available and configurable resource allocation.
    """

    def __init__(self):
        """
        Initialize the embedding deployment and load the transformer model.

        Configures device (CPU/GPU), loads tokenizer and model, and prepares
        for embedding generation. Configuration is controlled via environment variables.

        Enterprise optimization: Model warmup + cache verification
        """
        # Initialize ready state to False until model is fully loaded
        self.ready = False
        self.tokenizer = None
        self.model = None

        # Determine device: use GPU if available, otherwise CPU
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        # Load model configuration from centralized config
        self.model_name = EmbeddingConfig.MODEL
        self.max_length = EmbeddingConfig.MAX_LENGTH
        # Query prefix for asymmetric embedding (different for queries vs documents)
        self.query_prefix = EmbeddingConfig.QUERY_PREFIX.strip()

        # Set data type: float16 for GPU (faster), float32 for CPU (more stable)
        self.dtype = torch.float16 if self.device == "cuda" else torch.float32

        logger.info(f"🚀 Initializing EmbeddingDeployment: {self.model_name}")
        logger.info(f"   Device: {self.device}, dtype: {self.dtype}")

        try:
            # Load tokenizer and measure loading time
            t0 = time.time()
            logger.info("📥 Loading tokenizer...")
            self.tokenizer = AutoTokenizer.from_pretrained(self.model_name)
            t_tok = (time.time() - t0) * 1000
            logger.info(f"✅ Tokenizer loaded in {t_tok:.2f}ms")

            # Load model and move to appropriate device
            t1 = time.time()
            logger.info("📥 Loading model (this may take 30-60s for large models)...")
            self.model = AutoModel.from_pretrained(
                self.model_name, torch_dtype=self.dtype
            )
            # Set model to evaluation mode (disable dropout, etc.)
            self.model.to(self.device).eval()
            t_model = (time.time() - t1) * 1000
            logger.info(
                f"✅ Model loaded and moved to {self.device} in {t_model:.2f}ms"
            )

            # Enterprise optimization: Model warmup
            # Run a dummy inference to ensure all components are initialized
            logger.info("🔥 Warming up model with dummy inference...")
            try:
                t_warmup = time.time()
                dummy_text = "Hello world"
                with torch.no_grad():
                    inputs = self.tokenizer(
                        dummy_text,
                        max_length=self.max_length,
                        truncation=True,
                        padding=True,
                        return_tensors="pt",
                    ).to(self.device)
                    _ = self.model(**inputs)
                warmup_ms = (time.time() - t_warmup) * 1000
                logger.info(f"✅ Model warmup completed in {warmup_ms:.2f}ms")
            except Exception as e:
                logger.warning(f"⚠️  Model warmup failed (non-critical): {e}")

            # Multiple replicas provide better load distribution than single instance with lock

            # Mark deployment as ready for serving requests
            self.ready = True
            total_time = (time.time() - t0) * 1000
            logger.info(
                f"✅ EmbeddingDeployment ready! "
                f"Total initialization: {total_time:.2f}ms "
                f"(tokenizer: {t_tok:.2f}ms, model: {t_model:.2f}ms)"
            )

        except Exception as e:
            # Graceful handling of model loading failures
            # Instead of crashing, log error and keep pod running but unavailable
            logger.error(
                f"❌ Failed to load embedding model '{self.model_name}': {e}\n"
                f"   This deployment will remain unavailable.\n"
                f"   Possible causes:\n"
                f"   - Network connectivity issues\n"
                f"   - HuggingFace Hub unreachable\n"
                f"   - Model cache not available\n"
                f"   - Insufficient memory or disk space\n"
                f"   Resolution: Check network, HF_HOME cache, and resource limits"
            )
            self.ready = False
            self.tokenizer = None
            self.model = None
            # No lock needed with multi-replica deployment

    async def is_ready(self) -> bool:
        """
        Check if the embedding service is ready to handle requests.

        Returns:
            bool: True if model is loaded and ready, False otherwise.
        """
        return bool(self.ready)

    def _prep_text(self, text: str, is_query: bool) -> str:
        """
        Prepare text by adding query prefix if applicable.

        For asymmetric embedding models (like BGE), queries need a specific prefix
        to optimize retrieval performance, while documents don't need any prefix.

        Args:
            text: Input text to prepare.
            is_query: Whether the text is a search query (True) or document (False).

        Returns:
            str: Prepared text with prefix if applicable.
        """
        if is_query and self.query_prefix:
            return f"{self.query_prefix} {text}"
        return text

    def _embed_sync(self, text: str, is_query: bool = True):
        """
        Synchronous embedding generation (CPU/GPU intensive).

        This method contains the actual heavy computation and will be run
        in a thread pool to avoid blocking the event loop.

        Args:
            text: Input text string or list of text strings to embed.
            is_query: Whether the text is a search query (adds prefix) or document.

        Returns:
            List[float] or List[List[float]]: Embedding vector(s).
        """
        # Handle both single string and list of strings
        if isinstance(text, str):
            texts = [self._prep_text(text, is_query)]
            single = True
        else:
            texts = [self._prep_text(t, is_query) for t in text]
            single = False

        # Tokenize input texts with padding and truncation
        inputs = self.tokenizer(
            texts,
            max_length=self.max_length,
            padding=True,
            truncation=True,
            return_tensors="pt",
        )
        # Move tensors to appropriate device (CPU/GPU)
        inputs = {k: v.to(self.device) for k, v in inputs.items()}

        # Generate embeddings without computing gradients (inference mode)
        with torch.no_grad():
            outputs = self.model(**inputs)
            # Use CLS token (first token) as sentence representation
            embeddings = outputs.last_hidden_state[:, 0]
            # Normalize embeddings to unit length for cosine similarity
            embeddings = F.normalize(embeddings, p=2, dim=1)
            # Convert to Python list format
            vecs = embeddings.float().cpu().numpy().tolist()

        # Return single vector for single input, list of vectors for batch input
        return vecs[0] if single else vecs

    async def embed(self, text: str, is_query: bool = True):
        """
        Generate embedding vector(s) for input text(s) (async wrapper).

        Supports both single string and list of strings. Uses CLS token pooling
        and L2 normalization for embedding generation.

        This async method wraps the CPU/GPU-intensive embedding in asyncio.to_thread()
        to prevent blocking the Ray Serve event loop. Uses lock to ensure thread safety.

        Args:
            text: Input text string or list of text strings to embed.
            is_query: Whether the text is a search query (adds prefix) or document.

        Returns:
            List[float] or List[List[float]]: Single embedding vector for string input,
                or list of embedding vectors for list input.

        Raises:
            RuntimeError: If the embedding service is not ready.
            ValueError: If input text is empty.
        """
        if not self.ready:
            raise RuntimeError("Embedding model not ready")

        if not text or (isinstance(text, str) and not text.strip()):
            raise ValueError("Empty query")

        # Run CPU/GPU-intensive embedding in thread pool without lock
        # Multiple replicas handle concurrency, lock caused serialization bottleneck
        return await asyncio.to_thread(self._embed_sync, text, is_query)
