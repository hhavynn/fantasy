#!/usr/bin/env python3
"""Lightweight AST-based static safety checks for fantasy-intel.

Zero-dependency, fast (<0.05s) validation of repository-specific invariant rules:
1. Blocking synchronous DB operations inside async FastAPI routes.
2. Synchronous DB dependencies (e.g. `Depends(league_tx)`) in async route handlers.
3. Accidental logging or exposure of secrets (ESPN cookies, SWIDs, passwords, private keys).
4. Accidental introduction of privileged DB credentials into API configuration.

Detection boundaries:
- What this AST checker detects: direct synchronous DB functions (`connect`, `open_db`, `fetch_all`, `run_analytics`),
  synchronous connection methods (`con.execute`, `tx.execute`, `cur.execute`), synchronous DB dependencies
  (`Depends(league_tx)`, `Depends(REQUEST_TX)`, `Depends(signed_in_league)`), and logging of known secret patterns.
- What it does not detect: dynamic reflection or transitive cross-module helper calls where an async route
  calls an arbitrary utility that internally performs I/O. Those boundaries are enforced by runtime authorization
  matrices and concurrency tests.
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
    """Flags synchronous blocking database operations or dependencies inside `async def` route handlers."""

    DANGEROUS_CALLS = {
        "connect", "open_db", "duckdb_connect", "psycopg_connect",
        "fetch_all", "run_analytics", "split_database", "build_league_file"
    }
    DANGEROUS_SYNC_DEPENDENCIES = {
        "league_tx", "REQUEST_TX", "signed_in_league", "db", "require_enabled"
    }
    DANGEROUS_METHODS = {"execute", "fetchall", "fetchone", "fetchmany", "commit", "rollback"}

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

        # Check function parameters for synchronous DB dependencies like Depends(league_tx)
        for default in node.args.defaults + [d for d in node.args.kw_defaults if d]:
            if isinstance(default, ast.Call):
                func_name = ""
                if isinstance(default.func, ast.Name):
                    func_name = default.func.id
                if func_name == "Depends" and default.args:
                    dep = default.args[0]
                    dep_name = ""
                    if isinstance(dep, ast.Name):
                        dep_name = dep.id
                    elif isinstance(dep, ast.Attribute):
                        dep_name = dep.attr
                    if dep_name in self.DANGEROUS_SYNC_DEPENDENCIES:
                        self.findings.append(Finding(
                            rule_id="ASYNC002",
                            message=f"Async route handler '{node.name}' depends on synchronous database transaction dependency '{dep_name}'. "
                                    "Declare handler with standard 'def' for automatic FastAPI worker threadpooling.",
                            file_path=self.file_path,
                            line=node.lineno,
                            column=node.col_offset,
                            severity="error",
                        ))

        self.generic_visit(node)
        self._in_async_func = prev
        self._current_func_name = prev_name

    def visit_Call(self, node: ast.Call):
        if self._in_async_func:
            call_name = ""
            method_name = ""
            receiver_name = ""
            if isinstance(node.func, ast.Name):
                call_name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                call_name = node.func.attr
                method_name = node.func.attr
                if isinstance(node.func.value, ast.Name):
                    receiver_name = node.func.value.id
                    if receiver_name in ("duckdb", "psycopg", "dbc") and node.func.attr == "connect":
                        call_name = f"{receiver_name}_connect"

            # Check direct dangerous calls
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

            # Check con.execute, tx.execute, cur.execute on database variables
            if method_name in self.DANGEROUS_METHODS and receiver_name in ("con", "conn", "cur", "cursor", "tx", "ltx", "db"):
                self.findings.append(Finding(
                    rule_id="ASYNC001",
                    message=f"Dangerous blocking synchronous query execution '{receiver_name}.{method_name}()' in async function '{self._current_func_name}'. "
                            "Use standard 'def' or run query asynchronously.",
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


class V2LeagueRouteAuthVisitor(ast.NodeVisitor):
    """Flags route handlers touching league paths without declaring tenant authorization dependencies."""

    TENANT_DEPENDENCY_NAMES = {
        "league_tx", "league_scope", "valid_league_id", "authorize_league",
        "authorize_league_in", "signed_in_league", "require_enabled",
        "LeagueScope", "LeagueAccess", "LeagueContext"
    }

    def __init__(self, file_path: str):
        self.file_path = file_path
        self.findings: list[Finding] = []

    def visit_FunctionDef(self, node: ast.FunctionDef):
        self._check_route(node)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
        self._check_route(node)
        self.generic_visit(node)

    def _check_route(self, node: ast.FunctionDef | ast.AsyncFunctionDef):
        route_path = None
        for dec in node.decorator_list:
            if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute):
                if dec.func.attr in {"get", "post", "put", "delete", "patch", "api_route"}:
                    if dec.args and isinstance(dec.args[0], ast.Constant) and isinstance(dec.args[0].value, str):
                        p = dec.args[0].value
                        if "{league_id}" in p:
                            route_path = p
                            break

        if not route_path:
            return

        # Check if function has any tenant dependency
        has_auth = False
        all_defaults = [d for d in node.args.defaults + [kd for kd in node.args.kw_defaults if kd]]
        for d in all_defaults:
            if isinstance(d, ast.Call):
                call_func = getattr(d.func, "id", getattr(d.func, "attr", ""))
                if call_func == "Depends" and d.args:
                    arg0 = d.args[0]
                    dep_name = getattr(arg0, "id", getattr(arg0, "attr", ""))
                    if dep_name in self.TENANT_DEPENDENCY_NAMES:
                        has_auth = True
                        break
            dep_name = getattr(d, "id", getattr(d, "attr", ""))
            if dep_name in self.TENANT_DEPENDENCY_NAMES:
                has_auth = True
                break

        for arg in node.args.args + node.args.kwonlyargs:
            if arg.annotation:
                ann_str = ast.unparse(arg.annotation) if hasattr(ast, "unparse") else ""
                if any(t in ann_str for t in self.TENANT_DEPENDENCY_NAMES):
                    has_auth = True
                    break

        if not has_auth:
            self.findings.append(Finding(
                rule_id="AUTH001",
                message=f"Route '{node.name}' handles league path '{route_path}' without declaring a tenant authorization dependency (e.g. Depends(league_tx) or Depends(league_scope))",
                file_path=self.file_path,
                line=node.lineno,
                column=node.col_offset,
                severity="error",
            ))


def check_source(source: str, file_path: str) -> list[Finding]:
    """Run all AST checks on Python source code."""
    findings: list[Finding] = []
    try:
        tree = ast.parse(source, filename=file_path)
    except SyntaxError as e:
        return [Finding("SYNTAX001", f"Syntax error: {e}", file_path, e.lineno or 1, e.offset or 1)]

    rel_path = file_path.replace("\\", "/")

    # Rule 1 & Rule 4: Async DB check & League route auth in API routes
    if "/backend/app/api/" in rel_path or rel_path.startswith("app/api/"):
        v1 = AsyncRouteSyncDbVisitor(file_path)
        v1.visit(tree)
        findings.extend(v1.findings)

        v4 = V2LeagueRouteAuthVisitor(file_path)
        v4.visit(tree)
        findings.extend(v4.findings)

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
