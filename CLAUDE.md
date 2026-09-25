# VDG — Claude Code Context

Context for continuing development on this project. For usage/install docs see [README.md](README.md), [MANUAL.md](MANUAL.md), [INSTALL_MACOS.md](INSTALL_MACOS.md), [INSTALL_UBUNTU.md](INSTALL_UBUNTU.md).

## What this is

VDG (Video Derivative Generator, package `smpl-vdg`) is a batch video transcoding tool for the Stanford Media Preservation Lab's (SMPL) digitization and acquisition workflows. Michael is the primary developer/maintainer.

Two intake pipelines feed it:
1. **Digitized analog tape** (Betacam SP, Digital Betacam, VHS, U-matic, Hi8, DV) captured via `vrecord`/`dvrescue`, producing FFV1/MKV preservation masters.
2. **Born-digital HD deliverables** from vendors, often v210 QuickTime.

Three derivative types, each with a strict three-character suffix convention:

| Flag | Format | Suffix | Notes |
|---|---|---|---|
| `-h264` | H.264 MP4 + JP2 thumbnails | `_sl` | All sources |
| `-v210` | v210 uncompressed 10-bit 4:2:2 QuickTime | `_pm` | SD sources only |
| `-prores` | ProRes 422 HQ QuickTime | `_sh` | SD sources only |

**Lab infrastructure:** seven Apple Silicon Macs, two-plus Ubuntu 24.04 workstations (including a Threadripper), varying FFmpeg builds. Homebrew-managed Python is the preferred macOS baseline.

## Current state

At **v1.4.2**. This round of work (v1.4.0 → v1.4.2) is closed out — no fixes or features currently on the table. Future issues get filed in GitHub Issues and dealt with systematically later, rather than as an open-ended stream. README.md, MANUAL.md, INSTALL_MACOS.md, and INSTALL_UBUNTU.md were fully rewritten for v1.4.2 (2026-07-14) — all four output formats, clean aperture handling, the QUARANTINE workflow, VFR detection, filename disambiguation, MediaConch validation, and every current flag are now documented. Keep these in sync going forward as features land, rather than batching another catch-up rewrite.

## Shipped in v1.4.0 / v1.4.1 / v1.4.2

- `coded_width`/`coded_height` now used by default in `VideoInfo` instead of `width`/`height`.
- `--clean-aperture` flag: default disables automatic clean-aperture cropping via `-apply_cropping 0` (see Key Learnings); passing the flag lets ffmpeg apply its native clap crop instead. Verified against a real clap-tagged v210 source (macOS, FFmpeg 8.1.2) — MediaInfo confirmed 720x486 preserved by default, 704x480 with `--clean-aperture`.
- VFR source detection (`r_frame_rate` vs `avg_frame_rate` divergence) — quarantines the source and skips the transcode instead of producing a corrupt lossless roundtrip.
- Any v210/FFV1 failure (not just VFR) now moves the source into a `QUARANTINE` folder under `output_dir` for review, instead of leaving it mixed in with untried files.
- `--force-anamorphic` flag: forces 854×480 scaling for SD sources mistagged as 4:3 despite being anamorphic squeezed footage. Required a `setsar=1` fix to actually take effect — see Key Learnings.
- Filename disambiguation when the same unique ID has multiple role codes (e.g. `_pm.mov` + `_sh.mp4`) — previously these collided on the same output filename; now the source extension is appended (`_mov_sl.mp4` / `_mp4_sl.mp4`). Extension alone isn't always enough — see Key Learnings.
- Every ffmpeg/MediaConch command (encode, framemd5/streamhash hashing, policy check) is now logged verbatim to the process log for troubleshooting.
- Warn when a source has clean-aperture crop metadata — via `Frame Cropping` side data (the signal that actually fires in practice on FFmpeg 8.1.2), plus a defensive `coded_width`/`width` comparison for other FFmpeg builds. See Key Learnings for why the side-data check matters more than the literal dimension comparison.
- `--keep-failed` flag: retains v210/FFV1 output that fails lossless validation, renamed with a `_VALIDATION_FAILED` suffix, instead of deleting it — useful for inspecting what actually went wrong.
- FFV1 output's `VENDOR_ID` tag is now explicitly set to `Apple QuickTime` — but only when the source is a genuine QuickTime file (`major_brand` = `qt  `, via a new `VideoInfo.is_quicktime` field), instead of silently carrying through the source's own per-stream `vendor_id` tag (observed as `KeyG` — a capture-chain leftover, not meaningful authorship info). `-ffv1` isn't QuickTime-exclusive — MXF/MPEG/other non-QuickTime sources are left untouched, since "Apple QuickTime" would be actively wrong there.
- "AAC encoder: ..." is now only logged when `-h264` is requested — it was misleading on lossless-only (`-v210`/`-ffv1`) runs, where audio is PCM copy, not AAC.
- Dimension/clean-aperture warnings no longer print twice per file — `get_video_info()` takes a `warn` flag; the initial file-list scan logs it once, actual processing suppresses the duplicate.

## In progress — v1.5.0 (uncommitted, started 2026-09-24)

Issues are tracked at github.com/Stanford-Media-Preservation-Lab/vdg/issues. Done so far, all uncommitted in the working tree (Michael is batching them into one v1.5.0 commit):

- **#8 FFmpeg pinning** — docs pin `ffmpeg@7` on macOS (keg-only, `PATH`, `brew pin`) and stock 6.1.x held on Ubuntu; startup `check_ffmpeg_version()` warns above `MAX_TESTED_FFMPEG_MAJOR = 7`, on unparseable versions, and on ffmpeg/ffprobe mismatch.
- **#4 Missing directories** — `ConfigError` + `validate_directories()`: clean red error, exit 2, names the unmounted `/Volumes` drive or the deepest existing folder. A missing output *leaf* is created with a warning; a missing output *parent* stops the run (Michael approved this rule).
- **#3 Thumbnails only** — `--thumbs N` with no format flag. Sources left in place, no nested `finished_sources` created, nothing written to the resume CSV, completed files not skipped, process log appended (never overwritten). Shared `generate_thumbnail_set()` used by both paths.
- **#5 MediaInfo summary** — `mediainfo` is now a **required** dependency (Michael: lab workstation setup already requires it). Condensed MediaInfo text-output block after each `Source:` line, in the terminal, session log and process log.
- **#9 DV field order** — when stream `field_order` is missing/`unknown`, `detect_field_order_from_frames()` majority-votes the first 5 frames' `interlaced_frame`/`top_field_first`. Reproduced on synthetic raw DV and DV-in-MKV (stream `unknown`, frames BFF). `Scan:` line added to the process log.
- **DV frame rate correction** (follow-on to #9, no GitHub issue) — for `dvvideo`, `read_dv_system()` copies the first DV frame and reads the DSF bit (byte 3, bit 7 of the header DIF block: 0 = 525/60, 1 = 625/50) → 29.97/25, or 59.94/50 for DVCPRO HD 720p. It replaces container-reported rates that are off (raw `.dv` gives 60000/1, DV-in-MKV from raw DV gives 30000/1). Michael confirmed the real dvrescue symptom was the container reporting 60000.00 fps, which this covers. The whole-file avg/r VFR check is skipped when corrected; the packet-duration check still runs. Before this, raw DV + `-ffv1` was falsely quarantined as VFR and H.264 ran at `-r 15000`. Verified on synthetic DV25/DVCPRO/DV50/DVCPRO HD in raw DV, MOV and MKV.
- **No-audio sources** (found 2026-09-24, pre-existing, no GitHub issue) — `-ffv1`, `-v210` and `-prores` hardcoded `-map 0:a`, so any source without an audio track failed and was quarantined. Now `-map 0:a?`; validators take `has_audio` and log `Audio result: N/A — no audio stream in source`. Verified on no-audio DV, 8-bit and 10-bit FFV1 across all four formats; with-audio behavior unchanged. **Unverified:** whether MediaConch passes or fails the FFV1/v210 policies' `tracktype="Audio"` rules when there's no audio track (no mediaconch in the test VM). Test on a workstation with a silent source.
- **Test fix** — `test_ntsc_fps_tolerance` had failed since v1.1; the code (30.0 fps within 0.1 → NTSC) is intended. Michael wants true 30p SD to reach v210 and fail the MediaConch 29.970 rule as a flag, since 30p SD never comes off tape. Suite: 81 passing.

Still open:
- **#10 — implemented 2026-09-24, awaiting a real-file test Monday.** Michael: this is the priority. Long VHS transfers get audio treatment in Premiere and are exported as ProRes HQ (audio fine and in sync), then vdg's H.264 audio drops out around 35 min. It's a *new* problem, so either Premiere's ProRes export or FFmpeg changed. Implemented: pass 1 unchanged; pass 2 video-only to `process_logs/<base>_sl_video_tmp.mp4`; a separate mux step (`-map 1:v:0 -c:v copy`, `add_audio_async_filter()` prepends `aresample=async=1:min_hard_comp=0.100000:first_pts=0` to vdg's audio filters, `-max_interleave_delta 0`, `faststart`). The temp file is deleted on success and kept on failure. No-audio sources skip the mux. **Deliberately left out of the issue recipe:** `-ignore_editlist 1 -fflags +genpts` (the issue says they didn't fix it, and ignoring edit lists risks shifting A/V on other sources) and `-shortest` (it would truncate video when a source's audio is shorter). Add them only if the real file still drops out. Sync verified on a synthetic 3-min interlaced ProRes HQ + 24-bit PCM with flash/beep every 20 s, including a 0.5 s audio edit-list offset; all audio modes verified. Diagnostic idea for "why new": the FFmpeg 7.0 CLI became fully multithreaded (separate demux/decode/filter/encode/mux threads). Compare the old single-process command on Ubuntu 6.1 vs macOS 7+ with the same Premiere file.
- **#6/#7** DdD TFF FFV1 → v210 fails framemd5, and `--force-scan tff` doesn't yield TFF v210. Two leads: (1) `process_v210_output()` hardcodes `setfield=bff` for NTSC, whatever the source or `--force-scan` says (explains #7). (2) **Confirmed 2026-09-24: FFmpeg's v210 encoder clips 10-bit codes to 4–1019** (0–3 and 1020–1023 are SDI-reserved). A 0–1023 ramp comes back as 4–1019, so framemd5 fails. vrecord/SDI captures can't contain those codes; ld-decode (DdD) software-decoded output can. Likely cause of #6. Check the real clip's 10-bit min/max. If confirmed, framemd5 can't pass for such sources by design; decide with Michael whether that should quarantine (current behavior), warn, or be pre-checked.
- Before release: re-verify clean-aperture handling on FFmpeg 7.1 (see Key learnings), run one real-workstation batch with the real `mediainfo` CLI, bump the version to 1.5.0 and add a MANUAL version-history entry.

Dev test VM (Cowork, disposable): FFmpeg 7.1.5 static (BtbN) in `~/tools`, plus a `mediainfo` stand-in built on pymediainfo (libmediainfo 24.12); `source ~/tools/env.sh`.

## Key learnings & principles

- **`-ffv1` (and `-v210`) aren't QuickTime-exclusive, even though most of this lab's real sources are.** A hardcoded metadata fix (VENDOR_ID = "Apple QuickTime") would be wrong for non-QuickTime sources like MXF or MPEG that can also be run through the same flags — `VIDEO_EXTENSIONS` accepts a wide range of containers, and this session's own test set included a plain `.mpeg` file. Gate source-format-specific fixes on an actual detected signal (`major_brand` tag == `qt  ` in this case, exposed as `VideoInfo.is_quicktime`), not on which output flag was used.
- **Whole-file average frame rate (`avg_frame_rate` vs `r_frame_rate`) is too coarse to detect real-world VFR.** A production file was found with only a *local* dip to 14.985fps (a handful of held/duplicated frames out of 84,603 total) — the overall average still rounded to the nominal 29.970fps, so the average-based check missed it entirely and the file transcoded and validated as if it were clean. Fixed by also checking actual per-packet durations directly from the container index (`ffprobe -show_entries packet=duration_time`, no decode needed) — catches VFR regardless of how small a fraction of frames is affected. Both checks are kept (OR'd together); the packet-duration check is gated to only run when `-v210`/`-ffv1` is requested, since it's more expensive than the average check.
- **AppleDouble sidecar files (`._filename`) get created automatically by macOS on non-native filesystems** (exFAT/NTFS external drives) and match `VIDEO_EXTENSIONS` by extension alone. `collect_video_files()` now skips any filename starting with `.` during discovery — without this, a `._foo.mov` sidecar gets treated as a real source, fails ffprobe, and gets quarantined instead of (or in addition to) the actual file needing review.
- **Filename disambiguation by extension alone isn't always unique.** A real test case had `bm994hd4640_pm.mov` + `bm994hd4640_sh.mov` + `bm994hd4640_sl.mp4` sharing one ID — two of the three shared the `.mov` extension, so both resolved to the same disambiguated stem and the second silently overwrote the first's H.264 output and thumbnails. `compute_filename_disambiguation()` now tries extension alone first, and falls back to `role_code + extension` for the whole ID group if extension alone isn't unique across it. Always verify disambiguation fixes by listing the actual output directory, not just the run summary — a per-file "Success" doesn't reveal a same-batch overwrite.
- **`scale=WxH` alone doesn't change display aspect ratio — it silently gets reverted.** FFmpeg's `scale` filter, when given only literal width/height with no explicit `sar`, recalculates the output SAR to *preserve the source's original DAR*. So `--force-anamorphic`'s `scale=854:480` produced 854x480 pixels but FFmpeg re-tagged it with a narrow SAR that forced playback right back to the source's (wrong) 4:3 — completely negating the override. This was invisible in the two pre-existing (non-forced) scaling cases only because their target dimensions were already chosen to be DAR-correct as square pixels. Fixed by adding `setsar=1` after every `scale=` in both the H.264 encode filter chain and the thumbnail filter chain — confirmed via `ffprobe -show_entries stream=...,sample_aspect_ratio,display_aspect_ratio` on both the forced and normal paths.
- **On FFmpeg 8.1.2, `coded_width`/`coded_height` never actually differ from `width`/`height`, even on a confirmed clap-tagged file.** `bm994hd4640_pm.mov` has a real `Frame Cropping: 8/8/3/3` side-data entry, but ffprobe reported `width=720`/`coded_width=720` — identical. The clap crop is conveyed *only* through the `Frame Cropping` side-data block (`side_data_type`, `crop_top`/`crop_bottom`/`crop_left`/`crop_right` — field names confirmed against FFmpeg's own `fftools/ffprobe.c` source), not through a coded/display dimension split. The queued "warn when coded and display dimensions differ" item is kept as a defensive check for other FFmpeg builds, but the side-data check is the one that actually fires in this lab's environment.
- **FFmpeg is pinned: `ffmpeg@7` (7.1.x) on macOS, stock 6.1.x on Ubuntu 24.04.** FFmpeg 9.x broke ffprobe JSON parsing and FFV1→v210 lossless round-trips (framemd5 failures) on macOS (GitHub issue #8). macOS: Homebrew `ffmpeg@7` (keg-only, prepended to `PATH`, `brew pin`) — its core bottle already includes `libx264`, `libopenjpeg` and AudioToolbox, so the `homebrew-ffmpeg` tap (which tracks latest, 9.x) is no longer used. Ubuntu: `apt-mark hold ffmpeg`, no third-party PPAs. Documented in INSTALL_MACOS.md, INSTALL_UBUNTU.md, README.md and MANUAL.md (Supported FFmpeg versions).
- **Clean-aperture behavior was verified on FFmpeg 8.1.2, not 7.1.x — re-verify after the move to `ffmpeg@7`.** Run `bm994hd4640_pm.mov` with `-v210` both with and without `--clean-aperture`, check dimensions with MediaInfo, and confirm the `Frame Cropping` side-data warning still fires. If 7.1 doesn't export clap as side data, the warning won't fire and `--clean-aperture` may have no effect.
- **FFmpeg version divergence between macOS and Ubuntu is a recurring source of bugs**, particularly around `clap` atom handling and container behavior. Always consider both platforms when touching video probing/encoding code.
- **framemd5 for video, streamhash for audio.** Audio framemd5 validation fails due to container re-packetization differences between MKV and MOV; streamhash is correct for lossless audio validation.
- **`coded_width`/`coded_height` vs. `width`/`height`.** On FFmpeg 8.1.2 (macOS), ffprobe's `width`/`height` were observed equal to `coded_width`/`coded_height` for a clap-tagged v210 source — the crop instead showed up as stream-level `Side data: Frame Cropping` (crop_top/bottom/left/right). Using coded dimensions is still correct/harmless, but the real crop mechanism at transcode time is separate — see next point.
- **The actual clean-aperture crop is controlled by the generic decoder option `-apply_cropping`** (boolean, default `true`), not `-flags2 +ignorecrop` (that only affects codec-level SPS conformance-window cropping, not container-derived `clap` side data). Confirmed empirically on a real file (`bm994hd4640_pm.mov`, 720x486 with clap crop 8/8/3/3 → 704x480 display): `-flags2 +ignorecrop` had **no effect** (still cropped to 704x480 either way via MediaInfo), while `-apply_cropping 0` is the correct flag to preserve the full coded frame for lossless FFV1/v210 masters. `vdg`'s `--clean-aperture` flag toggles this (default off = `-apply_cropping 0` = full frame preserved).
- **framemd5 validation only proves internal self-consistency, not true losslessness against the original.** If the source-hash command and the encode command don't use identical crop-handling flags, framemd5 can pass while silently comparing two equally-wrong (or two equally-right) results — it can't catch a crop that's applied consistently to both sides. Always verify actual pixel dimensions (MediaInfo/ffprobe on the output) when testing crop-related fixes, not just the framemd5 PASS.
- **VFR sources break frame count assumptions.** Vendor-delivered v210 files may technically be variable frame rate, causing false validation failures on round-trip transcoding.
- **Standard Homebrew FFmpeg bottles omit `libopenjpeg`.** Machines needing correct JP2 output need the `homebrew-ffmpeg/ffmpeg` tap built `--with-openjpeg`.
- **Preserve all existing logic when adding features.** Changes should be surgical — only modify what's necessary.
- **Source and output directories must always be separate.** Resume logic incorrectly picks up derivative files as sources when directories are shared; this is a workflow convention, not a code-level filter.
- **Audio mapping must always be appended before conditional audio filter args.** Past bug: `audio_mapping` was omitted when `audio_filter_args` was present, causing silent audio failures.
- **`mono-merge` mode** uses `amerge` + `pan` to center-pan both mono streams equally onto left and right.

## Development approach

- Iterative, incremental — preserve existing behavior with each change.
- Features are driven by real production problems (specific tape formats, vendor file quirks, platform FFmpeg differences), not speculative design.
- Verbose terminal output is preferred: processing status, interlacing detection, and validation results should appear both in the terminal and in process logs.
- Log files include a consistent header banner identifying the lab and script version.
- Resume capability via CSV tracking is a core workflow feature.
- Per-machine default source/output paths are hardcoded by editing the installed `cli.py` directly (located via `grep -n "mangelet"`); on pipx installs, shell aliases in `.zshrc` are used instead.

## Tools & ecosystem

- **FFmpeg / ffprobe** — core transcoding/inspection engine; needs `libx264`, `libopenjpeg`, `prores_ks`. Version varies by machine.
- **vrecord** — analog tape capture on macOS workstations.
- **dvrescue** — DV tape capture and packaging.
- **MediaConch** — policy-based validation for FFV1/MKV and v210; CLI flag syntax is `--Policy=` (capital P, equals sign) for cross-platform compatibility.
- **MediaInfo (MediaArea)** — technical metadata inspection, used for manual QC alongside VDG output.
- **QCTools** — video QC analysis (signal metrics, filter scopes) for reviewing derivatives.
- **VLC** — manual playback review of derivatives.
- **Homebrew** — preferred macOS package manager; Homebrew-managed Python is the clean baseline.
