"""Deterministic offline writer for tests and dry runs. Reads only what the prompt and schema carry."""

from __future__ import annotations

import json
import re

SEGMENT = re.compile(r"Segment (s\d+) \(([a-z_]+)[^\n]*\), about (\d+) words\.")
PLACEHOLDER_LINE = re.compile(r"^\s+\{\{([a-z0-9_]+)\}\} = ", re.M)


class FakeLLM:
    def generate(self, system: str, user: str, schema: dict, max_tokens: int | None = None) -> str:
        props = schema.get("properties", {})
        if "sentences" in props:                         # fact-check pass: nothing to flag
            return json.dumps({"sentences": []})
        if "title_candidates" in props:
            return json.dumps({"title_candidates": ["The usage report for {{this_week}}",
                                                    "Usage risers and fallers, {{this_week}}",
                                                    "Who gained a role before {{this_week}}"]})
        out = {}
        parts = SEGMENT.split(user)[1:]                  # [id, section, words, body, id, section, words, body, ...]
        for seg_id, section, words, body in zip(parts[0::4], parts[1::4], parts[2::4], parts[3::4]):
            aliases = PLACEHOLDER_LINE.findall(body)
            refs = " ".join(f"Then {{{{{a}}}}}." for a in aliases[:4])
            text = f"This segment covers {section.replace('_', ' ')}. {refs}".strip()
            pad = max(0, int(words) - len(text.split()))
            text += " The role keeps moving." * (pad // 4)
            out[seg_id] = text
        return json.dumps(out)

    def info(self) -> dict:
        return {"provider": "fake", "model": "fake", "repo": None, "license": "n/a", "temperature": 0}
