# Installation — Ubuntu 24.04 LTS

---

## 1. Update package index

```bash
sudo apt update && sudo apt upgrade -y
```

---

## 2. Install FFmpeg (Ubuntu stock 6.1.x, held)

> **Use Ubuntu 24.04's own FFmpeg package (6.1.x) and hold it.** It is the tested build on Ubuntu workstations. Do **not** add third-party FFmpeg PPAs (e.g. `ppa:savoury1/...`) — they replace the system FFmpeg with newer major releases, and FFmpeg 9.x is known to break `vdg`'s lossless FFV1/v210 validation.

```bash
sudo apt install -y ffmpeg
sudo apt-mark hold ffmpeg
```

`apt-mark hold` stops `apt upgrade` from changing the `ffmpeg` package. Ubuntu 24.04 never moves FFmpeg to a new major version on its own, so the hold is a guard against a PPA or an accidental release upgrade rather than routine updates. To take an Ubuntu security update deliberately:

```bash
sudo apt-mark unhold ffmpeg
sudo apt update && sudo apt install --only-upgrade ffmpeg
sudo apt-mark hold ffmpeg
```

Check held packages at any time with `apt-mark showhold`.

Verify the version and that `libx264` and `libopenjpeg` are present in the build:

```bash
ffmpeg -version | head -1        # should report 6.1.x
ffmpeg -hide_banner -encoders | grep -E "libx264|libopenjpeg"
```

> **Note:** `aac_at` (AudioToolbox) is macOS-only. On Ubuntu, `vdg` will automatically fall back to `libfdk_aac` if available, or the built-in `aac` encoder. `libfdk_aac` requires a custom FFmpeg build (`--enable-libfdk-aac`); the built-in `aac` encoder in the stock package is the supported default.

---

## 3. Install MediaInfo (required)

`vdg` runs the MediaInfo command-line tool on every source to print a technical metadata summary. It checks for `mediainfo` at startup and exits if it's missing.

```bash
sudo apt install -y mediainfo
```

Verify with `mediainfo --Version`. For a newer release than Ubuntu's package, install it from the MediaArea repository set up in the MediaConch step below.

---

## 4. Install Python 3.10+

Ubuntu 24.04 ships with Python 3.12. Verify:

```bash
python3 --version
```

Install pip if not present:

```bash
sudo apt install -y python3-pip
```

---

## 5. Clone the repository

```bash
git clone https://github.com/Stanford-Media-Preservation-Lab/vdg.git
cd vdg
```

---

## 6. Install the package

```bash
pip3 install -e .
```

If your user `bin` directory is not in `PATH`:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

Add to `~/.bashrc` to make permanent.

---

## 7. Verify installation

```bash
vdg --help
vdg --version
```

---

## 8. Install MediaConch (optional)

MediaConch enables policy conformance checks on `-v210` and `-ffv1` output (Matroska/FFV1 structure, v210 NTSC technical profile). If it's not installed, `vdg` logs a warning at startup and skips the check — it does not fail the run. Ubuntu's default repos don't carry it; install it from the MediaArea repository:

```bash
wget https://mediaarea.net/repo/deb/repo-mediaarea_1.0-27_all.deb
sudo dpkg -i repo-mediaarea_1.0-27_all.deb
sudo apt update
sudo apt install -y mediaconch
```

> If the `.deb` filename above 404s, MediaArea has published a newer repo package — check [mediaarea.net/en/Repos](https://mediaarea.net/en/Repos) for the current version number and substitute it above.

Verify:

```bash
mediaconch --version
```

---

## Running tests

```bash
pip3 install pytest
pytest
```
