#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "numpy",
#     "pillow",
# ]
# ///
"""
screenshare-slidegrab.py - Extract one image per slide from a recording of a Teams meeting in which the
slides were shown as a plain screen share (no "Slide X of Y" counter to read - for that, see teams-slidegrab).
Everything runs locally; no OCR, no network.

Run (deps auto-installed from the inline script metadata above):
    uv run screenshare-slidegrab.py VIDEO OUTDIR [options]     # or ./screenshare-slidegrab.py VIDEO OUTDIR

Stages:
    1. scan    - one ffmpeg decode pass: every --step s, shrink the stage band (below the participant strip)
                 to a small thumbnail and locate the shared screen on it. Cached in OUTDIR/.slidegrab/.
    2. segment - find runs where the stage holds still for >= --min-dwell s and shows a screen share, then
                 compare what is on them *cropped to the share and normalised in size*, so that a presenter
                 resizing their window or Teams re-laying out the stage does not make a slide look new:
                 animation build steps fold into their final state and revisits of a slide are dropped.
    3. extract - grab each slide at full resolution, crop it to the shared screen (trimming black
                 letterbox bars), and write slide_NNN.png, slides.pdf and _manifest.md.
"""
import argparse, json, re, subprocess, shutil, sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
import numpy as np
from PIL import Image

TW, TH = 192, 108  # thumbnail of the stage band, for change detection
FW, FH = 96, 54    # a slide cropped to its share box and resized to this, for comparing slides with each other
PIX = 20           # a pixel counts as "changed" if it moves by more than this (0-255)
SCAN_VERSION = 3


def probe(ffprobe, video):
    j = json.loads(subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "format=duration:stream=width,height",
                                   "-of", "json", str(video)], capture_output=True, text=True, check=True).stdout)
    return float(j["format"]["duration"]), j["streams"][0]["width"], j["streams"][0]["height"]


def hms(t):
    t = int(round(t))
    return f"{t // 3600:d}:{t // 60 % 60:02d}:{t % 60:02d}"


def runs(mask):
    """(start, end) index pairs (end exclusive) of the True runs in a 1-D bool array."""
    d = np.diff(np.concatenate([[0], mask.astype(np.int8), [0]]))
    return list(zip(np.flatnonzero(d == 1), np.flatnonzero(d == -1)))


def slide_box(rgb, par=1.0):
    """Box (x0, y0, x1, y1) of the shared slide within an RGB image of the stage band, or None if it shows no screen share.

    The Teams stage is one flat colour, read off the left edge. The shared screen is the one dense block that differs
    from it (in colour: dark slides can match the stage in brightness), slide-shaped and not full width - which rules
    out gallery view. Black letterbox bars inside the share are trimmed. par is the pixel aspect ratio of the image."""
    h, w = rgb.shape[:2]
    edge = rgb[:, :max(2, w // 96)].reshape(-1, 3)
    if edge.std(0).max() > 3:
        return None
    m = np.abs(rgb.astype(np.int16) - np.median(edge, 0).astype(np.int16)).max(2) > 10
    rs, cs = runs(m.mean(1) > 0.5), runs(m.mean(0) > 0.5)
    if not rs or not cs:
        return None
    (y0, y1), (x0, x1) = max(rs, key=lambda r: r[1] - r[0]), max(cs, key=lambda r: r[1] - r[0])
    if not (x1 - x0 < 0.97 * w and y1 - y0 > 0.6 * h and 1.2 <= (x1 - x0) * par / (y1 - y0) <= 2.1):
        return None
    # letterbox bars are pure black; judge by the median, so PowerPoint's slideshow nav pill sitting in a bar doesn't count
    lum = rgb[y0:y1, x0:x1].max(2)
    lit_r, lit_c = np.flatnonzero(np.median(lum, axis=1) >= 8), np.flatnonzero(np.median(lum, axis=0) >= 8)
    if len(lit_r) > 0.5 * (y1 - y0) and len(lit_c) > 0.5 * (x1 - x0):  # otherwise a dark slide, not letterboxing: leave it be
        y0, y1, x0, x1 = y0 + lit_r[0], y0 + lit_r[-1] + 1, x0 + lit_c[0], x0 + lit_c[-1] + 1
    return int(x0), int(y0), int(x1), int(y1)


# ---------------------------------------------------------------- 1. scan
def scan(a, cache):
    """Decode once; keep a greyscale thumbnail (as a flat uint8 file) and the slide box of every sample."""
    raw, boxes_path, meta_path = cache / "thumbs.u8", cache / "boxes.npy", cache / "scan.json"
    t0, t1 = a.range or (0.0, a.duration)
    meta = dict(version=SCAN_VERSION, video=str(a.video), step=a.step, band=a.band, range=[t0, t1], size=[TW, TH])
    if not a.rescan and meta_path.exists() and boxes_path.exists() and json.loads(meta_path.read_text()).get("params") == meta:
        boxes = np.load(boxes_path)
        print(f"scan: cached ({len(boxes)} samples)")
        return np.memmap(raw, np.uint8, "r", shape=(len(boxes), TH, TW)), boxes, t0
    b0, b1 = a.band
    vf = f"fps=1/{a.step},crop=iw:ih*{b1 - b0:.4f}:0:ih*{b0:.4f},scale={TW}:{TH}:flags=area"
    cmd = [a.ffmpeg, "-nostdin", "-loglevel", "error", "-ss", f"{t0:.3f}", "-t", f"{t1 - t0:.3f}", "-i", str(a.video), "-an", "-vf", vf,
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    fsize, boxes, par = TW * TH * 3, [], a.par
    with subprocess.Popen(cmd, stdout=subprocess.PIPE) as proc, open(raw, "wb") as f:
        while chunk := proc.stdout.read(fsize * 120):
            frames = np.frombuffer(chunk[:len(chunk) // fsize * fsize], np.uint8).reshape(-1, TH, TW, 3)
            f.write((frames @ np.array([0.299, 0.587, 0.114])).round().astype(np.uint8).tobytes())
            boxes += [slide_box(fr, par) or (-1, -1, -1, -1) for fr in frames]
            print(f"\rscan: {hms(len(boxes) * a.step)} / {hms(t1 - t0)}", end="", flush=True)
    print()
    if proc.returncode:
        sys.exit(f"ffmpeg failed (exit {proc.returncode})")
    np.save(boxes_path, boxes := np.array(boxes, np.int16).reshape(-1, 4))
    meta_path.write_text(json.dumps(dict(params=meta)))
    return np.memmap(raw, np.uint8, "r", shape=(len(boxes), TH, TW)), boxes, t0


# ---------------------------------------------------------------- 2. segment
def frac_changed(x, y):
    return (np.abs(x.astype(np.int16) - y) > PIX).mean()


def fingerprint(gray, box):
    """The slide alone, at a fixed size: comparable across window moves, resizes and Teams layout changes."""
    x0, y0, x1, y1 = box if box[0] >= 0 else (0, 0, TW, TH)
    return np.asarray(Image.fromarray(np.ascontiguousarray(gray[y0:y1, x0:x1])).resize((FW, FH), Image.Resampling.BOX))


def is_build_of(prev, cur, same):
    """True if cur is prev plus added content (an animation build step): nearly all pixels that changed were blank in prev.
    A new slide on the same template fails this, because its old title and text sat where the new ones now are."""
    m = np.abs(prev.astype(np.int16) - cur) > PIX
    if not same <= m.mean() <= 0.5:
        return False
    return (np.hypot(*np.gradient(prev.astype(np.float32)))[m] > 6).mean() < 0.15


def segment(a, thumbs, boxes, t0):
    n = len(thumbs)
    moving = np.ones(n, bool)
    for i in range(1, n, 1000):  # vectorised in chunks; thumbs is a memmap of the whole recording
        j = min(n, i + 1000)
        moving[i:j] = (np.abs(thumbs[i:j].astype(np.int16) - thumbs[i - 1:j - 1]) > PIX).mean((1, 2)) > a.change
    # a stable run starts at a sample that moved and lasts until the next one that does
    starts = np.flatnonzero(moving)
    min_len = max(2, round(a.min_dwell / a.step))
    kept, skipped = [], []
    for s, e in zip(starts, np.append(starts[1:], n)):
        if e - s < min_len:
            continue
        rep = max(s, e - 2)  # last settled sample, one back from the next change so as not to catch a transition
        span = dict(t=t0 + s * a.step, dwell=(e - s) * a.step)
        if boxes[rep][0] < 0 and not a.keep_all:
            skipped.append(span)
            continue
        fp = fingerprint(thumbs[rep], boxes[rep])
        # a revisit: the same slide again, or an earlier build step of it (the presenter stepped back through the animation)
        if not a.keep_revisits and (hit := next((k for k in kept if frac_changed(k["fp"], fp) < a.same or
                                                 not a.keep_builds and is_build_of(fp, k["fp"], a.same)), None)):
            hit["dwell"] += span["dwell"]
            hit["revisits"].append(span["t"])
            continue
        # the next build step of the last slide - often far apart, as moving the mouse in between counts as motion
        if not a.keep_builds and kept and is_build_of(kept[-1]["fp"], fp, a.same):
            last = kept[-1]
            last.update(fp=fp, rep=t0 + rep * a.step, t_end=t0 + e * a.step, dwell=last["dwell"] + span["dwell"], builds=last["builds"] + 1)
            continue
        kept.append(dict(span, fp=fp, rep=t0 + rep * a.step, t_end=t0 + e * a.step, builds=0, revisits=[]))
    return kept, skipped


# ---------------------------------------------------------------- 3. extract
def grab(a, t):
    out = subprocess.run([a.ffmpeg, "-nostdin", "-loglevel", "error", "-ss", f"{t:.3f}", "-i", str(a.video), "-frames:v", "1", "-an",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(out, np.uint8).reshape(a.height, a.width, 3)


def extract(a, kept, skipped):
    clock = None
    if m := re.search(r"(\d{4}-\d{2}-\d{2})[ _](\d{2})-(\d{2})-(\d{2})", a.video.name):  # OBS default file name = start time
        clock = datetime.strptime(" ".join(m.groups()), "%Y-%m-%d %H %M %S")
    at = lambda t: hms(t) + (f" ({(clock + timedelta(seconds=t)):%H:%M})" if clock else "")
    for old in a.outdir.glob("slide_*.png"):
        old.unlink()

    def save(ik):
        i, k = ik
        img = grab(a, k["rep"])
        if not a.no_crop:
            img = img[int(a.band[0] * a.height):int(a.band[1] * a.height)]
            if box := slide_box(img):
                img = img[box[1]:box[3], box[0]:box[2]]
        Image.fromarray(np.ascontiguousarray(img)).save(a.outdir / f"slide_{i:03d}.png")

    with ThreadPoolExecutor(8) as pool:
        for n, _ in enumerate(pool.map(save, enumerate(kept, 1)), 1):
            print(f"\rextract: {n} / {len(kept)}", end="", flush=True)
    print()
    files = sorted(a.outdir.glob("slide_*.png"))
    if files and not a.no_pdf:
        imgs = (Image.open(f).convert("RGB") for f in files)
        next(imgs).save(a.outdir / "slides.pdf", save_all=True, append_images=imgs, resolution=96, quality=92)
    lines = [f"# Slides from `{a.video.name}`", "", f"{len(kept)} slides.",
             "Times are video offsets" + (", with the wall-clock time from the OBS file name in brackets." if clock else "."), "",
             "| # | first shown | on screen | builds | shown again at |", "|--:|---|--:|--:|---|"]
    lines += [f"| [{i}](slide_{i:03d}.png) | {at(k['t'])} | {hms(k['dwell'])} | {k['builds'] or ''} | {', '.join(hms(t) for t in k['revisits'])} |"
              for i, k in enumerate(kept, 1)]
    if skipped:
        lines += ["", f"Skipped {len(skipped)} still stretches that showed no screen share (gallery view etc.), longest first; "
                      "re-run with `--keep-all` to keep them:", ""]
        lines += [f"- {at(s['t'])} for {hms(s['dwell'])}" for s in sorted(skipped, key=lambda s: -s["dwell"])[:30]]
    (a.outdir / "_manifest.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("video", type=Path)
    p.add_argument("outdir", type=Path)
    p.add_argument("--step", type=float, default=1.0, help="sample every STEP seconds (default 1)")
    p.add_argument("--min-dwell", type=float, default=2.0, help="ignore anything on screen for less than this many seconds (default 2)")
    p.add_argument("--change", type=float, default=0.001, help="fraction of changed thumbnail pixels that counts as 'moved' (default 0.001)")
    p.add_argument("--same", type=float, default=0.01, help="fraction of changed pixels below which two slides are the same (default 0.01)")
    p.add_argument("--band", type=float, nargs=2, default=[0.18, 0.972], metavar=("TOP", "BOTTOM"),
                   help="stage band as fractions of frame height, excluding the participant strip above and name label below")
    p.add_argument("--range", type=float, nargs=2, metavar=("T0", "T1"), help="only look at this window (seconds)")
    p.add_argument("--keep-builds", action="store_true", help="keep every animation build step, not just the final state")
    p.add_argument("--keep-revisits", action="store_true", help="keep a slide again each time the presenter goes back to it")
    p.add_argument("--keep-all", action="store_true", help="keep still frames that show no screen share (e.g. gallery view)")
    p.add_argument("--no-crop", action="store_true", help="save the full frame instead of cropping to the slide")
    p.add_argument("--no-pdf", action="store_true", help="don't write slides.pdf")
    p.add_argument("--rescan", action="store_true", help="ignore the cached scan")
    p.add_argument("--dry-run", action="store_true", help="scan and segment, but write no images")
    p.add_argument("--ffmpeg", default=shutil.which("ffmpeg"))
    p.add_argument("--ffprobe", default=shutil.which("ffprobe"))
    a = p.parse_args()
    if not (a.ffmpeg and a.ffprobe):
        sys.exit("ffmpeg/ffprobe not found on PATH; pass --ffmpeg / --ffprobe")
    a.duration, a.width, a.height = probe(a.ffprobe, a.video)
    a.par = (a.width / TW) / (a.height * (a.band[1] - a.band[0]) / TH)
    cache = a.outdir / ".slidegrab"
    cache.mkdir(parents=True, exist_ok=True)
    thumbs, boxes, t0 = scan(a, cache)
    kept, skipped = segment(a, thumbs, boxes, t0)
    print(f"segment: {len(kept)} slides, {sum(k['builds'] for k in kept)} build steps folded, "
          f"{sum(len(k['revisits']) for k in kept)} revisits dropped, {len(skipped)} stills without a screen share skipped")
    if not a.dry_run:
        extract(a, kept, skipped)
        print(f"wrote {a.outdir}")


if __name__ == "__main__":
    main()
