"""Task 6b: apply one additive schema change to every spike league DB, sequentially and with 8 threads."""
import time
from concurrent.futures import ThreadPoolExecutor
from common import N_LEAGUES, connect, league_db, record

base = connect("md:", "laptop")

def alter(i, col):
    cur = base.cursor()
    t0 = time.perf_counter()
    try:
        cur.execute(f"ALTER TABLE {league_db(i)}.main._sync_meta ADD COLUMN IF NOT EXISTS {col} INTEGER")
        return round((time.perf_counter() - t0) * 1000, 1), None
    except Exception as e:
        return round((time.perf_counter() - t0) * 1000, 1), f"{league_db(i)}: {str(e)[:120]}"

t0 = time.perf_counter(); seq = [alter(i, "spike_col_a") for i in range(1, N_LEAGUES + 1)]; seq_s = time.perf_counter() - t0
t0 = time.perf_counter()
with ThreadPoolExecutor(8) as ex:
    par = list(ex.map(lambda i: alter(i, "spike_col_b"), range(1, N_LEAGUES + 1)))
par_s = time.perf_counter() - t0
out = {"sequential_s": round(seq_s, 1), "parallel8_s": round(par_s, 1),
       "seq_errors": [e for _, e in seq if e], "par_errors": [e for _, e in par if e]}
print(record("migrations", out)); print(out)
