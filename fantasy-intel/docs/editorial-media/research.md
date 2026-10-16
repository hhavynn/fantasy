# Real NFL photography and B-roll: where it can legitimately come from

Research for the editorial_media treatment (2026-10-16). The question: how a faceless fantasy-football channel
gets full-screen photos of real players, plus contextual B-roll, without breaking copyright. The reference is
The Athletic's Christian Watson TikTok (about 106 s): Getty action photos credited at the end ("Images Sourced
Via: … /Getty Images"), stock B-roll (a campus, a lab, a gym, cash), an article screenshot with a highlighted
line as evidence, and the reporter on camera. The Athletic pays for that Getty access. We have to earn each
picture's rights another way, or show a labelled fallback.

## Ratings

Scores are 1 (poor) to 5 (excellent). Checked with live API queries on 2026-10-16 where marked ✓.

| Source | NFL coverage | Quality | Action shots | Find a player | API | Download terms | Cost | Rate limits |
|---|---|---|---|---|---|---|---|---|
| **Wikimedia Commons** ✓ | 2: patchy, skewed to stars and to games teams posted as CC BY | 3: 1–6 MP; some are video stills | 2–3: a few real game frames | 3: categories and full-text search, plus licence metadata per file | 5: MediaWiki API, no key | Free reuse under each file's licence (CC0, PD, CC BY, CC BY-SA); attribution required for BY | Free | Polite use: serial requests, a User-Agent, `maxlag` |
| **Openverse** ✓ | 2: mostly Commons and Flickr mirrored | 3 | 2 | 3 | 5: no key needed | As the original licence | Free | Anonymous requests are throttled |
| **Flickr (CC)** | 2: fan photographers; a few game galleries | 3–4 | 3 | 2: tags vary | 4: needs a key | Per photo; many are NC (unusable) | Free | 3,600 requests/hour per key |
| **Team YouTube uploads marked CC BY** (Titans, Jets) ✓ | 2–3: those teams' games and media days | 3: 1080p stills | 3–4: real game frames | 2: you must know which video | – | CC BY covers copyright. YouTube's terms forbid downloading outside its own tools, so use stills already on Commons (licence-reviewed) and never pull video | Free | – |
| **DVIDS / US-government** ✓ | 1: military events, draft visits, Salute to Service | 4 | 1 | 2 | 3 | Public domain (US-government work); people shown can't be used to imply endorsement | Free | – |
| **Associated Press** (NFL's photo licensee) | 5 | 5 | 5 | 5 | 4: AP Newsroom | Editorial licence; commercial use only by agreement | Quote-based, typically a subscription | Contract |
| **Getty Images editorial** | 5 | 5 | 5 | 5 | 4: Getty API for licensees | Content marked editorial can't be used for commercial, promotional or advertising purposes without explicit authorisation. A monetised Short needs a written licence that covers it | Quote-based; rights-managed pricing depends on the use | Contract |
| **Imagn (USA TODAY Sports)** | 5 | 5 | 5 | 5 | 4: wire/API tier | Editorial; per photo or a flat-fee sports subscription | Quote-based (no public price) | Contract |
| **Official team and player media** | 4 | 5 | 4 | 4 | 1 | All rights reserved unless a team marks something CC BY or grants permission. Asking a team's PR desk is possible but slow | Free if granted | – |
| **Pexels** | 0: no NFL players | 4 | 0 (generic sport) | – | 5: free key | Pexels licence: free commercial use, no attribution required; no standalone resale; don't use people in a way that misleads or implies endorsement | Free | 200 requests/hour, 20,000/month (raisable) |
| **Pixabay** | 0 | 3–4 | 0 | – | 5: free key | Pixabay Content Licence: free commercial use; cache results (no hotlinking); no mass downloads | Free | About 100 requests/minute; cache 24 h |
| **Unsplash** | 0 | 5 | 0 | – | 5 | Unsplash licence; API terms require hotlinking and attribution | Free | 50/hour (demo), 5,000/hour (production) |
| **User-supplied** (the owner's own photos from games, practices, the stadium) | Whatever the owner shoots | Varies | Possible from the stands | – | – | The owner holds the copyright. Stadium rules may limit commercial use of photos taken inside | Free | – |

**NFL gameplay footage is never downloaded or reused** without a licence. That covers broadcast clips, NFL+ and
YouTube highlights. The studio has no code path that fetches video from YouTube or any game feed.
`licensed_video` exists only for clips the owner supplies with a declared licence.

## What is actually cleared today (in assets/media/manifest.json)

23 entries; files are cached locally and never committed. All Commons files start as review `pending`. No person
has approved them yet, so every demo is experimental and the publish check fails on purpose.

| Player / use | Cleared (pending review) | Blocked by the catalog | Gap |
|---|---|---|---|
| Puka Nacua (LA #12) | Rams training camp 2023 (CC BY-SA 4.0, #17 era); FanDuel TV interview 2024 (CC BY 3.0, TV lower-third cropped out); ESPN headshot (owner override, fallback) | BYU 2021 game photo: wrong uniform | No 2025–26 Rams **game** photo of him on any free source |
| Rams team | Run-out and sideline at Tennessee 2025 (Titans YouTube stills, CC BY 3.0) | – | – |
| Bo Nix (DEN #10) | Four 2025 Week 1 game frames: scramble, dropback, handoff, under centre (Titans YouTube stills, CC BY 4.0); 2025 Salute to Service camp (public domain); ESPN headshot | (Oregon 2023 and Auburn 2019 exist on Commons; not added) | The scramble and dropback are small (454–750 px wide): shown as cards, not full-bleed |
| Broncos team | Run-out vs TEN 2025 (CC BY 4.0) | – | – |
| Braelon Allen (NYJ #0) | ESPN headshot only (fallback) | High-school 2019 photo: not an NFL uniform | **No cleared photo of him as a Jet anywhere free.** The Short uses the labelled fallback |
| Breece Hall (NYJ #20) | Jets team video 2026 (CC BY 4.0); vs Bills 2023 (public domain, National Guard photographer); ESPN headshot | – | – |
| Jets team | MetLife Stadium, Bills at Jets 2023 (public domain) | – | – |
| Context B-roll | Tape measure (CC0), measuring tape (PD), wooden ruler (CC BY 2.0), ELF stadium at night (CC BY 4.0) | – | No video B-roll (Pexels/Pixabay keys not set) |

The Titans/Jets stills come from team YouTube videos the teams published under CC BY, which Commons licence
reviewers checked. Risk to note in review: a frame from a team video could in principle contain NFL Films or
broadcast footage the team didn't own. The frames used here (sideline/end-zone cameras, a tunnel run-out, a
media-day portrait) look team-produced. A person should confirm that when approving each one.

## Recommendation (most practical, lowest cost)

1. **Now, free:** Commons as the main source for real player photos, through the catalog. Fill gaps with the
   owner's ESPN-headshot override, always labelled FALLBACK. Get context B-roll from Commons and Openverse, and
   add Pexels/Pixabay keys (free) for video B-roll: stadiums, crowds, a ruler, a ticking clock.
2. **The real fix for coverage:** a paid editorial licence that names monetised short-form video:
   - an **Imagn** sports subscription or **AP** (the NFL's photo licensee), for every player, every week
   - a Getty licence is equivalent, but its default editorial terms exclude commercial use, so the written
     licence must cover a monetised channel
   - enter licensed files with `--license licensed --license_ref <invoice>`. The catalog then credits them on screen
   - get quotes before committing; none of these publish prices
3. **Cheap and good:** the owner's own photos from a game or practice, entered as `owner`. Check the
   venue's commercial-use rules first.
4. **Ask teams.** The Titans and Jets already release some video under CC BY; their social teams may grant more.

Sources:
[Getty licence agreement](https://www.gettyimages.in/eula) ·
[Getty help: the basics](https://www.gettyimages.fi/faq/basics) ·
[NFL/AP photo licensing (court record)](https://cite.case.law/f3d/903/185/) ·
[Imagn services](https://imagn.com/services/) ·
[Gannett launches Imagn (Poynter)](https://www.poynter.org/tech-tools/2019/gannett-just-launched-its-own-image-licensing-and-wire-service/) ·
[Pexels API documentation](https://pexels.com/api/documentation/) ·
[Pexels help: standalone use](https://help.pexels.com/hc/de-de/articles/900005880463) ·
[Free image API comparison 2026](https://blog.laozhang.ai/en/posts/free-image-api) ·
Wikimedia Commons and Openverse APIs (queried live).
