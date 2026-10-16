"""The swappable script-LLM interface. Implementations return a JSON string matching `schema`."""

from __future__ import annotations

from typing import Protocol


class ScriptLLM(Protocol):
    def generate(self, system: str, user: str, schema: dict, max_tokens: int | None = None) -> str: ...

    def info(self) -> dict:
        """Provider, model, checkpoint and license, recorded in script.json and provenance."""
        ...


def make_llm(cfg: dict) -> ScriptLLM:
    provider = cfg["provider"]
    if provider == "llamacpp":
        from .llamacpp import LlamaCppLLM
        return LlamaCppLLM(cfg)
    if provider == "fake":
        from .fake import FakeLLM
        return FakeLLM()
    raise ValueError(f"unknown llm provider {provider!r}")
