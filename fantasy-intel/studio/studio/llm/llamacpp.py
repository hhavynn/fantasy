"""llama.cpp `llama-server` through its OpenAI-compatible API, with JSON-schema-constrained output."""

from __future__ import annotations

import time

import httpx


class LlamaCppLLM:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.base_url = cfg["base_url"].rstrip("/")

    def generate(self, system: str, user: str, schema: dict, max_tokens: int | None = None) -> str:
        body = {
            "model": self.cfg["model"],
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": self.cfg["temperature"],
            "max_tokens": min(max_tokens or self.cfg["max_tokens"], self.cfg["max_tokens"]),
            # Grammar-constrained free text can fall into a loop ("He's involved. He's used. ..."); these
            # penalties discourage it and the per-call token budget cuts it short.
            "presence_penalty": self.cfg.get("presence_penalty", 0.0),
            "repeat_penalty": self.cfg.get("repeat_penalty", 1.0),
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "episode_script", "strict": True, "schema": schema}},
        }
        # The server can hit a transient Metal out-of-memory error when another app holds GPU memory; it
        # recovers on the next request, so 5xx responses are retried with a short backoff.
        for attempt in range(4):
            resp = httpx.post(f"{self.base_url}/chat/completions", json=body, timeout=900)
            if resp.status_code < 500 or attempt == 3:
                break
            time.sleep(2 * (attempt + 1))
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    def info(self) -> dict:
        served = None
        try:
            served = httpx.get(f"{self.base_url}/models", timeout=10).json()["data"][0]["id"]
        except (httpx.HTTPError, KeyError, IndexError, ValueError):
            pass
        return {"provider": "llamacpp", "model": self.cfg["model"], "served_model": served,
                "repo": self.cfg["model_repo"], "license": self.cfg["model_license"],
                "temperature": self.cfg["temperature"]}
