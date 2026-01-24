"""
Generation deployment for producing recommendation explanations.

This module provides LLM-based generation functionality to create natural language
explanations for why specific items are recommended to users. Uses Qwen2 models
for generating concise, single-sentence explanations.
"""

import os
import time
import re
import logging
from typing import Dict, Any
from ray import serve

logger = logging.getLogger("scalestyle.generation")


def _resolve_device(name: str) -> str:
    """
    Resolve device name to actual device.

    Args:
        name: Device name (cpu/cuda/mps/auto)

    Returns:
        str: Resolved device name
    """
    try:
        import torch

        name = (name or "").lower()
        if name in ("cpu", "cuda", "mps"):
            return name
        # auto
        if torch.cuda.is_available():
            return "cuda"
        if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            return "mps"
        return "cpu"
    except ImportError:
        return "cpu"


def _first_sentence(text: str) -> str:
    """
    Extract the first sentence from text.

    Args:
        text: Input text

    Returns:
        str: First sentence (handles Chinese and English punctuation)
    """
    text = (text or "").strip()
    if not text:
        return ""
    # Split on sentence endings (both Chinese and English)
    m = re.split(r"(?<=[。！？.!?])\s+", text, maxsplit=1)
    return m[0].strip()


@serve.deployment
class GenerationDeployment:
    """
    LLM generation deployment for creating recommendation explanations.

    Loads Qwen2-1.5B-Instruct model to generate natural language reasons
    for why specific items match user queries.
    """

    def __init__(self):
        """
        Initialize the generation deployment with conditional model loading.

        Configuration via environment variables:
        - GENERATION_ENABLED: Enable/disable generation (0/1)
        - GENERATION_MODE: Mode (template/llm)
        - GENERATION_MODEL: Model name (default: Qwen/Qwen2-1.5B-Instruct)
        - GENERATION_DEVICE: Device to use (cpu/cuda/mps/auto)
        - GENERATION_MAX_NEW_TOKENS: Maximum tokens to generate
        - GENERATION_TEMPERATURE: Sampling temperature
        - GENERATION_TOP_P: Top-p sampling parameter
        - GENERATION_DO_SAMPLE: Whether to use sampling
        - GENERATION_DTYPE: Model dtype (float16/float32)
        - GENERATION_WARMUP: Whether to run warmup
        - HF_HOME: HuggingFace cache directory
        """
        from src.config import GenerationConfig
        import asyncio

        # ---- Read config ----
        self.enabled = GenerationConfig.ENABLED
        self.mode = GenerationConfig.MODE

        # P0-2 Fix: Concurrency protection (prevent OOM under load)
        max_concurrency = int(os.getenv("GENERATION_MAX_CONCURRENCY", "1"))
        self._semaphore = asyncio.Semaphore(max_concurrency)
        logger.info(f"Generation concurrency limit: {max_concurrency}")

        # ---- Early exit if disabled ----
        if not self.enabled:
            logger.info("Generation disabled (GENERATION_ENABLED=0); skip model load.")
            self.model_name = None
            self.device = None
            self.tokenizer = None
            self.model = None
            return

        # ---- Early exit if template mode ----
        if self.mode == "template":
            logger.info("Generation mode=template; skip model load.")
            self.model_name = None
            self.device = None
            self.tokenizer = None
            self.model = None
            return

        # ---- cache dir (防重复下载) ----
        hf_home = os.getenv("HF_HOME", os.path.expanduser("~/.cache/huggingface"))
        os.environ.setdefault("HF_HOME", hf_home)
        os.environ.setdefault(
            "TRANSFORMERS_CACHE", os.path.join(hf_home, "transformers")
        )

        # ---- config ---- (P0-1 Fix: 支持双命名兼容GEN_*和GENERATION_*)
        self.model_name = (
            os.getenv("GENERATION_MODEL")
            or os.getenv("GEN_MODEL_NAME")
            or "Qwen/Qwen2-1.5B-Instruct"
        )
        self.device = _resolve_device(
            os.getenv("GENERATION_DEVICE") or os.getenv("GEN_DEVICE") or "auto"
        )
        self.max_new_tokens = int(
            os.getenv("GENERATION_MAX_NEW_TOKENS")
            or os.getenv("GEN_MAX_NEW_TOKENS")
            or "48"
        )
        self.temperature = float(
            os.getenv("GENERATION_TEMPERATURE") or os.getenv("GEN_TEMPERATURE") or "0.2"
        )
        self.top_p = float(
            os.getenv("GENERATION_TOP_P") or os.getenv("GEN_TOP_P") or "0.9"
        )
        self.do_sample = (
            os.getenv("GENERATION_DO_SAMPLE") or os.getenv("GEN_DO_SAMPLE") or "0"
        ) == "1"

        # ---- load model (only in llm mode) ----
        logger.info(
            f"Loading generation model: {self.model_name} on device: {self.device}"
        )
        t0 = time.time()

        try:
            from transformers import AutoTokenizer, AutoModelForCausalLM
            import torch

            self.tokenizer = AutoTokenizer.from_pretrained(
                self.model_name, trust_remote_code=True
            )

            # Determine dtype
            dtype_str = os.getenv("GENERATION_DTYPE", "float16")
            if self.device == "cpu":
                torch_dtype = torch.float32
            else:
                torch_dtype = getattr(torch, dtype_str, torch.float16)

            self.model = AutoModelForCausalLM.from_pretrained(
                self.model_name,
                torch_dtype=torch_dtype,
                trust_remote_code=True,
            )
            self.model.to(self.device)
            self.model.eval()

            self.init_ms = (time.time() - t0) * 1000
            logger.info(f"Model loaded successfully in {self.init_ms:.1f}ms")

            # ---- warmup (避免第一条请求冷启动) ----
            if os.getenv("GENERATION_WARMUP", "1") == "1":
                try:
                    logger.info("Running warmup...")
                    _ = self._generate_text(
                        "User query: hi. Item: red dress. Explain why in 1 sentence."
                    )
                    logger.info("Warmup completed")
                except Exception as e:
                    logger.warning(f"Warmup failed: {e}")

        except Exception as e:
            logger.error(f"Failed to load model: {e}")
            raise

    def _build_prompt(self, q: str, item: Dict[str, Any]) -> str:
        """
        Build prompt for LLM generation.

        Args:
            q: User query string
            item: Item metadata dictionary

        Returns:
            str: Formatted prompt
        """
        meta = item.get("meta") or item  # 兼容传进来是 item 或 meta
        title = meta.get("title") or meta.get("prod_name") or ""
        desc = meta.get("desc") or meta.get("detail_desc") or ""
        dept = meta.get("dept") or meta.get("department_name") or ""
        color = meta.get("color") or meta.get("colour_group_name") or ""
        price = meta.get("price") or ""

        # 尽量短：生成要快
        item_str = (
            f"title={title}; dept={dept}; color={color}; price={price}; desc={desc}"
        )
        return f"User query: {q}\nItem: {item_str}\nExplain why in 1 sentence."

    def _generate_text(self, prompt: str) -> str:
        """
        Generate text using the LLM model.

        Args:
            prompt: Input prompt

        Returns:
            str: Generated text (only generated part, not including prompt)
        """
        import torch

        with torch.inference_mode():
            inputs = self.tokenizer(prompt, return_tensors="pt").to(self.device)
            out = self.model.generate(
                **inputs,
                max_new_tokens=self.max_new_tokens,
                do_sample=self.do_sample,
                temperature=self.temperature,
                top_p=self.top_p,
                pad_token_id=self.tokenizer.eos_token_id,
            )

            # P0-2 Fix: Only decode the newly generated tokens (not the prompt)
            input_len = inputs["input_ids"].shape[-1]
            gen_ids = out[0][input_len:]
            text = self.tokenizer.decode(gen_ids, skip_special_tokens=True).strip()

            # Keep only 1 sentence (simple heuristic)
            text = text.split("\n")[0].strip()
            for sep in ["。", "！", "？", ".", "!", "?"]:
                if sep in text:
                    text = text.split(sep)[0].strip() + sep
                    break

            return text

    def _template_reason(self, q: str, item: Dict[str, Any]) -> str:
        """
        Generate a template-based recommendation reason (lightweight, no model).

        Args:
            q: User query string
            item: Item metadata dictionary

        Returns:
            str: Template-based reason
        """
        meta = item.get("meta") or item
        title = (meta.get("title") or meta.get("prod_name") or "this item").strip()
        color = (meta.get("color") or "").strip()
        price = meta.get("price")

        parts = [f"Recommended because it matches '{q}'."]
        if color:
            parts.append(f"Color: {color}.")
        if price not in (None, "", 0):
            parts.append(f"Price: {price}.")
        parts.append(f"Item: {title}.")

        return " ".join(parts)[:220]

    async def explain(self, q: str, item: Dict[str, Any]) -> Dict[str, Any]:
        """
        Generate explanation for why an item matches a query.

        Args:
            q: User query string
            item: Item metadata dictionary

        Returns:
            Dict containing:
                - reason: Generated explanation string
                - gen_ms: Generation time in milliseconds
                - mode: Generation mode used
                - model: Model name (if llm mode)
                - device: Device used (if llm mode)
        """
        import asyncio

        t0 = time.time()

        # P0-1 Fix: Early return if disabled
        if not self.enabled:
            return {
                "reason": "",
                "gen_ms": (time.time() - t0) * 1000,
                "mode": "disabled",
            }

        # P0-2 Fix: Concurrency protection - limit concurrent generations
        async with self._semaphore:
            # P0-1 Fix: Template mode - lightweight reason
            if self.mode == "template":
                try:
                    reason = self._template_reason(q, item)
                    return {
                        "reason": reason,
                        "gen_ms": (time.time() - t0) * 1000,
                        "mode": "template",
                    }
                except Exception as e:
                    logger.error(f"Template generation failed: {e}")
                    return {
                        "reason": "",
                        "gen_ms": (time.time() - t0) * 1000,
                        "mode": "template",
                        "error": str(e),
                    }

            # P0-1 Fix: LLM mode - heavy inference
            try:
                prompt = self._build_prompt(q, item)
                # P0-2 Fix: Offload heavy computation to thread to avoid blocking asyncio loop
                raw = await asyncio.to_thread(self._generate_text, prompt)
                reason = _first_sentence(raw)
                gen_ms = (time.time() - t0) * 1000

                return {
                    "reason": reason,
                    "gen_ms": gen_ms,
                    "mode": "llm",
                    "model": self.model_name,
                    "device": self.device,
                }
            except Exception as e:
                gen_ms = (time.time() - t0) * 1000
                logger.error(f"Generation failed: {e}")
                return {
                    "reason": "",
                    "gen_ms": gen_ms,
                    "mode": "llm",
                    "model": self.model_name,
                    "device": self.device,
                    "error": str(e),
                }
