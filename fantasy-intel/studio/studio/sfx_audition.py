"""Meme-sound audition pack: hear the candidates alone and inside the actual Bo Nix punchlines before anything
changes in the pipeline.

    uv run python -m studio.sfx_audition        # -> episodes/sfx-auditions/index.html

The meme sounds are real soundboard clips (myinstants.com), downloaded into episodes/sfx-auditions/sounds/, which is
gitignored: they are never committed. Each in-context clip is the same picture and the same narration, with only the
punchline's sound changed: the voice and the music are rebuilt as separate stems from the episode's own files, so a
"silence" treatment really stops the music.
"""

from __future__ import annotations

import html
import json
import subprocess
from pathlib import Path

import numpy as np
import soundfile as sf

from . import audio_design as A
from . import series_audio as SA
from .core import STUDIO_ROOT, Config

OUT = STUDIO_ROOT / "episodes" / "sfx-auditions"
SOUNDS = OUT / "sounds"
EPISODE = STUDIO_ROOT / "episodes" / "2026-w07-short-flame-bo-nix-seahawks"     # == ~/Downloads/ass.mp4
SR = 48000
MI = "https://www.myinstants.com/media/sounds/"

# id: (file, display name, category, (trim start, trim end) s, hit offset from trim start s, level vs narration dB,
#      what it is, when it is funny, NFL use, speech?)
CATALOG = {
    "fahhh": ("fahhhhhhhhhhhhhh.mp3", "FAHHH", "A", (0.06, 1.91), 0.03, 3,
              "A guy's long, pained 'FAAAHHH' (Taileons, viral on TikTok since Aug 2025; #1 on Myinstants' US trending, Oct 2026).",
              "Right after the number that ruins it: the viewer's own reaction, said for them.",
              "A QB's 2.96-yard aDOT, a 3-point offense, a drop on 3rd down.", True),
    "get_out": ("tuco-get-out.mp3", "GET OUT (Tuco)", "A", (0.05, 1.03), 0.02, 3,
                "A furious one-second 'GET OUT!' scream (GTA roleplay clip, viral April 2024; still Myinstants top 40).",
                "Ending a bit by throwing someone out: the line itself tells someone to leave.",
                "'Tell the deep safety he can go home.' / a benched QB / a coach's challenge flag.", True),
    "bruh": ("movie_1.mp3", "BRUH", "A", (0.0, 0.6), 0.03, 1,
             "The flat, deadpan 'bruh'.", "Under a joke, not over it: a tiny disappointed aside.",
             "After a setup that already sounds bad ('Grab a smaller ruler.').", True),
    "oh_hell_nah": ("oh-my-god-bro-oh-hell-nah-man.mp3", "oh my god bro, oh hell nah man", "A", (0.2, 3.0), 0.03, 1,
                    "A guy losing it: 'oh my god bro... oh hell nah man' (listed among current TikTok meme sounds).",
                    "A stat that gets worse the longer you look at it.", "'20 came in the air.'", True),
    "emotional_damage": ("emotional-damage-meme.mp3", "EMOTIONAL DAMAGE", "A", (0.4, 3.31), 0.0, 2,
                         "Steven He's 'EMOTIONAL DAMAGE!' (2021, still on the trending list).",
                         "When the number personally hurts the fan base.", "A team's own record or a franchise QB's stat.", True),
    "metal_pipe": ("metal-pipe-clang.mp3", "Metal pipe", "A", (0.15, 2.76), 0.05, 2,
                   "The 'metal pipe falling' clang with its long ring.", "Deadpan: the line ends, the pipe falls, nobody speaks.",
                   "A season-low stat delivered flat.", False),
    "spongebob_fail": ("spongebob-fail.mp3", "SpongeBob fail", "A", (0.0, 3.32), 0.25, 0,
                       "The SpongeBob fail sting.", "An anticlimax a kid would recognise.", "A drive that ends in a punt.", False),
    "sad_violin": ("tf_nemesis.mp3", "Sad violin (the meme one)", "A", (0.0, 4.3), 0.0, -3,
                   "The tiny sad violin.", "Mock sympathy for someone who has it worse than the player.",
                   "The deep safety who never got a ball thrown near him.", False),
    "vine_boom": ("vine-boom.mp3", "VINE BOOM", "B", (0.07, 1.23), 0.05, 2,
                  "The bass 'boom'. Still #2 on the trending list.", "Once, on the single most absurd number. Not on every joke.",
                  "'His? (pause) 3.'", False),
    "shocked": ("shocked-sound-effect.mp3", "Shocked", "B", (0.08, 1.21), 0.4, 1,
                "A shocked reaction stinger.", "A number nobody expected.", "A backup's 24 targets.", False),
    "anime_wow": ("anime-wow-sound-effect.mp3", "Anime Wow", "B", (0.15, 2.4), 0.19, 0,
                  "An anime-style 'wow'.", "Sarcastic awe, or real awe at something ridiculous.", "Puka's 24 targets.", True),
    "chicken_scream": ("chicken-on-tree-screaming.mp3", "Chicken on tree screaming", "E", (0.35, 2.6), 0.36, 0,
                       "A chicken in a tree, screaming.", "Pure brainrot: the stat is so bad the edit loses its mind.",
                       "Chaotic cut only.", False),
    "dexter": ("dexter-meme.mp3", "Dexter meme", "B", (0.0, 3.0), 0.04, -2,
               "The 'Dexter meme' sound (Myinstants trending #15).", "Listen first: a recognisable reaction bed.", "TBD by ear.", False),
    "rizz": ("rizz-sound-effect.mp3", "rizz sound effect", "C", (0.18, 1.3), 0.1, 0,
             "The 'rizz' sting.", "Ironic glazing: the player is smooth about it.", "A one-handed catch, a no-look pass.", False),
    "heavenly": ("heavenly-music-gaming-sound-effect-hd-mp3cut.mp3", "Heavenly music", "C", (0.9, 4.6), 0.3, -4,
                 "Angelic 'aaah' choir.", "Over-the-top worship of a performance (glazing).", "'Out of 412 receiver games, nobody's seen more.'",
                 False),
    "undertaker": ("undertakers-bell_2UwFCIe.mp3", "The Undertaker Bell", "C", (0.14, 3.57), 0.46, 0,
                   "WWE Undertaker entrance bell.", "A dominant player's arrival: the stat walks in like a final boss.",
                   "Puka's first slam: '24 targets.'", False),
    "yippee": ("yippeeeeeeeeeeeeee.mp3", "YIPPEEEE", "C", (0.42, 2.2), 0.16, 0,
               "The 'yippee' creature.", "Ironic joy: a tiny celebration of something mediocre (or genuinely great).",
               "A team that won anyway.", True),
    "airhorn": ("mlg-airhorn.mp3", "MLG air horn", "E", (0.0, 2.73), 0.36, 0,
                "The MLG air horn.", "Hype that's knowingly dated: fan-meltdown only.", "Meltdown format's last stat.", False),
    "mj_heehee": ("michael-jackson-hee-hee.mp3", "Michael Jackson 'hee hee'", "D", (0.48, 1.35), 0.14, 0,
                  "MJ's 'hee hee!'", "A cheeky button on a silly line.", "'Grab a smaller ruler.'", True),
    "few_moments": ("a-few-moments-later-sponge-bob-sfx-fun.mp3", "A few moments later", "D", (0.0, 1.97), 0.0, 0,
                    "The SpongeBob narrator's time card.", "A cut to the aftermath.", "Before 'The Broncos won anyway.'", True),
    "among_us": ("among-us-role-reveal-sound.mp3", "Among Us role reveal", "D", (0.5, 3.6), 0.6, -1,
                 "The 'you are the impostor' reveal sting.", "Revealing who was actually responsible.", "Exhibit C in the courtroom.", False),
    "crickets": ("awkward-cricket-sound-effect.mp3", "Awkward crickets", "D", (0.4, 3.0), 0.0, -6,
                 "Crickets.", "The silence after a joke that the player's stat line made for you.", "After 'His? ... 3.'", False),
    "taco_bell": ("taco-bell-bong-sfx.mp3", "Taco Bell bong", "D", (0.07, 1.54), 0.17, 1,
                  "The Taco Bell 'bong'.", "A sudden reveal or a reversal.", "'The Broncos won anyway.'", False),
    "rip_granny": ("rip-my-granny-loud-asf.mp3", "rip my granny loud asf", "E", (0.13, 2.6), 0.08, -2,
                   "Blown-out loud screaming (ear-rape meme).", "Chaos cut only, and short.", "Chaotic version's last hit.", True),
}
CATEGORIES = {"A": "Roast / disrespect", "B": "Absurd reactions", "C": "Ironic praise / glazing",
              "D": "Sudden reversals", "E": "Pure chaos"}

# The three Bo Nix punchlines (times in the reference render) and their treatments:
# (label, sound id or None, anchor time s, cut the music at this time?)
PUNCHLINES = [
    {"id": "ruler", "line": "Measuring Bo Nix's passes against the Seahawks? Grab a smaller ruler.", "window": (0.0, 5.6),
     "versions": [("Current (boing)", "CURRENT", None, False), ("A: BRUH", "bruh", 4.30, False),
                  ("B: MJ 'hee hee'", "mj_heehee", 4.30, False), ("C: silence (music cuts on 'ruler')", None, 4.26, True)]},
    {"id": "air", "line": "Of his 107 passing yards? 20 came in the air.", "window": (8.7, 14.4),
     "versions": [("Current (boom on 20)", "CURRENT", None, False), ("A: FAHHH", "fahhh", 12.72, False),
                  ("B: metal pipe", "metal_pipe", 12.72, True), ("C: oh hell nah", "oh_hell_nah", 12.72, False)]},
    {"id": "home", "line": "The Broncos won anyway, 17 to 14. Tell the deep safety he can go home.", "window": (13.0, 19.0),
     "versions": [("Current (sad trombone)", "CURRENT", None, False), ("A: GET OUT", "get_out", 17.90, True),
                  ("B: sad violin", "sad_violin", 17.90, False), ("C: hard cut (music stops on 'home')", None, 17.87, True)]},
]
TAIL_S = 1.4          # the last punchline holds its last frame this long so the reaction can finish


def _decode(path: Path) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.float32).copy()


def _active_rms(x: np.ndarray) -> float:
    """RMS over the sounding part only (blocks within 30 dB of the loudest), so silence doesn't dilute it."""
    blk = x[: len(x) // 480 * 480].reshape(-1, 480)
    r = np.sqrt((blk ** 2).mean(1))
    keep = r > r.max() * 10 ** (-30 / 20)
    return float(np.sqrt((r[keep] ** 2).mean())) if keep.any() else 1e-9


def sound(sid: str, ref_rms: float = 0.1) -> np.ndarray:
    """Trimmed, faded, and levelled against the narration (`ref_rms`): a reaction sits a few dB over the voice, a
    background bit under it, whatever the upload's own loudness was."""
    f, _, _, (a, b), _, level, *_ = CATALOG[sid]
    x = _decode(SOUNDS / f)[int(a * SR):int(b * SR)]
    n = min(len(x), int(0.04 * SR))
    x[-n:] *= np.linspace(1, 0, n)
    x = x * (ref_rms * 10 ** (level / 20) / _active_rms(x))
    return x.astype(np.float32)


def stems(cfg: Config) -> tuple[np.ndarray, np.ndarray]:
    """The reference episode's narration and music as separate stereo stems (no effects), 48 kHz."""
    script = json.loads((EPISODE / "script.json").read_text())
    tl = json.loads((EPISODE / "work/timeline.json").read_text())
    voice, sr = [], None
    for seg in script["narration_segments"]:
        data, sr = sf.read(EPISODE / f"audio/{seg['id']}.wav", dtype="float32")
        voice.append(data)
    v = A.upsample(np.concatenate(voice), sr)
    st = script["structure"]
    full = SA.mix(v, [], st, tl["scenes"], cfg["shorts"], with_meme=False)
    vo = SA.mix(v, [], {**st, "music": False}, tl["scenes"], cfg["shorts"], with_meme=False)
    return vo, full - vo


def _write(path: Path, x: np.ndarray) -> None:
    sf.write(path, x, SR, subtype="PCM_16")


def clip(p: dict, version: tuple, vo: np.ndarray, bed: np.ndarray, out: Path) -> None:
    label, sid, anchor, cut = version
    t0, t1 = p["window"]
    last = abs(t1 - 19.0) < 0.05
    if sid == "CURRENT":                                   # the render as it is now (what you heard in ass.mp4)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(t0), "-to", str(t1), "-i", str(EPISODE / "final.mp4"),
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "copy", str(out)], check=True)
        return
    i0, i1 = int(t0 * SR), int(t1 * SR)
    tail = int(TAIL_S * SR) if last else 0
    v = np.pad(vo[i0:i1], ((0, tail), (0, 0)))
    m = np.pad(bed[i0:i1], ((0, tail), (0, 0)))
    if cut:                                               # the music stops dead on the anchor
        k = int((anchor - t0) * SR)
        ramp = int(0.015 * SR)
        m[k:] = 0
        m[max(0, k - ramp):k] *= np.linspace(1, 0, k - max(0, k - ramp))[:, None]
    fx = np.zeros_like(v)
    if sid:
        s = sound(sid, _active_rms(vo[:, 0]))
        k = int((anchor - t0 - CATALOG[sid][4]) * SR)
        j = min(len(fx), k + len(s))
        fx[k:j] += s[: j - k, None] * 0.9
    mixed = v + m + fx
    mixed /= max(1.0, np.abs(mixed).max() / 0.89)
    wav = out.with_suffix(".wav")
    _write(wav, mixed)
    vf = ["-vf", f"tpad=stop_mode=clone:stop_duration={TAIL_S}"] if last else []
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(t0), "-to", str(t1), "-i", str(EPISODE / "final_nosfx.mp4"),
                    "-i", str(wav), "-map", "0:v", "-map", "1:a", *vf, "-c:v", "libx264", "-preset", "veryfast",
                    "-crf", "20", "-af", "loudnorm=I=-14:TP=-1.5:LRA=11,alimiter=limit=0.82:level=disabled", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
                    str(out)], check=True)
    wav.unlink()


PALETTES = {
    "FLAME (roasts)": [("fahhh", "the default reaction to the stat that ruins it"),
                       ("get_out", "when the line throws someone out (the safety, the QB, the play-caller)"),
                       ("bruh", "a small deadpan aside under a setup"),
                       ("metal_pipe", "the deadpan format's one hit, with the music cut"),
                       ("emotional_damage", "when the stat hurts the fan base itself"),
                       ("crickets", "after a reveal that lands to silence"),
                       ("mj_heehee", "a cheeky button on a goofy line")],
    "PRAISE (hype / glazing)": [("undertaker", "the arrival: the first giant number"),
                                ("anime_wow", "real awe at something ridiculous"),
                                ("heavenly", "the glazing peak: 'nobody's seen more'"),
                                ("rizz", "smooth, effortless plays"),
                                ("shocked", "the comparison that makes it absurd (his 24 vs their 18)"),
                                ("yippee", "ironic or genuine tiny celebration"),
                                ("airhorn", "fan-meltdown only")],
}
RETIRE = ["boing (synthesized)", "slide whistle (synthesized)", "sad trombone (synthesized 'wah wah')",
          "cinematic hit (synthesized trailer boom)", "choir (synthesized)", "bonk (synthesized)",
          "air horn (synthesized: if the format wants one, use the real MLG horn)",
          "the default 'boom' on every stat reveal (vine boom only on the single most absurd number)"]


def page(clips: dict) -> str:
    e = html.escape
    rows = []
    for p in PUNCHLINES:
        cells = "".join(f'<figure><video controls preload="metadata" src="clips/{e(f)}"></video><figcaption>{e(lbl)}'
                        f'</figcaption></figure>' for lbl, f in clips[p["id"]])
        rows.append(f'<section><h3>"{e(p["line"])}"</h3><div class="row">{cells}</div></section>')
    cats = []
    for c, title in CATEGORIES.items():
        items = []
        for sid, (f, name, cat, (a, b), _, _, what, when, nfl, speech) in CATALOG.items():
            if cat != c:
                continue
            items.append(f'<tr><td><b>{e(name)}</b><br><audio controls preload="none" src="trimmed/{sid}.wav"></audio></td>'
                         f'<td>{e(what)}</td><td>{e(when)}</td><td>{e(nfl)}</td><td>{b - a:.1f}s{" · speech" if speech else ""}'
                         f'</td><td><a href="{MI}{e(f)}">source</a></td></tr>')
        cats.append(f'<h3>{c}. {e(title)}</h3><table><tr><th>Sound</th><th>What it is</th><th>When it is funny</th>'
                    f'<th>NFL use</th><th>Length</th><th>Link</th></tr>{"".join(items)}</table>')
    pals = "".join(f'<h3>{e(k)}</h3><ul>' + "".join(f'<li><b>{e(CATALOG[s][1])}</b>: {e(w)}</li>' for s, w in v)
                   + "</ul>" for k, v in PALETTES.items())
    retire = "".join(f"<li>{e(r)}</li>" for r in RETIRE)
    labels = {"A-modern": "A, modern: BRUH / vine boom / FAHHH / GET OUT", "B-minimal": "B, minimal: one FAHHH, hard cut",
              "C-chaotic": "C, chaotic: hee hee / boom / FAHHH / Taco Bell / GET OUT"}
    drafts = "".join(f'<figure><video controls preload="metadata" src="drafts/{e(f.name)}"></video><figcaption>'
                     f'{e(next((v for k, v in labels.items() if k in f.name), f.stem))}</figcaption></figure>'
                     for f in sorted((OUT / "drafts").glob("*.mp4")))
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Meme SFX auditions</title><style>
:root{{color-scheme:light dark;--bg:#fff;--fg:#111;--mut:#666;--line:#ddd}}
@media (prefers-color-scheme:dark){{:root{{--bg:#111;--fg:#eee;--mut:#999;--line:#333}}}}
body{{background:var(--bg);color:var(--fg);font:15px/1.45 -apple-system,system-ui,sans-serif;margin:0 auto;max-width:1200px;padding:16px}}
.row{{display:flex;gap:12px;overflow-x:auto}} figure{{margin:0}} video{{width:220px;border-radius:8px;background:#000}}
figcaption{{font-weight:600;margin-top:4px}} table{{border-collapse:collapse;width:100%;margin-bottom:18px}}
td,th{{border-bottom:1px solid var(--line);padding:6px;vertical-align:top;text-align:left}} audio{{width:200px}}
p.note{{color:var(--mut)}}</style></head><body>
<h1>Meme sound auditions</h1>
<p class="note">Reference video: Bo Nix vs the Seahawks (identical to ~/Downloads/ass.mp4). Each row is the same shot and the
same narration; only the punchline's sound changes. Sounds are real soundboard clips (myinstants.com), local only,
never committed.</p>
<h2>0. Full drafts (the whole 19 s video)</h2><div class="row">{drafts}</div>
<h2>1. In context: three Bo Nix punchlines</h2>{"".join(rows)}
<h2>2. The palettes</h2>{pals}<h3>Retire now</h3><ul>{retire}</ul>
<h2>3. Every candidate</h2>{"".join(cats)}
</body></html>"""


def build() -> Path:
    cfg = Config.load()
    (OUT / "clips").mkdir(parents=True, exist_ok=True)
    (OUT / "trimmed").mkdir(parents=True, exist_ok=True)
    vo, bed = stems(cfg)
    for sid in CATALOG:                                   # the solo previews at the same level they'd sit in a video
        _write(OUT / "trimmed" / f"{sid}.wav", np.clip(sound(sid, _active_rms(vo[:, 0])) * 2.5, -0.95, 0.95))
    clips = {}
    for p in PUNCHLINES:
        clips[p["id"]] = []
        for i, ver in enumerate(p["versions"]):
            name = f"{p['id']}_{i}_{(ver[1] or 'silence').lower()}.mp4"
            clip(p, ver, vo, bed, OUT / "clips" / name)
            clips[p["id"]].append((ver[0], name))
    (OUT / "index.html").write_text(page(clips))
    (OUT / "catalog.json").write_text(json.dumps(
        {sid: {"file": v[0], "name": v[1], "category": CATEGORIES[v[2]], "trim_s": v[3], "hit_offset_s": v[4],
               "level_vs_narration_db": v[5], "what": v[6], "when_funny": v[7], "nfl_use": v[8], "speech": v[9],
               "source_url": MI + v[0], "status": "local only (soundboard clip, not committed)"}
         for sid, v in CATALOG.items()}, indent=2))
    return OUT / "index.html"


if __name__ == "__main__":
    print(build())


def full_draft(name: str, hits: list[tuple[str, float]], cuts: list[float] = ()) -> Path:
    """The whole reference video with these meme hits [(sound id, anchor s)] and music cuts, for a first listen."""
    cfg = Config.load()
    vo, bed = stems(cfg)
    tail = int(TAIL_S * SR)
    v, m = np.pad(vo, ((0, tail), (0, 0))), np.pad(bed, ((0, tail), (0, 0)))
    ramp = int(0.015 * SR)
    for c in cuts:                                         # the music stops dead here and stays out
        k = int(c * SR)
        m[k:] = 0
        m[max(0, k - ramp):k] *= np.linspace(1, 0, k - max(0, k - ramp))[:, None]
    fx = np.zeros_like(v)
    ref = _active_rms(vo[:, 0])
    for sid, anchor in hits:
        s = sound(sid, ref)
        k = int((anchor - CATALOG[sid][4]) * SR)
        j = min(len(fx), k + len(s))
        fx[k:j] += s[: j - k, None] * 0.9
    mixed = v + m + fx
    mixed /= max(1.0, np.abs(mixed).max() / 0.89)
    (OUT / "drafts").mkdir(parents=True, exist_ok=True)
    wav, out = OUT / "drafts" / f"{name}.wav", OUT / "drafts" / f"{name}.mp4"
    _write(wav, mixed)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(EPISODE / "final_nosfx.mp4"), "-i", str(wav), "-map", "0:v",
                    "-map", "1:a", "-vf", f"tpad=stop_mode=clone:stop_duration={TAIL_S}", "-c:v", "libx264",
                    "-preset", "veryfast", "-crf", "20", "-af", "loudnorm=I=-14:TP=-1.5:LRA=11,alimiter=limit=0.82:level=disabled",
                    "-c:a", "aac", "-b:a", "192k", "-ar", "48000", str(out)], check=True)
    wav.unlink()
    return out
