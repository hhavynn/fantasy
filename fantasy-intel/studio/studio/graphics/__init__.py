"""Graphic styles for Shorts: premium analytics, retro pixel art and modern broadcast.

The original renderer (stages/render_motion.py) stays the default and keeps writing final.mp4. A style renders
the same story, narration, timing, audio and captions into styles/<style>/final.mp4 with its own QC, so styles
can be compared and adopted without touching published videos. Design tokens live in assets/styles/*.toml.
"""

from __future__ import annotations

STYLES = ("premium", "pixel", "broadcast")
CHOICES = ("original", "auto") + STYLES

# A preference, not a rule: the story angle suggests a style (config [graphics.auto] overrides).
AUTO = {"vanishing_targets": "premium", "fill_in": "broadcast", "comparison": "pixel"}


def choose(cfg, story_angle: str, scene_types) -> str:
    """The style for a story under `auto`: the configured preference for its angle (auto_default without one),
    falling back to the original renderer when that style has no design for one of its scenes."""
    from .render import unsupported
    prefs = {**AUTO, **((cfg.raw.get("graphics") or {}).get("auto") or {})}
    style = prefs.get(story_angle, (cfg.raw.get("graphics") or {}).get("auto_default", "original"))
    if style not in STYLES or unsupported(style, scene_types):
        return "original"
    return style
