# screenshare-slidegrab

Extract one image per slide from a **recording of a Teams meeting** in which the slides were shown as a **plain screen share**, and bundle them into a PDF.
CPU-only, no OCR, and **nothing ever leaves the machine**.

## What this is for

Somebody presented for an hour (or a day), the meeting was recorded, and the deck was never sent round.
If they used PowerPoint Live, [teams-slidegrab](../teams-slidegrab/) reads the `Slide X of Y` counter and is the more exact tool.
But most presenters simply share their screen or a PowerPoint window — in slideshow mode, or often in the editor — and then there is no counter to read.

This tool works from the picture alone.
It looks for stretches in which the shared screen **holds still**, keeps one frame of each, and then throws out what isn't a new slide: animation build steps are folded into their final state, and slides the presenter goes back to are kept only once.
The comparison is made on the slide **cropped out and normalised in size**, so a presenter resizing their window, or Teams re-laying out the stage when someone turns on their camera, does not make an old slide look new.

## Requirements

- **ffmpeg / ffprobe** on `PATH` (or pass `--ffmpeg` / `--ffprobe`).
- **[uv](https://docs.astral.sh/uv/)** — the script declares its Python and dependencies (numpy, Pillow) inline, so there is nothing to install.

## Usage

```bash
uv run screenshare-slidegrab.py VIDEO OUTDIR        # or ./screenshare-slidegrab.py VIDEO OUTDIR
```

Output:

- `OUTDIR/slide_001.png …` — each slide, cropped to the shared screen (black letterbox bars trimmed).
- `OUTDIR/slides.pdf` — all of them in one file.
- `OUTDIR/_manifest.md` — when each slide first appeared, how long it was on screen, how many build steps were folded into it, when it was shown again, and the still stretches skipped because they showed no screen share.
  If the video has OBS's default file name (`2026-09-23 10-02-44.mp4`), times are also given as wall-clock times, handy for matching slides against the agenda.

An 8-hour 1080p recording takes about 8 minutes to scan on a 16-thread desktop; the scan is cached in `OUTDIR/.slidegrab/` (about 20 KB per second of video — delete it when you're happy), so re-running with different thresholds takes seconds.
To try settings on part of a long recording first, use `--range 0 3600`.

## How it works

1. **scan** — a single ffmpeg decode pass samples the video every `--step` s, crops the *stage band* (below Teams' participant strip, above the name label) and shrinks it to a 192×108 thumbnail.
   On each thumbnail it locates the shared screen: the Teams stage is one flat colour, read off the left edge, and the share is the one dense, slide-shaped block that differs from it.
   The difference is taken in colour, because a dark-purple slide can have the same *brightness* as the dark stage.
   Gallery view fills the whole width and so is not mistaken for a share.
2. **segment** — a sample "moves" when more than `--change` of its pixels differ from the previous one; the stretches in between that last at least `--min-dwell` s and show a share are candidate slides.
   Each is cropped to its share box and resized to 96×54, and compared with the slides kept so far:
   - nearly identical to one of them (under `--same` of pixels changed) → a revisit, dropped;
   - the last kept slide plus content that appeared where it was blank → a **build step**, which replaces it (the final, most complete state is what you keep).
     This is told apart from a new slide on the same template by where the change is: a build adds text to empty space, a new slide replaces old text.
   - an *earlier* build state of a kept slide (the presenter stepping back) → a revisit, dropped.
3. **extract** — each slide is grabbed at full resolution from just before it changed, cropped to the share, and written out.

## Options

| Flag | Default | Meaning |
|------|---------|---------|
| `--step S` | `1` | sample every S seconds |
| `--min-dwell S` | `2` | ignore anything on screen for less than S seconds |
| `--change F` | `0.001` | fraction of changed pixels that counts as motion — raise it if a moving mouse pointer splits slides |
| `--same F` | `0.01` | fraction of changed pixels under which two slides are the same |
| `--band TOP BOTTOM` | `0.18 0.972` | the stage band, as fractions of frame height |
| `--range T0 T1` | whole video | only look at this window (seconds) |
| `--keep-builds` | off | keep every animation build step |
| `--keep-revisits` | off | keep a slide again every time it is shown |
| `--keep-all` | off | also keep still stretches without a screen share (gallery view, a speaker's camera) |
| `--no-crop` / `--no-pdf` | off | save full frames / skip the PDF |
| `--dry-run` | off | scan and segment, write no images |

## Limitations

- It keys on stillness, so a slide with a **video** or a continuously moving element is skipped, and anything shown for under `--min-dwell` s is missed.
- Expect a few **near-duplicates** where the presenter was still arranging their share (resizing the window, switching from editor to slideshow), and in **editor view** the PowerPoint window is what you get — ribbon, thumbnail pane and all.
  Both are easy to weed out by eye; a missed slide is not, so the tool errs on the side of keeping.
- The stage band and the stage detection assume Teams' default layout at 16:9 with the participant strip on top; for other layouts or apps, adjust `--band`, or use `--no-crop`.
- Only slides that were actually shown can be recovered.
