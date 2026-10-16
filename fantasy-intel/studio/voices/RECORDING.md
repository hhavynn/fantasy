# Recording your voice (about 10 minutes, once)

The Shorts can be narrated in **your** voice: Kokoro still performs each line (timing, pacing, word
timestamps), and OpenVoice V2 re-voices it with your voice's tone. Only your own voice may be enrolled;
QC blocks publishing any cloned narration that lacks your declaration.

What it copies: the sound of your voice (timbre). What it doesn't: your accent, rhythm or delivery. Those
still come from Kokoro, so expect "your voice, reading like a newscaster." Clean, energetic recordings
give the best result.

## Setup

- A quiet room with soft furnishings (a closet full of clothes is ideal). No TV, music, fans or echo.
- Phone voice memo is fine; a USB mic is better. Hold it about a hand's width from your mouth, a little
  off to the side, and keep that distance for every take.
- Talk like you're telling a friend about a waiver pickup: relaxed, a bit of energy. Not a flat read.

## Record

1. Read the lines in `script.txt`: one file per line or a few lines per file, **90 seconds or more in
   total** (30 seconds is the minimum).
2. Re-record any take with a cough, a laugh over words, background noise, or a mic bump.
3. Export as WAV (or FLAC). From a phone's `.m4a`:

   ```bash
   ffmpeg -i take1.m4a -ac 1 -ar 44100 take1.wav
   ```

4. Put the files in `voices/<yourname>/raw/` (this folder is never committed).

## Enroll

From `fantasy-intel/studio`:

```bash
uv run enroll-voice --name yourname --i-am-the-speaker voices/yourname/raw/*.wav
```

`--i-am-the-speaker` records your declaration that you are the speaker and consent to a synthetic version
of your voice narrating this channel. The tool rejects clipped, very quiet, or too-short audio.

## Use it

Try it on one Short without touching the real episode:

```bash
uv run make-episode --week 7 --player dalton-kincaid --voice yourname
```

When you like it, set `voice_clone = "yourname"` under `[shorts]` in `config.toml`. Every description
then says "Narrated with an AI voice (a synthetic version of the creator's own voice)", and provenance
records the recordings' hashes, your declaration and the OpenVoice weights. OpenVoice also embeds an
inaudible watermark marking the audio as synthetic.
