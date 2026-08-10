#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "faster-whisper",
#     "numpy",
#     "pyannote.audio>=4",
#     "torch",
#     "torchaudio",
#     "nvidia-cublas-cu12",
#     "nvidia-cudnn-cu12==9.*",
# ]
# ///
"""
obs-interview-transcript.py - Turn a two-track OBS recording of an online interview into one
speaker-attributed transcript. Track 1 is Desktop Audio (everyone on the call), track 2 is your
own microphone. Everything runs locally: audio never leaves the machine.

Run (deps auto-installed from the inline script metadata above):
    uv run obs-interview-transcript.py RECORDING [OUTDIR]   # or ./obs-interview-transcript.py ...

Why two tracks: the desktop track cannot contain your own voice, because a conferencing app never
plays your microphone back to you. So every word on track 1 is someone else, and only needs
diarizing into who. Your microphone also picks up the far end through your speakers, so track 2 is
the only one needing a gate - which is easy, because during your speech the desktop track sits at
its noise floor (tens of dB of separation), while during bleed the mic sits *below* the clean
desktop signal. Attribution is per word, so a segment Whisper drew across a handover gets cut at
the boundary rather than assigned wholesale.

Diarization uses pyannote's speaker-diarization-community-1, which is self-contained (segmentation,
embedding and PLDA all live in the one gated repo), so only that single licence has to be accepted.

Stages (all run by default; each caches into OUTDIR/.cache so you can re-run or inspect):
    1. split      - copy both audio tracks out of the container losslessly (no re-encode)
    2. transcribe - faster-whisper on each track (word timestamps on the mic track)
    3. diarize    - pyannote on the desktop track, to tell the far-end speakers apart
    4. merge      - energy-gate the mic track, attach speaker labels, write the transcript

Use headphones and the gate becomes trivial (no bleed at all); it is built for the speakers case.
"""
import argparse, difflib, json, os, subprocess, sys, shutil, tempfile
from pathlib import Path

import numpy as np

SR, FRAME = 16000, 480                                        # 16 kHz mono, 30 ms analysis frames
STAGES = ("split", "transcribe", "diarize", "merge")


# ------------------------------------------------------------------ ffmpeg helpers
def find_exe(name, override=None):
    p = override or shutil.which(name) or shutil.which(name + ".exe")
    if not p:
        sys.exit(f"Could not find {name} on PATH; pass --{name}")
    return p


def audio_streams(ffprobe, video):
    """Indices of the audio streams, in container order."""
    out = subprocess.run([ffprobe, "-v", "error", "-select_streams", "a", "-show_entries",
                          "stream=index,channels,sample_rate", "-of", "json", video],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out).get("streams", [])


def split_tracks(ffmpeg, video, desktop_i, mic_i, cache, force=False):
    """Stream-copy the two audio tracks out. No re-encode, so these stay bit-identical to OBS's output."""
    paths = [cache / "desktop.m4a", cache / "mic.m4a"]
    if not force and all(p.exists() for p in paths):
        print("split: cached")
        return paths
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(video)]
    for path, idx in zip(paths, (desktop_i, mic_i)):
        cmd += ["-map", f"0:a:{idx}", "-c", "copy", str(path)]
    subprocess.run(cmd, check=True)
    print(f"split: {paths[0].name} (desktop, a:{desktop_i}), {paths[1].name} (mic, a:{mic_i})")
    return paths


def decode_mono(ffmpeg, path):
    """Decode to 16 kHz mono float32 in [-1, 1]."""
    raw = subprocess.run([ffmpeg, "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


# ------------------------------------------------------------------ timestamps
def ts(seconds):
    h, rem = divmod(int(seconds), 3600)
    return f"{h:02d}:{rem // 60:02d}:{rem % 60:02d}"


def srt_ts(seconds):
    return f"{ts(seconds)},{int((seconds - int(seconds)) * 1000):03d}"


def overlap(a0, a1, b0, b1):
    return max(0.0, min(a1, b1) - max(a0, b0))


# ------------------------------------------------------------------ transcription
def enable_cuda_dlls():
    """ctranslate2 resolves its CUDA dependencies through PATH, not os.add_dll_directory, so the
    cuBLAS/cuDNN DLLs shipped in the nvidia-* wheels are invisible to it unless PATH names them."""
    if sys.platform != "win32":
        return
    import importlib.util
    for pkg in ("nvidia.cublas", "nvidia.cudnn", "nvidia.cuda_nvrtc"):
        spec = importlib.util.find_spec(pkg)
        if spec and spec.submodule_search_locations:
            binpath = Path(list(spec.submodule_search_locations)[0]) / "bin"
            if binpath.is_dir():
                os.add_dll_directory(str(binpath))
                os.environ["PATH"] = f"{binpath}{os.pathsep}{os.environ['PATH']}"


def load_whisper(model_size, device, compute_type):
    from faster_whisper import WhisperModel
    if device != "auto":
        return WhisperModel(model_size, device=device, compute_type=compute_type), device
    try:
        return WhisperModel(model_size, device="cuda", compute_type=compute_type), "cuda"
    except Exception as e:                                     # no CUDA, wrong driver, DLLs missing
        print(f"  cuda unavailable ({str(e).splitlines()[0][:90]}), falling back to cpu")
        return WhisperModel(model_size, device="cpu", compute_type="int8"), "cpu"


def transcribe(model, path, label, words, language):
    segments, info = model.transcribe(str(path), beam_size=5, vad_filter=True, language=language,
                                      vad_parameters={"min_silence_duration_ms": 500},
                                      condition_on_previous_text=False,   # avoids repetition loops on sparse tracks
                                      word_timestamps=words)
    print(f"  [{label}] language={info.language} ({info.language_probability:.2f}) "
          f"duration={info.duration:.0f}s speech={info.duration_after_vad:.0f}s", flush=True)
    out = []
    for seg in segments:
        if not (text := seg.text.strip()):
            continue
        rec = {"start": seg.start, "end": seg.end, "text": text}
        if words and seg.words:
            rec["words"] = [{"start": w.start, "end": w.end, "word": w.word} for w in seg.words]
        out.append(rec)
        print(f"    [{ts(seg.start)}] {text}", flush=True)
    return out


def stage_transcribe(paths, cache, args):
    """Desktop track needs segments only; mic track needs word timestamps for boundary cutting."""
    targets = [(paths[0], cache / "desktop.json", "desktop", False), (paths[1], cache / "mic.json", "mic", True)]
    todo = [t for t in targets if args.retranscribe or not t[1].exists()]
    if not todo:
        print("transcribe: cached")
        return [json.loads(t[1].read_text(encoding="utf-8")) for t in targets]

    enable_cuda_dlls()
    model, device = load_whisper(args.model, args.device, args.compute_type)
    print(f"transcribe: {args.model} on {device}/{args.compute_type}")
    for audio, out, label, words in todo:
        out.write_text(json.dumps(transcribe(model, audio, label, words, args.language), ensure_ascii=False, indent=1),
                       encoding="utf-8")
    return [json.loads(t[1].read_text(encoding="utf-8")) for t in targets]


# ------------------------------------------------------------------ diarization
def stage_diarize(desktop_audio, cache, args):
    out = cache / "diarization.json"
    if out.exists() and not args.rediarize:
        print("diarize: cached")
        return json.loads(out.read_text(encoding="utf-8"))

    import torch
    from pyannote.audio import Pipeline
    try:
        pipeline = Pipeline.from_pretrained(args.diarization_model)
    except Exception as e:
        sys.exit(f"Could not load {args.diarization_model}: {str(e).splitlines()[0]}\n\n"
                 f"Accept the model's conditions at https://huggingface.co/{args.diarization_model}\n"
                 f"then store a read token:  uv tool install huggingface_hub && hf auth login")
    if pipeline is None:
        sys.exit(f"pyannote returned no pipeline for {args.diarization_model} - the licence is probably not accepted.")
    pipeline.to(torch.device(args.diarize_device))

    with tempfile.TemporaryDirectory() as td:                  # pyannote wants a waveform, not aac
        wav = Path(td) / "audio.wav"
        subprocess.run([args.ffmpeg, "-v", "error", "-y", "-i", str(desktop_audio), "-ac", "1", "-ar", str(SR), str(wav)],
                       check=True)
        print(f"diarize: pyannote on {args.diarize_device} (the embedding pass is slow on cpu - minutes, not seconds)",
              flush=True)
        kw = {k: v for k in ("num_speakers", "min_speakers", "max_speakers") if (v := getattr(args, k)) is not None}
        result = pipeline(str(wav), **kw)

    ann = getattr(result, "speaker_diarization", result)       # 4.x wraps the annotation in a result object
    turns = [{"start": t.start, "end": t.end, "speaker": s} for t, _, s in ann.itertracks(yield_label=True)]
    out.write_text(json.dumps(turns, indent=1), encoding="utf-8")
    speakers = sorted({t["speaker"] for t in turns})
    print(f"  {len(turns)} turns, {len(speakers)} speakers: {', '.join(speakers)}")
    return turns


# ------------------------------------------------------------------ attribution and merge
def frame_db(x):
    usable = (len(x) // FRAME) * FRAME
    return 20 * np.log10(np.sqrt((x[:usable].reshape(-1, FRAME) ** 2).mean(axis=1) + 1e-12) + 1e-12)


def split_by_word_attribution(mic, voiced, mic_dominant, frames):
    """Re-cut mic segments at speaker handovers using per-word energy dominance. Each word is scored
    mic-dominant or not, the sequence is median-smoothed so one noisy word cannot shatter a run, then
    consecutive same-label words are regrouped."""
    out = []
    for s in mic:
        if not (ws := s.get("words")):
            out.append(s)
            continue
        labels = []
        for w in ws:
            v = voiced[sl := frames(w["start"], w["end"])]
            labels.append(bool(mic_dominant[sl].sum() / max(1, v.sum()) > 0.5) if v.sum() else None)
        for i, l in enumerate(labels):                          # carry pauses forward from neighbours
            if l is None:
                labels[i] = next((x for x in labels[i + 1:] if x is not None),
                                 next((x for x in reversed(labels[:i]) if x is not None), False))
        smoothed = list(labels)
        for i in range(1, len(labels) - 1):
            smoothed[i] = sum(labels[i - 1:i + 2]) >= 2
        labels, run_start = smoothed, 0
        for i in range(1, len(labels) + 1):
            if i == len(labels) or labels[i] != labels[run_start]:
                run = ws[run_start:i]
                if text := "".join(w["word"] for w in run).strip():
                    out.append({"start": run[0]["start"], "end": run[-1]["end"], "text": text})
                run_start = i
    return out


def coalesce(segs, max_gap=1.0):
    """Rejoin consecutive same-speaker segments, so word-level cutting leaves no two-word stubs."""
    out = []
    for s in segs:
        if out and out[-1]["speaker"] == s["speaker"] and s["start"] - out[-1]["end"] <= max_gap:
            out[-1]["end"] = max(out[-1]["end"], s["end"])
            out[-1]["text"] = f"{out[-1]['text']} {s['text']}".strip()
        else:
            out.append(dict(s))
    return out


def stage_merge(paths, desktop, mic, turns, args):
    dd, md = frame_db(decode_mono(args.ffmpeg, paths[0])), frame_db(decode_mono(args.ffmpeg, paths[1]))
    n = min(len(dd), len(md))
    dd, md, = dd[:n], md[:n]
    delta = md - dd
    voiced = md > np.percentile(md, 5) + 10
    mic_dominant = voiced & (delta > args.threshold)

    def frames(t0, t1):
        return slice(max(0, int(t0 * SR / FRAME)), min(n, int(t1 * SR / FRAME) + 1))

    names = dict(p.split("=", 1) for p in args.names.split(",") if "=" in p)

    # Track 1 is the far end by construction - it only needs diarizing into who.
    out = []
    for s in desktop:
        spk = "OTHER"
        if turns:
            scores = {}
            for t in turns:
                if (ov := overlap(s["start"], s["end"], t["start"], t["end"])) > 0:
                    scores[t["speaker"]] = scores.get(t["speaker"], 0.0) + ov
            if scores:
                spk = max(scores, key=scores.get)
        out.append({**{k: v for k, v in s.items() if k != "words"}, "speaker": names.get(spk, spk), "source": "desktop"})

    # Track 2 is you plus bleed, so it is the only track needing the energy gate.
    if any("words" in s for s in mic):
        mic = split_by_word_attribution(mic, voiced, mic_dominant, frames)
    kept = dropped_energy = dropped_dupe = 0
    for s in mic:
        v = voiced[sl := frames(s["start"], s["end"])]
        if v.sum() < 3 or mic_dominant[sl].sum() / max(1, v.sum()) <= 0.5:
            dropped_energy += 1
            continue
        # Belt and braces: text that closely matches an overlapping desktop segment is bleed that
        # slipped the gate, not you.
        if any(overlap(s["start"], s["end"], d["start"], d["end"]) > 0.3 * (s["end"] - s["start"])
               and difflib.SequenceMatcher(None, s["text"].lower(), d["text"].lower()).ratio() > 0.6 for d in out):
            dropped_dupe += 1
            continue
        out.append({**s, "speaker": args.me, "source": "mic", "delta_db": float(np.median(delta[sl][v]))})
        kept += 1

    out.sort(key=lambda s: s["start"])
    out = coalesce(out)
    print(f"merge: {len(desktop)} desktop segments; mic kept {kept} as {args.me!r}, "
          f"dropped {dropped_energy} (energy) + {dropped_dupe} (text match)")
    return out


def write_outputs(segs, outdir, stem="transcript"):
    base = outdir / stem
    base.with_suffix(".json").write_text(json.dumps(segs, ensure_ascii=False, indent=1), encoding="utf-8")
    with base.with_suffix(".txt").open("w", encoding="utf-8") as f:
        last = None
        for s in segs:
            if s["speaker"] != last:
                f.write(f"\n[{ts(s['start'])}] {s['speaker']}:\n")
                last = s["speaker"]
            f.write(f"    {s['text']}\n")
    with base.with_suffix(".srt").open("w", encoding="utf-8") as f:
        for i, s in enumerate(segs, 1):
            f.write(f"{i}\n{srt_ts(s['start'])} --> {srt_ts(s['end'])}\n[{s['speaker']}] {s['text']}\n\n")

    talk = {}
    for s in segs:
        talk[s["speaker"]] = talk.get(s["speaker"], 0.0) + (s["end"] - s["start"])
    print("\nspeaking time:")
    for spk, secs in sorted(talk.items(), key=lambda kv: -kv[1]):
        print(f"  {spk:<24s} {secs / 60:5.1f} min")
    print(f"\n-> {base}.{{txt,srt,json}}")


# ------------------------------------------------------------------ cli
def main():
    ap = argparse.ArgumentParser(description="Speaker-attributed transcript from a two-track OBS interview recording.",
                                 formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("recording", type=Path, help="OBS .mp4/.mkv with desktop audio and mic on separate tracks")
    ap.add_argument("outdir", type=Path, nargs="?", help="output directory (default: alongside the recording)")
    ap.add_argument("--desktop-track", type=int, default=1, help="1-based audio track carrying the far end")
    ap.add_argument("--mic-track", type=int, default=2, help="1-based audio track carrying your microphone")
    ap.add_argument("--me", default="ME", help="label for your own speech")
    ap.add_argument("--names", default="", help="rename diarization labels, e.g. 'SPEAKER_00=Ben Trovato,SPEAKER_01=G.K.M. Tobin'")
    ap.add_argument("--model", default="large-v3", help="faster-whisper model size")
    ap.add_argument("--language", help="force a language code (e.g. en) instead of auto-detecting")
    ap.add_argument("--device", default="auto", choices=("auto", "cuda", "cpu"), help="whisper device")
    ap.add_argument("--compute-type", default="int8", help="ctranslate2 compute type; float16 needs compute capability >= 7.0")
    ap.add_argument("--threshold", type=float, default=10.0, help="dB the mic must exceed the desktop track by to count as you")
    ap.add_argument("--no-diarize", action="store_true", help="skip pyannote; the far end becomes a single OTHER")
    ap.add_argument("--diarization-model", default="pyannote/speaker-diarization-community-1", help="gated pyannote pipeline")
    ap.add_argument("--diarize-device", default="cpu", choices=("cpu", "cuda"), help="pyannote device (cuda needs a CUDA torch build)")
    ap.add_argument("--num-speakers", type=int, help="exact number of far-end speakers, if known")
    ap.add_argument("--min-speakers", type=int)
    ap.add_argument("--max-speakers", type=int)
    ap.add_argument("--stage", choices=STAGES, help="run a single stage and stop")
    ap.add_argument("--retranscribe", action="store_true", help="ignore cached transcripts")
    ap.add_argument("--rediarize", action="store_true", help="ignore cached diarization")
    ap.add_argument("--ffmpeg", help="path to ffmpeg")
    ap.add_argument("--ffprobe", help="path to ffprobe")
    args = ap.parse_args()

    if not args.recording.exists():
        sys.exit(f"No such recording: {args.recording}")
    args.ffmpeg, args.ffprobe = find_exe("ffmpeg", args.ffmpeg), find_exe("ffprobe", args.ffprobe)
    outdir = args.outdir or args.recording.parent / f"{args.recording.stem}-transcript"
    cache = outdir / ".cache"
    cache.mkdir(parents=True, exist_ok=True)

    streams = audio_streams(args.ffprobe, str(args.recording))
    if len(streams) < max(args.desktop_track, args.mic_track):
        sys.exit(f"{args.recording.name} has {len(streams)} audio track(s); need tracks "
                 f"{args.desktop_track} (desktop) and {args.mic_track} (mic). In OBS, set Recording > Audio Track "
                 f"so Desktop Audio and your mic record to separate tracks.")
    desktop_i, mic_i = args.desktop_track - 1, args.mic_track - 1

    run = (args.stage,) if args.stage else STAGES
    paths = split_tracks(args.ffmpeg, str(args.recording), desktop_i, mic_i, cache,
                         force=args.stage == "split") if "split" in run else [cache / "desktop.m4a", cache / "mic.m4a"]
    if args.stage == "split":
        return
    if not all(p.exists() for p in paths):
        sys.exit("Audio tracks not split yet - run without --stage first.")

    desktop, mic = stage_transcribe(paths, cache, args)
    if args.stage == "transcribe":
        return

    turns = [] if args.no_diarize else stage_diarize(paths[0], cache, args)
    if args.stage == "diarize":
        return

    write_outputs(stage_merge(paths, desktop, mic, turns, args), outdir)


if __name__ == "__main__":
    main()
