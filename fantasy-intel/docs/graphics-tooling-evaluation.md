# Graphics tooling evaluation

Which tools could make the Shorts look designed, what is already on this Mac, and the smallest toolchain that works. Short answer: **nothing new was installed.** The three new styles are built with the Python, Pillow, NumPy and FFmpeg the studio already uses, plus the Inter font already in the repo.

## What is already installed

Inventoried on 2026-10-15. The scan covered command-line tools, `/Applications`, the studio's own environment, Claude Code plugins and the model caches; it did not look through personal files.

| Tool | Status | Used by the studio |
|---|---|---|
| FFmpeg / ffprobe 9.0.2 (Homebrew) | installed | yes: encoding, loudness, QC probes. This build has no `drawtext` (so captions are drawn with Pillow), but has `gblur`, `tblend`, `minterpolate` |
| Python 3.11 via uv (studio `.venv`) | installed | yes |
| Pillow 12.3, NumPy 2.4, Matplotlib 3.11, SciPy 1.17 | installed in the studio env | Pillow and NumPy draw every frame; Matplotlib draws the long-form charts |
| Pillow libraqm (OpenType features such as `tnum`) | **not available** | tabular figures are emulated in `kit.text_layer(tabular=True)` |
| Node.js 26.3 / npm / npx | installed | no (Remotion would use it) |
| `rsvg-convert` (librsvg), cairo, pango, harfbuzz, freetype, fontconfig (Homebrew libraries) | installed | no; SVG rasterising is available if ever needed |
| Figma.app | installed | no |
| Blender, Aseprite, Pixelorama, LibreSprite, Inkscape, ImageMagick | **not installed** | — |
| Remotion, Motion Canvas | **not installed** (no projects found) | — |
| Fonts | Inter Regular/SemiBold/Bold/Black (OFL) in `studio/assets/fonts`; nothing extra in `~/Library/Fonts` | Inter is the only typeface. Apple system fonts are not licensed for burned-in video (see the studio plan) and stay unused |
| Claude Code plugins and skills | `superpowers`, `vercel`; the `impeccable` design skill | none of them render video |
| MCP servers relevant to design | none for Remotion, Aseprite or Penpot | — |
| Local AI (for reference, untouched) | Kokoro-82M (narration), Qwen3-30B-A3B GGUF with `llama-server` (scripts), MuseTalk, LatentSync 1.6, Wan2.2-TI2V-5B, Z-Image-Turbo / mflux, FLUX.1-schnell in the HF cache; about 81 GB under `~/models` | the graphics work uses none of them: the brief asks for designed graphics, not generated images |

## External options

| Option | Quality ceiling | Claude Code automation | Mac | Local now | Size and deps | Licence | Upkeep | Speed | Integration cost | Solves a real limit here? |
|---|---|---|---|---|---|---|---|---|---|---|
| **Current Python + Pillow + FFmpeg** (improved) | high for flat broadcast and data graphics; no GPU effects, no real typography engine (no kerning across tracked text, no OpenType features without raqm) | full: plain code | yes | yes | 0 | already in use | ours | 15–30 s per 25 s Short at 1080x1920 after this pass | none | — |
| **Remotion** + its [Claude Code plugin](https://www.remotion.dev/docs/ai/claude-code-plugin) | very high: React, CSS, SVG, real web typography, Lottie, springs | the plugin adds Remotion skills to Claude Code | yes (Node + Chromium) | no | Node packages plus a downloaded headless Chromium, roughly a few hundred MB | Remotion licence: free for individuals and small companies (about three people or fewer); larger companies need a paid company licence. Not an OSI licence | very active | slower than Pillow: a headless browser screenshots each frame, so expect minutes per Short on CPU | high: a second renderer in another language, and the claim data, word timings and audio mix would have to be handed across | partly: better type rendering and easier complex layout. Not needed for anything shown in this pass |
| **Motion Canvas** | high: TypeScript scenes with a timeline and an editor | ordinary code; no official plugin | yes | no | Node packages; renders in a browser | MIT | active but slower-moving | similar to Remotion (browser) | high, as above | same as Remotion; its timeline editor helps hand animation more than data-driven batch rendering |
| **Aseprite** + [aseprite-mcp](https://github.com/Vollkorn-Games/aseprite-mcp) | the pixel-art standard editor | the MCP drives Aseprite in batch mode through Lua: sprites, layers, palettes, export | yes | no | Aseprite itself (paid app, or build from its source-available code) plus Node 18 and pnpm | MCP: MIT. Aseprite: proprietary EULA | MCP: early (2 stars, 13 commits) | fast for sprite export | medium: sprites would become committed PNG assets | not yet. Hand-placed sprite art would beat the procedural sprites here, but the pixel style already renders sharp, on-grid art from code |
| **Pixelorama** | good pixel editor (Godot based) | no MCP; scriptable only through Godot | yes | no | about 100 MB app | MIT | active | — | medium | no; an editor for a human, and nothing for automation |
| **Penpot MCP** (official, now in the main Penpot repo; [docs](https://help.penpot.app/mcp/)) | design-file authoring | yes, a handful of tools centred on running code against a Penpot file | yes (hosted, or local via npm) | no | hosted account, or local Node | MPL-2.0 | active | n/a (it is not a renderer) | medium | no: it helps design static layouts, not render frames. Worth a look if a designer joins and wants the style guide as a Penpot file |
| **Figma MCP** | design-file authoring | yes (Dev Mode MCP) | Figma.app is installed | partly | paid seat features | proprietary | active | n/a | medium | no, for the same reason |
| Blender | 3D and motion graphics | Python API | yes | no | about 1 GB | GPL | active | slow for this | high | no; 3D isn't part of the brief |
| Lottie / rlottie, Manim, MoviePy | — | code | yes | no | small to medium | MIT / MIT / MIT | varies | — | medium | no limit they remove that Pillow doesn't already cover |

## Recommendation

**Keep the Python renderer and build on it.** The real problems (see `studio-graphics-audit.md`) were design problems: no tokens, no type hierarchy, competing backgrounds, decoration, hidden scales, and no layout QA. All of them were fixed in code, and the result renders fast and needs no new dependencies. A second rendering system (Remotion or Motion Canvas) would mean re-implementing the claim plumbing, word timings and audio hand-off in another language, for type-rendering gains this pass did not need.

Revisit only with a concrete trigger:
1. **A licensed display typeface** (for example an OFL condensed face such as Barlow Condensed for the broadcast look, about 100 KB per weight) would do more for broadcast than any new tool. It is a download, so it needs your OK; the broadcast style currently uses Inter Black with a shear for italics.
2. **Aseprite and aseprite-mcp**, if you want hand-drawn sprites (player poses, a run cycle, team helmets) instead of the procedural sprites. It would be a paid app plus an early-stage MCP; output would be committed PNG sprite sheets that `pixel.py` loads.
3. **Remotion**, only if a style needs things Pillow can't do well: complex text layout and kerning at scale, vector morphing, Lottie animations. If so, use it as an offline asset and overlay renderer, not a replacement.

Nothing in this list was installed, downloaded or cloned.

Sources: [Remotion Claude Code plugin](https://www.remotion.dev/docs/ai/claude-code-plugin), [aseprite-mcp](https://github.com/Vollkorn-Games/aseprite-mcp), [Penpot MCP docs](https://help.penpot.app/mcp/), [Penpot MCP announcement coverage](https://dev.to/creeta/penpots-mcp-server-runs-on-five-tools-figmas-uses-dozens-354e). The Remotion licence summary is from its published licence terms (remotion.dev/license) as of early 2026; check the current terms before adopting it.
