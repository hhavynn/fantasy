"""Fantasy Intel backend.

DuckDB checks each bound query parameter for pandas types by importing pandas (twice per parameter). pandas is not a
dependency, and a failed import is never cached, so every check searched all of sys.path again under the import lock:
a few hundred times per page, about 65 ms of a warm Overview (#331). When pandas is not installed, `_NoPandas` answers
that import at once with the ModuleNotFoundError the full search ends in. The one difference: importlib.util.find_spec
("pandas") raises that error instead of returning None (polars catches it; narwhals' Spark-only path, never reached
here, would not). The check runs once, when the package is first imported: pandas installed later in the same process
would still read as missing. It does nothing when pandas is installed."""

import importlib.machinery as _machinery
import sys as _sys


class _NoPandas:
    """Fail `import pandas` fast, with the error the full search would raise (only installed when pandas is absent)."""

    @staticmethod
    def find_spec(name, path=None, target=None):
        if name == "pandas" or name.startswith("pandas."):
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
        return None


# The finder check comes first: once it is installed, asking the import system about pandas raises.
if (not any(getattr(f, "__name__", "") == "_NoPandas" for f in _sys.meta_path)
        and _machinery.PathFinder.find_spec("pandas") is None):
    _sys.meta_path.insert(0, _NoPandas)
