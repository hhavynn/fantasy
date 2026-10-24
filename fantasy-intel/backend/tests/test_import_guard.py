"""The pandas fast-fail finder (app/__init__.py, #331): with pandas absent, a DuckDB query with bound parameters makes
no import search, `import pandas` still fails with ModuleNotFoundError, and optional-dependency checks still see
pandas as unavailable."""
import importlib
import sys

import duckdb
import pytest

import app

pytestmark = pytest.mark.skipif(app._NoPandas not in sys.meta_path, reason="pandas is installed: the finder is not used")


class _Spy:
    calls: list[str] = []

    @staticmethod
    def find_spec(name, path=None, target=None):
        _Spy.calls.append(name)
        return None


def test_the_finder_is_installed_once_ahead_of_the_path_search():
    from importlib.machinery import PathFinder
    guards = [f for f in sys.meta_path if getattr(f, "__name__", "") == "_NoPandas"]
    assert guards == [app._NoPandas] and sys.meta_path.index(app._NoPandas) < sys.meta_path.index(PathFinder)


def test_a_parameterised_query_makes_no_import_search():
    con = duckdb.connect()
    con.execute("create table t as select range a, 'x' || range::varchar b from range(10)")
    sys.meta_path.insert(1, _Spy)                           # behind the finder: sees only what gets past it
    try:
        _Spy.calls.clear()
        rows = [con.execute("select b from t where a = ? and b = ?", [i, f"x{i}"]).fetchall() for i in range(5)]
    finally:
        sys.meta_path.remove(_Spy)
    assert rows == [[(f"x{i}",)] for i in range(5)] and "pandas" not in _Spy.calls


def test_pandas_still_reads_as_not_installed():
    with pytest.raises(ModuleNotFoundError) as err:
        import pandas  # noqa: F401
    assert err.value.name == "pandas" and "pandas" not in sys.modules
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("pandas.core")
    with pytest.raises(ModuleNotFoundError):              # instead of None: the one change, caught by its users
        importlib.util.find_spec("pandas")
    from polars import _dependencies as deps
    assert deps._lazy_import("pandas")[1] is False         # polars still reads pandas as not installed
