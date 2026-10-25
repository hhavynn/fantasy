"""The pandas fast-fail finder (app/__init__.py, #331): with pandas absent, DuckDB still asks for pandas on every bound
parameter, the finder answers before any path search, `import pandas` still fails with ModuleNotFoundError, and
optional-dependency checks still see pandas as unavailable."""
import importlib
import importlib.machinery
import importlib.util
import sys

import duckdb
import pytest

import app

# Whether pandas really exists, asked of the path finder directly (never through the finder under test).
PANDAS_INSTALLED = importlib.machinery.PathFinder.find_spec("pandas") is not None
pytestmark = pytest.mark.skipif(PANDAS_INSTALLED, reason="pandas is installed: the finder is not used")


def _spy():
    class Spy:
        calls: list[str] = []

        @staticmethod
        def find_spec(name, path=None, target=None):
            Spy.calls.append(name)
            return None
    return Spy


def test_the_finder_is_installed_once_ahead_of_the_path_search():
    from importlib.machinery import PathFinder
    guards = [f for f in sys.meta_path if getattr(f, "__name__", "") == "_NoPandas"]
    assert guards == [app._NoPandas] and sys.meta_path.index(app._NoPandas) < sys.meta_path.index(PathFinder)


def test_running_the_package_init_again_adds_nothing_and_does_not_raise():
    before = list(sys.meta_path)
    with open(app.__file__) as f:
        exec(compile(f.read(), app.__file__, "exec"), {"__name__": "app_again"})
    assert sys.meta_path == before


def test_duckdb_asks_for_pandas_per_parameter_and_the_finder_answers_first():
    con = duckdb.connect()
    con.execute("create table t as select range a, 'x' || range::varchar b from range(10)")
    front, behind = _spy(), _spy()
    sys.meta_path.insert(0, front)                          # sees every lookup: DuckDB still asks (the mechanism)
    sys.meta_path.insert(sys.meta_path.index(app._NoPandas) + 1, behind)    # sees only what gets past the finder
    try:
        rows = [con.execute("select b from t where a = ? and b = ?", [i, f"x{i}"]).fetchall() for i in range(5)]
    finally:
        sys.meta_path.remove(front)
        sys.meta_path.remove(behind)
    assert rows == [[(f"x{i}",)] for i in range(5)]
    # 2 parameters x 5 queries, at least 2 lookups each; if this drops to 0 a DuckDB upgrade made the finder unneeded
    assert front.calls.count("pandas") >= 2 * 2 * 5
    assert "pandas" not in behind.calls


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
