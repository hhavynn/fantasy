"""Deterministic number formatting: one display form (captions, charts) and one spoken form (TTS).

Every number a viewer sees or hears goes through a formatter here, so the data-claim check can
re-derive the exact string from the snapshot.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
         "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
_ORD_IRREGULAR = {"one": "first", "two": "second", "three": "third", "five": "fifth", "eight": "eighth",
                  "nine": "ninth", "twelve": "twelfth"}


def round_half_up(value: float, places: int = 0) -> Decimal:
    q = Decimal(1).scaleb(-places)
    return Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP)


def int_words(n: int) -> str:
    if n < 0:
        return "minus " + int_words(-n)
    if n < 20:
        return _ONES[n]
    if n < 100:
        tens, ones = divmod(n, 10)
        return _TENS[tens] + ("-" + _ONES[ones] if ones else "")
    hundreds, rest = divmod(n, 100)
    return _ONES[hundreds] + " hundred" + (" " + int_words(rest) if rest else "")


def ordinal_words(n: int) -> str:
    words = int_words(n)
    head, _, last = words.rpartition("-")
    if last in _ORD_IRREGULAR:
        last = _ORD_IRREGULAR[last]
    elif last.endswith("y"):
        last = last[:-1] + "ieth"
    else:
        last += "th"
    return f"{head}-{last}" if head else last


def ordinal_suffix(n: int) -> str:
    if 10 <= n % 100 <= 20:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"


def decimal_words(d: Decimal) -> str:
    sign = "minus " if d < 0 else ""
    d = abs(d)
    whole = int(d)
    frac = str(d).partition(".")[2]
    out = int_words(whole)
    if frac and int(frac):
        out += " point " + " ".join(_ONES[int(c)] for c in frac)
    return sign + out


def fmt_pct(v: float) -> tuple[str, str]:
    """0.293 -> ('29%', 'twenty-nine percent')."""
    n = int(round_half_up(v * 100))
    return f"{n}%", f"{int_words(n)} percent"


def fmt_pp(v: float) -> tuple[str, str]:
    """Share difference in percentage points, signed: 0.12 -> ('+12 pts', 'twelve percentage points')."""
    n = int(round_half_up(v * 100))
    sign = "+" if n > 0 else "−" if n < 0 else ""
    return f"{sign}{abs(n)} pts", f"{int_words(abs(n))} percentage points"


def fmt_signed_pct1(v: float) -> tuple[str, str]:
    """A value already in percentage points (PROE), signed, one decimal: 3.14 -> ('+3.1%', 'plus three point one percent')."""
    d = round_half_up(v, 1)
    sign = "+" if d > 0 else "−" if d < 0 else ""
    spoken_sign = "plus " if d > 0 else "minus " if d < 0 else ""
    return f"{sign}{abs(d)}%", f"{spoken_sign}{decimal_words(abs(d))} percent"


def fmt_dec1(v: float) -> tuple[str, str]:
    """One decimal, dropping a trailing .0: 19.0 -> ('19', 'nineteen'), 4.5 -> ('4.5', 'four point five')."""
    d = round_half_up(v, 1)
    if d == d.to_integral_value():
        d = d.quantize(Decimal(1))
    return f"{d}", decimal_words(d)


def fmt_int(v: float) -> tuple[str, str]:
    n = int(round_half_up(v))
    return f"{n}", int_words(n)


def fmt_ordinal(v: float) -> tuple[str, str]:
    n = int(v)
    return ordinal_suffix(n), ordinal_words(n)


def fmt_week(v: float) -> tuple[str, str]:
    n = int(v)
    return f"Week {n}", f"Week {int_words(n)}"


def fmt_weeks_range(v: list) -> tuple[str, str]:
    a, b = int(v[0]), int(v[-1])
    return f"Weeks {a}–{b}", f"Weeks {int_words(a)} through {int_words(b)}"


def fmt_text(v) -> tuple[str, str]:
    if isinstance(v, list):
        text = _join(v)
        return text, text
    return str(v), str(v)


TEAM_NAMES = {
    "ARI": "Cardinals", "ATL": "Falcons", "BAL": "Ravens", "BUF": "Bills", "CAR": "Panthers", "CHI": "Bears",
    "CIN": "Bengals", "CLE": "Browns", "DAL": "Cowboys", "DEN": "Broncos", "DET": "Lions", "GB": "Packers",
    "HOU": "Texans", "IND": "Colts", "JAX": "Jaguars", "KC": "Chiefs", "LA": "Rams", "LAC": "Chargers",
    "LV": "Raiders", "MIA": "Dolphins", "MIN": "Vikings", "NE": "Patriots", "NO": "Saints", "NYG": "Giants",
    "NYJ": "Jets", "PHI": "Eagles", "PIT": "Steelers", "SEA": "Seahawks", "SF": "49ers", "TB": "Buccaneers",
    "TEN": "Titans", "WAS": "Commanders",
}


def fmt_team(v) -> tuple[str, str]:
    """nflverse team code -> nickname ('bye' passes through). The 49ers are spoken as 'Niners'."""
    if v == "bye":
        return "bye", "bye"
    name = TEAM_NAMES[v]
    return name, "Niners" if v == "SF" else name


def fmt_weeks_list(v: list) -> tuple[str, str]:
    """[4, 5] -> ('Weeks 4 and 5', 'Weeks four and five'); [4] -> ('Week 4', 'Week four')."""
    v = [int(x) for x in v]
    if len(v) == 1:
        return f"Week {v[0]}", f"Week {int_words(v[0])}"
    if v == list(range(v[0], v[-1] + 1)) and len(v) > 2:
        return f"Weeks {v[0]}–{v[-1]}", f"Weeks {int_words(v[0])} through {int_words(v[-1])}"
    return f"Weeks {_join([str(x) for x in v]).replace(',', '')}", f"Weeks {_join([int_words(x) for x in v]).replace(',', '')}"


PRACTICE = {"Did Not Participate In Practice": "not practicing", "Limited Participation in Practice":
            "limited in practice", "Full Participation in Practice": "practicing fully"}


def fmt_status(v: list) -> tuple[str, str]:
    """[code, week, detail] -> what follows "<player> is": the latest official word on his availability."""
    code, week, detail = v[0], int(v[1]), v[2]
    wd, ws = f"Week {week}", f"Week {int_words(week)}"
    if code == "ir":
        return "on injured reserve", "on injured reserve"
    if code == "designation":
        return f"listed {detail.lower()} for {wd}", f"listed {detail.lower()} for {ws}"
    if code == "practice":
        phrase = PRACTICE.get(detail, detail.lower())
        return f"{phrase}, per the {wd} injury report", f"{phrase}, per the {ws} injury report"
    return f"not on the {wd} injury report", f"not on the {ws} injury report"


def fmt_opponents(v: list) -> tuple[str, str]:
    """['SEA', 'bye', 'SF'] -> ('the Seahawks, a bye and the 49ers', 'the Seahawks, a bye and the Niners')."""
    display = [("a bye" if t == "bye" else f"the {fmt_team(t)[0]}") for t in v]
    spoken = [("a bye" if t == "bye" else f"the {fmt_team(t)[1]}") for t in v]
    return _join(display), _join(spoken)


def _join(items: list) -> str:
    items = [str(i) for i in items]
    if len(items) <= 1:
        return "".join(items)
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + f", and {items[-1]}"


FORMATTERS = {
    "pct": fmt_pct, "pp": fmt_pp, "signed_pct1": fmt_signed_pct1, "dec1": fmt_dec1, "int": fmt_int,
    "ordinal": fmt_ordinal, "week": fmt_week, "team": fmt_team, "opponents": fmt_opponents, "weeks_list": fmt_weeks_list,
    "status": fmt_status, "weeks_range": fmt_weeks_range, "text": fmt_text,
}


def render(fmt: str, value) -> tuple[str, str]:
    return FORMATTERS[fmt](value)
