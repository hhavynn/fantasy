"""D/ST projection research (dfp-r1): an opponent-adjusted component model, scored per league, vs ESPN.

Research only. Nothing in the app imports this module (tests/test_dst_research.py asserts it); it never
writes a database, never touches the independent projection tables, the production pin or the model
registry. scripts/dst_research.py drives it; docs/research/dst_projection_model.md reports the results.

What a D/ST earns, and how this module scores it
------------------------------------------------
ESPN reports a D/ST's week as raw statistics (statId -> value) and scores them with the league's items:

    points = sum over items of value(item, slot 16) * stats[statId]

value(item, slot 16) is `pointsOverrides["16"]` when present, else `points` (as kicker_scoring.py does for
slot 17). Every statistic is linear EXCEPT that the points-allowed and yards-allowed tiers are 0/1
indicators, so an expected score needs P(tier), not a tier of the expected points allowed:

    E[points] = sum_count w * E[count] + sum_tier w * P(PA or YA in tier) + w_120 * E[PA] + w_127 * E[YA]

ESPN's own projections use the same representation (a tier "count" is ESPN's probability of the tier), so
ESPN is rescored under the league's rules exactly like the model, and actual weeks are scored from ESPN's
actual raw statistics (the labels therefore use the same stat definitions the league is scored with;
`reconcile` checks them against ESPN's stored league points).

The model (per defense D facing offense O in one game)
-------------------------------------------------------
For each component X in RATED (sacks, interceptions, fumble recoveries, blocked kicks, safeties, return TDs,
points allowed, yards allowed, QB hits + sacks):

    rating_X(D, O) = mu_X * def_X(D) * off_X(O) * home_X

`def_X` / `off_X` are recency-weighted, shrunk, opponent-adjusted multiplicative ratings built from games
strictly before the projection's information cutoff (a few alternating passes: a defense's expected count in a
past game uses its opponent's offense rating, and vice versa). Weights decay with a half-life in weeks, an
offseason counts as `offseason_gap` extra weeks, and `k` pseudo-games of league average shrink every rating
(`k * rare_k_mult` for the rare events). A per-component linear stack, fitted only on earlier seasons'
point-in-time ratings, maps the ratings (and, in the market variant, the opponent's implied team total and the
defense's expected margin from the closing line) to E[X]. Defensive TDs are E[INT] * TD-per-INT, E[FR] *
TD-per-FR and E[blocked kick] * TD-per-block (league rates in the window); P(tier) for points and yards
allowed comes from a kernel-weighted conditional empirical distribution of earlier seasons' outcomes around
the predicted mean (it keeps the lumpiness of NFL scores: 13 vs 14 points allowed is a tier edge).
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field, replace
from typing import Iterable, Mapping, Sequence

import numpy as np

MODEL_VERSION = "dfp-r1"
DST_SLOT = "16"
WEEKS_PER_SEASON = 18

# ----------------------------------------------------------------------------- ESPN D/ST statistics

# Count statistics the model projects (statId -> component it is derived from).
COUNT_STATS: dict[int, str] = {
    93: "blk_td",      # blocked punt / FG returned for a TD
    95: "ints",        # interceptions
    96: "fr",          # fumbles recovered
    97: "blk",         # blocked kicks
    98: "saf",         # safeties
    99: "sacks",
    100: "half_sacks",  # ESPN counts half sacks: always 2 x sacks in the data
    101: "kr_td",      # kickoff return TD
    102: "pr_td",      # punt return TD
    103: "int_td",     # interception return TD
    104: "fr_td",      # fumble return TD
    206: "ret2",       # defensive 2-pt return
    209: "saf1",       # 1-pt safety
}
PA_STAT, YA_STAT = 120, 127
# tier statId -> inclusive (low, high); None = open-ended. Verified against ESPN's actual tier indicators.
PA_TIERS: dict[int, tuple[float | None, float | None]] = {
    89: (None, 0), 90: (1, 6), 91: (7, 13), 92: (14, 17), 121: (18, 21), 122: (22, 27), 123: (28, 34),
    124: (35, 45), 125: (46, None)}
YA_TIERS: dict[int, tuple[float | None, float | None]] = {
    128: (None, 99), 129: (100, 199), 130: (200, 299), 131: (300, 349), 132: (350, 399), 133: (400, 449),
    134: (450, 499), 135: (500, 549), 136: (550, None)}
MODELED = set(COUNT_STATS) | {PA_STAT, YA_STAT} | set(PA_TIERS) | set(YA_TIERS)
# Every statId ESPN reports on a D/ST (actual or projected, 2019-2026). A non-zero item outside this set is
# not something a D/ST accrues (it is a player statistic) and is ignored; one inside it but outside MODELED
# (tackles, return yards, the alternative 187-196 points-allowed family, ...) is unmodeled: the model abstains.
DST_UNIVERSE = set(range(89, 138)) | {155, 156, 160} | set(range(187, 197)) | {205, 206, 209, 210}
TD_STATS = (93, 101, 102, 103, 104)


def _num(stats: Mapping, sid: int) -> float:
    v = stats.get(sid, stats.get(str(sid)))
    return float(v) if v is not None else 0.0


def tier_of(value: float, tiers: Mapping[int, tuple]) -> int:
    v = math.floor(value) if value >= 0 else value  # ESPN tiers are integer ranges
    for sid, (lo, hi) in tiers.items():
        if (lo is None or v >= lo) and (hi is None or v <= hi):
            return sid
    raise ValueError(f"{value} falls in no tier")


@dataclass(frozen=True)
class DstScoring:
    weights: dict[int, float]                          # statId -> D/ST-slot points (non-zero only)
    unmodeled: list[dict] = field(default_factory=list)
    ignored: list[dict] = field(default_factory=list)

    @property
    def supported(self) -> bool:
        return bool(self.weights) and not self.unmodeled

    def points(self, stats: Mapping) -> float:
        """League points of one D/ST week (actual) or its expectation (ESPN projected statistics)."""
        return round(sum(w * _num(stats, sid) for sid, w in self.weights.items()), 6)

    def td_points(self, stats: Mapping) -> float:
        return round(sum(self.weights.get(s, 0.0) * _num(stats, s) for s in TD_STATS), 6)

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(sorted(self.weights.items()), separators=(",", ":"))
        return "dst:" + hashlib.sha256(payload.encode()).hexdigest()[:16]

    def describe(self) -> dict:
        return {"supported": self.supported, "fingerprint": self.fingerprint,
                "weights": {str(k): v for k, v in sorted(self.weights.items())},
                "unmodeled": self.unmodeled, "ignored_non_dst_items": len(self.ignored)}


def _slot_value(item: dict) -> float:
    overrides = {str(k): v for k, v in (item.get("pointsOverrides") or {}).items()}
    return float(overrides[DST_SLOT] if DST_SLOT in overrides else (item.get("points") or 0.0))


def resolve_dst_scoring(scoring_items: Sequence[dict] | None) -> DstScoring:
    if not scoring_items:
        return DstScoring(weights={}, unmodeled=[{"stat_id": None, "reason": "no scoring settings synced"}])
    weights, unmodeled, ignored = {}, [], []
    for item in scoring_items:
        sid, value = item.get("statId"), _slot_value(item)
        if not value:
            continue
        if sid not in DST_UNIVERSE:
            ignored.append({"stat_id": sid, "points": value, "reason": "not a D/ST statistic"})
            continue
        if item.get("isReverseItem"):
            unmodeled.append({"stat_id": sid, "points": value, "reason": "reverse scoring item"})
            continue
        weights[sid] = value
        if sid not in MODELED:
            unmodeled.append({"stat_id": sid, "points": value, "reason": "D/ST statistic the model does not project"})
    return DstScoring(weights=weights, unmodeled=unmodeled, ignored=ignored)


# ----------------------------------------------------------------------------- ESPN payloads

TEAM_ALIASES = {"OAK": "LV", "SD": "LAC", "STL": "LA", "LAR": "LA", "WSH": "WAS"}
ESPN_PRO_TEAMS = {1: "ATL", 2: "BUF", 3: "CHI", 4: "CIN", 5: "CLE", 6: "DAL", 7: "DEN", 8: "DET", 9: "GB",
                  10: "TEN", 11: "IND", 12: "KC", 13: "LV", 14: "LA", 15: "MIA", 16: "MIN", 17: "NE", 18: "NO",
                  19: "NYG", 20: "NYJ", 21: "PHI", 22: "ARI", 23: "PIT", 24: "LAC", 25: "SF", 26: "SEA", 27: "TB",
                  28: "WAS", 29: "CAR", 30: "JAX", 33: "BAL", 34: "HOU"}


def norm_team(abbr: str | None) -> str | None:
    return TEAM_ALIASES.get(abbr, abbr) if abbr else abbr


def parse_espn_dst(payload: Mapping, season: int) -> dict[str, dict[tuple[str, int], dict]]:
    """{'actual': {(team, week): stats}, 'espn': {(team, week): projected stats}} from a kona_player_info
    payload of D/STs (statSplitTypeId 1 = one scoring period; statSourceId 0 actual, 1 projected)."""
    out: dict[str, dict] = {"actual": {}, "espn": {}}
    for entry in payload.get("players") or []:
        p = entry.get("player", entry)
        if p.get("defaultPositionId") != 16:
            continue
        team = ESPN_PRO_TEAMS.get(p.get("proTeamId"))
        if team is None:
            continue
        for s in p.get("stats") or []:
            if s.get("seasonId") != season or s.get("statSplitTypeId") != 1 or not s.get("scoringPeriodId"):
                continue
            kind = {0: "actual", 1: "espn"}.get(s.get("statSourceId"))
            if kind and s.get("stats"):
                out[kind][(team, int(s["scoringPeriodId"]))] = {int(k): float(v) for k, v in s["stats"].items()}
    return out


# ----------------------------------------------------------------------------- game table

COMPONENTS = ("sacks", "ints", "fr", "blk", "saf", "ret_td", "pa", "ya")
RATED = COMPONENTS + ("hits", "pts")
RARE = {"blk", "saf", "ret_td"}


def build_games(schedules: Iterable[Mapping], team_week: Iterable[Mapping], espn: Mapping[int, Mapping],
                scoring: DstScoring) -> list[dict]:
    """One row per (season, week, defense) of a regular-season game.

    schedules: nflverse schedule rows (season, week, game_type, home_team, away_team, home_score, away_score,
               spread_line, total_line, location). team_week: nflverse offense rows (season, week, team,
               dropbacks, hit_or_sack). espn: {season: parse_espn_dst(...)}.
    Labels come from ESPN's actual D/ST statistics; a row without them has `final` False."""
    tw = {(int(r["season"]), int(r["week"]), norm_team(r["team"])): r for r in team_week}
    rows = []
    for g in schedules:
        if g.get("game_type") not in (None, "REG"):
            continue
        s, w = int(g["season"]), int(g["week"])
        home, away = norm_team(g["home_team"]), norm_team(g["away_team"])
        neutral = (g.get("location") or "Home") == "Neutral"
        spread, total = g.get("spread_line"), g.get("total_line")
        for team, opp, is_home in ((home, away, True), (away, home, False)):
            act = espn.get(s, {}).get("actual", {}).get((team, w))
            proj = espn.get(s, {}).get("espn", {}).get((team, w))
            off = tw.get((s, w, opp)) or {}
            margin = implied_opp = None
            if spread is not None and total is not None and not (isinstance(spread, float) and math.isnan(spread)):
                margin = float(spread) if is_home else -float(spread)
                implied_opp = (float(total) - margin) / 2
            r = {"season": s, "week": w, "key": s * 100 + w, "team": team, "opp": opp,
                 "home": 0.5 if neutral else float(is_home), "margin": margin, "implied_opp": implied_opp,
                 "final": act is not None and g.get("home_score") is not None,
                 "hits": _f(off.get("hit_or_sack")), "dropbacks": _f(off.get("dropbacks"))}
            if act is not None:
                r.update(_components(act))
                r["points"] = scoring.points(act)
                r["td_points"] = scoring.td_points(act)
                r["pts"] = r["points"]
                r["any_td"] = float(sum(_num(act, t) for t in TD_STATS) > 0)
                r["pa_tier"], r["ya_tier"] = tier_of(r["pa"], PA_TIERS), tier_of(r["ya"], YA_TIERS)
                r["espn_pa_tier"] = next((t for t in PA_TIERS if _num(act, t) >= 0.5), None)
                r["espn_ya_tier"] = next((t for t in YA_TIERS if _num(act, t) >= 0.5), None)
            if proj is not None:
                r["espn_points"] = scoring.points(proj)
                r["espn_td_points"] = scoring.td_points(proj)
                r["espn_lambda_td"] = sum(_num(proj, t) for t in TD_STATS)
                r["espn_p_pa_tier"] = {t: _num(proj, t) for t in PA_TIERS}
                r["espn_p_ya_tier"] = {t: _num(proj, t) for t in YA_TIERS}
                for c, v in _components(proj).items():
                    r["espn_" + c] = v
            rows.append(r)
    rows.sort(key=lambda r: (r["key"], r["team"]))
    return rows


def _f(v):
    return None if v is None or (isinstance(v, float) and math.isnan(v)) else float(v)


def _components(stats: Mapping) -> dict:
    return {"sacks": _num(stats, 99), "ints": _num(stats, 95), "fr": _num(stats, 96), "blk": _num(stats, 97),
            "saf": _num(stats, 98), "ret_td": _num(stats, 101) + _num(stats, 102), "pa": _num(stats, PA_STAT),
            "ya": _num(stats, YA_STAT), "blk_td": _num(stats, 93), "kr_td": _num(stats, 101),
            "pr_td": _num(stats, 102), "int_td": _num(stats, 103), "fr_td": _num(stats, 104),
            "ret2": _num(stats, 206), "saf1": _num(stats, 209)}


# ----------------------------------------------------------------------------- ratings


@dataclass(frozen=True)
class DConfig:
    half_life: float = 8.0          # weeks
    offseason_gap: float = 12.0     # extra weeks of age for each offseason crossed
    k: float = 6.0                  # pseudo-games of league average in every rating
    rare_k_mult: float = 4.0        # extra shrinkage for blocked kicks, safeties, return TDs
    max_age: float = 70.0           # weeks; older games are dropped
    iterations: int = 3             # alternating defense / offense passes
    opponent_adjust: bool = True    # False: a past game's expectation ignores who the opponent was (ablation)
    bw_pa: float = 2.0              # kernel bandwidths of the tier distributions (points / yards of predicted mean)
    bw_ya: float = 20.0
    market: bool = True             # closing line features in the stack (one-week horizon only)
    pressure: bool = False          # QB hits + sacks rating as an extra feature for sacks / INT / FR
    ratings: bool = True            # False: the market stack uses the line alone (ablation)

    def to_dict(self) -> dict:
        return asdict(self)


def _age(cut_key: int, s: np.ndarray, w: np.ndarray, cfg: DConfig) -> np.ndarray:
    cs, cw = divmod(cut_key, 100)
    return (cs - s) * (WEEKS_PER_SEASON + cfg.offseason_gap) + (cw - w)


class Ratings:
    """Ratings of every team at one information cutoff (games with key <= cut_key)."""

    def __init__(self, hist: dict[str, np.ndarray], teams: Sequence[str], cut_key: int, cfg: DConfig):
        self.teams = {t: i for i, t in enumerate(teams)}
        n = len(teams)
        age = _age(cut_key, hist["season"], hist["week"], cfg)
        m = (hist["key"] <= cut_key) & (age <= cfg.max_age)
        wts = np.power(0.5, age[m] / cfg.half_life)
        di, oi, home = hist["d_idx"][m], hist["o_idx"][m], hist["home"][m]
        self.mu, self.def_, self.off, self.home_adj, self.td_rate = {}, {}, {}, {}, {}
        self.n_games = int(m.sum())
        for x in RATED:
            v = hist[x][m]
            ok = ~np.isnan(v)
            if ok.sum() == 0:
                self.mu[x], self.def_[x], self.off[x], self.home_adj[x] = np.nan, np.ones(n), np.ones(n), (1.0, 1.0)
                continue
            ww, vv, dd, oo, hh = wts[ok], v[ok], di[ok], oi[ok], home[ok]
            mu = float(np.sum(ww * vv) / np.sum(ww))
            hw = ww * hh
            aw = ww * (1 - hh)
            h_home = (np.sum(hw * vv) / np.sum(hw) / mu) if mu > 0 and np.sum(hw) > 0 else 1.0
            h_away = (np.sum(aw * vv) / np.sum(aw) / mu) if mu > 0 and np.sum(aw) > 0 else 1.0
            hadj = h_away + (h_home - h_away) * hh
            k = cfg.k * (cfg.rare_k_mult if x in RARE else 1.0)
            d, o = np.ones(n), np.ones(n)
            if mu > 0:
                for _ in range(max(cfg.iterations, 1)):
                    exp_d = mu * (o[oo] if cfg.opponent_adjust else 1.0) * hadj
                    d = (np.bincount(dd, ww * vv, n) + k * mu) / (np.bincount(dd, ww * exp_d, n) + k * mu)
                    exp_o = mu * (d[dd] if cfg.opponent_adjust else 1.0) * hadj
                    o = (np.bincount(oo, ww * vv, n) + k * mu) / (np.bincount(oo, ww * exp_o, n) + k * mu)
            self.mu[x], self.def_[x], self.off[x], self.home_adj[x] = mu, d, o, (h_home, h_away)
        for td, base in (("int_td", "ints"), ("fr_td", "fr"), ("blk_td", "blk"), ("ret2", None), ("saf1", None)):
            num = np.nansum(wts * hist[td][m])
            den = np.nansum(wts * hist[base][m]) if base else np.sum(wts[~np.isnan(hist[td][m])])
            self.td_rate[td] = float(num / den) if den > 0 else 0.0
        # share of return TDs that are kickoff returns (the rest are punt returns), so a league that scores
        # 101 and 102 differently gets the observed mix; 0.5 with no return TD in the window
        kr, pr = np.nansum(wts * hist["kr_td"][m]), np.nansum(wts * hist["pr_td"][m])
        self.td_rate["kr_share"] = float(kr / (kr + pr)) if kr + pr > 0 else 0.5

    def predict(self, x: str, team: str, opp: str, home: float) -> float:
        d, o = self.teams.get(team), self.teams.get(opp)
        mu = self.mu[x]
        if mu is None or np.isnan(mu):
            return float("nan")
        h_home, h_away = self.home_adj[x]
        return float(mu * (self.def_[x][d] if d is not None else 1.0) * (self.off[x][o] if o is not None else 1.0)
                     * (h_away + (h_home - h_away) * home))

    def team_only(self, x: str, team: str, side: str) -> float:
        i = self.teams.get(team)
        r = (self.def_ if side == "def" else self.off)[x]
        return float(self.mu[x] * (r[i] if i is not None else 1.0))


def history_arrays(rows: Sequence[dict], teams: Sequence[str]) -> dict[str, np.ndarray]:
    idx = {t: i for i, t in enumerate(teams)}
    fin = [r for r in rows if r["final"]]
    out = {"season": np.array([r["season"] for r in fin], dtype=float),
           "week": np.array([r["week"] for r in fin], dtype=float),
           "key": np.array([r["key"] for r in fin], dtype=float),
           "d_idx": np.array([idx[r["team"]] for r in fin], dtype=int),
           "o_idx": np.array([idx[r["opp"]] for r in fin], dtype=int),
           "home": np.array([r["home"] for r in fin], dtype=float)}
    for x in RATED + ("int_td", "fr_td", "blk_td", "ret2", "saf1", "kr_td", "pr_td"):
        out[x] = np.array([np.nan if r.get(x) is None else r[x] for r in fin], dtype=float)
    return out


def cutoff_key(season: int, week: int, horizon: int) -> int:
    """Information cutoff for projecting (season, week) `horizon` weeks ahead: games through week - horizon of
    the same season (week 0 = only earlier seasons). Horizon 1 = the usual next-week projection."""
    return season * 100 + max(week - horizon, 0)


def rating_features(rows: Sequence[dict], hist: dict, teams: Sequence[str], cfg: DConfig,
                    horizon: int, cache: dict | None = None) -> list[dict]:
    """Point-in-time rating features for every row (no information from the row's week or later)."""
    cache = {} if cache is None else cache
    out = []
    for r in rows:
        ck = cutoff_key(r["season"], r["week"], horizon)
        rt = cache.get(ck)
        if rt is None:
            rt = cache[ck] = Ratings(hist, teams, ck, cfg)
        f = {f"r_{x}": rt.predict(x, r["team"], r["opp"], r["home"]) for x in RATED}
        f["naive_team"] = rt.team_only("pts", r["team"], "def")
        f["opp_allowed"] = rt.team_only("pts", r["opp"], "off")
        f.update({f"tau_{k}": v for k, v in rt.td_rate.items()})
        f["history_games"] = rt.n_games
        out.append(f)
    return out


# ----------------------------------------------------------------------------- stack + distributions


def _design(rows, feats, x: str, market: bool, pressure: bool, ratings: bool = True) -> np.ndarray:
    cols = [np.ones(len(rows))]
    if ratings or not market:
        cols.append(np.array([f[f"r_{x}"] for f in feats]))
    if pressure and x in ("sacks", "ints", "fr"):
        h = np.array([f["r_hits"] for f in feats], dtype=float)
        # a game without nflverse charting keeps the league-average pressure rating
        cols.append(np.where(np.isnan(h), np.nanmean(h) if (~np.isnan(h)).any() else 0.0, h))
    if market:
        cols.append(np.array([r["implied_opp"] for r in rows], dtype=float))
        cols.append(np.array([r["margin"] for r in rows], dtype=float))
    return np.column_stack(cols)


def _fit_ls(X: np.ndarray, y: np.ndarray, ridge: float = 1e-3) -> np.ndarray:
    ok = ~np.isnan(X).any(axis=1) & ~np.isnan(y)
    X, y = X[ok], y[ok]
    A = X.T @ X + ridge * np.eye(X.shape[1]) * len(y)
    A[0, 0] -= ridge * len(y)  # no penalty on the intercept
    return np.linalg.solve(A, X.T @ y)


def _kernel_tiers(mu_train: np.ndarray, y_train: np.ndarray, mu: np.ndarray, tiers: Mapping, bw: float) -> np.ndarray:
    """P(tier) for each predicted mean: kernel-weighted empirical distribution of earlier outcomes whose
    predicted mean was close (rows x tiers)."""
    ok = ~np.isnan(mu_train) & ~np.isnan(y_train)
    mt, yt = mu_train[ok], y_train[ok]
    onehot = np.zeros((len(yt), len(tiers)))
    keys = list(tiers)
    for j, t in enumerate(yt):
        onehot[j, keys.index(tier_of(t, tiers))] = 1.0
    out = np.full((len(mu), len(tiers)), np.nan)
    for start in range(0, len(mu), 256):
        m = mu[start:start + 256]
        w = np.exp(-0.5 * ((m[:, None] - mt[None, :]) / bw) ** 2)
        w[np.isnan(m)] = 0
        s = w.sum(axis=1, keepdims=True)
        out[start:start + 256] = np.where(s > 0, (w @ onehot) / np.where(s > 0, s, 1), np.nan)
    return out


def project_season(train_rows, train_feats, rows, feats, scoring: DstScoring, cfg: DConfig) -> list[dict]:
    """E[component], P(tier) and expected league points for `rows`, fitted only on `train_rows` (earlier
    seasons, final, with their own point-in-time features). Rows without a closing line fall back to the
    ratings-only stack."""
    train = [(r, f) for r, f in zip(train_rows, train_feats) if r["final"]]
    tr, tf = [t[0] for t in train], [t[1] for t in train]
    has_line = np.array([r["implied_opp"] is not None for r in rows])
    tr_line = [i for i, r in enumerate(tr) if r["implied_opp"] is not None]
    use_market = has_line & bool(cfg.market and tr_line)
    est: dict[str, np.ndarray] = {}
    fitted: dict[str, tuple[np.ndarray, np.ndarray | None]] = {}   # train means: (ratings-only, market)
    for x in COMPONENTS:
        floor = 0.0 if x in ("pa", "ya") else 1e-4
        y = np.array([r[x] for r in tr], dtype=float)
        coef0 = _fit_ls(_design(tr, tf, x, False, cfg.pressure), y)
        pred = _design(rows, feats, x, False, cfg.pressure) @ coef0
        fit0, fit1 = np.maximum(_design(tr, tf, x, False, cfg.pressure) @ coef0, floor), None
        if use_market.any():
            sub_r, sub_f = [tr[i] for i in tr_line], [tf[i] for i in tr_line]
            coef1 = _fit_ls(_design(sub_r, sub_f, x, True, cfg.pressure, cfg.ratings), y[tr_line])
            lr = [r for r, h in zip(rows, use_market) if h]
            lf = [f for f, h in zip(feats, use_market) if h]
            pred[use_market] = _design(lr, lf, x, True, cfg.pressure, cfg.ratings) @ coef1
            fit1 = np.maximum(_design(sub_r, sub_f, x, True, cfg.pressure, cfg.ratings) @ coef1, floor)
        est[x] = np.maximum(pred, floor)
        fitted[x] = (fit0, fit1)
    # P(tier | predicted mean) is learned from the SAME predictor's earlier means: the market stack's are sharper
    probs = {}
    for x, tiers, bw in (("pa", PA_TIERS, cfg.bw_pa), ("ya", YA_TIERS, cfg.bw_ya)):
        y = np.array([r[x] for r in tr], dtype=float)
        p = _kernel_tiers(fitted[x][0], y, est[x], tiers, bw)
        if use_market.any():
            p[use_market] = _kernel_tiers(fitted[x][1], y[tr_line], est[x][use_market], tiers, bw)
        probs[x] = p
    pa_p, ya_p = probs["pa"], probs["ya"]
    w = scoring.weights
    out = []
    for i, (r, f) in enumerate(zip(rows, feats)):
        e = {x: float(est[x][i]) for x in COMPONENTS}
        stats = {99: e["sacks"], 100: 2 * e["sacks"], 95: e["ints"], 96: e["fr"], 97: e["blk"], 98: e["saf"],
                 103: e["ints"] * f["tau_int_td"], 104: e["fr"] * f["tau_fr_td"], 93: e["blk"] * f["tau_blk_td"],
                 206: f["tau_ret2"], 209: f["tau_saf1"], PA_STAT: e["pa"], YA_STAT: e["ya"]}
        # return TDs: split the team's KR+PR expectation by the league's observed KR/PR mix
        stats[101] = e["ret_td"] * f["tau_kr_share"]
        stats[102] = e["ret_td"] * (1 - f["tau_kr_share"])
        for j, t in enumerate(PA_TIERS):
            stats[t] = float(pa_p[i, j])
        for j, t in enumerate(YA_TIERS):
            stats[t] = float(ya_p[i, j])
        lam_td = sum(stats[t] for t in TD_STATS)
        pts = scoring.points(stats)
        out.append({"pred": pts, "pred_td_points": scoring.td_points(stats), "lambda_td": lam_td,
                    "p_any_td": 1 - math.exp(-lam_td), **{"e_" + k: v for k, v in e.items()},
                    "p_pa_tier": {t: float(pa_p[i, j]) for j, t in enumerate(PA_TIERS)},
                    "p_ya_tier": {t: float(ya_p[i, j]) for j, t in enumerate(YA_TIERS)},
                    "unweighted_stats": [s for s in w if s not in stats],
                    # what `pts` scored: any league's D/ST rules applied to it give that league's points
                    "scoring_stats": {str(k): v for k, v in stats.items()}})
    return out


# ----------------------------------------------------------------------------- metrics


def spearman(a: np.ndarray, b: np.ndarray) -> float:
    if len(a) < 3:
        return float("nan")
    ra, rb = _rank(a), _rank(b)
    if ra.std() == 0 or rb.std() == 0:
        return float("nan")
    return float(np.corrcoef(ra, rb)[0, 1])


def _rank(v: np.ndarray) -> np.ndarray:
    order = np.argsort(v, kind="mergesort")
    ranks = np.empty(len(v))
    ranks[order] = np.arange(len(v))
    # average ties
    vals = v[order]
    i = 0
    while i < len(v):
        j = i
        while j + 1 < len(v) and vals[j + 1] == vals[i]:
            j += 1
        ranks[order[i:j + 1]] = (i + j) / 2
        i = j + 1
    return ranks


def pairwise_accuracy(pred: np.ndarray, actual: np.ndarray) -> tuple[int, int]:
    """Correctly ordered pairs (actual ties and prediction ties skipped) within one week."""
    dp = np.sign(pred[:, None] - pred[None, :])
    da = np.sign(actual[:, None] - actual[None, :])
    mask = np.triu(np.ones_like(dp, dtype=bool), 1) & (dp != 0) & (da != 0)
    return int((dp[mask] == da[mask]).sum()), int(mask.sum())


def evaluate(rows: Sequence[dict], methods: Mapping[str, str], *, top_k=(3, 5, 10), resamples: int = 2000,
             seed: int = 7, baseline: str | None = None) -> dict:
    """MAE / RMSE / bias (actual - pred) and within-week ranking metrics for each method column on the rows
    where every method has a prediction (common support). Week-block bootstrap CIs for MAE and Spearman
    differences against `baseline`."""
    rows = [r for r in rows if r.get("points") is not None and all(r.get(c) is not None and not
                                                                   (isinstance(r[c], float) and math.isnan(r[c]))
                                                                   for c in methods.values())]
    out: dict = {"n": len(rows), "weeks": len({r["key"] for r in rows}), "methods": {}}
    if not rows:
        return out
    y = np.array([r["points"] for r in rows])
    weeks = sorted({r["key"] for r in rows})
    widx = {k: np.array([i for i, r in enumerate(rows) if r["key"] == k]) for k in weeks}
    per_week: dict[str, dict] = {}
    for name, col in methods.items():
        p = np.array([r[col] for r in rows], dtype=float)
        e = y - p
        wk_mae = np.array([np.mean(np.abs(e[ix])) for ix in widx.values()])
        wk_rho = np.array([spearman(p[ix], y[ix]) for ix in widx.values()])
        corr = tot = 0
        tops = {k: [] for k in top_k}
        for ix in widx.values():
            c, t = pairwise_accuracy(p[ix], y[ix])
            corr, tot = corr + c, tot + t
            order = ix[np.argsort(-p[ix], kind="mergesort")]
            for k in top_k:
                tops[k].append(float(np.mean(y[order[:k]])))
        per_week[name] = {"mae": wk_mae, "rho": wk_rho, "n": np.array([len(ix) for ix in widx.values()])}
        out["methods"][name] = {
            "mae": round(float(np.mean(np.abs(e))), 4), "rmse": round(float(np.sqrt(np.mean(e ** 2))), 4),
            "bias_actual_minus_pred": round(float(np.mean(e)), 4), "mean_pred": round(float(np.mean(p)), 4),
            "within_week_spearman": round(float(np.nanmean(wk_rho)), 4),
            "pairwise_order_accuracy": round(corr / tot, 4) if tot else None,
            "top_k_actual_mean": {str(k): round(float(np.mean(v)), 4) for k, v in tops.items()}}
    out["actual_mean"] = round(float(np.mean(y)), 4)
    out["oracle_top_k_actual_mean"] = {str(k): round(float(np.mean([np.mean(np.sort(y[ix])[::-1][:k])
                                                                    for ix in widx.values()])), 4) for k in top_k}
    if baseline and baseline in per_week:
        rng = np.random.default_rng(seed)
        nw = len(weeks)
        draws = rng.integers(0, nw, size=(resamples, nw))
        b = per_week[baseline]
        out["vs_" + baseline] = {}
        for name, pw in per_week.items():
            if name == baseline:
                continue
            dm = (pw["mae"] - b["mae"]) * pw["n"]
            dr = pw["rho"] - b["rho"]
            mae_d = [float(dm[d].sum() / pw["n"][d].sum()) for d in draws]
            rho_d = [float(np.nanmean(dr[d])) for d in draws]
            out["vs_" + baseline][name] = {
                "mae_diff": round(float(dm.sum() / pw["n"].sum()), 4),
                "mae_diff_ci90": [round(float(np.quantile(mae_d, q)), 4) for q in (0.05, 0.95)],
                "spearman_diff": round(float(np.nanmean(dr)), 4),
                "spearman_diff_ci90": [round(float(np.quantile(rho_d, q)), 4) for q in (0.05, 0.95)]}
    return out


def residual_bands(pred: np.ndarray, actual: np.ndarray, n_bands: int = 4, coverage: float = 0.8) -> dict:
    qs = np.quantile(pred, np.linspace(0, 1, n_bands + 1)[1:-1]) if len(pred) else []
    edges = [-np.inf, *[float(q) for q in qs], np.inf]
    lo_q, hi_q = (1 - coverage) / 2, 1 - (1 - coverage) / 2
    bands = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (pred >= lo) & (pred < hi)
        r = actual[m] - pred[m]
        if len(r) >= 30:
            bands.append({"low": lo, "high": hi, "q_low": float(np.quantile(r, lo_q)),
                          "q_high": float(np.quantile(r, hi_q)), "n": int(m.sum())})
    return {"nominal_coverage": coverage, "bands": bands}


def coverage(bands: dict, pred: np.ndarray, actual: np.ndarray) -> dict:
    inside = n = 0
    for p, a in zip(pred, actual):
        b = next((b for b in bands["bands"] if b["low"] <= p < b["high"]), None)
        if b is None:
            continue
        n += 1
        inside += p + b["q_low"] <= a <= p + b["q_high"]
    return {"n": n, "empirical": round(inside / n, 4) if n else None, "nominal": bands["nominal_coverage"]}


def td_calibration(p: np.ndarray, y: np.ndarray, bins=(0, 0.1, 0.15, 0.2, 0.25, 0.3, 1.0)) -> dict:
    """Brier score, log loss and a reliability table for P(at least one D/ST TD)."""
    p = np.clip(p, 1e-6, 1 - 1e-6)
    table = []
    for lo, hi in zip(bins[:-1], bins[1:]):
        m = (p >= lo) & (p < hi)
        if m.sum():
            table.append({"bin": [lo, hi], "n": int(m.sum()), "mean_p": round(float(p[m].mean()), 4),
                          "observed": round(float(y[m].mean()), 4)})
    return {"brier": round(float(np.mean((p - y) ** 2)), 5),
            "log_loss": round(float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p))), 5),
            "mean_p": round(float(p.mean()), 4), "observed_rate": round(float(y.mean()), 4), "reliability": table}


def tier_calibration(probs: Sequence[Mapping[int, float]], actual_tiers: Sequence[int], tiers: Mapping) -> dict:
    """Mean predicted P(tier) vs observed frequency, and the multiclass log loss."""
    P = np.array([[pr[t] for t in tiers] for pr in probs])
    Y = np.array([[float(a == t) for t in tiers] for a in actual_tiers])
    ll = float(-np.mean(np.log(np.clip((P * Y).sum(axis=1), 1e-6, 1))))
    return {"log_loss": round(ll, 4),
            "by_tier": {str(t): {"mean_p": round(float(P[:, j].mean()), 4), "observed": round(float(Y[:, j].mean()), 4)}
                        for j, t in enumerate(tiers)}}


def replace_cfg(cfg: DConfig, **kw) -> DConfig:
    return replace(cfg, **kw)
