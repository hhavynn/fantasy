"""Ask Fantasy Intel (issue #219, contract ask/v1): a deterministic question parser over an allowlisted grammar.

A question either compiles to exactly one typed intent with bounded slots (positions, screener metric keys, comparison
operators, numbers inside each metric's bounds, weeks, a "last N" window, player names to resolve), or it does not
compile and the caller abstains. Nothing here reads a database, calls an endpoint or builds SQL or a request path:
app/api/ask.py resolves the slots (players, the team in view) and calls one existing handler with typed arguments.

The grammar is anchored: every template must match the whole normalized question, and every slot must validate, or
the next template is tried; when none does, the question is unsupported. Leftover words are never ignored, so a
question that adds an unsupported condition ("... who play for the Chiefs") abstains instead of being answered as if
the condition were not there. No model is involved (none is needed or used): the same question always gives the same
parse."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from types import MappingProxyType

from .screener import METRIC_BY_KEY, PRESETS

CONTRACT = "ask/v1"
MAX_CHARS = 200
MAX_WORDS = 40          # both bounds apply to the raw question and to its normalized text
MAX_NAME_CHARS = 40

# -------------------------------------------------------------------------------------------------- outcomes --


@dataclass(frozen=True)
class PlayerRef:
    """A player named in the question, to resolve against the league's player pool. `qualifier` narrows same-name
    players: a position, an NFL team abbreviation, or a league player id, as the clarification suggests them."""
    text: str
    position: str | None = None
    team: str | None = None
    player_id: int | None = None

    def ask_text(self) -> str:
        return self.text


@dataclass(frozen=True)
class Parsed:
    intent: str
    slots: MappingProxyType = field(default_factory=lambda: MappingProxyType({}))


@dataclass(frozen=True)
class Unsupported:
    """No answer: `code` says why (unsupported, unknown_metric, out_of_bounds, other_league, other_season, ...)."""
    code: str
    reason: str
    clarify: str | None = None        # a specific question back to the user, when one would resolve it


# ----------------------------------------------------------------------------------------------- vocabulary --

INTENTS = MappingProxyType({
    "screen": "Player screen",
    "screen_preset": "Saved player screen",
    "waivers_best": "Best available players",
    "start_sit": "Start/Sit check",
    "replacement": "Best legal replacement",
    "start_choice": "Start one player or the other",
    "matchup": "This week's matchup",
    "player": "Player snapshot",
    "ownership": "Roster status",
    "compare": "Compare two players",
    "trade_value": "Trade value",
    "trade_values_top": "Top trade values",
    "byes": "Bye weeks",
    "playoffs": "Playoff race",
})

# Supported question shapes, shown when a question abstains (one per intent).
EXAMPLES = (
    "WRs with a target share above 25% over the last 3 games",
    "RBs with backfield share at least 60 and targets per game at least 3",
    "Workhorse backs",
    "Who is the best available RB?",
    "Who should I start this week?",
    "Who is the best replacement for Papa WR?",
    "Should I start Papa WR or Quebec WR?",
    "Who am I playing this week?",
    "How is Papa WR doing?",
    "Is Waiver RB available?",
    "Compare Papa WR and Quebec WR",
    "What is Papa WR's trade value?",
    "Top trade values at WR",
    "Who is on bye this week?",
    "What does my playoff race look like?",
)

POSITION_WORDS = MappingProxyType({
    "wr": ("WR",), "wrs": ("WR",), "wide receiver": ("WR",), "wide receivers": ("WR",), "receiver": ("WR",),
    "receivers": ("WR",),
    "rb": ("RB",), "rbs": ("RB",), "running back": ("RB",), "running backs": ("RB",), "back": ("RB",),
    "backs": ("RB",),
    "te": ("TE",), "tes": ("TE",), "tight end": ("TE",), "tight ends": ("TE",),
    "qb": ("QB",), "qbs": ("QB",), "quarterback": ("QB",), "quarterbacks": ("QB",),
    "k": ("K",), "ks": ("K",), "kicker": ("K",), "kickers": ("K",),
    "d/st": ("D/ST",), "dst": ("D/ST",), "defense": ("D/ST",), "defenses": ("D/ST",), "def": ("D/ST",),
    "pass catcher": ("WR", "TE"), "pass catchers": ("WR", "TE"),
    "player": (), "players": (), "skill player": (), "skill players": (), "flex player": (), "flex players": (),
})
_POS_ALT = "|".join(sorted((re.escape(w) for w in POSITION_WORDS), key=len, reverse=True))
SCREEN_POSITIONS = frozenset({"WR", "RB", "TE"})

# Screener metric synonyms -> registry key. Only names that mean exactly the registry metric; "points per game" alone
# is not here (the metric is full PPR, whatever the league scores), it asks for "PPR points per game" instead.
METRIC_WORDS = MappingProxyType({
    "snap share": "snap_share", "snap %": "snap_share", "snap percentage": "snap_share", "snap rate": "snap_share",
    "snaps per game": "snaps_pg", "snaps a game": "snaps_pg", "offensive snaps per game": "snaps_pg",
    "route share": "route_participation", "route participation": "route_participation", "route %": "route_participation",
    "route rate": "route_participation", "routes per game": "routes_pg", "routes a game": "routes_pg",
    "targets per game": "targets_pg", "targets a game": "targets_pg", "targets/g": "targets_pg",
    "tgt/g": "targets_pg", "target share": "target_share", "tgt %": "target_share", "targets share": "target_share",
    "air yard share": "air_yards_share", "air yards share": "air_yards_share", "air-yard share": "air_yards_share",
    "team rush share": "team_rush_share", "rush share": "team_rush_share", "backfield share": "backfield_share",
    "carries per game": "carries_pg", "carries a game": "carries_pg",
    "opportunities per game": "opportunities_pg", "opportunities a game": "opportunities_pg",
    "carries plus targets per game": "opportunities_pg",
    "red zone opportunities per game": "rz_opportunities_pg", "red-zone opportunities per game": "rz_opportunities_pg",
    "red zone targets per game": "rz_targets_pg", "red-zone targets per game": "rz_targets_pg",
    "red zone carries per game": "rz_carries_pg", "red-zone carries per game": "rz_carries_pg",
    "xfp per game": "xfp_pg", "xfp/g": "xfp_pg", "expected points per game": "xfp_pg",
    "expected fantasy points per game": "xfp_pg",
    "ppr points per game": "ppr_pg", "ppr per game": "ppr_pg", "ppr/g": "ppr_pg",
    "fpoe per game": "fpoe_pg", "fpoe": "fpoe_pg", "points over expected per game": "fpoe_pg",
    "points vs xfp per game": "fpoe_pg",
    # plurals ("route shares above 80%")
    "snap shares": "snap_share", "route shares": "route_participation", "target shares": "target_share",
    "air yard shares": "air_yards_share", "air yards shares": "air_yards_share", "rush shares": "team_rush_share",
    "backfield shares": "backfield_share",
})
_METRIC_ALT = "|".join(sorted((re.escape(w) for w in METRIC_WORDS), key=len, reverse=True))
# Names users reach for that the screener does not have, or does not have under that meaning: abstain, never guess.
VAGUE_METRICS = MappingProxyType({
    "points per game": "Say \"PPR points per game\": the screener's points are full PPR whatever this league scores.",
    "fantasy points per game": "Say \"PPR points per game\": the screener's points are full PPR whatever this "
                               "league scores.",
})
_VAGUE_ALT = "|".join(sorted((re.escape(w) for w in VAGUE_METRICS), key=len, reverse=True))

OPS = MappingProxyType({
    "above": "gt", "over": "gt", "more than": "gt", "greater than": "gt", "higher than": "gt", "exceeding": "gt",
    ">": "gt", "at least": "gte", ">=": "gte", "no less than": "gte", "of at least": "gte",
    "below": "lt", "under": "lt", "less than": "lt", "fewer than": "lt", "lower than": "lt", "<": "lt",
    "at most": "lte", "<=": "lte", "no more than": "lte", "of at most": "lte",
})
_OP_ALT = "|".join(sorted((re.escape(w) for w in OPS), key=len, reverse=True))
_NUM = r"-?\d{1,4}(?:\.\d{1,2})?"
SMALL_NUMBERS = MappingProxyType({"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
                                  "eight": 8})
_SMALL = r"\d{1,2}|" + "|".join(SMALL_NUMBERS)

PRESET_WORDS = MappingProxyType({
    "workhorse backs": "rb_workhorse", "workhorse rbs": "rb_workhorse", "workhorse running backs": "rb_workhorse",
    "target earners": "wr_te_target_earners", "full-time tight ends": "te_full_time",
    "full time tight ends": "te_full_time", "full-time tes": "te_full_time", "full time tes": "te_full_time",
    "red-zone roles": "red_zone_roles", "red zone roles": "red_zone_roles",
    "volume without the points": "unlucky_volume", "unlucky volume": "unlucky_volume",
    "available with a real role": "available_upside", "available players with a real role": "available_upside",
    "wr routes and targets": "wr_routes_targets_last3",
})
PRESET_BY_KEY = MappingProxyType({p["key"]: p for p in PRESETS})
_PRESET_ALT = "|".join(sorted((re.escape(w) for w in PRESET_WORDS), key=len, reverse=True))

# ----------------------------------------------------------------------------------------------- normalizing --

_DASHES = dict.fromkeys(map(ord, "‐‑‒–—−"), "-")
_QUOTES = dict.fromkeys(map(ord, "‘’‛′"), "'") | dict.fromkeys(map(ord, "“”"), '"')
_SYMBOLS = {ord("≥"): " >= ", ord("≤"): " <= "}


def normalize(question: str) -> str:
    """NFKC, control characters dropped, dashes, quotes and >= / <= unified, case-folded, whitespace collapsed, and
    trailing punctuation removed. Casefolding is what makes the grammar case-blind; names keep their letters."""
    q = unicodedata.normalize("NFKC", question).translate(_DASHES).translate(_QUOTES).translate(_SYMBOLS)
    q = "".join(ch if unicodedata.category(ch)[0] != "C" else " " for ch in q)
    q = re.sub(r"\s+", " ", q.casefold()).strip()
    q = re.sub(r"[\s?!.]+$", "", q)
    q = re.sub(r"^(?:(?:hey|hi|ok|okay|so|please|pls),? )+", "", q)
    q = re.sub(r"^(?:(?:can|could|would) you |(?:please )?(?:tell|show|give|find|list|get) (?:me )?|i want to know |"
               r"i'd like to know |do you know )", "", q)
    q = re.sub(r",? (?:please|thanks|thank you)$", "", q)
    return q.strip()


def fold(text: str) -> str:
    """Name matching key: accents stripped, case-folded, punctuation (. ' -) dropped, whitespace collapsed. The same
    fold is applied to the league's player names (app/api/ask.py)."""
    t = unicodedata.normalize("NFKD", text)
    t = "".join(ch for ch in t if not unicodedata.combining(ch)).casefold()
    t = re.sub(r"[.'`’]", "", t).replace("-", " ")
    return re.sub(r"\s+", " ", t).strip()


# --------------------------------------------------------------------------------------------- slot parsers --

_NAME_OK = re.compile(r"[a-zÀ-ɏ][a-zÀ-ɏ.'/\- ]*[a-zÀ-ɏ.]", re.IGNORECASE)
_NAME_WORDS_BLOCKED = frozenset({
    "my", "i", "me", "the", "team", "lineup", "week", "who", "what", "which", "should", "start", "sit", "with", "and",
    "or", "league", "players", "matchup", "it", "him", "he", "she", "her", "they", "them", "this", "that", "next",
    "last", "is", "are",
})
_QUALIFIER = re.compile(r"^(?P<name>.+?)\s*\((?P<q>[^()]{1,24})\)$")
NFL_TEAMS = frozenset({
    "ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE", "DAL", "DEN", "DET", "GB", "HOU", "IND", "JAX", "KC",
    "LAC", "LAR", "LV", "MIA", "MIN", "NE", "NO", "NYG", "NYJ", "PHI", "PIT", "SEA", "SF", "TB", "TEN", "WAS", "FA",
})


def player_ref(text: str) -> PlayerRef | None:
    """A player slot: a name of letters, spaces, periods, apostrophes, hyphens and a slash (D/ST) (at most
    MAX_NAME_CHARS, at most five words, no grammar words), optionally followed by a qualifier in parentheses: a position, an NFL team, both
    ("WR, PHI"), or "id 1234". None when the text is not a name: the template does not apply."""
    text = text.strip().strip('"').strip()
    pos = team = pid = None
    m = _QUALIFIER.match(text)
    if m:
        text = m.group("name").strip()
        for part in (p.strip() for p in m.group("q").split(",")):
            up = part.upper()
            idm = re.fullmatch(r"(?:id|#)\s*(-?\d{1,9})", part)
            if idm:
                pid = int(idm.group(1))
            elif up in {"QB", "RB", "WR", "TE", "K", "D/ST", "DST"}:
                pos = "D/ST" if up == "DST" else up
            elif up in NFL_TEAMS:
                team = up
            else:
                return None
    if not text or len(text) > MAX_NAME_CHARS or not _NAME_OK.fullmatch(text):
        return None
    words = text.split()
    if len(words) > 5 or any(w in _NAME_WORDS_BLOCKED for w in words) or text in POSITION_WORDS:
        return None
    return PlayerRef(text=text, position=pos, team=team, player_id=pid)


def positions(text: str | None) -> tuple[str, ...] | None:
    """'wrs and tes', 'wr/te', 'rbs, wrs or tes' -> ('WR', 'TE'); None when any word is not a position."""
    if text is None:
        return ()
    out: list[str] = []
    for part in re.split(r"\s*(?:,|/|\band\b|\bor\b|&)\s*", text.strip()):
        if not part:
            continue
        if part not in POSITION_WORDS:
            return None
        for p in POSITION_WORDS[part]:
            if p not in out:
                out.append(p)
    return tuple(out)


def small_number(text: str) -> int:
    return SMALL_NUMBERS[text] if text in SMALL_NUMBERS else int(text)


_WEEK = r"(?:(?:in |for |during )?(?P<week>this week|next week|(?:in )?week (?P<wk>\d{1,2})))"


def week_slot(m: re.Match) -> object:
    """None (the league's current week), "next", or a week number (bounds are checked by the handler: 422)."""
    w = m.groupdict().get("week")
    if not w or w == "this week":
        return None
    if w == "next week":
        return "next"
    return int(m.group("wk"))


# ---------------------------------------------------------------------------------------------------- screen --

_COND_FORWARD = re.compile(rf"(?:an? |the |their |his )?(?P<metric>{_METRIC_ALT})(?: (?:of|at|is|are|that is))?"
                           rf" (?P<op>{_OP_ALT}) (?P<num>{_NUM}) ?(?P<pct>%|percent|pct)?")
_COND_REVERSE = re.compile(rf"(?P<op>{_OP_ALT}) (?P<num>{_NUM}) ?(?P<pct>%|percent|pct)? (?:of )?(?:an? |the |their )?"
                           rf"(?P<metric>{_METRIC_ALT})")
_COND_PLUS = re.compile(rf"(?P<num>{_NUM}) ?(?P<pct>%|percent|pct)?(?:\+| or more| or higher) (?P<metric>{_METRIC_ALT})")
_WINDOW_LAST = re.compile(rf"(?:,? (?:over|in|across|during|for|from) )?(?:,? )?(?:the |their |his )?last "
                          rf"(?P<n>{_SMALL}) (?P<unit>games?|weeks?)$")
_WINDOW_RANGE = re.compile(r"(?:,? (?:in|over|across|during|from|for) )?(?:,? )?weeks? (?P<a>\d{1,2})"
                           r"(?: ?- ?| to | through | thru )(?P<b>\d{1,2})$")
_WINDOW_SEASON = re.compile(r",? (?:this season|so far(?: this season)?|on the season|in 2\d{3}|this year)$")
_OTHER_SEASON = re.compile(r"\b(?:last (?:season|year)|in (?:19|20)\d{2}|(?:19|20)\d{2} season)\b")
_SCREEN_HEAD = re.compile(rf"^(?:(?:which|what|who are(?: the)?|find|list|show|any) )?(?:(?P<owner>available|"
                          rf"unrostered|free agent|rostered) )?(?P<pos>(?:{_POS_ALT})(?:(?:, | and | or |/| & )"
                          rf"(?:{_POS_ALT}))*) (?:(?:who|that) (?:have|has|had|are|were|with) |with |having |have |has |had |"
                          rf"where |whose |(?:who|that) (?:see|saw|get|got|run|ran|earn|earned) )(?P<rest>.+)$")


def _condition(text: str):
    """One condition -> (metric key, op, value) or an Unsupported (vague or unknown metric, unit mismatch, bounds)."""
    text = text.strip()
    for rx, op_from_word in ((_COND_FORWARD, True), (_COND_REVERSE, True), (_COND_PLUS, False)):
        m = rx.fullmatch(text)
        if not m:
            continue
        metric = METRIC_BY_KEY[METRIC_WORDS[m.group("metric")]]
        op = OPS[m.group("op")] if op_from_word else "gte"
        value = float(m.group("num"))
        percent = bool(m.group("pct"))
        if percent and metric.unit != "percent":
            return Unsupported("unit_mismatch", f"{metric.label} is {metric.public()['unit_label']}, not a "
                                                f"percentage.")
        if metric.unit == "percent" and not percent and 0 < value < 1 and "." in m.group("num"):
            return Unsupported("ambiguous_value", f"{metric.label} is a percentage (0-100).",
                               clarify=f"Did you mean {value * 100:g}%?")
        if not metric.lo <= value <= metric.hi:
            return Unsupported("out_of_bounds", f"{metric.label} takes values from {metric.lo:g} to {metric.hi:g}.")
        return (metric.key, op, value)
    vague = re.search(rf"\b(?:{_VAGUE_ALT})\b", text)
    if vague and not re.search(r"\bppr\b|\bxfp\b|expected", text):
        return Unsupported("vague_metric", VAGUE_METRICS[vague.group(0)],
                           clarify="Did you mean PPR points per game?")
    return Unsupported("unknown_metric", f"\"{text[:60]}\" is not a condition the screener can check. It checks: "
                                         + ", ".join(m.label.lower() for m in METRIC_BY_KEY.values()) + ".")


def _screen(q: str) -> Parsed | Unsupported | None:
    m = _SCREEN_HEAD.match(q)
    if not m:
        return None
    pos = positions(m.group("pos"))
    if pos is None:
        return None
    if any(p not in SCREEN_POSITIONS for p in pos):
        return Unsupported("unsupported_position", "The screener covers WR, RB and TE only.")
    rest = m.group("rest")
    window: dict = {}
    mw = _WINDOW_LAST.search(rest)
    if mw:
        n = small_number(mw.group("n"))
        if not 1 <= n <= 8:
            return Unsupported("out_of_bounds", "A \"last N\" window is 1 to 8 games or weeks.")
        window = {"last_games": n} if mw.group("unit").startswith("game") else {"last_weeks": n}
        rest = rest[:mw.start()]
    else:
        mr = _WINDOW_RANGE.search(rest)
        if mr:
            a, b = int(mr.group("a")), int(mr.group("b"))
            if not (1 <= a <= b <= 18):
                return Unsupported("out_of_bounds", "Weeks run from 1 to 18, first week first.")
            window = {"week_from": a, "week_to": b}
            rest = rest[:mr.start()]
        else:
            ms = _WINDOW_SEASON.search(rest)
            if ms:
                rest = rest[:ms.start()]
    rest = rest.strip().rstrip(",")
    # Split into OR groups, then AND conditions: "a and b or c" = (a AND b) OR c, the screener's own precedence.
    groups = []
    for g in re.split(r",? or (?!more\b|higher\b|fewer\b|less\b)", rest):
        conds = []
        for c in re.split(r",? and |, ", g):
            parsed = _condition(c)
            if isinstance(parsed, Unsupported):
                return parsed
            conds.append(parsed)
        groups.append(conds)
    filters = [(gi + 1, key, op, value) for gi, conds in enumerate(groups) for key, op, value in conds]
    if len(groups) > 4 or len(filters) > 8:
        return Unsupported("too_many_conditions", "A screen takes at most 8 conditions in at most 4 OR-groups.")
    for _, key, _, _ in filters:
        metric = METRIC_BY_KEY[key]
        if pos and not any(p in metric.positions for p in pos):
            return Unsupported("metric_position", f"{metric.label} is only defined for "
                                                  f"{', '.join(metric.positions)}.")
    owner = {"available": "available", "unrostered": "available", "free agent": "available",
             "rostered": "rostered"}.get(m.group("owner") or "", "any")
    first = filters[0]
    return Parsed("screen", MappingProxyType({
        "positions": pos, "filters": tuple(filters), "owner": owner, **window,
        "sort": first[1], "dir": "asc" if first[2] in ("lt", "lte") else "desc"}))


# ------------------------------------------------------------------------------------------------- templates --

_P = r"(?P<p>.+?)"
_P2 = r"(?P<p2>.+?)"


@dataclass(frozen=True)
class Template:
    intent: str
    pattern: re.Pattern
    players: tuple[str, ...] = ()     # named groups holding player slots
    week: bool = False
    position: bool = False            # a named group "pos" holding one position


def _t(intent: str, rx: str, *, players=(), week=False, position=False) -> Template:
    return Template(intent, re.compile(rx), tuple(players), week, position)


_WK = rf"(?: {_WEEK})?"
def _pos(name: str = "pos") -> str:
    return rf"(?P<{name}>{_POS_ALT})"


_POS_ONE = _pos()
_ME = r"(?:i|me)"

TEMPLATES: tuple[Template, ...] = (
    # Start one player or the other (both on the roster, competing for one slot).
    _t("start_choice", rf"(?:should i (?:start|play)|do i (?:start|play)|(?:start|play)) {_P} or {_P2}{_WK}",
       players=("p", "p2"), week=True),
    # Best legal replacement for a starter.
    _t("replacement", rf"(?:who(?: is|'s| are)? |what(?: is|'s) )?(?:the |my )?best (?:legal )?(?:replacements?|"
                      rf"backups?|subs?|substitutes?|options?) (?:for|to replace) {_P}{_WK}", players=("p",), week=True),
    _t("replacement", rf"who (?:should|can|could) {_ME} replace {_P} with(?: this week)?", players=("p",)),
    _t("replacement", rf"who (?:should|can|could) {_ME} start (?:instead of|in place of) {_P}(?: this week)?",
       players=("p",)),
    _t("replacement", rf"{_P} is (?:out|hurt|injured|questionable|doubtful),? who (?:should|do|can) {_ME} start"
                      rf"(?: instead)?", players=("p",)),
    # Start/Sit verdict.
    _t("start_sit", rf"(?:who|what) should {_ME} (?:start|play|sit|bench){_WK}", week=True),
    _t("start_sit", rf"(?:is|was) my lineup (?:set|optimal|right|good|ok|okay|fine){_WK}", week=True),
    _t("start_sit", rf"(?:any |what |which )?(?:lineup changes|lineup moves|start ?/ ?sit|start or sit|start sit)"
                    rf"(?: advice| moves| changes| decisions)?(?: should i make| for me| do i need)?{_WK}", week=True),
    _t("start_sit", rf"(?:check|optimi[sz]e|set) my lineup{_WK}", week=True),
    # Matchup.
    _t("matchup", rf"who (?:am i|do i) (?:playing|facing|play|face|up against){_WK}", week=True),
    _t("matchup", rf"(?:what(?: is|'s) |how(?: is|'s) )?my (?:matchup|opponent)(?: looking(?: like)?| going)?{_WK}", week=True),
    _t("matchup", rf"am i (?:projected to win|favored|winning|the favorite){_WK}", week=True),
    _t("matchup", rf"(?:what(?: is|'s) )?my projected (?:score|total|points){_WK}", week=True),
    # Waivers.
    _t("waivers_best", rf"(?:who(?: is|'s| are)? )?(?:the )?(?:best|top) (?:available|free agent|waiver(?: wire)?)"
                       rf"(?: {_POS_ONE})?(?: (?:pickups?|adds?|options?|players?|targets?|claims?))?",
       position=True),
    _t("waivers_best", rf"(?:who|what|which (?:player|{_POS_ONE})) (?:should|can) {_ME} (?:pick up|add|grab|claim)"
                       rf"(?: (?:at|from) {_pos('pos2')})?(?: (?:off|from|on) (?:the )?waivers)?", position=True),
    _t("waivers_best", rf"(?:best|top) {_POS_ONE} (?:on (?:the )?waivers|on the wire|available|to (?:pick up|add))",
       position=True),
    _t("waivers_best", rf"waiver (?:wire )?(?:targets|pickups|adds|advice|claims)(?: (?:at|for) {_POS_ONE})?",
       position=True),
    # Byes.
    _t("byes", rf"who(?: is|'s| are| do i have) on (?:a )?bye{_WK}", week=True),
    _t("byes", rf"(?:do i have )?(?:any )?bye(?:[- ]week)? (?:problems|issues|conflicts|trouble|holes){_WK}",
       week=True),
    _t("byes", rf"(?:my )?bye(?:[- ]week)? (?:planner|plan|weeks?|outlook){_WK}", week=True),
    # Playoffs (deterministic scenario: no probability is claimed or computed).
    _t("playoffs", r"(?:what (?:are|is|does) )?my playoff (?:odds|chances|outlook|picture|race|path)(?: look like)?"),
    _t("playoffs", r"(?:will|am|do|can) i (?:make|going to make|get into|in|get in) (?:the )?playoffs"),
    _t("playoffs", r"(?:the )?playoff (?:race|picture|standings)"),
    # Trade values.
    _t("trade_values_top", rf"(?:the )?(?:top|best|highest|most valuable) (?:{_POS_ONE} )?(?:players? )?"
                           rf"(?:by )?trade values?(?: at {_pos('pos2')})?", position=True),
    _t("trade_values_top", rf"trade values?(?: (?:at|for) {_POS_ONE})?", position=True),
    _t("trade_value", rf"(?:what(?: is|'s) |how much is )?{_P}(?:'s|s'|) (?:trade value|worth in a trade|value)",
       players=("p",)),
    _t("trade_value", rf"(?:what(?: is|'s) )?(?:the )?(?:trade )?value (?:of|for) {_P}", players=("p",)),
    _t("trade_value", rf"how much is {_P} worth(?: in a trade)?", players=("p",)),
    # Compare.
    _t("compare", rf"compare {_P} (?:and|with|to|vs|vs\.|versus) {_P2}", players=("p", "p2")),
    _t("compare", rf"{_P} (?:vs|vs\.|versus) {_P2}", players=("p", "p2")),
    _t("compare", rf"who(?: is|'s) better,? {_P} or {_P2}", players=("p", "p2")),
    # Roster status.
    _t("ownership", rf"is {_P} (?:available|a free agent|on waivers|on the wire|rostered|taken|owned|on a team)"
                    rf"(?: in (?:my|this) league)?", players=("p",)),
    _t("ownership", rf"who (?:owns|has|rostered|roster) {_P}", players=("p",)),
    _t("ownership", rf"can {_ME} (?:pick up|add|get|claim) {_P}", players=("p",)),
    _t("ownership", rf"(?:what|which) (?:fantasy )?team (?:is|has) {_P}(?: on)?", players=("p",)),
    # Player snapshot.
    _t("player", rf"how (?:is|has|was) {_P} (?:doing|playing|looking|been(?: doing| playing)?)(?: lately| this season)?",
       players=("p",)),
    _t("player", rf"(?:tell me about|about|what about|how about|info on|stats for|usage for|outlook for) {_P}",
       players=("p",)),
    _t("player", rf"{_P}(?:'s|s'|) (?:stats|usage|outlook|profile|game log|role)", players=("p",)),
)

_OTHER_LEAGUE = re.compile(r"\b(?:my other leagues?|another league|other leagues|all (?:of )?my leagues|every league|"
                           r"each (?:of my )?leagues?|both leagues|in league \S+)\b")
_OTHER_TEAM = re.compile(r"\b(?:for|on|of) team \d+\b|\bteam \d+'s\b")


def parse(question: str) -> Parsed | Unsupported:
    """The question's single intent with typed slots, or why it is not supported. Deterministic and pure."""
    q = normalize(question)
    if not q:
        return Unsupported("empty", "Ask a question about this league, for example one of the examples below.")
    if _OTHER_LEAGUE.search(q):
        return Unsupported("other_league", "Ask answers for the league you are viewing only. Open the other league, "
                                           "or see every league at once on the leagues page.")
    if _OTHER_TEAM.search(q):
        return Unsupported("other_team", "Ask answers for the team in view. Pick another team in the team menu "
                                         "first, then ask again.")
    if _OTHER_SEASON.search(q):
        return Unsupported("other_season", "Ask answers for the league's synced season only.")
    for name, key in PRESET_WORDS.items():
        if re.fullmatch(rf"(?:(?:which|what|who are) (?:are )?(?:the )?)?{re.escape(name)}(?: this (?:week|season))?",
                        q):
            return Parsed("screen_preset", MappingProxyType({"preset": key}))
    screen = _screen(q)
    if screen is not None:
        return screen
    for t in TEMPLATES:
        m = t.pattern.fullmatch(q)
        if not m:
            continue
        slots: dict = {}
        ok = True
        for g in t.players:
            ref = player_ref(m.group(g))
            if ref is None:
                ok = False
                break
            slots[g] = ref
        if not ok:
            continue
        if t.week:
            slots["week"] = week_slot(m)
        if t.position:
            raw = next((v for k, v in m.groupdict().items() if k.startswith("pos") and v), None)
            if raw:
                pos = POSITION_WORDS[raw]
                if len(pos) != 1:
                    continue
                slots["position"] = pos[0]
            else:
                slots["position"] = None
        # Two references to one player are caught after resolution (app/api/ask.py), where qualifiers have narrowed
        # each name: "Williams (RB, DAL)" and "Williams (WR, DET)" are two players.
        return Parsed(t.intent, MappingProxyType(slots))
    return Unsupported("unsupported", "That question is not one Ask can answer exactly. Ask only answers questions "
                                      "it can compute from this league's data; try one of the examples below.")
