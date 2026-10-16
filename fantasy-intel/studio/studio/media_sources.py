"""Where catalog media comes from, and how a file gets into assets/media/ with its rights recorded.

  commons     Wikimedia Commons (MediaWiki API): search, categories, and per-file licence/author/date metadata.
              The main free source of real NFL photos: Flickr CC uploads, US-government (DVIDS) public-domain
              photos, and stills from team YouTube videos that the team published under CC BY.
  openverse   discovery across CC sources (Flickr, Commons, ...) without a key; files are then added from source
  pexels      generic context B-roll (stadiums, crowds, a ruler): needs PEXELS_API_KEY, never NFL players
  pixabay     the same, needs PIXABAY_API_KEY
  local       a file the channel owner supplies, with the licence they declare (their own photo, a paid licence)
  espn        the owner-approved ESPN headshot (studio/headshots.py), only ever as a headshot_fallback

Nothing here downloads video from YouTube or any game footage: NFL gameplay is never fetched. Every added entry
starts with review status "pending"; a person approves it (`approve`) before a video using it can be published.

Usage (from fantasy-intel/studio):
  python -m studio.media_sources commons-search "Bo Nix"
  python -m studio.media_sources add-commons "File:Bo Nix scramble.png" --id nix-scramble-2025 --type action_photo \
      --player 00-0039732 --name "Bo Nix" --team DEN --season 2025 --relevance scramble,ball_in_hand
  python -m studio.media_sources approve nix-scramble-2025 --by owner
  python -m studio.media_sources report
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

from . import media_library as ML
from .core import sha256_file

UA = "fantasy-intel-studio/0.1 (rights-tracked media catalog; https://github.com/hhavynn/fantasy)"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
OPENVERSE_API = "https://api.openverse.org/v1/images/"
MAX_EDGE = 2400                 # download at most this long an edge: plenty for a 1080x1920 crop with motion
POLITE_DELAY = 1.0              # seconds between API calls (Commons asks for serial, unhurried requests)
_last_call = [0.0]

COMMONS_LICENSES = {            # Commons LicenseShortName -> catalog licence id
    "cc0": "CC0", "public domain": "PD", "pd": "PD",
    "cc by 2.0": "CC-BY-2.0", "cc by 3.0": "CC-BY-3.0", "cc by 4.0": "CC-BY-4.0",
    "cc by-sa 2.0": "CC-BY-SA-2.0", "cc by-sa 3.0": "CC-BY-SA-3.0", "cc by-sa 4.0": "CC-BY-SA-4.0",
}


def _get(url: str, timeout: int = 40) -> bytes:
    wait = POLITE_DELAY - (time.time() - _last_call[0])
    if wait > 0:
        time.sleep(wait)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        return urllib.request.urlopen(req, timeout=timeout).read()
    finally:
        _last_call[0] = time.time()


def _strip(html: str | None) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", str(html) if html is not None else "")).strip()


def commons_license(short_name: str) -> str | None:
    """Catalog licence id for a Commons licence name; None when the channel can't use it (NC, ND, unknown)."""
    s = short_name.strip().lower()
    if any(x in s for x in ("-nc", " nc", "-nd", " nd", "non-commercial", "noncommercial")):
        return None
    if s.startswith("public domain") or s.startswith("pd"):
        return "PD"
    return COMMONS_LICENSES.get(s)


# ------------------------------------------------------------------------------------------------ commons

def commons_api(**params) -> dict:
    params.update(format="json", formatversion=2, maxlag=5)
    return json.loads(_get(COMMONS_API + "?" + urllib.parse.urlencode(params)))


def commons_info(titles: list[str]) -> list[dict]:
    """Licence, author, date, size and download URL for Commons files ('File:...')."""
    d = commons_api(action="query", titles="|".join(titles), prop="imageinfo",
                    iiprop="url|size|extmetadata|mime|sha1", iiurlwidth=MAX_EDGE)
    out = []
    for pg in d.get("query", {}).get("pages", []):
        if "imageinfo" not in pg:
            out.append({"title": pg["title"], "missing": True})
            continue
        ii = pg["imageinfo"][0]
        m = {k: _strip(v.get("value")) for k, v in ii.get("extmetadata", {}).items()}
        out.append({"title": pg["title"], "width": ii["width"], "height": ii["height"], "mime": ii.get("mime"),
                    "url": ii["url"], "thumb": ii.get("thumburl"), "page": ii.get("descriptionurl"),
                    "license_name": m.get("LicenseShortName", ""), "license": commons_license(m.get("LicenseShortName", "")),
                    "author": m.get("Artist", ""), "credit": m.get("Credit", ""), "date": m.get("DateTimeOriginal", ""),
                    "description": m.get("ImageDescription", "")[:400]})
    return out


def commons_search(query: str, limit: int = 40) -> list[dict]:
    d = commons_api(action="query", generator="search", gsrsearch=query, gsrnamespace=6, gsrlimit=limit,
                    prop="imageinfo", iiprop="size|extmetadata")
    rows = []
    for pg in sorted(d.get("query", {}).get("pages", []), key=lambda p: p.get("index", 0)):
        ii = pg["imageinfo"][0]
        m = {k: _strip(v.get("value")) for k, v in ii.get("extmetadata", {}).items()}
        rows.append({"title": pg["title"], "size": f"{ii['width']}x{ii['height']}", "license": m.get("LicenseShortName", ""),
                     "date": m.get("DateTimeOriginal", "")[:10], "author": m.get("Artist", "")[:40],
                     "description": m.get("ImageDescription", "")[:160]})
    return rows


def openverse_search(query: str, limit: int = 20) -> list[dict]:
    d = json.loads(_get(OPENVERSE_API + "?" + urllib.parse.urlencode({"q": query, "page_size": limit})))
    return [{"source": r["source"], "license": f"{r['license']} {r['license_version']}", "title": r.get("title"),
             "size": f"{r.get('width')}x{r.get('height')}", "creator": r.get("creator"),
             "url": r["foreign_landing_url"]} for r in d.get("results", [])]


def pexels_search(query: str, video: bool = False, limit: int = 15) -> list[dict]:
    """Context B-roll candidates from Pexels (needs PEXELS_API_KEY; 200 requests/hour by default)."""
    key = os.environ.get("PEXELS_API_KEY")
    if not key:
        raise RuntimeError("PEXELS_API_KEY is not set")
    base = "https://api.pexels.com/videos/search" if video else "https://api.pexels.com/v1/search"
    req = urllib.request.Request(base + "?" + urllib.parse.urlencode({"query": query, "per_page": limit,
                                                                       "orientation": "portrait"}),
                                 headers={"Authorization": key, "User-Agent": UA})
    d = json.loads(urllib.request.urlopen(req, timeout=30).read())
    return d.get("videos" if video else "photos", [])


def pixabay_search(query: str, video: bool = False, limit: int = 15) -> list[dict]:
    """Context B-roll candidates from Pixabay (needs PIXABAY_API_KEY; results must be cached, not hotlinked)."""
    key = os.environ.get("PIXABAY_API_KEY")
    if not key:
        raise RuntimeError("PIXABAY_API_KEY is not set")
    base = "https://pixabay.com/api/videos/" if video else "https://pixabay.com/api/"
    d = json.loads(_get(base + "?" + urllib.parse.urlencode({"key": key, "q": query, "per_page": limit,
                                                              "safesearch": "true"})))
    return d.get("hits", [])


# ------------------------------------------------------------------------------------------ subject boxes

_VISION_SRC = Path(__file__).with_name("vision_subject.swift")
_VISION_BIN = ML.MEDIA / ".bin" / "vision_subject"


def detect_subject(path: Path) -> dict:
    """Faces and people in a photo (Apple Vision, on device). Falls back to a centre box when Vision is missing.
    The box it picks is the largest person; a person reviewing the entry can correct it in the manifest."""
    try:
        if not _VISION_BIN.exists() or _VISION_BIN.stat().st_mtime < _VISION_SRC.stat().st_mtime:
            _VISION_BIN.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["swiftc", "-O", str(_VISION_SRC), "-o", str(_VISION_BIN)], check=True,
                           capture_output=True, timeout=300)
        raw = subprocess.run([str(_VISION_BIN), str(path)], check=True, capture_output=True, text=True, timeout=60).stdout
        d = json.loads(raw)
    except (OSError, subprocess.SubprocessError, ValueError):
        return {"box": [0.2, 0.1, 0.8, 0.95], "face": None, "method": "centre_guess"}
    clip = lambda b: [round(min(1.0, max(0.0, v)), 4) for v in b[:4]]
    humans = [clip(b) for b in d.get("humans", []) if b[4] >= 0.3]
    faces = [clip(b) for b in d.get("faces", []) if b[4] >= 0.5]
    box = humans[0] if humans else None
    face = faces[0] if faces else None
    if face and box and not (box[0] <= (face[0] + face[2]) / 2 <= box[2]):
        box = next((h for h in humans if h[0] <= (face[0] + face[2]) / 2 <= h[2]), box)   # the person the face is on
    if face and not box:          # a face but no body (a tight portrait): the body is below it
        fw, fh = face[2] - face[0], face[3] - face[1]
        box = [max(0, face[0] - fw), max(0, face[1] - fh * 0.4), min(1, face[2] + fw), min(1, face[3] + fh * 4)]
    if not box:
        box = [0.2, 0.1, 0.8, 0.95]
    return {"box": box, "face": face, "people": len(humans), "method": "apple_vision"}


# ---------------------------------------------------------------------------------------------- adding

def _store(data: bytes, eid: str) -> tuple[Path, int, int]:
    im = Image.open(io.BytesIO(data))
    im.load()
    if max(im.size) > MAX_EDGE:
        im.thumbnail((MAX_EDGE, MAX_EDGE), Image.LANCZOS)
    ML.CACHE.mkdir(parents=True, exist_ok=True)
    path = ML.CACHE / f"{eid}.jpg"
    im.convert("RGB").save(path, quality=93)
    return path, im.width, im.height


def _year(date: str) -> int | None:
    m = re.search(r"(19|20)\d\d", date or "")
    return int(m.group(0)) if m else None


def add_commons(lib: ML.Library, title: str, eid: str, mtype: str, **fields) -> ML.Entry:
    """Download one Commons file into the cache and add it to the catalog (review: pending)."""
    info = commons_info([title if title.startswith("File:") else "File:" + title])[0]
    if info.get("missing"):
        raise ValueError(f"{title}: not on Commons")
    if not info["license"]:
        raise ValueError(f"{title}: licence {info['license_name']!r} is not usable on a monetised channel")
    path, w, h = _store(_get(info["thumb"] or info["url"]), eid)
    author = fields.pop("attribution", None) or info["author"]
    e = ML.Entry(id=eid, type=mtype, path=path.name, license=info["license"], attribution=author,
                 source={"kind": "wikimedia_commons", "url": info["url"], "page": info["page"], "title": info["title"],
                         "author": info["author"], "credit": info["credit"], "description": info["description"],
                         "license_name": info["license_name"], "original_size": [info["width"], info["height"]],
                         "fetched": datetime.now(timezone.utc).isoformat(timespec="seconds")},
                 date=info["date"][:10] or None, width=w, height=h, sha256=sha256_file(path),
                 subject=detect_subject(path), **fields)
    if e.season is None:
        e.season = _year(info["date"])
    return lib.add(e)


def add_local(lib: ML.Library, src: Path, eid: str, mtype: str, license: str, attribution: str, url: str = "",
              **fields) -> ML.Entry:
    """A file the owner supplies (their own photo, a paid licence, a cleared clip), with the licence they declare."""
    src = Path(src)
    ML.CACHE.mkdir(parents=True, exist_ok=True)
    video = None
    if src.suffix.lower() in (".mp4", ".mov", ".webm", ".m4v"):
        dst = ML.CACHE / f"{eid}{src.suffix.lower()}"
        shutil.copyfile(src, dst)
        probe = json.loads(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                           "stream=width,height,r_frame_rate,codec_name:format=duration", "-of", "json",
                                           str(dst)], capture_output=True, text=True, check=True).stdout)
        st = probe["streams"][0]
        num, den = (int(x) for x in st["r_frame_rate"].split("/"))
        video = {"duration": round(float(probe["format"]["duration"]), 3), "fps": round(num / max(den, 1), 3),
                 "codec": st["codec_name"]}
        w, h = st["width"], st["height"]
        subject = fields.pop("subject", None) or {"box": [0.15, 0.1, 0.85, 0.95], "face": None, "method": "manual"}
    else:
        dst, w, h = _store(src.read_bytes(), eid)
        subject = fields.pop("subject", None) or detect_subject(dst)
    e = ML.Entry(id=eid, type=mtype, path=dst.name, license=license, attribution=attribution,
                 source={"kind": "user_supplied", "url": url, "original": src.name}, width=w, height=h,
                 video=video, sha256=sha256_file(dst), subject=subject, **fields)
    return lib.add(e)


def add_espn_headshot(lib: ML.Library, player: dict, team: str, season: int) -> ML.Entry | None:
    """The ESPN headshot as a catalog fallback (owner's override; only used when [shorts] headshots = true)."""
    from . import headshots
    src = headshots.fetch(player.get("espn_id"))
    if src is None:
        return None
    eid = f"espn-{int(player['espn_id'])}"
    ML.CACHE.mkdir(parents=True, exist_ok=True)
    dst = ML.CACHE / f"{eid}.png"
    shutil.copyfile(src, dst)
    im = Image.open(dst)
    e = ML.Entry(id=eid, type="headshot_fallback", path=dst.name, license="espn_override",
                 attribution="ESPN (owner override)", player_id=player["gsis_id"], espn_id=int(player["espn_id"]),
                 name=player["name"], team=team, season=season, jersey=str(player.get("jersey") or ""),
                 source={"kind": "espn_headshot", "url": headshots.URL.format(id=int(player["espn_id"])),
                         "rights": headshots.RIGHTS},
                 width=im.width, height=im.height, sha256=sha256_file(dst),
                 subject={"box": [0.0, 0.0, 1.0, 1.0], "face": [0.3, 0.12, 0.7, 0.55], "method": "headshot_layout"},
                 relevance=["portrait"], review={"status": "approved", "by": "owner override (config shorts.headshots)"})
    return lib.add(e)


def approve(lib: ML.Library, eid: str, by: str, note: str = "", status: str = "approved") -> ML.Entry:
    e = lib.get(eid)
    e.review = {"status": status, "by": by, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "note": note}
    return e


def restore(lib: ML.Library) -> list[str]:
    """Re-download catalog files missing from the (git-ignored) cache, from each entry's recorded source."""
    done = []
    for e in lib.entries.values():
        if e.file.exists():
            continue
        if e.source.get("kind") == "wikimedia_commons":
            info = commons_info([e.source["title"]])[0]
            _store(_get(info["thumb"] or info["url"]), e.id)
        elif e.source.get("kind") == "espn_headshot" and e.espn_id:
            from . import headshots
            src = headshots.fetch(e.espn_id)
            if src:
                ML.CACHE.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, e.file)
        else:
            continue
        if e.file.exists():
            changed = e.sha256 and sha256_file(e.file) != e.sha256
            done.append(f"{e.id}{' (file changed at the source: re-review it)' if changed else ''}")
    return done


def report(lib: ML.Library) -> str:
    rows = ["| id | type | who | team | season | licence | review | file |", "|---|---|---|---|---|---|---|---|"]
    for e in lib.entries.values():
        rows.append(f"| {e.id} | {e.type} | {e.name or '-'} | {e.team or '-'} | {e.season or '-'} | {e.license} | "
                    f"{e.review.get('status')} | {'ok' if lib.available(e) else 'MISSING'} |")
    return "\n".join(rows)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="media_sources", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("commons-search")
    s.add_argument("query")
    s = sub.add_parser("openverse-search")
    s.add_argument("query")
    s = sub.add_parser("add-commons")
    s.add_argument("title")
    for name in ("id", "type", "player", "name", "team", "jersey", "context", "relevance", "attribution"):
        s.add_argument(f"--{name}")
    s.add_argument("--season", type=int)
    s.add_argument("--illustrative", action="store_true")
    s = sub.add_parser("add-local")
    s.add_argument("path")
    for name in ("id", "type", "license", "attribution", "url", "player", "name", "team", "context", "relevance"):
        s.add_argument(f"--{name}")
    s.add_argument("--license-ref", default="", help="invoice / agreement id: required with --license licensed")
    s.add_argument("--season", type=int)
    s.add_argument("--illustrative", action="store_true")
    s = sub.add_parser("approve")
    s.add_argument("id")
    s.add_argument("--by", required=True)
    s.add_argument("--note", default="")
    s.add_argument("--reject", action="store_true")
    sub.add_parser("report")
    sub.add_parser("restore", help="re-download catalog files missing from assets/media/cache/")
    a = ap.parse_args(argv)
    if a.cmd == "commons-search":
        for r in commons_search(a.query):
            print(json.dumps(r))
        return 0
    if a.cmd == "openverse-search":
        for r in openverse_search(a.query):
            print(json.dumps(r))
        return 0
    lib = ML.Library.load()
    if a.cmd == "report":
        print(report(lib))
        return 0
    if a.cmd == "restore":
        print("\n".join(restore(lib)) or "nothing missing")
        return 0
    if a.cmd == "approve":
        approve(lib, a.id, a.by, a.note, "rejected" if a.reject else "approved")
    else:
        common = dict(player_id=a.player, name=a.name, team=a.team, season=a.season, context=a.context or "",
                      relevance=[r for r in (a.relevance or "").split(",") if r], illustrative=a.illustrative)
        if a.cmd == "add-commons":
            add_commons(lib, a.title, a.id, a.type, jersey=a.jersey, attribution=a.attribution, **common)
        else:
            add_local(lib, Path(a.path), a.id, a.type, a.license, a.attribution or "", a.url or "",
                      license_ref=a.license_ref, **common)
    lib.save()
    print(report(lib))
    return 0


if __name__ == "__main__":
    sys.exit(main())
