"""Importing this package registers every collector."""

from social_intel.collectors import attention, bluesky, news, reddit, video, x  # noqa: F401
from social_intel.collectors.base import REGISTRY, CollectContext, Collector, SourceConfig, run_collectors

__all__ = ["REGISTRY", "Collector", "CollectContext", "SourceConfig", "run_collectors"]
