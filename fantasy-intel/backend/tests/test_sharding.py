"""`pytest --shard=K/N` (tests/conftest.py): the CI shards together run every test exactly once."""
from conftest import shard_of


def _tests_for(files: dict[str, int]) -> dict[str, list[str]]:
    return {f: [f"{f}::t{i}" for i in range(n)] for f, n in files.items()}


def test_shards_cover_every_test_once_and_keep_light_files_together():
    weights = {"tests/a.py": 30.0, "tests/b.py": 20.0, "tests/c.py": 10.0, "tests/d.py": 10.0}
    tests = _tests_for({"tests/a.py": 3, "tests/b.py": 2, "tests/c.py": 4, "tests/d.py": 1, "tests/new.py": 2})
    owner = shard_of(tests, 3, weights)
    assert set(owner) == {t for ids in tests.values() for t in ids}
    assert set(owner.values()) == {0, 1, 2}
    for f in ("tests/b.py", "tests/c.py", "tests/new.py"):
        assert len({owner[t] for t in tests[f]}) == 1, f"{f} stays in one shard"
    assert owner == shard_of(dict(reversed(list(tests.items()))), 3, weights), "independent of collection order"


def test_a_file_heavier_than_an_even_share_is_dealt_out_test_by_test():
    weights = {"tests/slow.py": 300.0, "tests/x.py": 50.0, "tests/y.py": 50.0}
    tests = _tests_for({"tests/slow.py": 30, "tests/x.py": 5, "tests/y.py": 5})
    owner = shard_of(tests, 4, weights)
    assert {owner[t] for t in tests["tests/slow.py"]} == {0, 1, 2, 3}
    loads = [0.0] * 4
    for f, ids in tests.items():
        for t in ids:
            loads[owner[t]] += weights[f] / len(ids)
    assert max(loads) - min(loads) <= 10.0 + 1e-9


def test_one_shard_is_the_whole_suite():
    assert set(shard_of(_tests_for({"tests/a.py": 2, "tests/b.py": 1}), 1, {}).values()) == {0}
