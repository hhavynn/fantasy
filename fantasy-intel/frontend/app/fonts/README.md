# Self-hosted fonts

Loaded by `next/font/local` in `app/layout.tsx`, so `next build` never fetches Google Fonts.
These are the same files Google Fonts serves for these families: the latin subset (preloaded) plus IBM Plex Sans
Greek, loaded only on pages that show a "Δ". Each one was checked against Google's served font (same version,
metrics, glyph count, axes).

| File | Source package | License |
| --- | --- | --- |
| `barlow-condensed-latin-{500,600}-normal.woff2` | `@fontsource/barlow-condensed@5.3.0` (`files/`) | OFL-1.1, `LICENSE-barlow-condensed.txt` |
| `ibm-plex-sans-{latin,greek}-wght-normal.woff2` (variable, wght 100–700) | `@fontsource-variable/ibm-plex-sans@5.3.0` (`files/`) | OFL-1.1, `LICENSE-ibm-plex-sans.txt` |
| `ibm-plex-mono-latin-{400,500}-normal.woff2` | `@fontsource/ibm-plex-mono@5.3.0` (`files/`) | OFL-1.1, `LICENSE-ibm-plex-mono.txt` |

To refresh: `npm pack <package>@<version>`, then copy the files above from the tarball's `package/files/` and its `LICENSE`.
