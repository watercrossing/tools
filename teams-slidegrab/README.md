# teams-slidegrab

Extract one clean screenshot per slide from a **recording of a presentation**, by reading the on-screen **slide counter** (e.g. Teams PowerPoint Live's `N of 61`) with local OCR.
CPU-only, no GPU, and **nothing ever leaves the machine** — only the numeric counter is read; slide content is never uploaded.

## What this is for

This tool is for the common Teams situation where a presenter shared their deck **live** — via PowerPoint Live in the meeting — so everyone could see the slides, but **never shared the actual file afterwards**.
You can see the slides in your recording, but you don't have the `.pptx`, and asking again isn't always an option.

If you have a **recording of that meeting**, this tool reconstructs the deck as a folder of per-slide images.
It works because PowerPoint Live renders a **`Slide X of Y` counter** at the bottom of the shared view: it reads that counter frame-by-frame, detects each advance, and grabs the most-complete frame of every slide — so you get one screenshot per slide, in order, verified against the counter.

A suitable recording is anything that captured the shared screen, for example:

- an **[OBS](https://obsproject.com/)** capture of the meeting window you recorded yourself, or
- the meeting's own **Microsoft Stream** recording (the "Recording" that Teams saves — download the `.mp4` and point the tool at it).

Because the whole method keys off the visible counter, it needs a presentation that **shows one** (PowerPoint Live does; a plain screen-share of slideshow mode may not — see [Limitations](#limitations)).
It is not OCR of the slide *content* and it is not a `.pptx` reconstruction — it recovers the slides as **images**, which is usually exactly what you wanted when the file was never shared.

## Requirements

- **ffmpeg / ffprobe** (any recent build).
- **[uv](https://docs.astral.sh/uv/)** — the script declares its Python and dependencies inline (PEP 723), so `uv run` fetches them into a throwaway env; there is nothing to install.
- **Windows** — OCR uses `winocr`, a wrapper around the built-in `Windows.Media.Ocr` engine.
  On other OSes, swap the OCR backend (e.g. `pytesseract`); see *Porting* below.

## Usage

```bash
uv run teams-slidegrab.py VIDEO OUTDIR        # or ./teams-slidegrab.py VIDEO OUTDIR
```

Example:

```bash
uv run teams-slidegrab.py "meeting.mp4" ./slides \
    --ffmpeg "C:/tools/ffmpeg/bin/ffmpeg.exe" --ffprobe "C:/tools/ffmpeg/bin/ffprobe.exe"
```

Add `--crop` to trim each saved slide down to just the slide, dropping the surrounding Teams UI (see [Cropping](#cropping-to-the-slide---crop)):

```bash
uv run teams-slidegrab.py "meeting.mp4" ./slides --crop
```

Output: `OUTDIR/slide_01.png …`, plus `OUTDIR/_manifest.md` (slide → timestamp → verification).
Intermediates and caches live in `OUTDIR/.slidegrab/` (`calib.json`, `timeline.json`, `report.json`).

## How it works (3 cached stages)

1. **calibrate** — samples `--calib-samples` frames across the video and OCRs each full frame (upscaled `--calib-scale`× so small counters are legible).
   It finds every `N of M` with its **bounding box**, infers the deck total `M`, and clusters positions into counter **regions**.
   This auto-handles recordings where the counter **moves** (layout changes) mid-talk.
2. **scan** — one ffmpeg decode pass emits a tight counter crop every `--step` seconds, and each is OCR'd (per region) into a slide-number **timeline**.
   Batch extraction is ~100× faster than seeking per frame.
3. **extract** — refines every short transition at **native frame rate** (to catch sub-second slides and pin the exact advance time), then saves the full frame `--lead` s before each advance (its most-complete state).
   Every saved frame is **counter-verified** to match.

Re-running reuses the caches; force with `--recalibrate` / `--rescan`.
`--stage {calibrate,scan,extract,crop}` runs a single stage.
`--dry-run` plans without writing images.

## Cropping to the slide (`--crop`)

Teams frames the slide inside a lot of chrome: the participant thumbnail strip, the people panel, the meeting toolbar, and the slide-nav bar.
`--crop` trims each saved frame down to just the slide, so the output is presentation-ready.

Detection is geometric, not OCR: the slide is the largest solid bright block sitting on Teams' dark "stage", so it is found by scanning brightness profiles rather than matching a template.
This tracks the slide automatically as the layout changes — for example when the people panel is toggled and the slide shifts and resizes — and it tolerates dense-table or dark-image slides whose interior isn't uniformly bright.
Each crop is sanity-checked; if no genuinely pillarboxed slide is found (an already-cropped image, or a capture with no dark stage), the frame is left untouched and noted in the manifest rather than mis-cropped.

Run it as its own stage over an already-extracted folder, without re-reading the video:

```bash
uv run teams-slidegrab.py --stage crop ./slides
```

`--stage crop` rewrites `OUTDIR/slide_*.png` in place and is idempotent (a second pass is a no-op).
Keep the originals if you want them: copy the folder first, or extract without `--crop` and crop a copy.

## Key options

| Flag | Default | Meaning |
|------|---------|---------|
| `--total N` | auto | deck size, if you don't trust auto-detection |
| `--counter-regex RE` | `(\d+)\s*(?:of|/)\s*(\d+)` | counter format (supports `N of M` and `N/M`) |
| `--step S` | `2.0` | scan resolution in seconds |
| `--lead L` | `1.0` | grab this many seconds before a slide advances |
| `--range T0 T1` | whole video | limit to a time window |
| `--gap-thr G` | `15` | counter-gap over G s ⇒ "presenter left the slides" → grab last confirmed frame |
| `--brief-thr B` | `2.5` | slide shown under B s ⇒ grab window midpoint instead |
| `--calib-scale` / `--calib-samples` | `2.0` / `40` | calibration OCR upscale / sample count |
| `--crop` | off | crop each saved slide to the slide region, removing the surrounding Teams UI |
| `--crop-band T B` | `0.25 0.935` | slide-search band as fractions of frame height (excludes thumbnail strip / nav bar) |

## What it handles well

- Counter that **moves** or **auto-hides** partway through (multiple regions; native-fps refine).
- **Skipped/hidden** slides (numbers that never appear → reported "not shown", not faked).
- **Briefly-shown** slides (clicked through in ~1 s) and **long dwells / demos** (presenter leaves the slides, then returns).

## Limitations

- Needs a **visible numeric counter**.
  PowerPoint Live shows one; a plain screen-share of PowerPoint's slideshow mode usually does *not*, and decks with no counter have no reliable signal — pure scene-detection is too noisy (fires on webcams, cursors, animations) to trust.
- Only slides the presenter **actually displayed** can be recovered — if they skipped a slide, it isn't in the recording, so it can't be extracted (it's reported "not shown").
- Tuned defaults suit Teams PowerPoint Live at 1080p/30fps; adjust `--step`, `--calib-scale`, and `--counter-regex` for other sources.
- `--crop` assumes the Teams **dark** stage (the slide as a bright block pillarboxed on near-black); on a light theme, or any capture without that dark surround, it declines and leaves the frame full-size.
  For a differently-proportioned layout, adjust `--crop-band` to the fractions that clear the thumbnail strip and nav bar.

## Porting off Windows

OCR is isolated to `ocr_text()` / `_ocr()`.
Replace `winocr.recognize_pil_sync` with your engine (e.g. `pytesseract.image_to_string`, or `pytesseract.image_to_data` for the bounding boxes that calibration needs) and keep the rest.
