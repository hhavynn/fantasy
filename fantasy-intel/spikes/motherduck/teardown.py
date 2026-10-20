"""Drop every spike_* MotherDuck database (and nothing else)."""

from common import connect

con = connect("md:", "laptop")
names = [r[0] for r in con.execute("SELECT database_name FROM MD_INFORMATION_SCHEMA.DATABASES").fetchall()
         if r[0].startswith("spike_")]  # hard guard: never fantasy_intel/my_db
con.execute("DROP SHARE IF EXISTS spike_share_a")  # a share pins its source database
for n in names:
    assert n.startswith("spike_")
    con.execute(f"DROP DATABASE {n}")
print(f"dropped {len(names)} spike databases and the spike share")
