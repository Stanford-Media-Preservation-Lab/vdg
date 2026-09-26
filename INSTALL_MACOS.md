# Installation — macOS (Apple Silicon)

Tested on macOS 13+ with M1/M2/M3/M4 processors.

---

## 1. Install Homebrew

If not already installed:

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

---

## 2. Install FFmpeg 7 (pinned)

> **Use FFmpeg 7 only.** FFmpeg 9.x breaks `vdg` on macOS — ffprobe JSON parsing and FFV1/v210 lossless round-trips (framemd5 validation failures) have both been observed. Do **not** use the unversioned `ffmpeg` formula or the `homebrew-ffmpeg/ffmpeg` tap: both track the latest FFmpeg release and will move you to 9.x on the next `brew upgrade`.

Homebrew's versioned `ffmpeg@7` formula stays on the 7.1.x series and its bottle already includes everything `vdg` needs — `libx264`, `libopenjpeg` (JPEG 2000 thumbnails), and AudioToolbox (`aac_at`). No tap or source build is required.

### New machine

```bash
brew install ffmpeg@7
brew pin ffmpeg@7
```

`ffmpeg@7` is *keg-only* — Homebrew does not put it on your `PATH` automatically. Add it to the front of `PATH` in `~/.zshrc`:

```bash
echo 'export PATH="/opt/homebrew/opt/ffmpeg@7/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc
```

### Machine that already has a newer FFmpeg

Check what is installed and whether anything else depends on it:

```bash
ffmpeg -version | head -1
brew list --versions ffmpeg ffmpeg@7
brew uses --installed ffmpeg
```

If `brew uses` prints nothing, remove the unversioned formula (use the fully qualified name if it came from the tap):

```bash
brew uninstall ffmpeg                           # Homebrew core
brew uninstall homebrew-ffmpeg/ffmpeg/ffmpeg    # or, if installed from the tap
```

If another formula depends on `ffmpeg`, leave it installed — the `PATH` line above puts `ffmpeg@7` ahead of it, which is all `vdg` needs.

Then install, pin and add to `PATH` exactly as for a new machine.

### Verify

Open a **new** terminal and confirm the right binaries are first on `PATH`:

```bash
which ffmpeg ffprobe             # both should be /opt/homebrew/opt/ffmpeg@7/bin/...
ffmpeg -version | head -1        # should report 7.1.x
ffmpeg -hide_banner -encoders | grep -E "libx264|libopenjpeg|aac_at"
```

All three encoders should be listed.

### About pinning

`brew pin ffmpeg@7` stops `brew upgrade` from touching it at all (Homebrew has no `brew ignore` — `pin` is the equivalent). The `@7` formula name already prevents a jump to 8.x or 9.x; the pin additionally holds the exact 7.1.x build you validated. Pinned formulae show up in `brew outdated` but are skipped by `brew upgrade`.

To take a 7.1.x point release deliberately — e.g. after testing it on one workstation:

```bash
brew unpin ffmpeg@7
brew upgrade ffmpeg@7
brew pin ffmpeg@7
```

If `brew upgrade` later updates a library `ffmpeg@7` links against (e.g. `x264`, `openjpeg`) and `ffmpeg` fails to launch with a `dyld: Library not loaded` error, run `brew reinstall ffmpeg@7` to relink it.

> **vrecord / dvrescue:** vrecord uses its own keg-only `ffmpegdecklink` build and is unaffected by any of this.

---

## 3. Install MediaInfo (required)

`vdg` runs the MediaInfo command-line tool on every source to print a technical metadata summary. It checks for `mediainfo` at startup and exits if it's missing.

```bash
brew install media-info
```

Verify with `mediainfo --Version`.

---

## 4. Install Python 3.10+

macOS ships with Python 3, but it is recommended to use a Homebrew-managed version:

```bash
brew install python@3.12
```

---

## 5. Clone the repository

```bash
git clone https://github.com/michaelangeletti/vdg.git
cd vdg
```

---

## 6. Install the package

Install in editable mode so that changes to the source are reflected immediately:

```bash
pip3 install -e .
```

This installs the `vdg` command into your Python environment's `bin` directory. If it is not in your `PATH`, add it:

```bash
export PATH="$(python3 -m site --user-base)/bin:$PATH"
```

Add the above line to your `~/.zshrc` to make it permanent.

---

## 7. Verify installation

```bash
vdg --help
vdg --version
```

---

## 8. Install MediaConch (optional)

MediaConch enables policy conformance checks on `-v210` and `-ffv1` output (Matroska/FFV1 structure, v210 NTSC technical profile). If it's not installed, `vdg` logs a warning at startup and skips the check — it does not fail the run.

```bash
brew install mediaconch
```

Verify:

```bash
mediaconch --version
```

---

## Optional: run directly from bin/

The `bin/vdg` script can also be run directly without installation, as long as the repository root is in your `PYTHONPATH`:

```bash
export PYTHONPATH=/path/to/vdg
python3 bin/vdg --help
```

---

## Running tests

```bash
pip3 install pytest
pytest
```
