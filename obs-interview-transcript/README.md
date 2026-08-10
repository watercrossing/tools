# obs-interview-transcript

Turn a two-track [OBS](https://obsproject.com/) recording of an online interview into one speaker-attributed transcript.
Track 1 is Desktop Audio (everyone on the call), track 2 is your own microphone.
Everything runs locally — the audio never leaves the machine, which is the point when the recording is research data.

## What this is for

Recording a research interview that happens over Teams, without depending on Teams to do it.

Teams' own transcript is not always available to you: it depends on tenant policy, it may not be retrievable for external participants, and for a study you often need the audio itself under your own control, on your own storage, with your own retention rules.
Recording locally with OBS solves that, but introduces a problem this tool exists to fix.

The obvious OBS approach — **Application Audio Output Capture**, which grabs the audio of one specific process — [does not work with Microsoft Teams](https://github.com/obsproject/obs-studio/issues/6339).
Teams renders audio through a process tree that the capture source cannot follow, so you get silence or an intermittent stream.
The reliable alternative is to capture **Desktop Audio** (everything your speakers play, which is the far end) on one track and your **microphone** on another.

That gives a usable recording, but the two tracks are not clean:

- Desktop audio is everyone *except* you.
- Your microphone is you, *plus* the far end bleeding back in through your speakers.

Naively transcribing both and concatenating produces every remote utterance twice, once clean and once badly.
This tool separates them properly and emits a single transcript with speaker labels.

If you record with **headphones**, there is no bleed and the job is easier — the tool handles that case too, it is simply built for the harder one.

## How the separation works

The useful asymmetry is that **the desktop track cannot contain your voice**, because a conferencing app never plays your own microphone back to you.
So every word on track 1 belongs to someone else and only needs diarizing into *who*; track 2 is the only track needing a gate.

That gate is an energy comparison per 30 ms frame.
Measured on a real 30-minute interview recorded through speakers:

| Condition | median (mic dB − desktop dB) |
| --- | --- |
| You speaking (desktop at its noise floor) | **+27.5 dB** |
| Far end speaking (mic hears only bleed) | **−9.9 dB** |

Roughly 37 dB of separation, so the default `--threshold 10` sits in empty space rather than being finely tuned; anything from +12 to +20 gives the same answer on that recording.

Attribution runs **per word**, not per segment.
Whisper regularly draws one segment straight across a speaker handover and a whole-segment decision has to pick one and be wrong about half the text.
Each word is scored against the energy mask, the sequence is median-smoothed over three words so a single noisy word cannot shatter a run, and consecutive same-label words are regrouped into segments cut at the actual boundary.

A text-similarity backstop then drops any kept mic segment that overlaps a desktop segment in time and matches it closely in wording, on the grounds that it is bleed which slipped the gate rather than you.

## Requirements

- **ffmpeg / ffprobe** (any recent build) on `PATH`, or pass `--ffmpeg` / `--ffprobe`.
- **[uv](https://docs.astral.sh/uv/)** — the script declares its Python and dependencies inline (PEP 723), so `uv run` fetches them into a throwaway env; there is nothing to install.
  The first run downloads a lot (torch, cuDNN, cuBLAS, the Whisper weights); afterwards it is cached.
- **An NVIDIA GPU** is optional but strongly wanted for transcription: `large-v3` on a (fairly ancient) GTX 1070 does 30 minutes of audio in about 8 minutes, where CPU takes hours.
  Without one, pass `--device cpu` and use a smaller `--model`.
- **A Hugging Face account and token**, for diarization only — see below.
  Skip it entirely with `--no-diarize`.

### Hugging Face setup (diarization only)

pyannote's models are *gated*: the weights sit behind a click-through form that collects your contact details.
It is not a paywall and not a restrictive licence (the model is MIT) — but it does require an account, and the download has to prove which account is asking.

1. Accept the conditions at <https://huggingface.co/pyannote/speaker-diarization-community-1>.
   That is the only one needed — the pipeline is self-contained, unlike the older `speaker-diarization-3.1`, which also required `pyannote/segmentation-3.0`.
2. Create a **read**-scope token at <https://huggingface.co/settings/tokens>.
3. Store it where every Hugging Face library looks for it:

   ```bash
   uv tool install huggingface_hub    # provides the `hf` command
   hf auth login                      # paste the token; answer "n" to the git-credential question
   ```

   That writes `~/.cache/huggingface/token`.
   Prefer this to an `HF_TOKEN` environment variable, which is inherited by every child process you spawn and can end up in crash dumps and CI logs.

Accepting the licence shares your contact details with pyannote; it does **not** send them your audio.
The models download to your machine and diarization runs locally.

## OBS setup

In OBS, under **Settings → Output → Recording**, enable at least two audio tracks, then in the **Audio Mixer** route the sources via each source's *Advanced Audio Properties*:

- **Desktop Audio** → Track 1 only
- **Mic/Aux** → Track 2 only

Untick the other tracks for each source, so the two never mix.
Record to `.mkv` if you want crash resilience, or `.mp4` if you want to hand the file straight to something else.

If your tracks are the other way round, pass `--desktop-track 2 --mic-track 1`.

## Usage

```bash
uv run obs-interview-transcript.py RECORDING [OUTDIR]     # or ./obs-interview-transcript.py ...
```

Example:

```bash
uv run obs-interview-transcript.py "2026-08-01 09-05-00.mp4" ./interview \
    --me "Interviewer" --language en
```

Output lands in `OUTDIR` (default: a `<recording>-transcript` folder beside the recording):

- `transcript.txt` — grouped by speaker with timestamps, for reading
- `transcript.srt` — subtitles with `[Speaker]` prefixes, for playing back against the video
- `transcript.json` — segments with `start`, `end`, `speaker`, `source` track, for further processing

Intermediates cache in `OUTDIR/.cache/` (`desktop.m4a`, `mic.m4a`, `desktop.json`, `mic.json`, `diarization.json`), so re-running is nearly free.

### Naming the speakers

pyannote separates voices but has no idea of names, so the far end comes out as `SPEAKER_00`, `SPEAKER_01`, ….
Read the opening minutes of `transcript.txt` — people usually introduce themselves — then re-run with the mapping:

```bash
uv run obs-interview-transcript.py RECORDING OUTDIR --names "SPEAKER_02=G.K.M. Tobin,SPEAKER_01=Ben Trovato"
```

Everything is cached by then, so this completes in about a second and can be repeated until the labels are right.

### Other options

| Option | Purpose |
| --- | --- |
| `--me NAME` | label for your own speech (default `ME`) |
| `--model` | Whisper size; `large-v3` default, `small` is much faster and noticeably worse |
| `--language en` | skip auto-detection, which helps on recordings that open with crosstalk |
| `--device cpu` | no GPU available |
| `--compute-type` | `int8` default; `float16` is faster but needs compute capability ≥ 7.0 (Pascal cards like the 1070 do not qualify) |
| `--threshold` | dB the mic must exceed the desktop track by; raise it if bleed leaks in, lower it if your quiet interjections are dropped |
| `--no-diarize` | skip pyannote; the far end becomes a single `OTHER` |
| `--num-speakers` | tell pyannote the exact count when you know it |
| `--diarization-model` | a different gated pyannote pipeline (default `pyannote/speaker-diarization-community-1`) |
| `--stage` | run one of `split` / `transcribe` / `diarize` / `merge` and stop |
| `--retranscribe`, `--rediarize` | ignore the relevant cache |

## Diarization is slow on CPU

The pyannote speaker-embedding pass is the most expensive part of the pipeline: roughly **15 minutes for 30 minutes of audio** on a 16-core CPU, against about 8 minutes for `large-v3` transcription on a GPU.
It prints nothing while it works.

`--diarize-device cuda` would cut that to a minute or two, but torch installs here as the CPU build from PyPI.
Using the GPU means installing a CUDA torch from PyTorch's own index, so it is not the default.

## Windows notes

One fix is baked into the script that is tedious to rediscover: **ctranslate2 finds its CUDA DLLs through `PATH`**, not `os.add_dll_directory`, so the cuBLAS/cuDNN DLLs shipped inside the `nvidia-*` wheels are invisible to it until `PATH` names them.
Without it, GPU transcription fails with `Library cublas64_12.dll is not found`.

### Why community-1 rather than speaker-diarization-3.1

The older `pyannote/speaker-diarization-3.1` works, but only on `pyannote.audio` 3.x, and pinning to that drags in a chain of incompatibilities worth recording so nobody re-derives it: 3.3.2 needs `huggingface_hub` < 1.0 (for `use_auth_token`) and `torchaudio` < 2.9 (for `AudioMetaData`), and on torch ≥ 2.6 it trips `weights_only=True`, which refuses the ordinary Python objects pyannote stores beside its weights and names only *one* rejected class per run.
`community-1` on 4.x needs none of that: no pins, no `add_safe_globals` shim, and it is self-contained — segmentation, embedding and PLDA all live in the one repo, so there is a single licence to accept instead of two.

On a 30-minute four-participant test both produced **identical** output — 75/75 segments agreeing under a 1:1 relabelling — except that 3.1 emitted a spurious fifth speaker cluster and `community-1` did not.

## Limitations

- **Diarization sees only the desktop track**, so simultaneous remote speakers usually collapse to one label. `--num-speakers` helps when you know the count.
- **Short backchannels at the gate boundary** (`"mm"`, `"yeah"`, a name) can land on the wrong side during crosstalk. Substantive turns are reliable; one-word interjections in a busy greeting are not.
- **Whisper mishears numbers and proper nouns.** For an interview where specific dates, figures or names matter, check them against the audio using the timestamps — that is what the `.srt` is for.
- **The bleed figure is room-specific.** The −9.9 dB above depends on speaker volume and mic placement; a different setup shifts it, though the default threshold has a wide margin.
- **Two tracks, one local speaker.** If two people share your microphone in the room, they both become `--me`; separating them would need diarization on the mic track as well.
