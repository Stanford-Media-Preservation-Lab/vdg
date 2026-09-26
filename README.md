# vdg

**Video Derivative Generator** — Stanford Media Preservation Lab

`vdg` is a batch video transcoding tool for SMPL digitization and acquisition workflows. It processes video files from a source directory, generates one or more derivative formats (H.264 MP4, v210 uncompressed, ProRes 422 HQ, FFV1/MKV), produces JPEG 2000 thumbnails alongside H.264 output, validates lossless output against the source, and maintains a per-file process log and cumulative CSV summary. Sources that show signs of variable frame rate, or that fail lossless validation, are quarantined for review instead of being silently processed or left to overwrite a prior attempt.

---

## Supported output formats

| Flag | Format | Notes |
|------|--------|-------|
| `-h264` | H.264 MP4 + JP2 thumbnails | All sources |
| `-v210` | v210 uncompressed 10-bit 4:2:2 QuickTime | SD (NTSC/PAL) sources only; framemd5 + MediaConch validated |
| `-prores` | ProRes 422 HQ QuickTime | All sources, native resolution (no scaling) |
| `-ffv1` | FFV1 v3 lossless MKV | All sources; framemd5 + MediaConch validated |

Multiple format flags may be combined in a single run (e.g. `-ffv1 -h264` for a preservation master plus an access copy). At least one is required.

---

## Quick start

```bash
# H.264 derivatives from a tape digitization batch
vdg --source-dir /Volumes/disk/1/source --output-dir /Volumes/disk/1/output -h264

# FFV1 preservation master + H.264 access copy in one pass
vdg --source-dir /Volumes/disk/1/source --output-dir /Volumes/disk/1/output -ffv1 -h264

# DV content from dvrescue — override FPS and scan type
vdg --source-dir /path/to/source --output-dir /path/to/output \
    --force-fps 29.97 --force-scan bff -h264

# Overmodulated camera audio — apply peak ceiling
vdg --source-dir /path/to/source --output-dir /path/to/output \
    --clip-ceiling -10 --force-scan bff -h264
```

See [MANUAL.md](MANUAL.md) for the full command reference and worked examples, including clean aperture crop handling, the quarantine workflow, and filename disambiguation.

---

## Installation

See [INSTALL_MACOS.md](INSTALL_MACOS.md) or [INSTALL_UBUNTU.md](INSTALL_UBUNTU.md).

## Documentation

See [MANUAL.md](MANUAL.md) for the full command reference, flag descriptions, scaling tables, naming conventions, clean aperture handling, the quarantine workflow, and troubleshooting guide.

---

## Dependencies

- Python 3.10+
- `ffmpeg` and `ffprobe` in `PATH` (must be compiled with `libx264`, `libopenjpeg`, `prores_ks`, and FFV1 support)
  - **macOS:** Homebrew `ffmpeg@7`, pinned. **Ubuntu 24.04:** stock `ffmpeg` 6.1.x, held. FFmpeg 9.x is **not supported** — it breaks ffprobe parsing and lossless FFV1/v210 validation. See the install guides.
- `mediainfo` (MediaInfo CLI) in `PATH` — per-file technical metadata summary
- `tqdm`
- `mediaconch` in `PATH` *(optional)* — enables policy conformance checks on `-v210`/`-ffv1` output. If absent, `vdg` logs a warning at startup and skips the check without failing the run.

---

## Running tests

```bash
pip install pytest
pytest
```

---

*v1.5.0*
