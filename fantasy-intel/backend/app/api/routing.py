"""Enumerate an app's HTTP routes with their full paths (FastAPI >= 0.140 keeps an included router's own paths and
records the include prefix and dependencies on a wrapper). Used by the golden harness and the authorization tests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator


@dataclass(frozen=True)
class RouteInfo:
    path: str                 # full path, include prefixes applied
    methods: frozenset[str]
    route: Any                # the APIRoute
    include_dependencies: tuple  # dependencies added by include_router (outermost first)
    in_schema: bool

    def dependency_calls(self) -> set:
        """Every dependency callable this route runs: include-level, route-level and their sub-dependencies."""
        calls = {d.dependency for d in self.include_dependencies}
        stack = list(getattr(self.route, "dependant", None).dependencies) if hasattr(self.route, "dependant") else []
        while stack:
            dep = stack.pop()
            calls.add(dep.call)
            stack.extend(dep.dependencies)
        return calls


def iter_routes(app) -> Iterator[RouteInfo]:
    def walk(routes, prefix: str, deps: tuple, in_schema: bool):
        for r in routes:
            inner = getattr(r, "original_router", None)
            if inner is not None:
                ctx = r.include_context
                yield from walk(inner.routes, prefix + (ctx.prefix or ""), deps + tuple(ctx.dependencies or ()),
                                in_schema and ctx.include_in_schema is not False)
            elif hasattr(r, "methods") and r.methods:
                yield RouteInfo(prefix + r.path, frozenset(r.methods - {"HEAD", "OPTIONS"}), r, deps,
                                in_schema and getattr(r, "include_in_schema", True))
    yield from walk(app.routes, "", (), True)
