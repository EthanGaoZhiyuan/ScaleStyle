import sys
from types import ModuleType

from src import config as config_module
from src.deployments import generation as generation_module


def test_generation_loader_pins_revision_without_remote_code(monkeypatch):
    tokenizer_calls = []
    model_calls = []

    class FakeModel:
        def to(self, device):
            self.device = device
            return self

        def eval(self):
            self.eval_called = True

    class FakeAutoTokenizer:
        @staticmethod
        def from_pretrained(model_name, **kwargs):
            tokenizer_calls.append({"model_name": model_name, "kwargs": kwargs})
            return object()

    class FakeAutoModelForCausalLM:
        @staticmethod
        def from_pretrained(model_name, **kwargs):
            model_calls.append({"model_name": model_name, "kwargs": kwargs})
            return FakeModel()

    fake_transformers = ModuleType("transformers")
    fake_transformers.AutoTokenizer = FakeAutoTokenizer
    fake_transformers.AutoModelForCausalLM = FakeAutoModelForCausalLM

    fake_torch = ModuleType("torch")
    fake_torch.float16 = "float16"
    fake_torch.float32 = "float32"

    monkeypatch.setitem(sys.modules, "transformers", fake_transformers)
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setattr(config_module.GenerationConfig, "ENABLED", True)
    monkeypatch.setattr(config_module.GenerationConfig, "MODE", "llm")
    monkeypatch.setattr(
        config_module.GenerationConfig,
        "MODEL",
        "Qwen/Qwen2-1.5B-Instruct",
    )
    monkeypatch.setattr(
        config_module.GenerationConfig,
        "REVISION",
        "ba1cf1846d7df0a0591d6c00649f57e798519da8",
    )
    monkeypatch.setenv("GENERATION_WARMUP", "0")
    monkeypatch.setenv("GENERATION_DTYPE", "float16")
    monkeypatch.setattr(generation_module, "_resolve_device", lambda _: "cpu")

    deployment = generation_module.GenerationDeployment()

    assert deployment.model_name == "Qwen/Qwen2-1.5B-Instruct"
    assert deployment.model_revision == "ba1cf1846d7df0a0591d6c00649f57e798519da8"
    assert len(tokenizer_calls) == 1
    assert len(model_calls) == 1

    tokenizer_kwargs = tokenizer_calls[0]["kwargs"]
    model_kwargs = model_calls[0]["kwargs"]

    assert tokenizer_calls[0]["model_name"] == "Qwen/Qwen2-1.5B-Instruct"
    assert tokenizer_kwargs["revision"] == deployment.model_revision
    assert "trust_remote_code" not in tokenizer_kwargs

    assert model_calls[0]["model_name"] == "Qwen/Qwen2-1.5B-Instruct"
    assert model_kwargs["revision"] == deployment.model_revision
    assert model_kwargs["torch_dtype"] == fake_torch.float32
    assert "trust_remote_code" not in model_kwargs