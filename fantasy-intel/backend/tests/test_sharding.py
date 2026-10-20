"""`pytest --shard=K/N` (tests/conftest.py): the CI shards together run every test file exactly once."""
from conftest import shard_of


def test_shards_cover_every_file_once_and_balance_the_weights():
    weights = {f"tests/test_{i}.py": float(w) for i, w in enumerate([300, 120, 90, 60, 60, 30, 10, 5, 5, 1])}
    files = set(weights) | {"tests/test_new.py"}                  # a file the weights do not know yet
    owner = shard_of(files, 3, weights)
    assert set(owner) == files and set(owner.values()) == {0, 1, 2}
    assert owner == shard_of(sorted(files, reverse=True), 3, weights), "independent of collection order"
    loads = [sum(weights.get(f, 30.0) for f in files if owner[f] == k) for k in range(3)]
    assert max(loads) == 300                                         # the heaviest file alone bounds the run


def test_one_shard_is_the_whole_suite():
    assert set(shard_of({"tests/a.py", "tests/b.py"}, 1, {}).values()) == {0}
