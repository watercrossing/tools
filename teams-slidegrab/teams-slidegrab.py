#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "winocr",
#     "pillow",
#     "numpy",
# ]
# ///
"""
teams-slidegrab.py - Extract slide screenshots from a screen recording using its on-screen
slide counter (e.g. Teams PowerPoint Live "N of 61"). CPU-only: reads the counter with
Windows OCR (winocr). No frames or slide content are sent anywhere.

Run (deps auto-installed from the inline script metadata above):
    uv run teams-slidegrab.py VIDEO OUTDIR [options]     # or ./teams-slidegrab.py VIDEO OUTDIR

Stages (all run by default; each caches its result so you can re-run/inspect):
    1. calibrate - sample frames, OCR full-frame, auto-find the counter region(s) and deck total
    2. scan      - one ffmpeg decode pass -> counter crops every --step s -> OCR timeline
    3. extract   - refine transitions at native fps, then save one full frame per slide
                   (~--lead s before it advances) + manifest.md, each counter-verified

Add --crop to also crop each saved slide down to just the slide, dropping the surrounding
Teams UI (thumbnail strip, people panel, nav bar). `--stage crop` runs that crop on an
already-extracted OUTDIR without touching the video.

Works on any recording with a visible numeric slide counter. It does NOT handle decks
with no counter (there is no reliable signal to key on).
"""
import argparse, subprocess, re, os, json, glob, shutil, sys
from collections import Counter, defaultdict
from PIL import Image
import numpy as np
import winocr

# ---------------------------------------------------------------- ffmpeg/ocr utils
def find_exe(name, override):
    if override:
        return override
    p = shutil.which(name) or shutil.which(name + ".exe")
    if p:
        return p
    # common Windows fallback
    for base in glob.glob(r"C:/Users/*/tools/ffmpeg*/bin") + [r"C:/ffmpeg/bin"]:
        cand = os.path.join(base, name + ".exe")
        if os.path.exists(cand):
            return cand
    sys.exit(f"Could not find {name}; pass --{name}")

def probe(ffprobe, video):
    out = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0",
        "-show_entries", "format=duration:stream=width,height,r_frame_rate",
        "-of", "json", video], capture_output=True, text=True, check=True).stdout
    j = json.loads(out)
    st = j["streams"][0]
    num, den = (st["r_frame_rate"].split("/") + ["1"])[:2]
    fps = float(num) / float(den or 1)
    return float(j["format"]["duration"]), int(st["width"]), int(st["height"]), fps

def _ocr(img):
    res = winocr.recognize_pil_sync(img, "en-US")
    return res if isinstance(res, dict) else {"text": getattr(res, "text", ""), "lines": []}

def ocr_text(img):
    return _ocr(img)["text"]

def frame(ffmpeg, video, t, vf, out):
    cmd = [ffmpeg, "-nostdin", "-loglevel", "error", "-ss", f"{t:.3f}", "-i", video,
           "-frames:v", "1", "-an"]
    if vf:
        cmd += ["-vf", vf]
    cmd += ["-q:v", "2", out, "-y"]
    subprocess.run(cmd, check=True)

# ---------------------------------------------------------------- counter parsing
def make_parser(regex, total):
    rx = re.compile(regex, re.I)
    def norm(s):
        s = s.lower().replace("l", "1").replace("i", "1").replace("t", "1").replace("o", "0")
        try: return int(s)
        except ValueError: return None
    def parse(text):
        t = text.replace("\n", " ")
        for m in rx.finditer(t):
            n = int(re.sub(r"\D", "", m.group(1)) or -1)
            tot = norm(m.group(2))
            if n >= 1 and (total is None or tot == total) and (tot is None or 1 <= n <= tot):
                return n
        # single-digit merge fallback e.g. "80f61" -> 8 when total known
        if total is not None:
            m = re.search(r"\b(\d)[o0]f\s*" + str(total)[0] + r"[0-9oOlLiItT]", t.lower())
            if m: return int(m.group(1))
        return None
    return parse

# ---------------------------------------------------------------- stage: calibrate
def calibrate(ff, video, dur, W, H, args, work):
    rx = re.compile(args.counter_regex, re.I)
    K = args.calib_samples
    cs = args.calib_scale          # upscale full frame so small counters are legible to OCR
    times = [dur * (i + 0.5) / K for i in range(K)]
    hits = []
    print(f"[calibrate] sampling {K} frames (OCR at {cs}x) for the counter ...", flush=True)
    tmp = os.path.join(work, "_cal.png")
    for t in times:
        frame(ff, video, t, f"scale=iw*{cs}:ih*{cs}", tmp)
        res = _ocr(Image.open(tmp).convert("RGB"))
        for ln in res.get("lines", []):
            txt = ln.get("text", "")
            m = rx.search(txt)
            if not m:
                continue
            words = ln.get("words", [])
            xs = [w["bounding_rect"]["x"] for w in words]
            ys = [w["bounding_rect"]["y"] for w in words]
            xe = [w["bounding_rect"]["x"] + w["bounding_rect"]["width"] for w in words]
            ye = [w["bounding_rect"]["y"] + w["bounding_rect"]["height"] for w in words]
            if not xs:
                continue
            try:
                tot = int(re.sub(r"\D", "", m.group(2)))
            except ValueError:
                continue
            # map boxes from upscaled coords back to native frame coords
            hits.append((int(re.sub(r"\D", "", m.group(1)) or -1), tot,
                         min(xs) / cs, min(ys) / cs, max(xe) / cs, max(ye) / cs))
    if not hits:
        sys.exit("[calibrate] no counter found. Check --counter-regex / --calib-samples.")
    total = args.total or Counter(h[1] for h in hits).most_common(1)[0][0]
    hits = [h for h in hits if h[1] == total]
    # greedy cluster by box center
    clusters = []
    for h in hits:
        cx, cy = (h[2] + h[4]) / 2, (h[3] + h[5]) / 2
        for c in clusters:
            if abs(cx - c["cx"]) < 120 and abs(cy - c["cy"]) < 30:
                c["boxes"].append(h[2:]); c["cx"] = (c["cx"] * c["n"] + cx) / (c["n"] + 1)
                c["cy"] = (c["cy"] * c["n"] + cy) / (c["n"] + 1); c["n"] += 1; break
        else:
            clusters.append({"cx": cx, "cy": cy, "n": 1, "boxes": [h[2:]]})
    regions = []
    for c in clusters:
        x0 = min(b[0] for b in c["boxes"]); y0 = min(b[1] for b in c["boxes"])
        x1 = max(b[2] for b in c["boxes"]); y1 = max(b[3] for b in c["boxes"])
        regions.append({"x": max(0, x0 - 12), "y": max(0, y0 - 8),
                        "w": min(W, x1 + 12) - max(0, x0 - 12),
                        "h": min(H, y1 + 8) - max(0, y0 - 8), "n": c["n"]})
    ux0 = min(r["x"] for r in regions); uy0 = min(r["y"] for r in regions)
    ux1 = max(r["x"] + r["w"] for r in regions); uy1 = max(r["y"] + r["h"] for r in regions)
    union = {"x": ux0, "y": uy0, "w": ux1 - ux0, "h": uy1 - uy0}
    calib = {"total": total, "width": W, "height": H, "regions": regions, "union": union,
             "scale": 4}
    json.dump(calib, open(os.path.join(work, "calib.json"), "w"), indent=1)
    print(f"[calibrate] total={total}  regions={[ (r['x'],r['y'],r['w'],r['h']) for r in regions]}",
          flush=True)
    return calib

# ---------------------------------------------------------------- crop helpers
def union_vf(calib):
    u, s = calib["union"], calib["scale"]
    return f"crop={u['w']}:{u['h']}:{u['x']}:{u['y']},scale=iw*{s}:ih*{s}"

def read_union_image(im, calib, parse):
    """OCR each calibrated region (tight) inside the scaled union crop; return first valid N."""
    u, s = calib["union"], calib["scale"]
    for r in calib["regions"]:
        rx = (r["x"] - u["x"]) * s; ry = (r["y"] - u["y"]) * s
        sub = im.crop((int(max(0, rx - 8)), int(max(0, ry - 8)),
                       int(rx + r["w"] * s + 8), int(ry + r["h"] * s + 8)))
        n = parse(ocr_text(sub))
        if n is not None:
            return n
    return parse(ocr_text(im))

# ---------------------------------------------------------------- stage: scan
def batch_frames(ff, video, t0, t1, fps_expr, vf, outdir):
    for f in glob.glob(os.path.join(outdir, "*.png")):
        os.remove(f)
    subprocess.run([ff, "-nostdin", "-loglevel", "error", "-ss", f"{t0:.3f}",
        "-t", f"{t1 - t0:.3f}", "-i", video, "-an", "-vf", f"fps={fps_expr},{vf}",
        "-start_number", "0", os.path.join(outdir, "f_%06d.png"), "-y"], check=True)
    return sorted(glob.glob(os.path.join(outdir, "f_*.png")))

def scan(ff, video, calib, t0, t1, step, parse, work):
    vf = union_vf(calib)
    sd = os.path.join(work, "scan"); os.makedirs(sd, exist_ok=True)
    print(f"[scan] extracting frames [{t0:.0f},{t1:.0f}] every {step}s ...", flush=True)
    files = batch_frames(ff, video, t0, t1, f"1/{step}", vf, sd)
    print(f"[scan] {len(files)} frames; OCR ...", flush=True)
    timeline = [[round(t0 + i * step, 2), read_union_image(Image.open(f).convert("RGB"), calib, parse)]
                for i, f in enumerate(files)]
    shutil.rmtree(sd, ignore_errors=True)
    json.dump({"t0": t0, "t1": t1, "step": step, "timeline": timeline},
              open(os.path.join(work, "timeline.json"), "w"))
    return timeline

def refine(ff, video, calib, t0, t1, fps, parse, work):
    vf = union_vf(calib)
    rd = os.path.join(work, "refine"); os.makedirs(rd, exist_ok=True)
    files = batch_frames(ff, video, t0, t1, f"{fps}", vf, rd)
    seq = [[t0 + i / fps, read_union_image(Image.open(f).convert("RGB"), calib, parse)]
           for i, f in enumerate(files)]
    shutil.rmtree(rd, ignore_errors=True)
    return seq

# ---------------------------------------------------------------- analysis
def median3(vals):
    """Median-of-3 filter: removes isolated single-sample OCR spikes (high or low)."""
    if len(vals) < 3:
        return list(vals)
    out = list(vals)
    for i in range(1, len(vals) - 1):
        out[i] = sorted(vals[i - 1:i + 2])[1]
    return out

def build_first_appearance(timeline, step, fps, ff, video, calib, parse, work, refine_max):
    """Return dict slide->(first_t, last_t) for slides confirmed shown.

    Coarse scan (median-filtered to kill isolated OCR spikes) + native-fps refinement of every
    short transition AND of the leading/trailing edges (to catch brief slides that flash by
    before the first / after the last coarse hit). A forward-monotonic pass then rejects reads
    that are inconsistent with the deck's progress (e.g. a stray 'N of 61' misread mid-demo),
    so each slide number ends up as one clean block.
    """
    pts = [(t, n) for t, n in timeline if n is not None]
    if not pts:
        return {}
    ns = median3([n for _, n in pts])
    seq = [(pts[i][0], ns[i]) for i in range(len(pts))]
    obs = list(seq)

    def add_refine(t0, t1):
        if t1 <= t0:
            return
        rp = [(t, n) for t, n in refine(ff, video, calib, max(0, t0), t1, fps, parse, work)
              if n is not None]
        if rp:
            rns = median3([n for _, n in rp])
            obs.extend((rp[j][0], rns[j]) for j in range(len(rp)))

    print("[extract] refining transitions at native fps ...", flush=True)
    for i in range(1, len(seq)):
        (ta, na), (tb, nb) = seq[i - 1], seq[i]
        if na != nb and 0 < (tb - ta) <= refine_max:
            add_refine(ta - 0.3, tb + 0.3)
    edge = max(6.0, 3 * step)                       # leading/trailing flash-slide capture
    add_refine(seq[0][0] - edge, seq[0][0] + 0.3)
    add_refine(seq[-1][0] - 0.3, seq[-1][0] + edge)

    obs.sort()
    # forward-monotonic clean: the deck advances, so drop reads below the established level
    # (kills sustained low strays that the median filter can't; real slides survive in order)
    clean, cur = [], None
    for t, n in obs:
        if cur is None or n >= cur:
            clean.append((t, n)); cur = n
    span, support = {}, defaultdict(int)
    coarse_ts = {round(t, 2) for t, _ in seq}
    cbn = defaultdict(set)
    for t, n in clean:
        support[n] += 1
        span.setdefault(n, [t, t])[1] = t
        cbn[n].add(round(t, 2))
    # Monotonic cleaning already dropped inconsistent strays, and the median filter removed
    # isolated single-frame spikes, so anything left is trustworthy - including 1-frame flash
    # slides (e.g. a title slide whose counter is legible for only one frame). Any residual
    # error still gets caught by the per-slide counter verification during extraction.
    lead_n = clean[0][1] if clean else None          # always keep the very first slide seen
    first = {}
    for n, (a, b) in span.items():
        if support[n] >= 2 or (cbn[n] & coarse_ts) or n == lead_n:
            first[n] = (a, b)
    return first

def choose_shot(k, first, order, lead, gap_thr, brief_thr, end_of_show):
    start_k, end_k = first[k]
    nxt = next((s for s in order if s > k), None)
    nxt_start = first[nxt][0] if nxt else end_of_show
    if (nxt_start - end_k) > gap_thr:          # presenter left slides / long dwell after
        return end_k, "last-confirmed"
    if (nxt_start - start_k) < brief_thr:       # very brief slide
        return (start_k + nxt_start) / 2.0, "midpoint(brief)"
    return nxt_start - lead, "lead-before-next"

# ---------------------------------------------------------------- slide cropping
def longest_run(mask, gap=25):
    """Start/end index of the longest run of True in a 1-D bool array, after bridging
    interior False gaps shorter than `gap` px (so thin table borders don't split the slide)."""
    m = np.asarray(mask).copy()
    n = len(m)
    i = 0
    while i < n:
        if not m[i]:
            j = i
            while j < n and not m[j]:
                j += 1
            if 0 < i < j < n and (j - i) < gap:        # interior short gap -> bridge across it
                m[i:j] = True
            i = j
        else:
            i += 1
    best, i = (0, 0, 0), 0                              # (length, start, end)
    while i < n:
        if m[i]:
            j = i
            while j < n and m[j]:
                j += 1
            if j - i > best[0]:
                best = (j - i, i, j - 1)
            i = j
        else:
            i += 1
    return best[1], best[2]

def detect_slide_bbox(im, band, bright_thr=40, frac_thr=0.5, gap=25):
    """Locate the shared slide inside a Teams frame: the largest solid bright block on the
    dark 'stage'. `band` = (top, bottom) as fractions of height, excluding the thumbnail strip
    and nav bar. The people panel is a shorter, stage-separated run, so the longest-run search
    skips it; interior gaps from table borders / dark content are bridged.

    Returns (l, t, r, b), or None if the frame has no dark-stage-pillarboxed slide to crop
    (e.g. an already-cropped bare slide, or a light-themed capture with no dark stage)."""
    a = np.asarray(im.convert("RGB")).astype(np.int16)
    H, W = a.shape[:2]
    bright = a.max(axis=2) > bright_thr                # "not near-black" -> slide vs stage
    y0, y1 = int(band[0] * H), int(band[1] * H)
    left, right = longest_run(bright[y0:y1, :].mean(axis=0) > frac_thr, gap)
    rmask = bright[:, left:right + 1].mean(axis=1) > frac_thr
    rmask[:y0] = False
    rmask[y1:] = False
    top, bottom = longest_run(rmask, gap)
    w, h = right - left + 1, bottom - top + 1
    # A real PowerPoint-Live slide is pillarboxed on the dark stage: there is a dark margin to
    # its left. If the block reaches the left edge (or that margin isn't dark) there is nothing
    # to crop - the image is already a bare slide - so decline rather than clip it to the band.
    if left <= 3 or bright[y0:y1, :left].mean() > 0.2:
        return None
    if w < 0.35 * W or h < 0.35 * H or w * h > 0.985 * W * H:
        return None
    return left, top, right, bottom

def crop_one(path, band):
    """Crop `path` in place to its detected slide region. Returns (l, t, r, b) on success, or
    None if no crop-worthy slide region was found - in which case the file is left untouched."""
    im = Image.open(path).convert("RGB")
    bbox = detect_slide_bbox(im, band)
    if bbox is None:
        return None
    l, t, r, b = bbox
    im.crop((l, t, r + 1, b + 1)).save(path)
    return bbox

def crop_dir(outdir, band):
    """Crop every OUTDIR/slide_*.png in place to its slide region (standalone --stage crop)."""
    files = sorted(glob.glob(os.path.join(outdir, "slide_*.png")))
    if not files:
        sys.exit(f"[crop] no slide_*.png found in {outdir}")
    ok = 0
    for f in files:
        if crop_one(f, band):
            ok += 1
        else:
            print(f"[crop] {os.path.basename(f)}: no crop-worthy slide region; left unchanged", flush=True)
    print(f"[crop] {ok}/{len(files)} cropped to the slide region -> {outdir}")

# ---------------------------------------------------------------- stage: extract
def extract(ff, video, calib, timeline, step, fps, args, work):
    total = calib["total"]
    parse = make_parser(args.counter_regex, total)
    first = build_first_appearance(timeline, step, fps, ff, video, calib, parse, work,
                                   args.refine_max)
    order = sorted(first)
    if not order:
        sys.exit("[extract] no slides confirmed. Inspect timeline.json.")
    end_of_show = max(t for t, n in timeline if n is not None) + step
    os.makedirs(args.outdir, exist_ok=True)
    vf = union_vf(calib)
    tmp = os.path.join(work, "_ver.png")

    def counter_at(t):
        frame(ff, video, t, vf, tmp)
        return read_union_image(Image.open(tmp).convert("RGB"), calib, parse)

    report = []
    for k in range(1, total + 1):
        if k not in first:
            report.append({"slide": k, "status": "not shown"})
            print(f"slide {k:3d}:  NOT SHOWN", flush=True); continue
        shot, mode = choose_shot(k, first, order, args.lead, args.gap_thr,
                                 args.brief_thr, end_of_show)
        v = counter_at(shot)
        if v is not None and v != k:            # walk back into the slide if we overshot
            s = shot
            while s > first[k][0] + 0.1:
                s -= 0.4
                vv = counter_at(s)
                if vv == k or vv is None:
                    shot, v, mode = s, vv, mode + "+adj"; break
        cropped = None                               # None = not attempted; else True/False
        if not args.dry_run:
            path = os.path.join(args.outdir, f"slide_{k:02d}.png")
            frame(ff, video, shot, None, path)
            if args.crop:
                cropped = crop_one(path, args.crop_band) is not None
        ok = (v == k) or (v is None)
        report.append({"slide": k, "t": round(shot, 2), "mode": mode, "counter": v,
                       "status": "ok" if ok else "MISMATCH", "cropped": cropped})
        flag = "" if v == k else (" (counter hidden)" if v is None else f"  <- reads {v}!")
        if cropped is False:
            flag += "  [uncropped]"
        print(f"slide {k:3d}: t={shot:9.1f}  {mode:20s} counter={v}{flag}", flush=True)
    write_manifest(report, calib, video, args)
    json.dump(report, open(os.path.join(work, "report.json"), "w"), indent=1)
    shown = sum(1 for r in report if r.get("status") == "ok")
    missing = [r["slide"] for r in report if r["status"] == "not shown"]
    print(f"\n[done] {shown}/{total} slides extracted -> {args.outdir}")
    if missing:
        print(f"       not shown in recording: {missing}")

def hhmmss(t):
    t = int(round(t)); return f"{t // 3600}:{(t % 3600) // 60:02d}:{t % 60:02d}"

def write_manifest(report, calib, video, args):
    crop_note = ", cropped to the slide region (Teams UI removed)" if args.crop else ""
    L = ["# Slide extraction manifest", "",
         f"Source: `{os.path.basename(video)}`  -  deck total: {calib['total']} slides.",
         "Counter read locally with Windows OCR (winocr); no images left the machine.",
         f"Each shot is the frame ~{args.lead}s before the slide advances, counter-verified{crop_note}.",
         ""]
    if args.crop:
        bad = [r["slide"] for r in report if r.get("cropped") is False]
        if bad:
            L += [f"No slide region was detected for slides {bad}; these are left as full frames.", ""]
    L += ["| Slide | File | Video time | Check |", "|------:|------|-----------:|:-----:|"]
    for r in report:
        k = r["slide"]
        if r["status"] == "not shown":
            L.append(f"| {k} | - | - | not shown |")
        else:
            chk = f"counter={r['counter']}" + (" OK" if r["status"] == "ok" else " MISMATCH")
            L.append(f"| {k} | slide_{k:02d}.png | {hhmmss(r['t'])} | {chk} |")
    open(os.path.join(args.outdir, "_manifest.md"), "w", encoding="utf-8").write("\n".join(L))

# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Extract slide screenshots via on-screen counter OCR.")
    ap.add_argument("video", nargs="?", help="source recording (not needed with --stage crop)")
    ap.add_argument("outdir")
    ap.add_argument("--stage", choices=["all", "calibrate", "scan", "extract", "crop"], default="all")
    ap.add_argument("--ffmpeg"); ap.add_argument("--ffprobe")
    ap.add_argument("--work", help="intermediate dir (default: OUTDIR/.slidegrab)")
    ap.add_argument("--total", type=int, help="deck total M (else auto-detected)")
    ap.add_argument("--counter-regex", default=r"(\d+)\s*(?:of|/)\s*(\d+)")
    ap.add_argument("--calib-samples", type=int, default=40)
    ap.add_argument("--calib-scale", type=float, default=2.0, help="full-frame OCR upscale for calibration")
    ap.add_argument("--step", type=float, default=2.0, help="scan resolution (s)")
    ap.add_argument("--range", nargs=2, type=float, metavar=("T0", "T1"))
    ap.add_argument("--lead", type=float, default=1.0, help="seconds before advance to grab")
    ap.add_argument("--gap-thr", type=float, default=15.0, help="gap(s) => 'presenter left'")
    ap.add_argument("--brief-thr", type=float, default=2.5, help="window(s) below => midpoint")
    ap.add_argument("--refine-max", type=float, default=20.0, help="max transition window to refine")
    ap.add_argument("--recalibrate", action="store_true"); ap.add_argument("--rescan", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--crop", action="store_true",
                    help="crop each saved slide down to the slide region (removes surrounding Teams UI)")
    ap.add_argument("--crop-band", nargs=2, type=float, default=(0.25, 0.935), metavar=("TOP", "BOT"),
                    help="slide-search band as fractions of frame height, excluding thumbnail strip / nav bar")
    args = ap.parse_args()

    if args.stage == "crop":                          # standalone: crop existing OUTDIR/slide_*.png
        crop_dir(args.outdir, args.crop_band)
        return
    if not args.video:
        ap.error("VIDEO is required (except with --stage crop)")

    ff = find_exe("ffmpeg", args.ffmpeg); fp = find_exe("ffprobe", args.ffprobe)
    work = args.work or os.path.join(args.outdir, ".slidegrab"); os.makedirs(work, exist_ok=True)
    dur, W, H, fps = probe(fp, args.video)
    t0, t1 = (args.range if args.range else (0.0, dur))
    parse = make_parser(args.counter_regex, args.total)

    calib_path = os.path.join(work, "calib.json")
    if args.stage in ("all", "calibrate") and (args.recalibrate or not os.path.exists(calib_path)):
        calib = calibrate(ff, args.video, dur, W, H, args, work)
    else:
        calib = json.load(open(calib_path))
    parse = make_parser(args.counter_regex, calib["total"])   # lock total from calibration
    if args.stage == "calibrate":
        return

    tl_path = os.path.join(work, "timeline.json")
    if args.stage in ("all", "scan") and (args.rescan or not os.path.exists(tl_path)):
        timeline = scan(ff, args.video, calib, t0, t1, args.step, parse, work)
    else:
        timeline = json.load(open(tl_path))["timeline"]
    if args.stage == "scan":
        return

    extract(ff, args.video, calib, timeline, args.step, fps, args, work)

if __name__ == "__main__":
    main()
