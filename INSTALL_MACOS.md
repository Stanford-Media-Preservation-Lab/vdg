# Installation — macOS (Apple Silicon)

Tested on macOS 13+ with M1/M2/M3/M4 processors.

---

## 1. Install Homebrew

If not already installed:

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

---

## 2. Install FFmpeg 7 (pinned, for vdg only)

> **vdg uses FFmpeg 7 only.** FFmpeg 9.x breaks `vdg` on macOS: both ffprobe JSON parsing and FFV1/v210 lossless round-trips (framemd5 validation failures) have been observed. Homebrew's versioned `ffmpeg@7` formula stays on the 7.1.x series, and its bottle (Sequoia and Tahoe, Apple Silicon) includes everything `vdg` needs: `libx264`, `libopenjpeg` (JPEG 2000 thumbnails) and AudioToolbox (`aac_at`).

**Leave Homebrew's regular `ffmpeg` installed.** vrecord and other AMIA Open Source tools depend on it (`brew uninstall ffmpeg` refuses while they're installed), and they should keep using the current Homebrew FFmpeg they were built against. So `ffmpeg@7` is **not** put on the global `PATH`. vdg gets it through its shell alias instead, and every other command in Terminal is unaffected.

### Install and pin

```bash
brew install ffmpeg@7
brew pin ffmpeg@7
```

`ffmpeg@7` is *keg-only*: Homebrew installs it in `/opt/homebrew/opt/ffmpeg@7/bin` without linking it into `/opt/homebrew/bin`. Leave it that way.

### Point vdg at it: the alias

Add one `vdg` alias to `~/.zshrc` (`nano ~/.zshrc`) that sets the PATH for vdg's own run only, plus the machine's default folders:

```bash
alias vdg='PATH="/opt/homebrew/opt/ffmpeg@7/bin:$PATH" command vdg --source-dir /Volumes/<drive>/source --output-dir /Volumes/<drive>/output'
```

Keep it on a line of its own. If a file doesn't end with a line break, text appended with `echo >>` gets glued onto the previous line. Remove any earlier global `export PATH="/opt/homebrew/opt/ffmpeg@7/bin:$PATH"` line, then check:

```bash
sed -i '' '/^export PATH="\/opt\/homebrew\/opt\/ffmpeg@7/d' ~/.zshrc
grep -n "ffmpeg@7" ~/.zshrc
```

`grep` should show only the alias line. Open a new Terminal window.

Running `\vdg`, `command vdg` or vdg's full path bypasses the alias and picks up Homebrew's regular (9.x) FFmpeg. vdg's startup version warning will flag it.

### Verify

In a new Terminal window:

```bash
which ffmpeg
/opt/homebrew/opt/ffmpeg@7/bin/ffmpeg -version | head -1
/opt/homebrew/opt/ffmpeg@7/bin/ffmpeg -hide_banner -encoders | grep -E "libx264|libopenjpeg|aac_at"
```

`which ffmpeg` should still show `/opt/homebrew/bin/ffmpeg`: that's the regular FFmpeg for vrecord, and it's expected. `ffmpeg@7` should report 7.1.x and list all three encoders. Then start any `vdg` job: its startup dependency list should show `ffmpeg 7.1.x /opt/homebrew/opt/ffmpeg@7/bin/ffmpeg` with no FFmpeg warning.

### About pinning

`brew pin ffmpeg@7` stops `brew upgrade` from touching it at all (Homebrew has no `brew ignore` — `pin` is the equivalent). The `@7` formula name already prevents a jump to 8.x or 9.x; the pin additionally holds the exact 7.1.x build you validated. Pinned formulae show up in `brew outdated` but are skipped by `brew upgrade`.

To take a 7.1.x point release deliberately — e.g. after testing it on one workstation:

```bash
brew unpin ffmpeg@7
brew upgrade ffmpeg@7
brew pin ffmpeg@7
```

If `brew upgrade` later updates a library `ffmpeg@7` links against (e.g. `x264`, `openjpeg`) and `ffmpeg` fails to launch with a `dyld: Library not loaded` error, run `brew reinstall ffmpeg@7` to relink it.

> **vrecord / AMIA tools:** they keep using Homebrew's regular `ffmpeg` (and vrecord its `ffmpegdecklink` build for capture). Nothing in this section changes what they run.

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
