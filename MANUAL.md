# Video Derivative Generator (vdg)
## User Manual — v1.5.0
**Stanford Media Preservation Lab**
*September 2026*

---

## Table of Contents

1. [Overview](#overview)
2. [Dependencies](#dependencies)
3. [Intake Pipelines](#intake-pipelines)
4. [Output Formats](#output-formats)
5. [Clean Aperture Handling](#clean-aperture-handling)
6. [Scaling and Resolution Handling](#scaling-and-resolution-handling)
7. [Frame Rate Handling](#frame-rate-handling)
8. [Quarantine Workflow](#quarantine-workflow)
9. [Directory Structure](#directory-structure)
10. [Naming Conventions](#naming-conventions)
11. [Command Reference](#command-reference)
12. [Audio Configuration](#audio-configuration)
13. [Override Flags](#override-flags)
14. [Thumbnails](#thumbnails)
15. [Validation and Logging](#validation-and-logging)
16. [Usage Examples](#usage-examples)
17. [Troubleshooting](#troubleshooting)
18. [Version History](#version-history)

---

## Overview

`vdg` (Video Derivative Generator) is a batch video transcoding tool for the Stanford Media Preservation Lab. It processes video files from a source directory, generates one or more derivative formats, produces JPEG 2000 thumbnails alongside H.264 output, and moves completed source files to a finished archive folder. Sources that are detected as variable frame rate before encoding, or whose lossless output fails validation, are moved to a `QUARANTINE` folder instead of the finished archive, so review candidates never get silently mixed back in with untried or successfully-processed sources. All operations are logged to a per-file process log and a cumulative CSV summary.

The script is designed around two distinct intake pipelines:

- **Tape digitization** — FFV1/MKV preservation masters produced by the SMPL digitization workflow from analog formats (Betacam SP, Digital Betacam, VHS, U-matic, Hi8, DV)
- **Acquired digital content** — born-digital HD deliverables from vendors and distributors

Four output formats are available: `-h264`, `-v210`, `-prores`, `-ffv1`. At least one must be specified on every invocation; they may be combined freely (e.g. `-ffv1 -h264` to generate a preservation master and an access copy in the same pass).

---

## Dependencies

| Tool | Purpose |
|------|---------|
| `ffmpeg` | All encoding, filtering, hashing, and thumbnail generation |
| `ffprobe` | Source file metadata detection |
| `mediainfo` | Per-file technical metadata summary (MediaInfo CLI) |
| Python 3.10+ | Runtime |
| `tqdm` | Progress bar display |
| `mediaconch` | *(Optional)* Policy conformance checks on `-v210`/`-ffv1` output |

`ffmpeg`, `ffprobe` and `mediainfo` must be present in `PATH`. The script checks for them at startup and exits if any is missing. `mediaconch` is checked separately — if it's missing, `vdg` logs a warning and skips policy conformance checks for the run rather than exiting.

At startup, `vdg` lists every dependency with its version and location, in the terminal and the session log, so each run's log records exactly which toolchain produced it:

```
INFO: Dependencies:
  ffmpeg      7.1.5  /opt/homebrew/bin/ffmpeg
  ffprobe     7.1.5  /opt/homebrew/bin/ffprobe
  mediainfo   26.05  /opt/homebrew/bin/mediainfo
  mediaconch  25.04  /opt/homebrew/bin/mediaconch
  Python      3.13.7 /opt/homebrew/opt/python@3.13/bin/python3.13
  tqdm        4.67.1
```

### Supported FFmpeg versions

| Platform | FFmpeg | How it's installed |
|----------|--------|--------------------|
| macOS (Apple Silicon) | 7.1.x | Homebrew `ffmpeg@7`, pinned with `brew pin ffmpeg@7`, first on `PATH` |
| Ubuntu 24.04 | 6.1.x | Stock Ubuntu `ffmpeg` package, held with `apt-mark hold ffmpeg` |

**FFmpeg 9.x is not supported.** On macOS it has broken ffprobe JSON parsing and FFV1 → v210 lossless transcodes (framemd5 validation failures). Newer FFmpeg releases will be adopted only after they've been tested against lossless FFV1/v210 round-trips on both platforms. See [INSTALL_MACOS.md](INSTALL_MACOS.md) and [INSTALL_UBUNTU.md](INSTALL_UBUNTU.md) for installation and pinning.

At startup, `vdg` logs the FFmpeg version and path it's using, in both the terminal and the session log. It warns without stopping the run if: FFmpeg is newer than the tested major version (7.x); the release version can't be determined (e.g. a git snapshot build); or `ffmpeg` and `ffprobe` report different versions (usually a `PATH` problem). The tested ceiling is set by `MAX_TESTED_FFMPEG_MAJOR` in `vdg/cli.py`.

Check which FFmpeg `vdg` will use with `which ffmpeg && ffmpeg -version | head -1`.

**AAC encoder autodetection:** The script probes for AAC encoders at startup in priority order: `aac_at` (macOS AudioToolbox, preferred) → `libfdk_aac` → `aac` (FFmpeg native). The selected encoder is logged and used for all H.264 output in that session. This is only logged when `-h264` is requested — on lossless-only runs (`-v210`/`-ffv1`), audio is copied (PCM/native), not AAC-encoded, so logging an AAC encoder in that case would be misleading.

---

## Intake Pipelines

### Tape Digitization (SD)

Source files are FFV1-encoded MKV containers produced by the SMPL tape digitization workflow. These files follow a strict naming convention using the `_pm` role code suffix. The script is well-tested against this population.

Supported source formats: Betacam SP, Digital Betacam, VHS, U-matic, Hi8, DV

**DV note:** DV content is captured and packaged using dvrescue (MIPoPS). dvrescue-packaged DV-in-MKV files can present unreliable container metadata — in particular, ffprobe may misread the frame rate. The frame rate is now corrected automatically from the DV frame header (see [DV frame rate correction](#dv-frame-rate-correction)); `--force-fps 29.97` remains available as an override. DV sources usually have no stream-level field order either, but `vdg` now falls back to the field order in the first frames (see [`--force-scan`](#--force-scan)). Keeping `--force-scan bff` on DV runs is still a harmless safeguard.

**v210 output is restricted to SD sources** that resolve to a recognized NTSC or PAL video standard (see [Scaling and Resolution Handling](#scaling-and-resolution-handling)). Attempting to generate v210 from a source that doesn't resolve to NTSC or PAL raises an error before encoding starts. **`-prores` and `-ffv1` are not restricted by video standard** — both encode at the source's native dimensions with no scaling filter applied, so they run against SD or HD sources equally.

### Acquired Digital Content (HD)

Born-digital deliverables from vendors, distributors, and licensing partners. These files may arrive as H.264 or H.265 in `.mov` or `.mp4` containers, or occasionally as other formats. This population is more variable in its technical attributes. The `-h264` output flag is the appropriate derivative for this pipeline; `-ffv1` is also commonly used here to produce a lossless preservation copy of an HD deliverable.

---

## Output Formats

### H.264 MP4 (`-h264`)

Two-pass H.264 encode targeting the `main` profile. JPEG 2000 thumbnails are generated alongside every H.264 output.

| Parameter | SD (≤576 lines) | HD (>576 lines) |
|-----------|----------------|----------------|
| Target bitrate | 1000 kbps | 2800 kbps |
| Max bitrate | 1200 kbps | 2900 kbps |
| Buffer size | 2000 kbps | 5800 kbps |
| GOP | 2× output frame rate | 2× output frame rate |
| Profile | Main | Main |
| Pixel format | yuv420p | yuv420p |

Interlaced sources are deinterlaced using `bwdif` before encoding. Progressive sources pass through without deinterlacing.

Audio: AAC stereo, 48 kHz, 128 kbps. See [Audio Configuration](#audio-configuration) for channel routing options.

**Encode steps:** video and audio are never encoded in the same ffmpeg process:

1. **Pass 1**: x264 analysis pass, video only.
2. **Pass 2**: x264 final encode, video only, written to `process_logs/<base>_sl_video_tmp.mp4`.
3. **Audio mux**: the pass-2 video is stream-copied (not re-encoded) into the final MP4, and the audio is decoded, filtered and AAC-encoded from the source.

Keeping video encoding and audio encoding in separate ffmpeg processes is the fix for audio dropping out partway through long files, observed around 35 minutes into long Premiere ProRes HQ exports ([issue #10](https://github.com/Stanford-Media-Preservation-Lab/vdg/issues/10)). When slow x264 video and near-instant AAC audio were encoded in one process, the audio went silent while the video kept playing and the file duration still looked correct. The mux step also passes `aresample=async=1:min_hard_comp=0.1:first_pts=0` (ahead of any `--audio-mode`/`--clip-ceiling` filters) and `-max_interleave_delta 0`. Neither fixed the dropout on its own. They're kept because they're part of the command confirmed on a real 1:47:12 Premiere export. The split applies to every `-h264` source with audio: the mux step only reads the file and decodes audio, so it adds little time. The temp video is deleted after a successful mux. If pass 2 or the mux fails, it's left in `process_logs/` for inspection. Sources with no audio skip the mux step: pass 2 writes the final file directly.

Output filename: `<base>_sl.mp4`

### v210 QuickTime (`-v210`)

Uncompressed 10-bit 4:2:2 in a QuickTime container. **SD sources only** (see [Intake Pipelines](#intake-pipelines)). A framemd5 lossless validation is performed after encoding, comparing the video stream of the output against the source frame by frame, and comparing audio stream hashes — see [Validation and Logging](#validation-and-logging). If `mediaconch` is available, a policy conformance check also runs, with separate policies for NTSC and PAL. The output file is deleted (or retained with a `_VALIDATION_FAILED` suffix, with `--keep-failed`) if any of these checks fail, and the source is quarantined — see [Quarantine Workflow](#quarantine-workflow).

Color metadata is set per standard:

| Standard | Field order | SAR | Primaries / matrix | Transfer |
|----------|-------------|-----|--------------------|----------|
| NTSC | BFF | 10:11 | `smpte170m` (BT.601 NTSC / BT.601) | BT.709 |
| PAL | TFF | 12/11 | `bt470bg` (BT.601 PAL / BT.470 System B/G) | BT.709 |

These match the tags vrecord writes on its NTSC and PAL captures. Before v1.5.0, PAL v210 output was tagged with NTSC primaries. The field order is fixed per standard, which is correct for SDI captures. PAL DV (bottom field first) and TFF NTSC sources would be mislabelled, but neither is sent to v210 in this lab's workflow (see the note on RF captures below).

**Not for RF captures (lab rule):** don't transcode Domesday Duplicator / ld-decode (RF-decoded) captures to v210. FFmpeg's v210 encoder can only store 10-bit values 4–1019, because 0–3 and 1020–1023 are reserved for SDI sync. RF-decoded footage routinely contains those codes (a DdD test clip had luma 0–1023 in every frame), so framemd5 validation can never pass, and the v210 would be no more faithful than ProRes. RF captures are also often TFF NTSC with 44.1 kHz audio, which the NTSC policy rejects. Use `-prores` for mezzanine/editing copies of RF captures. The FFV1 capture remains the preservation master and is the only copy that keeps the full 0–1023 range.

Audio: PCM 24-bit little-endian (`pcm_s24le`).

Output filename: `<stem>.mov` (no role code change)

### ProRes 422 HQ QuickTime (`-prores`)

Apple ProRes 422 HQ using the `prores_ks` encoder, profile 3, video tag `apch` (the ProRes 422 HQ fourCC), vendor tag `apl0`. No scaling filter is applied — output dimensions match the source exactly. Audio is copied from the source stream without re-encoding. No lossless validation or MediaConch check is performed for ProRes output.

**Interlaced sources** are encoded as interlaced ProRes (`-flags +ildct`), so each frame is compressed and flagged as two fields in the ProRes bitstream itself, not just labelled TFF/BFF in the container. When the field order comes from `--force-scan` or from the frame-sampling fallback (see [`--force-scan`](#--force-scan)), it's also pinned with `setfield`, so every frame carries it, including frames from a damaged tape head that have no field flags of their own. Progressive sources are encoded progressive, as before.

**Value range:** ProRes stores 10-bit samples in the 4–1019 range, like v210. Codes 0–3 and 1020–1023, which can occur in RF-decoded (Domesday Duplicator/ld-decode) material, are clipped. Those values are far outside the black-to-white picture range, so the clip isn't visible, and ProRes is not validated as lossless.

Output filename: `<base>_sh.mov`

### FFV1 v3 Lossless MKV (`-ffv1`)

Mathematically lossless intra-frame video in a Matroska container, intended as an alternative or supplement to a v210 preservation master. No scaling filter is applied — output dimensions match the source exactly (subject to [clean aperture handling](#clean-aperture-handling)).

| Parameter | Value | Purpose |
|-----------|-------|---------|
| `-level 3` | FFV1 version 3 | Multithreading, per-slice CRCs; accepted by most digital preservation repositories |
| `-g 1` | Keyframe every frame | Required for random access and error recovery in archival use |
| `-slices 16` | 16-slice multithreading | Appropriate for Apple Silicon and modern x86 |
| `-slicecrc 1` | Per-slice CRC | Embeds a CRC in every slice header for error detection |

Audio is copied without re-encoding, preserving the original stream exactly.

**`VENDOR_ID` tagging:** when the source is a genuine QuickTime file (identified by the `major_brand` container tag equal to `qt`, exposed internally as `VideoInfo.is_quicktime`), the output's `VENDOR_ID` is explicitly set to `Apple QuickTime`. Without this override, ffmpeg carries the source's own per-stream `vendor_id` tag straight through — on this lab's QuickTime sources that tag has been observed as `KeyG`, a capture-chain leftover rather than meaningful authorship metadata. For non-QuickTime sources (MXF, MPEG, etc.), the tag is left untouched, since "Apple QuickTime" would be actively wrong there.

Like v210, FFV1 output undergoes framemd5 + audio streamhash validation and, if `mediaconch` is available, a MediaConch policy check (unconditional — unlike v210, the FFV1 policy is not gated to a specific video standard). Failure deletes or retains (`--keep-failed`) the output and quarantines the source.

Output filename: `<base>_pm.mkv`

---

## Clean Aperture Handling

Some QuickTime-family sources — in particular v210 masters produced by other tools, and some camera-native files — carry a *clean aperture*: metadata instructing players to display only a cropped subregion of the full coded frame. On this lab's FFmpeg builds (confirmed on 8.1.2), this is conveyed through a `Frame Cropping` side-data entry on the video stream (`crop_top`, `crop_bottom`, `crop_left`, `crop_right`), not through a coded/display dimension split — `coded_width`/`coded_height` and `width`/`height` were observed identical even on a confirmed clap-tagged file.

This matters for `-v210` and `-ffv1`, which are meant to be mathematically lossless: if ffmpeg applied the clean-aperture crop during decode, the output would only contain the *display* pixels, discarding real captured data outside the clean aperture — a lossy operation with respect to the source's coded frame, even though the video codec itself is lossless.

### Default: full coded frame preserved

By default, `vdg` passes `-apply_cropping 0` to ffmpeg on the input side of every `-v210` and `-ffv1` encode. This disables automatic clean-aperture cropping, so the full coded frame is preserved in the output — the correct default for preservation masters, where nothing outside what was actually captured should be discarded.

An earlier iteration of this logic used `-flags2 +ignorecrop`, which turned out to have **no effect** on clap-derived cropping (confirmed via MediaInfo: a source stayed cropped to 704×480 with or without it). That flag only suppresses codec-level SPS conformance-window cropping — a different mechanism from the container-derived clap crop. `-apply_cropping` is the flag that actually controls it.

### `--clean-aperture`: honor the crop instead

Pass `--clean-aperture` to do the opposite: omit `-apply_cropping 0` and let ffmpeg apply its native clap crop, producing display-cropped output at non-standard dimensions. For example, a 720×486 source with an 8/8/3/3 crop becomes 704×480. Only use this if you specifically want the display-cropped result rather than the full preservation frame — it is not the recommended setting for archival masters. With `-v210`, the cropped output can't satisfy the NTSC policy's 720×486 rule, so `vdg` skips just the MediaConch width and height rules for that run. Every other rule still applies. It logs a yellow warning with the actual output size and notes the omission in the process log, so a cropped v210 is always flagged rather than quarantined.

### The dimension/crop warnings

At file-list time, and once (not twice) during actual processing, `vdg` checks each source for two independent signals and logs a warning if either fires:

1. **`coded_width`/`coded_height` vs. `width`/`height` divergence** — a defensive check for FFmpeg builds where ffprobe reports the coded (full) and display (cropped) dimensions differently. `vdg` always uses coded dimensions (falling back to display dimensions if coded ones aren't reported) for all downstream calculations — scaling, GOP, thumbnails.
2. **A `Frame Cropping` side-data entry** on the video stream — the signal that reliably fires on this lab's FFmpeg 8.1.2 builds, including cases where check (1) above reports no divergence at all. The warning includes the actual crop values from the source.

Both warnings are informational; they don't change encoding behavior on their own. They exist so a clean-aperture source doesn't get processed under the silent assumption that the full frame was captured when it wasn't specifically evaluated.

### Verifying a run

framemd5 validation alone cannot catch a clean-aperture mismatch — it only proves that the source-hash and output-hash commands agree with *each other*, not that either one matches the true, uncropped source. `vdg` always uses matching `--clean-aperture` handling between the source-hash command and the encode command specifically to avoid this blind spot. If you're debugging a custom workflow, verify actual pixel dimensions directly (`ffprobe` or MediaInfo on the output) rather than relying on a framemd5 PASS alone.

---

## Scaling and Resolution Handling

The script resolves output scale (for `-h264` and its thumbnails) based on source width, height, and DAR. `-prores` and `-ffv1` are not scaled — they always encode at native source dimensions. The table below summarizes all H.264 scaling cases.

| Source dimensions | DAR | Output scale | Notes |
|-------------------|-----|--------------|-------|
| 720×480 or 720×486 (NTSC) | 4:3 | 640×480 | Standard NTSC SD |
| 720×480 or 720×486 (NTSC) | 16:9 | 854×480 | Widescreen NTSC SD |
| 352×480 (NTSC) | 4:3 | 640×480 | Half-D1 NTSC — direct-to-disc DVD recorders |
| 720×576 (PAL) | 4:3 | 640×480 | Standard PAL SD |
| 720×576 (PAL) | 16:9 | 854×480 | Widescreen PAL SD |
| 1440×1080 | 16:9 | 1280×720 | Anamorphic HD (e.g. HDV) |
| 1920×1080 | any | 1280×720 | Full HD downscale |
| 1280×720 | any | 1280×720 | Native 720p passthrough |
| >1920 or >1080 | 16:9 | 1280×720 | Ultra-HD downscale |
| >1920 or >1080 | other | -2:720 | Width calculated to preserve AR |
| All other | any | Source dimensions (mod-2 safe) | Passthrough with dimension rounding |

Every `scale=` operation in both the H.264 filter chain and the thumbnail filter chain is followed by `setsar=1`. Without this, ffmpeg's `scale` filter recalculates the output SAR to preserve the *source's original* DAR whenever only literal width/height are given — which silently reverts any intentional aspect-ratio override (see `--force-anamorphic` below) back to the source's original, uncorrected display ratio.

### `--force-anamorphic`

Some SD tape sources are digitized as 4:3 full-frame despite actually containing anamorphic squeezed 16:9 footage — the detected DAR in this case is unreliable. `--force-anamorphic` overrides scaling to 854×480 (16:9) for 720-wide SD sources (480, 486, or 576 lines) regardless of detected DAR. It affects H.264 scaling and thumbnails only; it has no effect on HD-resolution sources.

```bash
vdg --source-dir ... --output-dir ... --force-anamorphic -h264
```

---

## Frame Rate Handling

### Standard frame rate halving

Sources with frame rates above the standard threshold for their standard (>30 fps for NTSC, >25 fps for PAL) are halved on H.264 output. This handles 50i/60i sources correctly.

### DV frame rate correction

DV's frame rate is fixed by its signal system, and every DV frame header records that system: 525/60 (NTSC family) or 625/50 (PAL family). Raw `.dv` files, and MKVs built from raw DV (e.g. by dvrescue), have no real container timing, so ffprobe's frame rate fields can come out nonsensical: 60,000 fps, 30,000 fps, or hundreds of fps.

For every `dvvideo` source, `vdg` reads the DSF flag from the first DV frame's header, in any container, and derives the true rate:

| DV system | SD, DVCPRO HD 1080i | DVCPRO HD 720p |
|-----------|---------------------|----------------|
| 525/60 | 29.97 | 59.94 |
| 625/50 | 25 | 50 |

If the container-reported rate disagrees, `vdg` uses the DV rate and logs a warning, which is also recorded in the process log:

```
WARNING: ab111cd2222_pm.dv: DV frame rate misreported by container — container reported 60000.00 fps — using DV system rate 29.97 fps (525/60, from DV frame header)
```

If the header can't be read, SD frame height is used instead (480/486 means 525/60, 576 means 625/50). For corrected sources, the whole-file average-rate VFR check below is skipped, because the container's rate fields have just been shown to be meaningless. The per-packet duration check still runs and catches real dropped or held frames. Without this correction, raw DV sent to `-ffv1` was falsely quarantined as VFR, and H.264 encodes failed at absurd output rates. `--force-fps` still overrides the corrected rate if passed. Non-DV sources are unaffected.

This corrects misreported *rate fields*. If a DV file's timestamps are also wrong, its duration will be wrong too; check the MediaInfo summary's `Duration` against the capture length.

### Variable frame rate (VFR) detection

`-v210` and `-ffv1` requests trigger an additional VFR check before encoding, because lossless roundtrip (framemd5) validation assumes constant frame timing and is unreliable against a genuinely VFR source. Two independent checks are combined — either one firing is sufficient to flag the source:

1. **Average rate divergence** — ffprobe's per-stream `r_frame_rate` (nominal/guessed rate) compared against `avg_frame_rate` (total frames ÷ duration). A gap greater than 0.05 fps flags the source.
2. **Per-packet duration check** — `ffprobe -show_entries packet=duration_time` against the actual container index, with no decode required. This catches VFR that the whole-file average misses entirely: a source can have a handful of held or duplicated frames scattered through tens of thousands of total frames and still average out to the nominal rate. This check only runs for `-v210`/`-ffv1` requests, since it's more expensive than the average check and irrelevant to lossy H.264/ProRes output.

A source that trips either check is quarantined — moved to `QUARANTINE/` before any encoding is attempted — rather than transcoded and left to fail (or worse, silently pass) framemd5 validation. See [Quarantine Workflow](#quarantine-workflow).

---

## Quarantine Workflow

Sources that cannot be reliably or losslessly processed are moved to `<output-dir>/QUARANTINE/` instead of `finished_sources/`, so they're visually segregated from both successfully processed files and files not yet attempted. A file is quarantined when:

- It is requested for `-v210` or `-ffv1` output and is detected as **variable frame rate** before encoding starts (see [Frame Rate Handling](#frame-rate-handling)) — no encode is attempted in this case.
- A `-v210` or `-ffv1` encode completes but **fails lossless validation** — framemd5 mismatch, audio streamhash mismatch, or MediaConch policy failure (see [Validation and Logging](#validation-and-logging)).
- Any other exception occurs while `-v210` or `-ffv1` output was requested for that file (e.g. an ffmpeg crash mid-encode).

`-h264`-only and `-prores`-only failures are logged as ordinary errors and left in place in the source directory — they are not moved, since there's no lossless-integrity concern driving the quarantine decision for those formats.

Quarantined files are recorded with `Quarantined` status in `transcode_summary.csv`, and listed separately (marked `Q`) in the live status display and end-of-run summary. They are not automatically retried. Investigate the cause via the per-file process log, then either fix the underlying issue (or re-run with an override flag, e.g. `--force-fps`, if the cause was misdetected metadata rather than a genuine VFR/corruption problem) and move the file back into the source directory manually.

---

## Directory Structure

```
<source-dir>/
    source_file_pm.mkv          ← input files processed from here
    finished_sources/           ← source files moved here after successful processing

<output-dir>/
    source_file_sl.mp4          ← H.264 derivative
    source_file.mov             ← v210 derivative (no role code change)
    source_file_sh.mov          ← ProRes derivative
    source_file_pm.mkv          ← FFV1 derivative
    source_file_thumb_1.jp2     ← thumbnails (alongside H.264 output)
    source_file_thumb_2.jp2
    ...
    QUARANTINE/                 ← sources that failed the VFR check or lossless validation
        source_file_pm.mkv
    transcode_summary.csv       ← cumulative run log
    process_logs/
        source_file_pm_process.log      ← per-file detailed log
        transcode_YYYYMMDD_HHMMSS.log   ← session log
        <stem>_source_video.framemd5    ← retained only with --keep-framemd5
        <stem>_output_video.framemd5
        policy_ffv1.xml                 ← retained only with --keep-mediaconch
        policy_v210_ntsc.xml
```

The `finished_sources`, `QUARANTINE`, and `process_logs` directories are created automatically. Source files are moved to `finished_sources` on successful completion by default (see `--no-move-finished`); files that fail a lossless format go to `QUARANTINE` instead (see [Quarantine Workflow](#quarantine-workflow)).

---

## Naming Conventions

### Role code suffixes

Source files are expected to use a three-character role code suffix:

| Suffix | Role |
|--------|------|
| `_pm` | Preservation master |
| `_sh` | Service high |
| `_sl` | Service low |

### Output filename derivation

The script strips a recognized role code suffix (`_pm`, `_sh`, or `_sl`) from the source stem before constructing output names. If no recognized suffix is present, the full stem is preserved and the output role code is appended directly.

**Examples:**

| Source filename | H.264 output | ProRes output | FFV1 output |
|-----------------|-------------|---------------|-------------|
| `abc123_pm.mkv` | `abc123_sl.mp4` | `abc123_sh.mov` | `abc123_pm.mkv` |
| `abc123_sh.mkv` | `abc123_sl.mp4` | `abc123_sh.mov` | `abc123_pm.mkv` |
| `vendor_delivery.mov` | `vendor_delivery_sl.mp4` | `vendor_delivery_sh.mov` | `vendor_delivery_pm.mkv` |

**Spaces in filenames:** All spaces in source filename stems are replaced with underscores in every output filename. This is applied universally regardless of whether the source filename follows the role code convention.

### Filename disambiguation

If two or more source files share the same base ID after role-code stripping — e.g. `abc123_pm.mov` and `abc123_sh.mp4` delivered for the same item — their output filenames would otherwise collide (both producing `abc123_sl.mp4`, with the second job silently overwriting the first's H.264 output and thumbnails). `vdg` detects these groups automatically at the start of every run and disambiguates:

1. **Extension alone**, if it's unique within the group: `abc123_pm.mov` → `abc123_mov_sl.mp4`; `abc123_sh.mp4` → `abc123_mp4_sl.mp4`.
2. **Role code + extension**, if extension alone isn't unique across the group (e.g. two `.mov` files sharing an ID): `abc123_pm.mov` → `abc123_pm_mov_sl.mp4`; `abc123_sh.mov` → `abc123_sh_mov_sl.mp4`.

A warning is logged at the start of a run reporting how many unique IDs were affected. After a run involving disambiguation, verify the actual output directory listing rather than relying solely on per-file `Success` statuses in the summary — a collision (before this feature, or in some not-yet-anticipated grouping) produces two individually "successful" jobs where the second overwrote the first, and a per-file status alone won't reveal that.

---

## Command Reference

### Required flags (at least one must be specified)

| Flag | Output |
|------|--------|
| `-h264` | H.264 MP4 with JPEG 2000 thumbnails |
| `-v210` | v210 uncompressed 10-bit 4:2:2 QuickTime (SD only) |
| `-prores` | ProRes 422 HQ QuickTime (native resolution) |
| `-ffv1` | FFV1 v3 lossless MKV (native resolution) |

Exception: `--thumbs N` on its own, with no format flag, runs [thumbnails-only mode](#thumbnails-only).

### General flags

| Flag | Description |
|------|-------------|
| `--version` | Print the script name/version and exit |

### Directory flags

| Flag | Default | Description |
|------|---------|-------------|
| `--source-dir PATH` | (set in script) | Directory containing source video files |
| `--output-dir PATH` | (set in script) | Directory for all output files and logs |

### Processing flags

| Flag | Default | Description |
|------|---------|-------------|
| `--workers N` | `1` | Number of parallel worker processes. `1` = sequential processing |
| `--dry-run` | off | Simulate processing without writing any files or moving sources |
| `--cleanup-only` | off | Delete temporary files only; skip all encoding |
| `--move-finished` | on | Move source files to `finished_sources/` after successful processing |
| `--no-move-finished` | — | Disable source file movement |
| `--skip-validation` | off | Skip post-encode H.264 validation (faster but less safe). Does not affect v210/FFV1 lossless validation. |

### Validation and retention flags

| Flag | Default | Description |
|------|---------|-------------|
| `--keep-framemd5` | off | Retain framemd5 files in `process_logs/` after lossless validation instead of deleting them. Applies to `-v210` and `-ffv1`. A digest summary is always written to the process log regardless of this flag. |
| `--keep-mediaconch` | off | Retain the MediaConch policy XML written to `process_logs/` after conformance checks instead of deleting it. |
| `--keep-failed` | off | Retain `-v210`/`-ffv1` output that fails lossless validation, renamed with a `_VALIDATION_FAILED` suffix, instead of deleting it. |

### Override flags (quick reference)

| Flag | Default | Description |
|------|---------|-------------|
| `--force-scan [progressive\|tff\|bff]` | auto-detect | Override ffprobe scan type detection |
| `--force-fps FLOAT` | auto-detect | Override ffprobe frame rate detection |
| `--force-anamorphic` | off | Force 854×480 (16:9) scaling for SD sources mistagged as 4:3 |
| `--clean-aperture` | off | Honor clean aperture crop during `-v210`/`-ffv1` transcodes instead of preserving the full coded frame |
| `--thumbs N` | `4` | Override number of thumbnails generated |

See [Override Flags](#override-flags), [Clean Aperture Handling](#clean-aperture-handling), and [Scaling and Resolution Handling](#scaling-and-resolution-handling) for details on each.

---

## Audio Configuration

All audio options apply to H.264 output only. ProRes audio is copied from the source stream. v210 and FFV1 audio are copied/encoded as PCM, not AAC.

### `--audio-stream`

Selects the audio stream to use for H.264 output. Default: `0:a:0` (first audio stream).

```bash
--audio-stream 0:a:0    # first audio stream (default)
--audio-stream 0:a:1    # second audio stream
```

### `--audio-mode`

Controls channel routing. Default: `stereo`.

| Mode | Behavior |
|------|---------|
| `stereo` | Pass the selected stream through as-is |
| `mono-duplicate` | Take the channel selected by `--audio-channel` from the selected stream and duplicate it to both L and R output channels |
| `mono-merge` | Merge streams `0:a:0` and `0:a:1` (both channels summed and center-panned to L+R) |

`mono-merge` overrides `--audio-stream` and always combines the first two audio streams. This is useful for DV sources where left and right channels are packaged as two separate mono streams.

### `--audio-channel`

Used with `--audio-mode mono-duplicate` to select which channel of the chosen stream to duplicate to both output channels. `0` = left (default), `1` = right. Has no effect with `stereo` or `mono-merge` modes.

```bash
--audio-mode mono-duplicate --audio-channel 0   # duplicate left channel (default)
--audio-mode mono-duplicate --audio-channel 1   # duplicate right channel
```

### `--audio-pan-center`

When used with `--audio-mode stereo`, mixes both input channels equally to both output channels (`c0 = 0.5*c0 + 0.5*c1`, same for c1). Useful for content where dialogue and music are split across channels and both should be present at equal level on both sides.

### `--clip-ceiling FLOAT`

Applies a peak ceiling to the audio output at the specified level in dBFS. Accepts a negative float value.

```bash
--clip-ceiling -10    # limit peaks to -10 dBFS
```

Uses `dynaudnorm` configured with a maximum gain factor of 1.0, meaning the filter **only reduces, never boosts**. Quiet passages are left at their original level. Loud passages are brought down toward the ceiling. Response time is fast (100ms analysis frames, 3-frame Gaussian window) to minimize audible level transitions.

This flag is intended for overmodulated camera audio where the source recording level was set too high. It does not remove analog distortion that was introduced at record time — it only prevents peaks in the derivative from exceeding the specified ceiling.

`--clip-ceiling` is compatible with all `--audio-mode` and `--audio-pan-center` combinations.

---

## Override Flags

### `--force-scan`

Overrides field order detection. Choices: `progressive`, `tff`, `bff`.

**How detection works without the override:**

1. The stream-level `field_order` reported by ffprobe is used when present.
2. If it's missing or `unknown`, `vdg` reads the `interlaced_frame`/`top_field_first` flags from 5 frames at each of 10%, 30%, 50%, 70% and 90% of the duration, decides by majority, and logs the result: `Source: 720x480 @ 29.97fps (interlaced, BFF — from frame metadata sampled across the file, no stream field order)`. Sampling across the file matters. On a real dvrescue capture, the first 30+ seconds (blank or damaged tape at the head) decoded as progressive while the rest of the tape was BFF. This catches DV sources (raw `.dv` and dvrescue DV-in-MKV), where the field order exists only at the codec level. Before v1.5 those were silently treated as progressive, which left combing in the H.264 output.
3. If neither source has field order metadata, the file is treated as progressive and a warning suggests `--force-scan`.

The scan type and its source are also written to the per-file process log (`Scan:` line). If MediaInfo's `Scan type` (Interlaced vs Progressive) contradicts vdg's decision, a yellow `scan type disagreement` warning suggests checking the output or rerunning with `--force-scan`. The check is skipped when `--force-scan` is passed.

| Value | Effect |
|-------|--------|
| `progressive` | Skip deinterlacing; treat source as progressive |
| `tff` | Force top-field-first deinterlacing |
| `bff` | Force bottom-field-first deinterlacing |

Standard NTSC DV is bottom-field-first. The first-frame fallback normally detects this on its own. Use `--force-scan bff` if a DV source's frames carry no field flags either, or as a safeguard.

### `--force-fps`

Overrides ffprobe's frame rate detection. Accepts a float value.

```bash
--force-fps 29.97
--force-fps 25
```

This flag is applied before video standard detection, so it correctly propagates to GOP calculation, output frame rate, progress bar frame count, and NTSC/PAL classification. DV sources are now corrected automatically (see [DV frame rate correction](#dv-frame-rate-correction)), so `--force-fps` is mainly for non-DV sources whose container misreports the rate, or as a safeguard.

Both `--force-fps` and `--force-scan` can be used together.

### Other override flags

- **`--force-anamorphic`** — see [Scaling and Resolution Handling](#scaling-and-resolution-handling).
- **`--clean-aperture`** — see [Clean Aperture Handling](#clean-aperture-handling).
- **`--keep-failed`** — see [Quarantine Workflow](#quarantine-workflow) and [Validation and Logging](#validation-and-logging).

---

## Thumbnails

JPEG 2000 thumbnails (`.jp2`) are generated alongside every H.264 output. They are not generated for v210-, ProRes-, or FFV1-only runs.

- **Default:** 4 thumbnails at positions 10%, 40%, 60%, and 90% of the file's duration
- **Override:** `--thumbs N` generates N thumbnails at randomly selected positions between 5% and 95% of duration

Thumbnails are scaled to the same output resolution as the H.264 derivative (including `--force-anamorphic`, if passed). Interlaced sources are deinterlaced before thumbnail extraction using the same field order as the H.264 encode.

**Technical specifications:** 8-bit RGB, JPEG 2000 compression, encoded via `libopenjpeg` (chosen over the native `jpeg2000` encoder, which maps to 9-bit sYCC regardless of input format).

**Naming:** `<base>_thumb_1.jp2`, `<base>_thumb_2.jp2`, etc.

### Thumbnails only

Pass `--thumbs N` with **no** format flag to regenerate thumbnails without encoding a video derivative — e.g. when none of the original thumbnails are usable:

```bash
vdg --source-dir /Volumes/disk/1/source/finished_sources --output-dir /Volumes/disk/1/output --thumbs 30
```

- N thumbnails are taken at random positions between 5% and 95% of duration. Scaling, `--force-anamorphic`, deinterlacing and `--force-scan` field order are applied exactly as for thumbnails made alongside `-h264`.
- Thumbnails use the same names as the H.264 run, so `_thumb_1` … `_thumb_N` **overwrite** any existing thumbnails with those numbers. Existing higher-numbered thumbnails (e.g. `_thumb_5` onward when N is 4) are left alone.
- Source files are **left in place**. They're never moved to `finished_sources`, and no `finished_sources` folder is created inside the source directory, so you can point `--source-dir` straight at an existing `finished_sources` folder.
- Nothing is recorded in `transcode_summary.csv`, and files already marked complete there are **not** skipped.
- The run is **appended** to the file's existing process log under a `THUMBNAIL-ONLY RUN` header, so the original transcode's commands and validation results are preserved.
- `--thumbs` must be at least 1 in this mode.

If H.264 encoding fails, all thumbnails for that file are deleted as part of cleanup.

---

## Validation and Logging

### H.264 validation

After pass 2 completes, the output MP4 is decoded end-to-end using `ffmpeg -f null`. Any decode errors cause the file to be flagged as failed. Can be skipped with `--skip-validation`.

### v210 lossless validation

After v210 encoding, a framemd5 comparison is performed:

1. Frame hashes are generated for the source video stream (using the same `--clean-aperture` handling as the encode, so a crop-handling divergence doesn't masquerade as a framemd5 mismatch)
2. Frame hashes are generated for the output video stream
3. Hashes are compared frame by frame
4. Audio stream hashes (streamhash, MD5) are generated and compared for source and output

If any mismatch is detected, the output is deleted (or retained with `--keep-failed`), the source is quarantined, and the job is marked as an error. A digest summary (frame count, video/audio pass status) is always written to the per-file process log, regardless of `--keep-framemd5`.

### FFV1 lossless validation

Identical framemd5 (video) + streamhash (audio) methodology to v210, described above.

### MediaConch policy checks

If `mediaconch` is installed and in `PATH`, `vdg` runs an embedded policy check against `-v210` and `-ffv1` output after lossless validation passes:

| Format | Policy file | Scope | Checks |
|--------|-------------|-------|--------|
| FFV1 | `policy_ffv1.xml` | All video standards | Matroska container, FFV1 codec, GOP N=1 (intra), per-slice and container-level CRC error detection, audio is PCM or FLAC |
| v210 | `policy_v210_ntsc.xml` | NTSC | MPEG-4/QuickTime container, v210 codec, 720×486 @ 29.970fps, 4:2:2, 10-bit, interlaced BFF, BT.601 NTSC primaries, BT.709 transfer, BT.601 matrix, PCM 24-bit 48kHz audio |
| v210 | `policy_v210_pal.xml` | PAL | MPEG-4/QuickTime container, v210 codec, 720×576 @ 25.000fps, 4:2:2, 10-bit, interlaced TFF, BT.601 PAL primaries, BT.709 transfer, BT.470 System B/G matrix, PCM 24-bit 48kHz audio |

For sources with no audio track, the audio rules are omitted. With `--clean-aperture`, the width and height rules are omitted.

If `mediaconch` is not found, the check is skipped with a startup warning and treated as passing — it does not block otherwise-successful output. A policy failure after a passing framemd5 result still fails the job and quarantines the source. The policy XML is written to `process_logs/` for the duration of the check and deleted afterward unless `--keep-mediaconch` is passed.

### Command logging

Every ffmpeg and MediaConch command run for a file — encode passes, framemd5/streamhash hashing, policy checks — is logged verbatim to that file's process log, for troubleshooting.

### MediaInfo summary

For each file, right after the `Source:` line, `vdg` prints a condensed MediaInfo summary to the terminal and to both the session log and the per-file process log:

```
INFO: MediaInfo — ab123cd4567_pm.mkv
Container            : Matroska
File size            : 4.70 MiB
Format               : FFV1
Codec ID             : V_MS/VFW/FOURCC / FFV1
Duration             : 6 s 6 ms
Bit rate             : 5 375 kb/s
Width                : 720 pixels
Height               : 486 pixels
Display aspect ratio : 3:2
Frame rate mode      : Constant
Frame rate           : 29.970 (30000/1001) FPS
Standard             : NTSC
Color space          : YUV
Chroma subsampling   : 4:2:0
Bit depth            : 8 bits
Scan type            : Interlaced
Scan order           : Bottom Field First
Audio                : PCM, 1 channel, 44.1 kHz, 24 bits
```

Values are MediaInfo's own, taken from the first video track, with one line per audio track. Fields the source doesn't report are omitted. MediaInfo's scan type and scan order are independent of ffprobe's field-order detection, which makes the summary a quick cross-check for interlacing problems. If `mediainfo` fails on a particular file, a warning is logged and processing continues.

### Log files

**Per-file process log** (`process_logs/<stem>_process.log`): Records every ffmpeg/MediaConch command run for the file, audio configuration, interlacing status, and v210/FFV1 validation results if applicable.

**Session log** (`process_logs/transcode_YYYYMMDD_HHMMSS.log`): Records all INFO and above events for the session.

**CSV summary** (`transcode_summary.csv`): Appended on every run. Columns: Timestamp, Source File, Status, Audio, Details. `Status` may be `Success`, `Error`, `Skipped`, or `Quarantined`. Files already present in the CSV with a `Success` status are skipped on subsequent runs unless their output derivatives are missing.

### Skip logic

A file is skipped (not re-processed) if both conditions are true:
- The source filename appears in `transcode_summary.csv` with `Success` status from a previous run
- All expected output files (every requested derivative, plus thumbnails if `-h264` was requested) are present on disk

If output files are missing despite a CSV entry, the file will be re-processed.

---

## Usage Examples

### Basic tape digitization — H.264 only

```bash
vdg --source-dir /Volumes/disk/1/source --output-dir /Volumes/disk/1/output -h264
```

### FFV1 preservation master + H.264 access copy

```bash
vdg --source-dir /Volumes/disk/1/source --output-dir /Volumes/disk/1/output -ffv1 -h264
```

### H.264 plus v210 for NTSC tape sources

```bash
vdg --source-dir /Volumes/disk/1/source --output-dir /Volumes/disk/1/output -h264 -v210
```

### All four output formats

```bash
vdg --source-dir /Volumes/disk/1/source --output-dir /Volumes/disk/1/output -h264 -v210 -prores -ffv1
```

### DV source from dvrescue — override FPS and scan type

```bash
vdg --source-dir /Volumes/disk/1/source --output-dir /Volumes/disk/1/output \
    --force-fps 29.97 --force-scan bff -h264
```

### Overmodulated DV audio — apply peak ceiling

```bash
vdg --source-dir /Volumes/disk/1/source --output-dir /Volumes/disk/1/output \
    --force-scan bff --clip-ceiling -10 -h264
```

### DV source with two-channel mono audio — merge to stereo

```bash
vdg --source-dir /Volumes/disk/1/source --output-dir /Volumes/disk/1/output \
    --audio-mode mono-merge --force-scan bff -h264
```

### DV source with single mono channel — duplicate right channel to L+R

```bash
vdg --source-dir /Volumes/disk/1/source --output-dir /Volumes/disk/1/output \
    --audio-mode mono-duplicate --audio-channel 1 --force-scan bff -h264
```

### Select a specific audio stream

```bash
vdg --source-dir /Volumes/disk/1/source --output-dir /Volumes/disk/1/output \
    --audio-stream 0:a:1 -h264
```

### Anamorphic SD tape mistagged as 4:3

```bash
vdg --source-dir /Volumes/disk/1/source --output-dir /Volumes/disk/1/output \
    --force-anamorphic -h264
```

### Honor clean aperture crop instead of preserving the full coded frame

```bash
vdg --source-dir /Volumes/disk/1/source --output-dir /Volumes/disk/1/output \
    --clean-aperture -ffv1
```

### Retain failed lossless output for inspection

```bash
vdg --source-dir /Volumes/disk/1/source --output-dir /Volumes/disk/1/output \
    --keep-failed --keep-framemd5 -ffv1
```

### Regenerate thumbnails only (no video derivative)

```bash
vdg --source-dir /Volumes/disk/1/source/finished_sources --output-dir /Volumes/disk/1/output \
    --thumbs 30
```

### Custom thumbnail count

```bash
vdg --source-dir /Volumes/disk/1/source --output-dir /Volumes/disk/1/output \
    --thumbs 6 -h264
```

### Dry run — preview without encoding

```bash
vdg --source-dir /Volumes/disk/1/source --output-dir /Volumes/disk/1/output \
    --dry-run -h264
```

### Keep source files in place after processing

```bash
vdg --source-dir /Volumes/disk/1/source --output-dir /Volumes/disk/1/output \
    --no-move-finished -h264
```

### Parallel processing (use with care — monitor disk I/O)

```bash
vdg --source-dir /Volumes/disk/1/source --output-dir /Volumes/disk/1/output \
    --workers 2 -h264
```

---

## Troubleshooting

### "Cannot create v210 output: video is not NTSC or PAL standard"

v210 output is restricted to SD sources that resolve to NTSC (720×480/486, 29.97 fps) or PAL (720×576, 25 fps). HD sources, non-standard frame rates, or non-standard dimensions will not qualify. If the source is a valid SD tape output but the error still occurs, check whether ffprobe is misreading the frame rate — use `--force-fps` to correct it. If you need a lossless derivative from an HD or non-standard source, use `-ffv1` instead — it has no video-standard restriction.

### "Source directory does not exist" / "Output directory does not exist, and neither does its parent folder"

`vdg` checks both directories before creating anything, and exits with a plain error message and exit code 2 rather than a traceback. The message names the path that's missing and either the drive that isn't mounted (for `/Volumes/...` paths) or the deepest part of the path that does exist. Check the `--source-dir`/`--output-dir` values, including default paths set in a shell alias (`~/.zshrc`/`~/.bashrc`) or in the installed `cli.py`.

A missing output folder is created automatically only when its parent folder exists, and a warning is logged when that happens. If the parent is missing too, `vdg` stops instead. That guards against a typo or an unmounted external drive silently creating a new directory tree somewhere unexpected, such as under `/Volumes` on the boot disk.

### "At least one output format must be specified"

One of `-h264`, `-v210`, `-prores`, or `-ffv1` must be included in every invocation, unless `--thumbs N` is passed on its own to [generate thumbnails only](#thumbnails-only).

### A source file ended up in QUARANTINE

Check the per-file process log in `process_logs/` for the reason. The three causes are: variable frame rate detected before encoding, a lossless validation failure (framemd5, streamhash, or MediaConch) after encoding, or another exception during a `-v210`/`-ffv1` run. See [Quarantine Workflow](#quarantine-workflow). Quarantined files are not retried automatically — after resolving the cause, move the file back into the source directory and re-run.

### DV source processes but frame rate / GOP is wrong

dvrescue-packaged DV-in-MKV and raw `.dv` files can report incorrect frame rates in container metadata. `vdg` now corrects DV sources automatically from the DV frame header and logs a `DV frame rate misreported by container` warning when it does (see [DV frame rate correction](#dv-frame-rate-correction)). If the rate is still wrong, or the source isn't DV, pass `--force-fps 29.97` (or `25`). Very large GOP values or oddly fast/slow progress bars are the usual sign.

### Audio sounds wrong — only one channel has content

If the source has two discrete mono audio streams (common in DV), the default `stereo` audio mode will pass stream `0:a:0` as the left channel and may result in silence on the right. Use `--audio-mode mono-merge` to combine both streams, or `--audio-mode mono-duplicate` (with `--audio-channel` to pick which side) to mirror a single channel to both sides.

### H.264 file exists but job re-processes anyway

The skip logic requires both a `Success` entry in the CSV *and* all output files present on disk. If any thumbnail (or any other requested derivative) is missing, the file will be re-processed from scratch. Check that all expected files are present in the output directory.

### v210 or FFV1 lossless validation fails

**v210 only — reserved code values:** FFmpeg's v210 encoder clips 10-bit samples to 4–1019, because 0–3 and 1020–1023 are reserved for sync in SDI. A source containing those values can't round-trip through v210, so framemd5 fails even though the transcode is otherwise correct. SDI captures (e.g. vrecord) can't contain them; software-decoded sources such as Domesday Duplicator/ld-decode output can.


Check the process log for the specific frame numbers where the mismatch occurred, and for the MediaConch output if the framemd5 comparison passed but validation still failed. Common causes: source file corruption, interrupted encoding run, disk I/O errors during encoding, or (for MediaConch) a genuinely non-conformant source. Re-run the job; if validation fails consistently on the same file, use `--keep-failed` to retain the output for manual inspection rather than having it deleted each time.

### Lossless validation fails on files that used to pass, or ffprobe errors on every file

Check the FFmpeg version first: `which ffmpeg && ffmpeg -version | head -1`. If it reports 8.x or 9.x, a `brew upgrade` or a third-party PPA has replaced the pinned build. Reinstall per [Supported FFmpeg versions](#supported-ffmpeg-versions) — on macOS, confirm `/opt/homebrew/opt/ffmpeg@7/bin` is at the front of `PATH` in a new terminal.

### "mediaconch not found in PATH — policy conformance checks will be skipped"

MediaConch is optional; without it, `vdg` still runs framemd5/streamhash validation for `-v210`/`-ffv1` but skips the policy conformance step. See [INSTALL_MACOS.md](INSTALL_MACOS.md) or [INSTALL_UBUNTU.md](INSTALL_UBUNTU.md) for install instructions if you want policy checks enabled.

### MediaConch policy check fails on a v210 NTSC source with "Video frame rate is 29.970"

The source is probably true 30.000 fps. `vdg` treats 720×480/486 sources within 0.1 fps of 29.97 as NTSC, so 30.0 is accepted, because ffprobe can misreport genuine 29.97 material as 30.0. The v210 encode keeps the source frame rate, so a true 30p file fails the policy's 29.970 fps rule and is quarantined. This is intended: 30p SD doesn't come off tape. It usually means a born-digital or acquired file that needs a closer look before it becomes a v210 master. Check the file's `Frame rate` in the MediaInfo summary.

### MediaConch policy check fails on a v210 PAL source

PAL v210 output is checked against `policy_v210_pal.xml` (added in v1.5.0). Look at which rules failed in the process log. `Scan order is TFF` failing means the source is bottom field first, most likely PAL DV, which shouldn't go to v210. A frame rate, size or standard failure usually means the source isn't really 625-line/25 fps; check the MediaInfo summary. Audio rules fail on 44.1 kHz or 16-bit audio, since vdg converts to 24-bit but keeps the source sample rate.

### Clean aperture crop warning appears, but I don't want it to change anything

The warning is informational. By default `vdg` already preserves the full coded frame (`--clean-aperture` is off), so the warning simply confirms the source *has* clap metadata that `vdg` is deliberately ignoring. No action is needed unless you specifically want display-cropped output, in which case pass `--clean-aperture`. See [Clean Aperture Handling](#clean-aperture-handling).

### Two files' outputs collided (one overwrote the other)

This should no longer happen automatically — `vdg` disambiguates output filenames for source files that share a base ID with different role codes (see [Filename disambiguation](#filename-disambiguation)). If you still see a collision, it likely involves a naming pattern not covered by the current grouping logic (e.g. identical stem, role code, *and* extension — true duplicates); verify the actual output directory listing rather than the CSV summary to confirm which file was overwritten, and rename one of the sources before re-running.

### FFV1 output's VENDOR_ID looks wrong (or isn't set)

`VENDOR_ID` is only overridden to `Apple QuickTime` when the source is detected as a genuine QuickTime file (container `major_brand` tag `qt`). For non-QuickTime sources (MXF, MPEG, plain MP4, etc.), the tag is intentionally left untouched — setting it to "Apple QuickTime" would misrepresent the source's actual origin.

### Output from a DVD-sourced MPEG-2 file is 352×480 instead of 640×480

The source is likely half-D1 NTSC (352×480), a reduced horizontal resolution format used by direct-to-disc DVD recorders. These files use non-square pixels (SAR 20:11) with a 4:3 DAR, which should display at 640×480. The script handles this case explicitly and will scale to 640×480 to match standard NTSC derivative output (H.264 only — `-prores`/`-ffv1` preserve native dimensions regardless). If the output is coming out at the wrong resolution, confirm the DAR reported by ffprobe is `4:3` — if the container metadata is incorrect, the passthrough branch may have been triggered instead.

### Source has no audio track

Sources with no audio track are supported for every format. `-ffv1`, `-v210` and `-prores` produce video-only output, and lossless validation compares video frames only, logging `Audio result: N/A — no audio stream in source`. Before v1.5, these formats failed on any source without audio.

### H.264 audio drops out partway through (video keeps playing)

This was issue #10, fixed in v1.5 by encoding video and muxing audio in separate ffmpeg processes (see [H.264 MP4](#h264-mp4--h264)). If it happens on v1.5 or later, send the process log: it contains the pass-2 and audio-mux commands verbatim. Also check whether the source itself has the gap, using MediaInfo or by listening to the source around the same timestamp.

### Output file has no audio

Confirm the source has an audio stream (`ffprobe <file>`). If the stream exists but uses a non-default stream index, use `--audio-stream 0:a:1` (or the appropriate index). If the source genuinely has no audio, `[No Audio]` will be logged and an audio-free output is expected behavior.

### Low disk space warning at startup

The script checks for a minimum of 10 GB free in the output directory before processing. This is a warning, not a hard stop, but running with insufficient space will cause encoding failures mid-job. Uncompressed v210 output in particular requires significant disk space — a 30-minute NTSC file produces approximately 50–60 GB. FFV1 is smaller than v210 (it's compressed, just losslessly) but still substantially larger than H.264 or ProRes.

---

## Version History

### v1.5.0 — September 2026
- **H.264 audio dropout on long files fixed** ([#10](https://github.com/Stanford-Media-Preservation-Lab/vdg/issues/10)): video is encoded and audio muxed in separate ffmpeg processes. Verified on a 1:47:12 Premiere ProRes HQ export.
- **FFmpeg pinned** ([#8](https://github.com/Stanford-Media-Preservation-Lab/vdg/issues/8)): `ffmpeg@7` on macOS, stock 6.1.x on Ubuntu, with a startup warning for untested versions and for ffmpeg/ffprobe mismatches. Startup now lists every dependency's version and path.
- **MediaInfo summary per file** ([#5](https://github.com/Stanford-Media-Preservation-Lab/vdg/issues/5)). `mediainfo` is now a required dependency.
- **Thumbnails-only mode** ([#3](https://github.com/Stanford-Media-Preservation-Lab/vdg/issues/3)): `--thumbs N` with no format flag.
- **Clear errors for missing source/output directories** ([#4](https://github.com/Stanford-Media-Preservation-Lab/vdg/issues/4)), naming unmounted drives. A missing output folder is created only if its parent exists.
- **DV field order** ([#9](https://github.com/Stanford-Media-Preservation-Lab/vdg/issues/9)): when the stream reports none, vdg falls back to frame metadata sampled across the file, so DV is no longer treated as progressive. A warning fires if MediaInfo's scan type disagrees.
- **DV frame rate** is read from the DV frame header, correcting container-reported rates such as 60000 fps on raw `.dv`. Raw DV sent to `-ffv1` is no longer falsely quarantined as VFR.
- **Sources with no audio track** now work with `-ffv1`, `-v210` and `-prores`. MediaConch audio rules are omitted for them.
- **`--clean-aperture` with `-v210`** skips only the MediaConch width/height rules, with a warning showing the cropped size, instead of always being quarantined.
- **Interlaced ProRes:** `-prores` output from interlaced sources is now encoded as interlaced (TFF/BFF) instead of progressive frames in an interlaced-labelled container.
- **PAL v210:** a MediaConch policy for PAL (720×576, 25 fps, TFF), and PAL v210 output is now tagged with PAL primaries/matrix (`bt470bg`) instead of NTSC's.
- **RF captures:** documented lab rule — no v210 for Domesday Duplicator/ld-decode captures; use `-prores` ([#6](https://github.com/Stanford-Media-Preservation-Lab/vdg/issues/6), [#7](https://github.com/Stanford-Media-Preservation-Lab/vdg/issues/7)).
- Terminal output: log lines no longer collide with progress bars, the summary prints once, quarantined runs no longer report "SUCCESSFULLY", and there's no trailing whitespace when copying output.

### v1.4.2 — July 2026
- FFV1 `VENDOR_ID` override is now gated on genuinely detected QuickTime sources (`major_brand` = `qt`) rather than being applied unconditionally.
- Clean-aperture/dimension warnings are now logged once per file instead of twice (initial file-list scan and actual processing no longer both emit it).

### v1.4.1 — July 2026
- Added `--keep-failed` and the clean-aperture dimension/crop warning.
- Fixed `--force-anamorphic` being silently undone by ffmpeg's default SAR recalculation (`setsar=1` added after every `scale=`).
- Fixed silent output-overwrite when two source files with different role codes shared a filename extension (automatic filename disambiguation).
- Fixed a VFR false-negative (added the per-packet duration check) and an AppleDouble sidecar (`._foo.mov`) false-positive during file discovery.

### v1.4.0 — July 2026
- Added `-ffv1` output validation/quarantine parity with v210, the `QUARANTINE` workflow, clean aperture crop handling (`--clean-aperture`, `-apply_cropping 0` default), `--force-anamorphic`, and automatic filename disambiguation.
- Switched to `coded_width`/`coded_height` by default instead of display dimensions.
- Every ffmpeg/MediaConch command is now logged verbatim to the process log.

### v1.3.0
- Added MediaConch policy conformance checks (FFV1 and v210-NTSC policies), `--keep-framemd5`, and a framemd5 digest summary in the process log.

### v1.2.0
- Added the `-ffv1` output format, `--version` flag, `--audio-channel` flag, and a terminal color fix for Ubuntu.

### v1.1 — May 2026
- Initial manual version: `-h264`, `-v210`, `-prores` output, audio configuration, override flags, thumbnails, H.264/v210 validation.

---

*Stanford Media Preservation Lab — Video Derivative Generator v1.5.0 — September 2026*
