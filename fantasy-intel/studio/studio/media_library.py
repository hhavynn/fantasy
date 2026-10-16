"""The media catalog: real photography, B-roll and evidence for story-aware Shorts, with rights recorded per file.

The manifest (assets/media/manifest.json, tracked) holds metadata only: who is in a file, which team and season,
what it shows, where it came from, its licence and attribution, and whether a person has reviewed it. The files
themselves live in assets/media/cache/ (git-ignored), re-downloadable from each entry's source.

Every use of an entry in a video goes through `check_use`, which knows the editorial traps:
  - a player in another team's (or his college) uniform used for current analysis
  - generic injury imagery presented as a specific player's injury
  - a stock clip or photo implying a specific player did something (stock stays visibly illustrative)
  - a teammate photo that is not actually that teammate
  - a licence that does not clear monetised use, an ESPN headshot outside its owner switch, unreviewed media

Licences are judged by `LICENSES`. "cleared" means usable in a monetised Short with the attribution shown; the
ESPN headshot is the channel owner's explicit override (studio/headshots.py) and is only ever a fallback.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .core import ASSETS, sha256_file

MEDIA = ASSETS / "media"
MANIFEST = MEDIA / "manifest.json"
CACHE = MEDIA / "cache"

PLAYER_PHOTOS = ("action_photo", "catch_photo", "rush_photo", "celebration_photo", "sideline_photo",
                 "practice_photo", "interview_still", "headshot_fallback")
TEAM_MEDIA = ("team_photo",)
CONTEXT_MEDIA = ("licensed_video", "context_broll", "source_evidence")
MEDIA_TYPES = PLAYER_PHOTOS + TEAM_MEDIA + CONTEXT_MEDIA
GAME_PHOTOS = ("action_photo", "catch_photo", "rush_photo", "celebration_photo")   # shows him playing a game

# commercial: usable in a monetised video; attribution: credit required; share_alike: adaptations carry the licence
LICENSES = {
    "CC0": {"commercial": True, "attribution": False, "share_alike": False, "url": "https://creativecommons.org/publicdomain/zero/1.0/"},
    "PD": {"commercial": True, "attribution": False, "share_alike": False, "url": ""},
    "PD-USGov": {"commercial": True, "attribution": False, "share_alike": False, "url": ""},
    "CC-BY-2.0": {"commercial": True, "attribution": True, "share_alike": False, "url": "https://creativecommons.org/licenses/by/2.0/"},
    "CC-BY-3.0": {"commercial": True, "attribution": True, "share_alike": False, "url": "https://creativecommons.org/licenses/by/3.0/"},
    "CC-BY-4.0": {"commercial": True, "attribution": True, "share_alike": False, "url": "https://creativecommons.org/licenses/by/4.0/"},
    "CC-BY-SA-2.0": {"commercial": True, "attribution": True, "share_alike": True, "url": "https://creativecommons.org/licenses/by-sa/2.0/"},
    "CC-BY-SA-3.0": {"commercial": True, "attribution": True, "share_alike": True, "url": "https://creativecommons.org/licenses/by-sa/3.0/"},
    "CC-BY-SA-4.0": {"commercial": True, "attribution": True, "share_alike": True, "url": "https://creativecommons.org/licenses/by-sa/4.0/"},
    "Pexels": {"commercial": True, "attribution": False, "share_alike": False, "url": "https://www.pexels.com/license/"},
    "Pixabay": {"commercial": True, "attribution": False, "share_alike": False, "url": "https://pixabay.com/service/license-summary/"},
    "owner": {"commercial": True, "attribution": False, "share_alike": False, "url": ""},            # shot or made by the channel owner
    "licensed": {"commercial": True, "attribution": True, "share_alike": False, "url": ""},          # paid licence; needs license_ref
    "espn_override": {"commercial": False, "attribution": False, "share_alike": False, "url": ""},  # see headshots.py
}
NONCOMMERCIAL = ("CC-BY-NC", "CC-BY-NC-SA", "CC-BY-ND", "CC-BY-NC-ND")      # never cleared for a monetised channel

STALE_SEASONS = 1           # a photo more than this many seasons old is labelled "FILE PHOTO <year>"
MIN_SHORT_SIDE = 400        # px: below this a full-bleed crop is mush


@dataclass
class Entry:
    id: str
    type: str
    path: str                                   # relative to CACHE
    license: str
    attribution: str
    source: dict                                # {kind, url, page, author, ...}
    player_id: str | None = None                # gsis id of the person shown (None: no specific player)
    espn_id: int | None = None
    name: str | None = None
    team: str | None = None                     # NFL abbreviation, or e.g. "BYU" / "HS" for a non-NFL uniform
    season: int | None = None
    date: str | None = None
    jersey: str | None = None
    width: int = 0
    height: int = 0
    video: dict | None = None                   # {duration, fps, codec} for clips
    sha256: str = ""
    subject: dict = field(default_factory=dict)  # {box: [l,t,r,b] fractions, face: [...], method}
    relevance: list[str] = field(default_factory=list)   # what it shows: "throwing", "scramble", "catch", "interview"
    context: str = ""                           # where/when, e.g. "2025 Week 1 vs TEN"
    depicts_injury: bool = False                # a photo of this player's actual injury moment
    illustrative: bool = False                  # stock/context: never presented as a specific event
    license_ref: str = ""                       # invoice / agreement id for "licensed"
    review: dict = field(default_factory=lambda: {"status": "pending"})   # pending | approved | rejected
    notes: str = ""

    @property
    def file(self) -> Path:
        return CACHE / self.path

    @property
    def is_video(self) -> bool:
        return self.video is not None or self.type == "licensed_video"


def validate(e: Entry) -> list[str]:
    """Schema problems with one entry (empty when it is well-formed)."""
    errs = []
    if e.type not in MEDIA_TYPES:
        errs.append(f"{e.id}: unknown type {e.type!r}")
    if e.license not in LICENSES:
        errs.append(f"{e.id}: licence {e.license!r} is not one the channel can use")
    if LICENSES.get(e.license, {}).get("attribution") and not e.attribution.strip():
        errs.append(f"{e.id}: {e.license} needs an attribution line")
    if e.license == "licensed" and not e.license_ref:
        errs.append(f"{e.id}: a paid licence needs license_ref")
    if e.license == "espn_override" and e.type != "headshot_fallback":
        errs.append(f"{e.id}: an ESPN image may only be a headshot_fallback")
    if e.type in PLAYER_PHOTOS and not (e.player_id and e.name and e.team):
        errs.append(f"{e.id}: a player photo needs player_id, name and team")
    if e.type == "team_photo" and not e.team:
        errs.append(f"{e.id}: a team photo needs its team")
    if e.type == "context_broll" and e.player_id:
        errs.append(f"{e.id}: context B-roll must not be tied to a player (use a player photo type)")
    if e.type == "context_broll" and not e.illustrative:
        errs.append(f"{e.id}: context B-roll is always illustrative")
    if not e.source.get("url") and e.license not in ("owner",):
        errs.append(f"{e.id}: no source url")
    if e.review.get("status") not in ("pending", "approved", "rejected"):
        errs.append(f"{e.id}: review status must be pending, approved or rejected")
    return errs


class Library:
    def __init__(self, entries: list[Entry] | None = None, path: Path = MANIFEST):
        self.entries = {e.id: e for e in (entries or [])}
        self.path = path

    @classmethod
    def load(cls, path: Path = MANIFEST) -> "Library":
        if not Path(path).exists():
            return cls([], path)
        raw = json.loads(Path(path).read_text())
        return cls([Entry(**e) for e in raw.get("entries", [])], path)

    def save(self) -> None:
        errs = [m for e in self.entries.values() for m in validate(e)]
        if errs:
            raise ValueError("manifest not saved: " + "; ".join(errs))
        body = {"about": "Rights-tracked media for the Shorts studio (studio/media_library.py). Files are cached "
                         "in assets/media/cache/ and never committed.",
                "entries": [asdict(e) for e in sorted(self.entries.values(), key=lambda x: x.id)]}
        tmp = Path(self.path).with_suffix(".json.tmp")      # write, re-read, then replace: never a half-written manifest
        tmp.write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n")
        json.loads(tmp.read_text())
        tmp.replace(self.path)

    def add(self, e: Entry) -> Entry:
        errs = validate(e)
        if errs:
            raise ValueError("; ".join(errs))
        self.entries[e.id] = e
        return e

    def get(self, eid: str) -> Entry:
        return self.entries[eid]

    def available(self, e: Entry) -> bool:
        return e.file.exists() and (not e.sha256 or sha256_file(e.file) == e.sha256)

    def query(self, player_id: str | None = None, team: str | None = None, types=None,
              include_rejected: bool = False) -> list[Entry]:
        out = []
        for e in self.entries.values():
            if not include_rejected and e.review.get("status") == "rejected":
                continue
            if player_id is not None and e.player_id != player_id:
                continue
            if team is not None and e.team != team:
                continue
            if types and e.type not in types:
                continue
            out.append(e)
        return sorted(out, key=lambda x: x.id)


# ------------------------------------------------------------------------------------------- use checks

def cleared(e: Entry, cfg_headshots: bool) -> bool:
    if e.license == "espn_override":
        return cfg_headshots and e.type == "headshot_fallback"
    return LICENSES.get(e.license, {}).get("commercial", False)


def labels_for(e: Entry, season: int) -> list[str]:
    """The on-screen labels an entry must carry wherever it appears."""
    out = []
    if e.type == "context_broll" or e.illustrative:
        out.append("ILLUSTRATIVE")
    elif e.type == "team_photo" or (e.season and season - e.season > STALE_SEASONS) or e.type == "practice_photo":
        out.append(f"FILE PHOTO{' ' + str(e.season) if e.season else ''}")
    if e.type == "headshot_fallback":
        out.append("FALLBACK")
    return out


def check_use(e: Entry, use: dict, *, season: int, cfg_headshots: bool, publish: bool = False) -> list[dict]:
    """Problems with putting entry `e` on screen for one shot.

    use: {subject_id, subject_team, intent, names_player, injury_claim, labels}
      subject_id    gsis id of the person the narration is about at this moment (None: nobody specific)
      subject_team  that person's current team
      intent        the planner's intent for the shot (e.g. "injury_status", "dominance")
      names_player  the narration says a specific player did something here
      labels        the labels the render will draw
    Each problem: {severity: block|warn, check, message}. A block stops a render; a warn goes to review."""
    out = []

    def add(sev, check, msg):
        out.append({"severity": sev, "check": check, "asset": e.id, "message": msg})

    if not cleared(e, cfg_headshots):
        add("block", "license", f"{e.license} does not clear this use")
    if e.review.get("status") == "rejected":
        add("block", "review", "rejected in review")
    elif e.review.get("status") != "approved":
        add("block" if publish else "warn", "review", "new media: needs a person to approve it before publishing")
    sid, steam = use.get("subject_id"), use.get("subject_team")
    if e.type in PLAYER_PHOTOS:
        if sid and e.player_id != sid:
            add("block", "identity", f"shows {e.name} ({e.player_id}), but the shot is about {sid}")
        if steam and e.team != steam:
            add("block", "team", f"{e.name} in a {e.team} uniform; current analysis needs {steam}")
        if e.season and season - e.season > STALE_SEASONS and "FILE PHOTO" not in " ".join(use.get("labels", [])):
            add("warn", "season", f"{e.season} photo for a {season} story without a FILE PHOTO label")
    if e.type == "team_photo" and steam and e.team != steam:
        add("block", "team", f"{e.team} team photo for a {steam} story")
    injury = use.get("intent") == "injury_status" or use.get("injury_claim")
    if injury and e.type != "source_evidence" and not e.depicts_injury:
        if e.type in ("context_broll", "licensed_video") or "injury" in e.relevance:
            if "ILLUSTRATIVE" not in use.get("labels", []):
                add("block", "injury", "generic injury imagery presented as this player's injury")
        # a photo of the player himself is identification, not a picture of the injury: fine
    if e.type in ("context_broll", "licensed_video") and e.player_id is None and use.get("names_player"):
        if "ILLUSTRATIVE" not in use.get("labels", []):
            add("block", "stock_implication", "stock footage under a line about a specific player, unlabelled")
    if e.width and e.height and min(e.width, e.height) < MIN_SHORT_SIDE and not e.is_video:
        add("warn", "resolution", f"{e.width}x{e.height} is small for a full-bleed shot")
    return out


def attribution_lines(entries: list[Entry]) -> list[str]:
    """One credit line per file, as the licence asks: author, source, licence (with its URL)."""
    out = []
    for e in sorted({x.id: x for x in entries}.values(), key=lambda x: x.id):
        lic = LICENSES.get(e.license, {})
        bits = [e.attribution or e.source.get("author", "")]
        if e.source.get("page"):
            bits.append(e.source["page"])
        bits.append(e.license + (f" ({lic['url']})" if lic.get("url") else ""))
        out.append(f"{e.id}: " + " | ".join(b for b in bits if b))
    return out


def short_credit(e: Entry) -> str:
    """The small on-screen credit: 'Tennessee Titans / CC BY 4.0'."""
    who = (e.attribution or e.source.get("author") or "").split(",")[0].strip()
    lic = e.license.replace("-", " ").replace("CC BY SA", "CC BY-SA")
    if e.license in ("PD", "PD-USGov"):
        lic = "public domain"
    if e.license == "espn_override":
        return "ESPN"
    return f"{who} / {lic}" if who else lic
