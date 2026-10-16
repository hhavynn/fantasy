"""Stage 5a: one still per shot (shots/<shot_id>.png), drawn only from facts.json and its claims.

Usage charts are matplotlib; cards are Pillow. Every number shown comes from a claim's display string or
from the snapshot series formatted by the same numbers.render, so charts and narration cannot disagree.
"""

from __future__ import annotations

import io

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager  # noqa: E402
from PIL import Image  # noqa: E402

from .. import visual as V  # noqa: E402
from ..core import FONTS, Episode, read_json, run_stage  # noqa: E402
from ..numbers import TEAM_NAMES, render  # noqa: E402

VERSION = 2
# Everything except the footer stays above this line; the band below it belongs to burned-in captions.
CONTENT_BOTTOM = 810

for _f in FONTS.glob("Inter-*.ttf"):
    font_manager.fontManager.addfont(str(_f))


class Shots:
    def __init__(self, ep: Episode):
        self.ep = ep
        self.facts = read_json(ep.path("facts.json"))
        self.claims = {c["id"]: c for c in self.facts["claims"]}
        self.players = {p["gsis_id"]: p for p in self.facts["players"]}
        self.size = ep.size
        self.week_label = self.d(self.facts["episode"]["week"])

    def d(self, cid: str) -> str:
        return self.claims[cid]["display"]

    def canvas(self) -> V.Canvas:
        return V.Canvas(self.size)

    def header(self, c: V.Canvas, p: dict, kicker: str):
        color = V.POSITIVE if p["role"] == "riser" else V.NEGATIVE
        c.text((80, 70), kicker.upper(), "bold", 26, color)
        c.text((80, 104), p["name"], "black", 64, V.INK)
        c.text((80 + c.text_width(p["name"], "black", 64) + 28, 140),
               f"{p['position']} · {self.d(p['team_name'])}", "semibold", 34, V.INK_MUTED, "ls")

    def window_note(self, p: dict) -> str:
        return f"Last {self.d(p['recent_games'])} games vs the {self.d(p['baseline_games'])} before"

    # ------------------------------------------------------------------------------------- shots

    def hero_stat(self, shot: dict, path):
        p = self.players[shot["player"]]
        m = p["metrics"][shot.get("metric", 0)]
        c = self.canvas()
        up = p["role"] == "riser"
        color = V.POSITIVE if up else V.NEGATIVE
        c.text((c.w / 2, 140), f"{self.week_label} usage report".upper(), "bold", 30, V.INK_FAINT, "mm")
        c.text((c.w / 2, 240), p["name"], "black", 96, V.INK, "mm")
        c.text((c.w / 2, 318), f"{p['position']} · {self.d(p['team_name'])}", "semibold", 40, V.INK_MUTED, "mm")
        big = f"{self.d(m['baseline'])}  →  {self.d(m['recent'])}"
        size = c.fit_text(big, "black", 200, c.w - 240)
        c.text((c.w / 2, 510), big, "black", size, V.INK, "mm")
        c.text((c.w / 2, 650), m["label"].upper(), "bold", 46, color, "mm")
        c.text((c.w / 2, 715), f"{self.d(m['change'])}  ·  {self.window_note(p)}", "semibold", 34, V.INK_MUTED, "mm")
        c.footer("Source: nflverse play-by-play and snap counts")
        c.save(path)

    def method_card(self, shot: dict, path):
        c = self.canvas()
        c.text((120, 110), "What we measure", "black", 76, V.INK)
        c.text((120, 215), "Recent games vs earlier games. No fantasy points. No projections.", "semibold", 34,
               V.ACCENT)
        rows = [("Snap share", "share of the team's offensive snaps he played"),
                ("Target share", "share of the team's targets"),
                ("Air-yard share", "share of the team's air yards"),
                ("Backfield rush share", "share of the running backs' designed rushes"),
                ("Pass rate over expectation", "how pass-heavy the team is versus situation"),
                ("Fantasy playoff schedule", f"{self.d(self.facts['episode']['playoff_weeks'])} opponents")]
        y = 290
        for name, desc in rows:
            c.rect((120, y, c.w - 120, y + 74), fill=V.SURFACE, outline=V.LINE)
            c.text((160, y + 37), name, "bold", 36, V.INK, "lm")
            c.text((760, y + 37), desc, "regular", 32, V.INK_MUTED, "lm")
            y += 88
        c.footer(f"{self.week_label} usage report")
        c.save(path)

    def section_card(self, shot: dict, path):
        c = self.canvas()
        up = "riser" in shot["title"].lower()
        c.text((c.w / 2, c.h / 2 - 40), shot["title"].upper(), "black", 150, V.POSITIVE if up else V.NEGATIVE, "mm")
        c.text((c.w / 2, c.h / 2 + 90), f"{self.week_label} · biggest role changes", "semibold", 44, V.INK_MUTED, "mm")
        c.save(path)

    def usage_chart(self, shot: dict, path):
        p = self.players[shot["player"]]
        c = self.canvas()
        self.header(c, p, "Usage riser" if p["role"] == "riser" else "Usage faller")
        c.text((80, 196), self.window_note(p), "regular", 30, V.INK_MUTED)
        chart = self._metric_panels(p, (c.w - 160, CONTENT_BOTTOM - 250))
        c.img.paste(chart, (80 * V.SCALE, 250 * V.SCALE))
        c.footer("Source: nflverse play-by-play and snap counts", "Shaded: recent window")
        c.save(path)

    def _metric_panels(self, p: dict, size: tuple[int, int]) -> Image.Image:
        metrics = p["metrics"][:3]
        dpi = 100
        fig, axes = plt.subplots(1, len(metrics), figsize=(size[0] * V.SCALE / dpi, size[1] * V.SCALE / dpi), dpi=dpi)
        axes = axes if len(metrics) > 1 else [axes]
        fig.patch.set_facecolor(V.BG)
        recent = set(p["recent_weeks"])
        fs = 11 * V.SCALE
        for ax, m in zip(axes, metrics):
            weeks = [s["week"] for s in m["series"]]
            vals = [s["value"] if s["value"] is not None else 0 for s in m["series"]]
            colors = [V.ACCENT if w in recent else "#3a4656" for w in weeks]
            ax.set_facecolor(V.BG)
            bars = ax.bar(range(len(weeks)), vals, color=colors, width=0.62)
            top = max(max(vals), 0.05)
            for b, s in zip(bars, m["series"]):
                label = render("pct", s["value"])[0] if s["value"] is not None else "—"
                ax.text(b.get_x() + b.get_width() / 2, b.get_height() + top * 0.03, label, ha="center", va="bottom",
                        color=V.INK, fontsize=fs, fontfamily="Inter", fontweight="semibold")
            idx = [i for i, w in enumerate(weeks) if w in recent]
            if idx:
                ax.axvspan(min(idx) - 0.45, max(idx) + 0.45, color=V.ACCENT, alpha=0.08, lw=0)
            ax.set_xticks(range(len(weeks)), [f"Wk {w}" for w in weeks])
            ax.set_ylim(0, top * 1.25)
            ax.set_yticks([])
            for side in ("top", "right", "left"):
                ax.spines[side].set_visible(False)
            ax.spines["bottom"].set_color(V.LINE)
            ax.tick_params(colors=V.INK_MUTED, labelsize=fs * 0.9, length=0)
            for t in ax.get_xticklabels():
                t.set_fontfamily("Inter")
            change = self.d(m["change"])
            ax.set_title(f"{m['label'].capitalize()}\n{self.d(m['baseline'])} → {self.d(m['recent'])}  ({change})",
                         color=V.INK, fontsize=fs * 1.25, fontfamily="Inter", fontweight="bold", loc="left", pad=18)
        fig.tight_layout(w_pad=4)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", facecolor=V.BG, dpi=dpi)
        plt.close(fig)
        img = Image.open(buf).convert("RGB")
        return img.resize((size[0] * V.SCALE, size[1] * V.SCALE), Image.LANCZOS)

    def context_card(self, shot: dict, path):
        p = self.players[shot["player"]]
        ctx = p["context"]
        c = self.canvas()
        self.header(c, p, "Team context")
        cards = [("Starting QB, recent games", self.d(ctx["qbs_recent"])),
                 ("Head coach", self.d(ctx["head_coach"])),
                 ("Pass rate over expectation", f"{self.d(ctx['proe'])}  ·  {self.d(ctx['pass_tendency'])}"),
                 ("League rank, PROE", f"{self.d(ctx['proe_rank'])} (1st = most pass-heavy)")]
        x0, y0, cw, ch = 80, 210, (c.w - 160 - 3 * 24) / 4, 130
        for i, (label, value) in enumerate(cards):
            x = x0 + i * (cw + 24)
            c.rect((x, y0, x + cw, y0 + ch), fill=V.SURFACE, outline=V.LINE)
            c.text((x + 24, y0 + 40), label.upper(), "bold", 20, V.INK_FAINT, "lm")
            c.text((x + 24, y0 + 88), value, "bold", c.fit_text(value, "bold", 36, cw - 48), V.INK, "lm")
        self._schedule_strip(c, p, 385)
        c.footer(f"Defense ranks: {p['defense_rank_label']}, so far this season (1st = most generous)")
        c.save(path)

    def _schedule_strip(self, c: V.Canvas, p: dict, y: int):
        c.text((80, y), "REST-OF-SEASON SCHEDULE", "bold", 24, V.INK_FAINT)
        c.text((c.w - 80, y), "Fantasy playoffs highlighted", "semibold", 24, V.ACCENT, "ra")
        sched = p["schedule"]
        n = len(sched)
        gap = 12
        bw = (c.w - 160 - gap * (n - 1)) / n
        top = y + 50
        for i, s in enumerate(sched):
            x = 80 + i * (bw + gap)
            playoff = s["playoff"]
            if s["opponent_value"] == "bye":
                c.rect((x, top, x + bw, top + 300), fill=V.SURFACE, outline=V.LINE)
                c.text((x + bw / 2, top + 40), f"Wk {s['week']}", "semibold", 24, V.INK_FAINT, "mm")
                c.text((x + bw / 2, top + 150), "BYE", "bold", 28, V.INK_FAINT, "mm")
                continue
            rank = s["rank_value"]
            band = "favorable" if rank <= 10 else "tough" if rank >= 23 else "middle-of-the-pack"
            color = V.BAND_COLOR[band]
            c.rect((x, top, x + bw, top + 300), fill=V.SURFACE_RAISED if playoff else V.SURFACE,
                   outline=V.ACCENT if playoff else V.LINE, width=4 if playoff else 2)
            c.text((x + bw / 2, top + 40), f"Wk {s['week']}", "semibold", 24, V.ACCENT if playoff else V.INK_FAINT, "mm")
            name = TEAM_NAMES[s["opponent_value"]]
            prefix = "vs" if s.get("home") else "at"
            c.text((x + bw / 2, top + 100), prefix, "regular", 22, V.INK_MUTED, "mm")
            c.text((x + bw / 2, top + 140), name, "bold", c.fit_text(name, "bold", 26, bw - 16), V.INK, "mm")
            label = render("ordinal", rank)[0]
            c.text((x + bw / 2, top + 215), label, "black", c.fit_text(label, "black", 44, bw - 24), color, "mm")
            c.rect((x + 14, top + 268, x + bw - 14, top + 276), fill=color, radius=4)

    def playoff_table(self, shot: dict, path):
        c = self.canvas()
        c.text((80, 90), "Fantasy playoff schedules", "black", 64, V.INK)
        c.text((80, 170), f"{self.d(self.facts['episode']['playoff_weeks'])} · opponent and defense rank so far this "
                          "season (1st = most generous)", "regular", 28, V.INK_MUTED)
        rows = [self.players[g] for g in shot["players"]]
        weeks = [s for s in rows[0]["schedule"] if s["playoff"]]
        y = 240
        rh = min(78, (CONTENT_BOTTOM - y) / (len(rows) + 1))
        cols = [80, 600, 1010, 1420]
        c.text((cols[0], y + rh / 2), "PLAYER", "bold", 22, V.INK_FAINT, "lm")
        for i, s in enumerate(weeks):
            c.text((cols[i + 1], y + rh / 2), f"WEEK {s['week']}", "bold", 22, V.ACCENT, "lm")
        y += rh
        for p in rows:
            c.line((80, y, c.w - 80, y))
            role_color = V.POSITIVE if p["role"] == "riser" else V.NEGATIVE
            c.text((cols[0], y + rh / 2), "▲" if p["role"] == "riser" else "▼", "bold", 22, role_color, "lm")
            c.text((cols[0] + 36, y + rh / 2), f"{p['name']}  ", "bold", 30, V.INK, "lm")
            c.text((cols[0] + 36 + c.text_width(p["name"] + "  ", "bold", 30), y + rh / 2),
                   f"{p['position']}", "regular", 24, V.INK_MUTED, "lm")
            for i, s in enumerate(x for x in p["schedule"] if x["playoff"]):
                if s["opponent_value"] == "bye":
                    c.text((cols[i + 1], y + rh / 2), "BYE", "bold", 28, V.INK_FAINT, "lm")
                    continue
                rank = s["rank_value"]
                band = "favorable" if rank <= 10 else "tough" if rank >= 23 else "middle-of-the-pack"
                c.text((cols[i + 1], y + rh / 2), f"{'vs' if s.get('home') else 'at'} {TEAM_NAMES[s['opponent_value']]}",
                       "semibold", 28, V.INK, "lm")
                c.text((cols[i + 1] + 290, y + rh / 2), render("ordinal", rank)[0], "black", 30,
                       V.BAND_COLOR[band], "lm")
            y += rh
        c.footer("Green = favorable (1st–10th) · Red = tough (23rd–32nd)")
        c.save(path)

    def cta_card(self, shot: dict, path):
        c = self.canvas()
        cta = self.ep.cfg["cta"]
        c.text((c.w / 2, c.h / 2 - 120), "THE USAGE REPORT", "black", 110, V.INK, "mm")
        c.text((c.w / 2, c.h / 2 + 10), "Every week: who gained a role, who lost one", "semibold", 44, V.INK_MUTED, "mm")
        line = cta["url"] or "Subscribe · free usage tool coming soon"
        c.rect((c.w / 2 - 520, c.h / 2 + 110, c.w / 2 + 520, c.h / 2 + 220), fill=V.ACCENT, radius=18)
        c.text((c.w / 2, c.h / 2 + 165), line, "bold", c.fit_text(line, "bold", 44, 980), "#08111d", "mm")
        c.save(path)


def _run(ep: Episode) -> None:
    script = read_json(ep.path("script.json"))
    shots = Shots(ep)
    out = ep.path("shots")
    out.mkdir(parents=True, exist_ok=True)
    for shot in script["shots"]:
        getattr(shots, shot["type"])(shot, out / f"{shot['id']}.png")
    ep.log(f"  charts: {len(script['shots'])} stills")


def run(ep: Episode) -> None:
    run_stage(ep, "charts", VERSION, {"size": ep.size, "cta": ep.cfg["cta"]},
              ["script.json", "facts.json"], ["shots"], lambda: _run(ep))
