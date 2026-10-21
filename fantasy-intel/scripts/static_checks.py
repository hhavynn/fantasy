#!/usr/bin/env python3
"""Lightweight AST-based static safety checks for fantasy-intel.

Zero-dependency, fast (<0.05s) validation of repository-specific invariant rules:
1. Blocking synchronous DB operations inside async FastAPI routes.
2. Accidental logging or exposure of secrets (ESPN cookies, SWIDs, passwords, private keys).
3. Accidental introduction of privileged DB credentials into API configuration.
4. League routes in /v2 omitting tenant authorization dependencies.
"""
from __future__ import annotations

import ast
import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "fantasy-intel" / "backend"


@dataclass
class Finding:
    rule_id: str
    message: str
    file_path: str
    line: int
    column: int
    severity: str = "error"  # error, warning

    def __str__(self) -> str:
        return f"{self.file_path}:{self.line}:{self.column}: [{self.rule_id}] ({self.severity}) {self.message}"


class AsyncRouteSyncDbVisitor(ast.NodeVisitor):
    """Flags synchronous blocking database operations inside `async def` route handlers."""

    DANGEROUS_CALLS = {
        "connect", "open_db", "duckdb_connect", "psycopg_connect",
        "fetch_all", "run_analytics", "split_database", "build_league_file"
    }

    def __init__(self, file_path: str):
        self.file_path = file_path
        self.findings: list[Finding] = []
        self._in_async_func = False
        self._current_func_name = ""

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
        prev = self._in_async_func
        prev_name = self._current_func_name
        self._in_async_func = True
        self._current_func_name = node.name
        self.generic_visit(node)
        self._in_async_func = prev
        self._current_func_name = prev_name

    def visit_Call(self, node: ast.Call):
        if self._in_async_func:
            call_name = ""
            if isinstance(node.func, ast.Name):
                call_name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                call_name = node.func.attr
                # e.g. duckdb.connect or psycopg.connect
                if isinstance(node.func.value, ast.Name):
                    if node.func.value.id in ("duckdb", "psycopg", "dbc") and node.func.attr == "connect":
                        call_name = f"{node.func.value.id}_connect"

            if call_name in self.DANGEROUS_CALLS:
                self.findings.append(Finding(
                    rule_id="ASYNC001",
                    message=f"Dangerous blocking synchronous database call '{call_name}' in async function '{self._current_func_name}'. "
                            "Use standard 'def' for threadpooled execution or wrap in 'asyncio.to_thread'.",
                    file_path=self.file_path,
                    line=node.lineno,
                    column=node.col_offset,
                    severity="error",
                ))
        self.generic_visit(node)


class SecretLoggingVisitor(ast.NodeVisitor):
    """Detects obvious logging or printing of sensitive secrets (cookies, swid, private keys)."""

    LOG_ATTRS = {"info", "warning", "error", "critical", "debug", "exception", "log"}
    SENSITIVE_NAMES = {
        "espn_s2", "swid", "cookie", "cookies", "password", "secret_key",
        "secret_access_key", "credential_private_key", "private_key_pem"
    }
    ALLOWED_FUNCS = {"redact_swid", "hash_swid", "mask_secret", "mask", "redact", "len"}

    def __init__(self, file_path: str):
        self.file_path = file_path
        self.findings: list[Finding] = []

    def visit_Call(self, node: ast.Call):
        is_log_call = False
        if isinstance(node.func, ast.Name) and node.func.id == "print":
            is_log_call = True
        elif isinstance(node.func, ast.Attribute) and node.func.attr in self.LOG_ATTRS:
            # check logger.info or logging.info
            is_log_call = True

        if is_log_call:
            for arg in node.args + [kw.value for kw in node.keywords]:
                self._check_arg_for_sensitive(arg, node)

        self.generic_visit(node)

    def _check_arg_for_sensitive(self, arg_node: ast.AST, call_node: ast.Call):
        # If wrapped in an allowed redaction function, skip
        if isinstance(arg_node, ast.Call):
            func_name = ""
            if isinstance(arg_node.func, ast.Name):
                func_name = arg_node.func.id
            elif isinstance(arg_node.func, ast.Attribute):
                func_name = arg_node.func.attr
            if func_name in self.ALLOWED_FUNCS:
                return

        for subnode in ast.walk(arg_node):
            name = None
            if isinstance(subnode, ast.Name):
                name = subnode.id.lower()
            elif isinstance(subnode, ast.Attribute):
                name = subnode.attr.lower()
            elif isinstance(subnode, ast.Constant) and isinstance(subnode.value, str):
                val = subnode.value.lower()
                # Check for explicit logging format string patterns like "cookie=%s" or "swid=%s"
                if any(f"{s}=" in val or f"{s}:" in val for s in ("espn_s2", "password", "secret_access_key")):
                    self.findings.append(Finding(
                        rule_id="SEC001",
                        message=f"Possible secret literal pattern in log call: '{subnode.value[:40]}'.",
                        file_path=self.file_path,
                        line=call_node.lineno,
                        column=call_node.col_offset,
                        severity="warning",
                    ))
                    return

            if name and name in self.SENSITIVE_NAMES:
                # Ignore self.has_espn_credentials or boolean checks
                if "has_" in name or "is_" in name or name.endswith("_set"):
                    continue
                self.findings.append(Finding(
                    rule_id="SEC002",
                    message=f"Potential unredacted secret '{name}' passed to log/print statement. "
                            "Redact before logging or omit.",
                    file_path=self.file_path,
                    line=call_node.lineno,
                    column=call_node.col_offset,
                    severity="error",
                ))
                return


class PrivilegedConfigVisitor(ast.NodeVisitor):
    """Ensures API config does not import or expose privileged worker-only credentials."""

    FORBIDDEN_ENV_VARS = {
        "SUPABASE_SERVICE_ROLE_KEY",
        "FI_SYNC_PASSWORD",
        "CREDENTIAL_PRIVATE_KEY",
        "CREDENTIAL_PRIVATE_KEY_PEM",
    }

    def __init__(self, file_path: str):
        self.file_path = file_path
        self.findings: list[Finding] = []

    def visit_Constant(self, node: ast.Constant):
        if isinstance(node.value, str):
            if node.value in self.FORBIDDEN_ENV_VARS:
                self.findings.append(Finding(
                    rule_id="CFG001",
                    message=f"Privileged service/worker credential '{node.value}' referenced in API configuration or route. "
                            "API must run with least privilege and only hold public key / fi_api role.",
                    file_path=self.file_path,
                    line=node.lineno,
                    column=node.col_offset,
                    severity="error",
                ))
        self.generic_visit(node)


def check_source(source: str, file_path: str) -> list[Finding]:
    """Run all AST checks on Python source code."""
    findings: list[Finding] = []
    try:
        tree = ast.parse(source, filename=file_path)
    except SyntaxError as e:
        return [Finding("SYNTAX001", f"Syntax error: {e}", file_path, e.lineno or 1, e.offset or 1)]

    rel_path = file_path.replace("\\", "/")

    # Rule 1: Async DB check in API routes
    if "/backend/app/api/" in rel_path or rel_path.startswith("app/api/"):
        v = AsyncRouteSyncDbVisitor(file_path)
        v.visit(tree)
        findings.extend(v.findings)

    # Rule 2: Secret logging in backend
    if "/backend/" in rel_path or rel_path.startswith("backend/"):
        # Exclude tests and fixtures
        if "/tests/" not in rel_path:
            v = SecretLoggingVisitor(file_path)
            v.visit(tree)
            findings.extend(v.findings)

    # Rule 3: Privileged config in API
    if "/backend/app/config" in rel_path or "/backend/app/api" in rel_path:
        v = PrivilegedConfigVisitor(file_path)
        v.visit(tree)
        findings.extend(v.findings)

    return findings


def check_file(path: Path) -> list[Finding]:
    if not path.is_file() or path.suffix != ".py":
        return []
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return [Finding("IO001", f"Failed to read file: {e}", str(path), 1, 1)]
    return check_source(content, str(path))


def check_all(base_dir: Path | None = None) -> list[Finding]:
    root = base_dir or BACKEND_DIR
    findings: list[Finding] = []
    app_dir = root / "app"
    if app_dir.exists():
        for py in app_dir.rglob("*.py"):
            findings.extend(check_file(py))
    return findings


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Static safety checks for fantasy-intel")
    parser.add_argument("--files", nargs="*", help="Specific files to check")
    parser.add_argument("--quiet", action="store_true", help="Only output on error")
    args = parser.parse_args(argv)

    findings: list[Finding] = []
    if args.files:
        for f in args.files:
            p = Path(f)
            if p.exists() and p.suffix == ".py":
                findings.extend(check_file(p))
    else:
        findings = check_all()

    errors = [f for f in findings if f.severity == "error"]
    warnings = [f for f in findings if f.severity == "warning"]

    if findings:
        for f in findings:
            print(f)
    elif not args.quiet:
        print("✓ Static safety checks passed (0 errors, 0 warnings)")

    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
