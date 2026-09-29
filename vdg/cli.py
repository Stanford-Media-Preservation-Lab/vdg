"""
vdg.cli — core logic for the Video Derivative Generator.

Stanford Media Preservation Lab
Video Derivative Generator - v1.5.0
September 2026
"""

import os
import sys
import subprocess
import json
import math
import csv
import shutil
import shlex
import re
import random
import argparse
import platform
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Set
from dataclasses import dataclass
from enum import Enum
import tqdm as tqdm_module
from tqdm import tqdm
from concurrent.futures import ProcessPoolExecutor, as_completed

SCRIPT_TITLE = "Stanford Media Preservation Lab"
SCRIPT_NAME = "Video Derivative Generator, v1.7.0, September 2026"
SCRIPT_SEPARATOR = "----"

# Highest FFmpeg major version vdg has been validated against (lossless
# FFV1/v210 round-trips, ffprobe JSON parsing). macOS runs Homebrew ffmpeg@7,
# Ubuntu 24.04 runs stock 6.1.x. FFmpeg 9.x is known to break both ffprobe
# parsing and FFV1/v210 framemd5 validation on macOS (GitHub issue #8).
# Raise this only after a newer release has been tested on both platforms.
MAX_TESTED_FFMPEG_MAJOR = 7

def _supports_color() -> bool:
    """Return True if the terminal supports ANSI color codes.
    Checks isatty(), the TERM variable, and the NO_COLOR convention."""
    if os.environ.get('NO_COLOR'):
        return False
    if not sys.stdout.isatty():
        return False
    term = os.environ.get('TERM', '')
    colorterm = os.environ.get('COLORTERM', '')
    if colorterm in ('truecolor', '24bit', 'yes'):
        return True
    return any(t in term for t in ('xterm', 'color', 'ansi', 'vt100', 'linux', 'screen', 'tmux'))

_COLOR = _supports_color()

class Colors:
    GREEN  = '\033[92m' if _COLOR else ''
    YELLOW = '\033[93m' if _COLOR else ''
    RED    = '\033[91m' if _COLOR else ''
    BLUE   = '\033[94m' if _COLOR else ''
    CYAN   = '\033[96m' if _COLOR else ''
    BOLD   = '\033[1m'  if _COLOR else ''
    RESET  = '\033[0m'  if _COLOR else ''

ERASE_LINE = '\x1b[K'

def console_write(text: str, file=None) -> None:
    """Print a line (or block) above any active tqdm progress bars.
    tqdm clears a bar by overwriting it with spaces; text printed over that
    leaves those spaces behind as trailing whitespace, which shows up when
    terminal output is copied. Prefixing each line with ERASE_LINE (only on a
    real terminal) blanks the row first, so copied text comes out clean."""
    file = file or sys.stdout
    try:
        is_tty = file.isatty()
    except Exception:
        is_tty = False
    if is_tty:
        text = "\n".join(ERASE_LINE + line for line in text.split("\n"))
    tqdm.write(text, file=file)

class ProcessStatus(Enum):
    SUCCESS = "Success"
    ERROR = "Error"
    SKIPPED = "Skipped"
    INCOMPLETE = "Incomplete"
    QUARANTINED = "Quarantined"

class VideoStandard(Enum):
    NTSC = "ntsc"
    PAL = "pal"
    UNKNOWN = "unknown"

VIDEO_EXTENSIONS = (
    '.mp4', '.mov', '.mkv', '.mxf', '.avi', '.mpg', '.mpeg', '.m2v', '.ts',
    '.vob', '.wmv', '.asf', '.flv', '.f4v', '.rm', '.rmvb', '.dv', '.dif',
    '.webm', '.ogg', '.ogv', '.3gp', '.3g2', '.m4v'
)

BITRATE_CONFIG = {
    'sd': {'bitrate': '1000k', 'maxrate': '1200k', 'bufsize': '2000k'},
    'hd': {'bitrate': '2800k', 'maxrate': '2900k', 'bufsize': '5800k'},
    # Desktop review derivatives (--desktop-review): higher bitrate than the
    # _sl access copy above, since these are meant to look good on a projector
    # or a meeting-room display, not just stream cleanly.
    'desktop_sd': {'bitrate': '9000k', 'maxrate': '9500k', 'bufsize': '18000k'},
    'desktop_hd': {'bitrate': '18000k', 'maxrate': '19000k', 'bufsize': '36000k'}
}

THUMBNAIL_POSITIONS = [0.10, 0.40, 0.60, 0.90]
GOP_MULTIPLIER = 2
VALIDATION_TIMEOUT = 300

# ---------------------------------------------------------------------------
# Embedded MediaConch policy definitions
# ---------------------------------------------------------------------------
# FFV1 policy — used as-is from the public MediaConch policy library.
# Checks: Matroska container, FFV1 video, GOP N=1 (intra), per-slice CRC,
# container-level CRC, and audio as PCM or FLAC.
POLICY_FFV1 = """\
<?xml version="1.0"?>
<policy type="or" name="Video file is MKV + FFV1-Intra + PCM or FLAC with CRC32 everywhere" license="CC-BY-SA-4.0+">
  <description>Container format is Matroska with error detection (CRC). Video format is FFV1
with error detection (CRC) and Intra mode (each frame is independent).
Audio format is PCM or FLAC.</description>
  <policy type="and" name="MKV, FFV1 Intra, PCM/FLAC, error detection">
    <rule name="Container is MKV" value="Format" tracktype="General" occurrence="*" operator="=">Matroska</rule>
    <rule name="Video is FFV1" value="Format" tracktype="Video" occurrence="*" operator="=">FFV1</rule>
    <rule name="GOP size of 1" value="Format_Settings_GOP" tracktype="Video" occurrence="*" operator="=">N=1</rule>
    <rule name="Container uses error detection" value="extra/ErrorDetectionType" tracktype="General" occurrence="*" operator="=">Per level 1</rule>
    <rule name="Video uses error detection" value="extra/ErrorDetectionType" tracktype="Video" occurrence="*" operator="=">Per slice</rule>
    <policy type="or" name="Audio is PCM or FLAC">
      <rule name="Audio is PCM" value="Format" tracktype="Audio" occurrence="*" operator="=">PCM</rule>
      <rule name="Audio is FLAC" value="Format" tracktype="Audio" occurrence="*" operator="=">FLAC</rule>
    </policy>
  </policy>
</policy>
"""

# v210 NTSC policy — derived from the vrecord 10-bit MOV Master public policy.
# Audio channel count and track count are intentionally unconstrained to accommodate
# variable configurations (mono, stereo, multi-track). All audio tracks must be
# PCM 24-bit 48kHz little-endian signed regardless of count.
# NTSC; see POLICY_V210_PAL for the PAL equivalent.
POLICY_V210_NTSC = """\
<?xml version="1.0"?>
<policy type="and" name="VDG v210 MOV Master (NTSC SD) — Apple TN2162 conformant">
  <description>10-bit Uncompressed v210 QuickTime MOV, NTSC SD (720x486, 29.970fps, BFF).
Audio must be PCM 24-bit 48kHz. Channel count and track count are not constrained.
Asserts conformance with Apple TN2162 ("Uncompressed Y'CbCr Video in QuickTime
Files") for the colr atom (colour_primaries/transfer_characteristics/matrix_coefficients
rules below), the fiel atom (ScanType/ScanOrder rules below), and the pasp atom
(PixelAspectRatio rule below). The clap (clean aperture) atom is NOT asserted here:
vdg's default (--clean-aperture not passed) preserves the full coded frame with no
display/coded size mismatch, so there is nothing for a clap atom to declare —
clap conformance only applies when --clean-aperture is used, and isn't currently
checked by this policy (see MANUAL.md).</description>
  <rule name="Container is MPEG-4" value="Format" tracktype="General" occurrence="*" operator="=">MPEG-4</rule>
  <rule name="Format profile is QuickTime" value="Format_Profile" tracktype="General" occurrence="*" operator="=">QuickTime</rule>
  <rule name="File extension is mov" value="FileExtension" tracktype="General" occurrence="*" operator="=">mov</rule>
  <rule name="Video codec is v210" value="CodecID" tracktype="Video" occurrence="*" operator="=">v210</rule>
  <rule name="Video width is 720" value="Width" tracktype="Video" occurrence="*" operator="=">720</rule>
  <rule name="Video height is 486" value="Height" tracktype="Video" occurrence="*" operator="=">486</rule>
  <rule name="Video frame rate is 29.970" value="FrameRate" tracktype="Video" occurrence="*" operator="=">29.970</rule>
  <rule name="Video standard is NTSC" value="Standard" tracktype="Video" occurrence="*" operator="=">NTSC</rule>
  <rule name="Chroma subsampling is 4:2:2" value="ChromaSubsampling" tracktype="Video" occurrence="*" operator="=">4:2:2</rule>
  <rule name="Bit depth is 10" value="BitDepth" tracktype="Video" occurrence="*" operator="=">10</rule>
  <rule name="Scan type is Interlaced" value="ScanType" tracktype="Video" occurrence="*" operator="=">Interlaced</rule>
  <rule name="Scan order is BFF" value="ScanOrder" tracktype="Video" occurrence="*" operator="=">BFF</rule>
  <rule name="Color primaries is BT.601 NTSC" value="colour_primaries" tracktype="Video" occurrence="*" operator="=">BT.601 NTSC</rule>
  <rule name="Transfer characteristics is BT.709" value="transfer_characteristics" tracktype="Video" occurrence="*" operator="=">BT.709</rule>
  <rule name="Matrix coefficients is BT.601" value="matrix_coefficients" tracktype="Video" occurrence="*" operator="=">BT.601</rule>
  <!-- TN2162 pasp atom (pixel aspect ratio). vdg encodes setsar=10/11 (vrecord's
       own recommended NTSC 4:3 default, per vrecord's Config > Aspect Ratio
       tab). MediaConch has no three-state result, so this passes on ANY of
       vrecord's four user-configurable NTSC 4:3 PAR values (720x486 D-1) —
       vdg's own output warns separately (Python-level, not MediaConch) when
       the actual value isn't vrecord's default. 2026-09-28: corrected from an
       earlier single-value 0.900 rule, which reflected a since-fixed
       setdar=4/3 bug that silently overrode setsar on every v210 output
       (see process_v210_output) rather than any legitimate PAR target. -->
  <policy type="or" name="Pixel aspect ratio is a vrecord-acceptable NTSC 4:3 value">
    <rule name="PAR is 0.909 (10/11, vrecord default)" value="PixelAspectRatio" tracktype="Video" occurrence="*" operator="=">0.909</rule>
    <rule name="PAR is 0.889 (8/9)" value="PixelAspectRatio" tracktype="Video" occurrence="*" operator="=">0.889</rule>
    <rule name="PAR is 0.900 (9/10)" value="PixelAspectRatio" tracktype="Video" occurrence="*" operator="=">0.900</rule>
    <rule name="PAR is 0.912 (4320/4739)" value="PixelAspectRatio" tracktype="Video" occurrence="*" operator="=">0.912</rule>
  </policy>
  <rule name="Audio format is PCM" value="Format" tracktype="Audio" occurrence="*" operator="=">PCM</rule>
  <rule name="Audio is 24-bit" value="BitDepth" tracktype="Audio" occurrence="*" operator="=">24</rule>
  <rule name="Audio sample rate is 48kHz" value="SamplingRate" tracktype="Audio" occurrence="*" operator="=">48000</rule>
  <rule name="Audio is little-endian" value="Format_Settings_Endianness" tracktype="Audio" occurrence="*" operator="=">Little</rule>
  <rule name="Audio is signed" value="Format_Settings_Sign" tracktype="Audio" occurrence="*" operator="=">Signed</rule>
</policy>
"""

@dataclass
class VideoInfo:
    width: int
    height: int
    duration: float
    fps: float
    dar: str
    has_audio: bool
    total_frames: int
    codec: str
    interlaced: bool
    is_vfr: bool
    is_quicktime: bool
    # Where the scan type came from: 'stream' (ffprobe stream field_order),
    # 'frames' (first-frame fallback, GitHub issue #9) or 'none' (no metadata —
    # assumed progressive).
    field_order_source: str = 'stream'
    field_order_detail: str = ''
    # Set when a DV source's container-reported frame rate was replaced by the
    # rate from the DV frame header (e.g. "container reported 60000.00 fps").
    fps_note: str = ''

@dataclass
class ProcessingResult:
    source_file: str
    status: ProcessStatus
    audio_status: str
    message: str
    timestamp: str

@dataclass
class ProcessingStats:
    total: int = 0
    success: int = 0
    error: int = 0
    skipped: int = 0
    quarantined: int = 0
    failed_files: List[str] = None
    quarantined_files: List[str] = None

    def __post_init__(self):
        if self.failed_files is None:
            self.failed_files = []
        if self.quarantined_files is None:
            self.quarantined_files = []

class ConfigError(Exception):
    """A user-facing setup problem (bad directory, missing format flag) —
    reported as a plain error message, not a traceback."""

def describe_missing_path(path: Path) -> str:
    """Explain why a directory path doesn't exist: an unmounted volume, or the
    deepest part of the path that does exist."""
    parts = path.parts
    if len(parts) >= 3 and parts[0] == '/' and parts[1] == 'Volumes':
        volume = Path('/Volumes') / parts[2]
        if not volume.exists():
            return f"The drive '{parts[2]}' is not mounted ({volume} does not exist)."
    existing = path
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    return f"Deepest existing folder on that path: {existing}"

def validate_directories(source_dir: Path, output_dir: Path) -> List[str]:
    """Check source/output directories before anything is created. Raises
    ConfigError with a descriptive message; returns notices to log once
    logging is set up. A missing output folder is created only when its
    parent exists — otherwise a typo or an unmounted drive would silently
    create a whole new directory tree (e.g. under /Volumes on the boot disk)."""
    hint = ("Check --source-dir/--output-dir, and any default paths set in your "
            "shell alias (~/.zshrc or ~/.bashrc) or in cli.py.")
    notices = []
    if not source_dir.exists():
        raise ConfigError(f"Source directory does not exist: {source_dir}\n"
                          f"  {describe_missing_path(source_dir)}\n  {hint}")
    if not source_dir.is_dir():
        raise ConfigError(f"Source path is not a directory: {source_dir}\n  {hint}")
    if output_dir.exists():
        if not output_dir.is_dir():
            raise ConfigError(f"Output path is not a directory: {output_dir}\n  {hint}")
    elif not output_dir.parent.exists():
        raise ConfigError(f"Output directory does not exist, and neither does its parent folder: {output_dir}\n"
                          f"  {describe_missing_path(output_dir)}\n  {hint}")
    else:
        notices.append(f"Output directory did not exist — creating it: {output_dir}")
    return notices

class Config:
    def __init__(self, args: argparse.Namespace):
        self.source_dir = Path(args.source_dir)
        self.output_dir = Path(args.output_dir)
        self.finished_dir = self.source_dir / "finished_sources"
        self.quarantine_dir = self.output_dir / "QUARANTINE"
        self.log_dir = self.output_dir / "process_logs"
        self.csv_log = self.output_dir / "transcode_summary.csv"
        self.cleanup_only = args.cleanup_only
        self.move_finished = args.move_finished
        self.dry_run = args.dry_run
        self.workers = args.workers
        self.skip_validation = args.skip_validation
        self.output_h264 = args.h264
        self.output_v210 = args.v210
        self.output_prores = args.prores
        self.output_ffv1 = args.ffv1
        self.output_desktop_review = args.desktop_review
        self.audio_stream = args.audio_stream
        self.audio_mode = args.audio_mode
        self.audio_pan_center = args.audio_pan_center
        self.force_scan = args.force_scan
        self.force_fps = args.force_fps
        self.thumb_count = args.thumbs
        self.clip_ceiling = args.clip_ceiling
        self.audio_channel = args.audio_channel
        self.keep_framemd5 = args.keep_framemd5
        self.keep_mediaconch = args.keep_mediaconch
        self.clean_aperture = args.clean_aperture
        self.force_anamorphic = args.force_anamorphic
        self.keep_failed = args.keep_failed
        self.aac_encoder = detect_aac_encoder()

        # Thumbnail-only mode: --thumbs N with no video format flag regenerates
        # JP2 thumbnails without encoding a derivative. Sources are left in place
        # (never moved to finished_sources) and nothing is recorded in the resume
        # CSV, since the source has normally already been processed.
        any_format = (self.output_h264 or self.output_v210 or self.output_prores or self.output_ffv1
                      or self.output_desktop_review)

        # Trim mode: --trim-in/--trim-out with no format flag and no --thumbs is
        # a third standalone mode, alongside thumbs_only — a lossless stream-copy
        # trim of a single over-run capture (see run_trim()). Like thumbs_only,
        # the source is left in place and nothing is recorded in the resume CSV.
        self.trim_in_str = args.trim_in
        self.trim_out_str = args.trim_out
        self.trim_in_seconds: Optional[float] = None
        self.trim_out_seconds: Optional[float] = None
        trim_requested = (self.trim_in_str is not None) or (self.trim_out_str is not None)
        if trim_requested:
            if self.trim_in_str is None or self.trim_out_str is None:
                raise ConfigError("--trim-in and --trim-out must be given together")
            if any_format or self.thumb_count is not None:
                raise ConfigError("--trim-in/--trim-out is a standalone mode and cannot be combined with an "
                                  "output format flag (-h264/-v210/-prores/-ffv1/-desktop-review) or --thumbs")
            try:
                self.trim_in_seconds = parse_trim_timestamp(self.trim_in_str)
            except ValueError as e:
                raise ConfigError(f"Invalid --trim-in value: {e}")
            try:
                self.trim_out_seconds = parse_trim_timestamp(self.trim_out_str)
            except ValueError as e:
                raise ConfigError(f"Invalid --trim-out value: {e}")
            if self.trim_out_seconds <= self.trim_in_seconds:
                raise ConfigError(f"--trim-out ({self.trim_out_str}) must come after --trim-in ({self.trim_in_str})")
        self.trim_mode = trim_requested

        self.thumbs_only = not any_format and not trim_requested and self.thumb_count is not None
        if not any_format and not self.thumbs_only and not trim_requested:
            raise ConfigError("At least one output format must be specified (-h264, -v210, -prores, -ffv1, "
                              "or -desktop-review), --thumbs N on its own to generate thumbnails only, or "
                              "--trim-in/--trim-out on its own to trim one file")
        if self.thumbs_only:
            if self.thumb_count < 1:
                raise ConfigError("--thumbs must be at least 1 when generating thumbnails only")
            self.move_finished = False
        if self.trim_mode:
            self.move_finished = False
        self.setup_notices = validate_directories(self.source_dir, self.output_dir)
        directories = [self.output_dir, self.log_dir, self.quarantine_dir]
        if not self.thumbs_only and not self.trim_mode:
            # Thumbnail-only and trim runs are often pointed at an existing
            # finished_sources folder — don't create a nested finished_sources
            # inside it, and neither mode moves sources there anyway.
            directories.append(self.finished_dir)
        for directory in directories:
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                raise ConfigError(f"Cannot create directory {directory}: {e.strerror}")
def setup_logging(log_dir: Path, dry_run: bool) -> logging.Logger:
    logger = logging.getLogger('video_transcoder')
    logger.setLevel(logging.DEBUG)
    
    class ColoredFormatter(logging.Formatter):
        FORMATS = {
            logging.DEBUG: Colors.CYAN + '%(levelname)s: %(message)s' + Colors.RESET,
            logging.INFO: Colors.GREEN + '%(levelname)s: %(message)s' + Colors.RESET,
            logging.WARNING: Colors.YELLOW + '%(levelname)s: %(message)s' + Colors.RESET,
            logging.ERROR: Colors.RED + '%(levelname)s: %(message)s' + Colors.RESET,
            logging.CRITICAL: Colors.RED + Colors.BOLD + '%(levelname)s: %(message)s' + Colors.RESET,
        }
        def format(self, record):
            log_fmt = self.FORMATS.get(record.levelno)
            formatter = logging.Formatter(log_fmt)
            return formatter.format(record)
    
    class TqdmConsoleHandler(logging.StreamHandler):
        """Writes console log messages via tqdm.write, which clears any active
        progress bar, prints the message on its own line, then redraws the bar.
        A plain StreamHandler appended messages to the bar's line — the first
        character landed at the end of the bar and the rest wrapped."""
        def emit(self, record):
            try:
                console_write(self.format(record), file=self.stream)
                self.flush()
            except Exception:
                self.handleError(record)

    console_handler = TqdmConsoleHandler(sys.stdout)
    # Records logged with extra={'file_only': True} go to the session log only —
    # used where the same content is already printed to the terminal in color
    # (e.g. the processing summary), so it isn't shown twice.
    console_handler.addFilter(lambda record: not getattr(record, 'file_only', False))
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(ColoredFormatter())
    logger.addHandler(console_handler)
    
    if not dry_run:
        log_file = log_dir / f"transcode_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(logging.DEBUG)
        file_format = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        file_handler.setFormatter(file_format)
        logger.addHandler(file_handler)
        with open(log_file, 'a') as f:
            f.write(SCRIPT_TITLE + "\n" + SCRIPT_NAME + "\n" + SCRIPT_SEPARATOR + "\n" + SCRIPT_SEPARATOR + "\n\n")
    return logger

def detect_aac_encoder() -> str:
    for encoder in ['aac_at', 'libfdk_aac', 'aac']:
        try:
            result = subprocess.run(
                ['ffmpeg', '-hide_banner', '-encoders'],
                capture_output=True, text=True, timeout=10
            )
            if f' {encoder} ' in result.stdout:
                return encoder
        except Exception:
            continue
    return 'aac'

def parse_ffmpeg_version(version_output: str) -> Tuple[Optional[str], Optional[int]]:
    """Extract (version string, major version) from `ffmpeg -version` / `ffprobe -version` output.

    Handles release builds ("ffmpeg version 7.1.5", "ffprobe version 6.1.1-3ubuntu5",
    "ffmpeg version n7.1"). Git snapshot builds ("ffmpeg version N-118000-g...")
    have no release number, so the major version is returned as None.
    """
    first_line = version_output.strip().splitlines()[0] if version_output.strip() else ""
    match = re.match(r'^\S+ version (\S+)', first_line)
    if not match:
        return None, None
    version = match.group(1)
    major_match = re.match(r'^n?(\d+)\.\d+', version)
    return version, int(major_match.group(1)) if major_match else None

def get_tool_version(tool: str) -> Tuple[Optional[str], Optional[int]]:
    try:
        result = subprocess.run([tool, '-version'], capture_output=True, text=True, timeout=10)
        return parse_ffmpeg_version(result.stdout)
    except Exception:
        return None, None

def check_ffmpeg_version() -> None:
    """Log the FFmpeg/ffprobe versions in use and warn (without exiting) when
    they're newer than the tested major version, unparseable, or mismatched."""
    logger = logging.getLogger('video_transcoder')
    ffmpeg_version, ffmpeg_major = get_tool_version('ffmpeg')
    ffprobe_version, ffprobe_major = get_tool_version('ffprobe')
    if ffprobe_version != ffmpeg_version:
        logger.warning(f"ffmpeg ({ffmpeg_version or 'unknown'}) and ffprobe ({ffprobe_version or 'unknown'}) "
                       f"versions differ — check PATH so both come from the same FFmpeg install")
    checks = [('FFmpeg', ffmpeg_version, ffmpeg_major)]
    if ffprobe_version != ffmpeg_version:
        checks = [('ffmpeg', ffmpeg_version, ffmpeg_major), ('ffprobe', ffprobe_version, ffprobe_major)]
    for tool, version, major in checks:
        if major is None:
            logger.warning(f"Could not determine {tool} release version ({version or 'unknown'}) — "
                           f"vdg is tested with FFmpeg {MAX_TESTED_FFMPEG_MAJOR}.x and earlier")
        elif major > MAX_TESTED_FFMPEG_MAJOR:
            logger.warning(f"{tool} {version} is newer than the tested FFmpeg {MAX_TESTED_FFMPEG_MAJOR}.x — "
                           f"ffprobe parsing and lossless FFV1/v210 validation may fail. "
                           f"See 'Supported FFmpeg versions' in MANUAL.md")

def get_cli_version(cmd: List[str], pattern: str) -> Optional[str]:
    """Run a tool's version command and return the last match of `pattern`
    in its output (stdout and stderr), or None."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        matches = re.findall(pattern, result.stdout + result.stderr)
        return matches[-1] if matches else None
    except Exception:
        return None

def log_dependency_versions() -> None:
    """Log every dependency's version and location, in the terminal and the
    session log, so each run records exactly which toolchain produced it."""
    logger = logging.getLogger('video_transcoder')
    rows = []
    for tool in ('ffmpeg', 'ffprobe'):
        version, _ = get_tool_version(tool)
        rows.append((tool, version or 'unknown', shutil.which(tool) or ''))
    rows.append(('mediainfo', get_cli_version(['mediainfo', '--Version'], r'v(\d+(?:\.\d+)+)') or 'unknown',
                 shutil.which('mediainfo') or ''))
    if shutil.which('mediaconch'):
        rows.append(('mediaconch', get_cli_version(['mediaconch', '--version'], r'(\d+\.\d+(?:\.\d+)?)') or 'unknown',
                     shutil.which('mediaconch')))
    else:
        rows.append(('mediaconch', 'not found', 'policy conformance checks will be skipped'))
    rows.append(('Python', platform.python_version(), sys.executable))
    rows.append(('tqdm', getattr(tqdm_module, '__version__', 'unknown'), ''))
    name_w = max(len(r[0]) for r in rows)
    ver_w = max(len(r[1]) for r in rows)
    lines = [f"  {name:<{name_w}}  {version:<{ver_w}}  {where}".rstrip() for name, version, where in rows]
    logger.info("Dependencies:\n" + "\n".join(lines))

# MediaInfo summary (GitHub issue #5): fields shown per file, in MediaInfo's own
# text-output labels and formatting.
MEDIAINFO_VIDEO_FIELDS = ["Format", "Format profile", "Format settings", "Codec ID", "Duration", "Bit rate",
                          "Width", "Height", "Display aspect ratio", "Frame rate mode", "Frame rate",
                          "Standard", "Color space", "Chroma subsampling", "Bit depth", "Scan type", "Scan order"]
MEDIAINFO_TIMEOUT = 120

def parse_mediainfo_text(text: str) -> List[Tuple[str, Dict[str, str]]]:
    """Split MediaInfo's default text output into [(section, {label: value})].
    Section names are as printed ("General", "Video", "Audio #1", ...); the first
    occurrence of a label within a section wins."""
    sections: List[Tuple[str, Dict[str, str]]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        if ' : ' not in line:
            sections.append((line.strip(), {}))
        elif sections:
            label, value = line.split(' : ', 1)
            sections[-1][1].setdefault(label.strip(), value.strip())
    return sections

def format_mediainfo_summary(text: str) -> List[str]:
    """Condense MediaInfo text output into the per-file summary lines: container,
    the first video track's key fields, and one line per audio track."""
    sections = parse_mediainfo_text(text)
    general = next((f for name, f in sections if name == 'General'), {})
    video = next((f for name, f in sections if name == 'Video' or name.startswith('Video #')), {})
    audio = [(name, f) for name, f in sections if name == 'Audio' or name.startswith('Audio #')]
    width = max(len(label) for label in MEDIAINFO_VIDEO_FIELDS + ["Container", "File size", "Audio #10"])
    lines = []
    container = " / ".join(v for v in (general.get("Format"), general.get("Format profile")) if v)
    if container:
        lines.append(f"{'Container':<{width}} : {container}")
    if general.get("File size"):
        lines.append(f"{'File size':<{width}} : {general['File size']}")
    for label in MEDIAINFO_VIDEO_FIELDS:
        value = video.get(label)
        if value is None and label == "Duration":
            value = general.get("Duration")
        if value is None and label == "Bit rate":
            value = general.get("Overall bit rate")
        if value:
            lines.append(f"{label:<{width}} : {value}")
    if not audio:
        lines.append(f"{'Audio':<{width}} : none")
    for name, f in audio:
        parts = [f.get(k) for k in ("Format", "Channel(s)", "Sampling rate", "Bit depth")]
        lines.append(f"{name:<{width}} : {', '.join(p for p in parts if p)}")
    return lines

def mediainfo_scan_type(summary_lines: List[str]) -> Optional[str]:
    """'Interlaced' / 'Progressive' (etc.) from the summary's Scan type line."""
    for line in summary_lines or []:
        label, _, value = line.partition(' : ')
        if label.strip() == 'Scan type':
            return value.strip()
    return None

def scan_type_disagreement(summary_lines: List[str], interlaced: bool) -> Optional[str]:
    """Return MediaInfo's scan type if it clearly contradicts vdg's interlaced/
    progressive decision (only 'Interlaced' vs 'Progressive' — MBAFF/Mixed are
    left alone), else None."""
    mi = mediainfo_scan_type(summary_lines)
    if mi == 'Interlaced' and not interlaced:
        return mi
    if mi == 'Progressive' and interlaced:
        return mi
    return None

# vrecord's user-configurable Config > Aspect Ratio values (per Michael's
# screenshot, 2026-09-28), rounded to 3dp to match MediaInfo's PixelAspectRatio
# formatting. vdg's v210 encode always targets the *_DEFAULT value via
# -vf setsar=... in process_v210_output; MediaConch's POLICY_V210_NTSC/PAL pass
# on any value in the corresponding *_ACCEPTABLE set (nested OR sub-policy) and
# fail on anything else. This dict-based check is a separate, non-MediaConch
# warning for the middle tier: acceptable-but-not-vrecord's-default, which
# MediaConch's binary pass/fail can't distinguish on its own.
V210_PAR_NTSC_DEFAULT = 0.909  # 10/11
V210_PAR_NTSC_ACCEPTABLE = {0.909: "10/11 (default)", 0.889: "8/9", 0.900: "9/10", 0.912: "4320/4739"}
V210_PAR_PAL_DEFAULT = 1.091  # 12/11
V210_PAR_PAL_ACCEPTABLE = {1.091: "12/11 (default)", 1.067: "16/15", 1.094: "128/117"}

def par_default_mismatch(output_path: Path, video_standard: VideoStandard) -> Optional[str]:
    """Read the v210 output's actual PixelAspectRatio via ffprobe and, if it's a
    vrecord-acceptable value other than vrecord's own default for this standard,
    return a description of the mismatch for a Python-level warning (not a
    MediaConch failure — MediaConch already passes any vrecord-acceptable value,
    see POLICY_V210_NTSC/PAL). Returns None if the PAR matches the default, or
    if it couldn't be read (that case is left to MediaConch, which will fail the
    file outright if the PAR is truly outside the acceptable set)."""
    try:
        result = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                                 '-show_entries', 'stream=sample_aspect_ratio',
                                 '-of', 'csv=p=0', str(output_path)],
                                capture_output=True, text=True, timeout=MEDIAINFO_TIMEOUT)
        sar = result.stdout.strip()
        if result.returncode != 0 or not sar or ':' not in sar:
            return None
        num, den = sar.split(':', 1)
        par = float(num) / float(den)
    except Exception:
        return None
    default, acceptable = {
        VideoStandard.NTSC: (V210_PAR_NTSC_DEFAULT, V210_PAR_NTSC_ACCEPTABLE),
        VideoStandard.PAL: (V210_PAR_PAL_DEFAULT, V210_PAR_PAL_ACCEPTABLE),
    }.get(video_standard, (None, None))
    if default is None:
        return None
    match = min(acceptable, key=lambda v: abs(v - par)) if any(abs(v - par) < 0.002 for v in acceptable) else None
    if match is None or abs(match - default) < 0.0005:
        return None
    return f"PAR {match:.3f} ({acceptable[match]}) — vrecord default for this standard is {default:.3f}"

def get_mediainfo_summary(file_path: Path) -> Optional[List[str]]:
    """Run the mediainfo CLI and return summary lines, or None if it fails —
    a missing summary is logged as a warning and never fails the file."""
    try:
        result = subprocess.run(['mediainfo', str(file_path)], capture_output=True, text=True,
                                timeout=MEDIAINFO_TIMEOUT)
        if result.returncode != 0 or not result.stdout.strip():
            return None
        return format_mediainfo_summary(result.stdout)
    except Exception:
        return None

def check_dependencies() -> bool:
    logger = logging.getLogger('video_transcoder')
    missing_required = False
    for tool in ['ffmpeg', 'ffprobe', 'mediainfo']:
        if shutil.which(tool) is None:
            logger.error(f"Required tool '{tool}' not found in PATH")
            missing_required = True
    if missing_required:
        return False
    logger.info("All required dependencies found")
    log_dependency_versions()
    check_ffmpeg_version()
    if shutil.which('mediaconch') is None:
        logger.warning("mediaconch not found in PATH — policy conformance checks will be skipped")
    return True

def check_disk_space(output_dir: Path, required_gb: float = 10.0) -> bool:
    logger = logging.getLogger('video_transcoder')
    stat = shutil.disk_usage(output_dir)
    available_gb = stat.free / (1024 ** 3)
    if available_gb < required_gb:
        logger.warning(f"Low disk space: {available_gb:.2f} GB available (recommended: {required_gb} GB)")
        return False
    logger.info(f"Disk space OK: {available_gb:.2f} GB available")
    return True

def get_completed_files(csv_path: Path) -> Set[str]:
    completed = set()
    if not csv_path.exists():
        return completed
    try:
        with open(csv_path, mode='r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get('Status') == ProcessStatus.SUCCESS.value:
                    completed.add(row.get('Source File'))
    except Exception as e:
        logging.getLogger('video_transcoder').warning(f"Error reading CSV log: {e}")
    return completed

def log_to_csv(csv_path: Path, result: ProcessingResult, dry_run: bool):
    if dry_run:
        return
    file_exists = csv_path.exists()
    try:
        with open(csv_path, mode='a', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(['Timestamp', 'Source File', 'Status', 'Audio', 'Details'])
            writer.writerow([result.timestamp, result.source_file, result.status.value, result.audio_status, result.message])
    except Exception as e:
        logging.getLogger('video_transcoder').error(f"Error writing to CSV: {e}")

def format_file_size(size_bytes: int) -> str:
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} PB"

def format_duration(seconds: float) -> str:
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}" if hours > 0 else f"{minutes:02d}:{secs:02d}"

def parse_trim_timestamp(value: str) -> float:
    """Parse a --trim-in/--trim-out value: HH:MM:SS[.ms], MM:SS[.ms], or plain
    seconds ('125' or '125.5'). Raises ValueError on anything else."""
    value = value.strip()
    if re.fullmatch(r'\d+(\.\d+)?', value):
        return float(value)
    fields = value.split(':')
    if len(fields) not in (2, 3):
        raise ValueError(f"unrecognized timestamp {value!r} (expected HH:MM:SS, MM:SS, or plain seconds)")
    try:
        fields = [float(f) for f in fields]
    except ValueError:
        raise ValueError(f"unrecognized timestamp {value!r} (expected HH:MM:SS, MM:SS, or plain seconds)")
    if len(fields) == 2:
        # MM:SS — minutes is the leading unit here, so it isn't capped at 60
        # (e.g. '76:55' meaning 76 minutes, 55 seconds is fine); only seconds is.
        hours, minutes, seconds = 0.0, fields[0], fields[1]
        if minutes < 0 or not (0 <= seconds < 60):
            raise ValueError(f"timestamp component out of range: {value!r}")
    else:
        # HH:MM:SS — minutes and seconds are both capped at 60 here, since hours
        # is the leading unit.
        hours, minutes, seconds = fields
        if hours < 0 or not (0 <= minutes < 60) or not (0 <= seconds < 60):
            raise ValueError(f"timestamp component out of range: {value!r}")
    return hours * 3600 + minutes * 60 + seconds

def get_file_info_for_display(file_path: Path) -> Tuple[int, float]:
    try:
        size = file_path.stat().st_size
        info = get_video_info(file_path)
        return size, info.duration
    except Exception:
        return 0, 0.0

def _parse_frame_rate(rate_str: str) -> float:
    """Safely parse an ffprobe 'num/den' frame rate string to a float."""
    try:
        num, den = rate_str.split('/')
        den = float(den)
        return float(num) / den if den else 0.0
    except Exception:
        return 0.0

def detect_variable_packet_durations(file_path: Path, timeout: int = 180) -> bool:
    """Inspect actual per-packet durations directly from the container index
    (no decode needed) to catch VFR that a whole-file average frame rate
    misses — e.g. a small number of held/duplicated frames scattered through
    a file whose overall average still rounds to the nominal rate. Fails
    open (returns False) if the probe itself can't run; the coarser
    avg_frame_rate/r_frame_rate check in get_video_info is the fallback
    signal in that case.
    """
    cmd = ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
           '-show_entries', 'packet=duration_time', '-of', 'csv=p=0', str(file_path)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        durations = {round(float(line), 3) for line in result.stdout.splitlines() if line.strip()}
        return len(durations) > 1
    except Exception:
        return False

FIELD_ORDER_PROBE_FRAMES = 5
# Where in the file to sample frames for field order (fractions of duration).
# Sampling only the start failed on a real dvrescue capture whose first ~30 s
# (blank/damaged tape head) decoded as progressive while the rest was BFF.
FIELD_ORDER_PROBE_POSITIONS = (0.1, 0.3, 0.5, 0.7, 0.9)

def field_order_read_intervals(duration: float) -> str:
    """ffprobe -read_intervals spec: FIELD_ORDER_PROBE_FRAMES frames at each of
    FIELD_ORDER_PROBE_POSITIONS, or from the start if the duration is unknown."""
    if duration <= 0:
        return f"%+#{FIELD_ORDER_PROBE_FRAMES}"
    return ",".join(f"{duration * pos:.3f}%+#{FIELD_ORDER_PROBE_FRAMES}" for pos in FIELD_ORDER_PROBE_POSITIONS)

def parse_dv_system(frame_bytes: bytes) -> Optional[str]:
    """Read the DV system from the start of a DV frame. The first DIF block is
    the header section (section type 0 in the top 3 bits of byte 0); bit 7 of
    byte 3 is the DSF flag: 0 = 525/60 (NTSC family), 1 = 625/50 (PAL family).
    Same position for DV25, DVCPRO, DV50 and DVCPRO HD."""
    if len(frame_bytes) < 4 or (frame_bytes[0] >> 5) != 0:
        return None
    return '625/50' if frame_bytes[3] & 0x80 else '525/60'

def read_dv_system(file_path: Path, timeout: int = 30) -> Optional[str]:
    """Copy the first DV frame out of any container (raw .dv, MOV, MKV) and
    read its DSF flag — independent of container timing, which is what's
    unreliable on raw/dvrescue DV."""
    cmd = ['ffmpeg', '-v', 'quiet', '-i', str(file_path), '-map', '0:v:0', '-c', 'copy',
           '-frames:v', '1', '-f', 'data', '-']
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return parse_dv_system(result.stdout)
    except Exception:
        return None

def dv_nominal_fps(dv_system: str, height: int) -> float:
    """Frame rate implied by the DV system. DVCPRO HD 720p is progressive at
    field rate (59.94/50); every other DV variant runs at frame rate (29.97/25)."""
    if dv_system == '525/60':
        return 60000 / 1001 if height == 720 else 30000 / 1001
    return 50.0 if height == 720 else 25.0

def classify_frame_field_order(frames: List[Dict]) -> Optional[str]:
    """Majority vote over per-frame interlaced_frame/top_field_first flags.
    Returns 'tff', 'bff', 'progressive', or None if no frames were read."""
    if not frames:
        return None
    interlaced = [f for f in frames if int(f.get('interlaced_frame', 0)) == 1]
    if len(interlaced) * 2 <= len(frames):
        return 'progressive'
    tff = sum(1 for f in interlaced if int(f.get('top_field_first', 0)) == 1)
    return 'tff' if tff * 2 > len(interlaced) else 'bff'

def detect_field_order_from_frames(file_path: Path, duration: float = 0.0, timeout: int = 60) -> Optional[str]:
    """Fallback scan detection for sources whose stream-level field_order is
    missing or 'unknown' — notably DV (raw .dv and dvrescue DV-in-MKV), where the
    field order lives only in the decoded frames. Samples frames at several
    points across the file and takes the majority."""
    cmd = ['ffprobe', '-v', 'quiet', '-select_streams', 'v:0', '-read_intervals', field_order_read_intervals(duration),
           '-show_entries', 'frame=interlaced_frame,top_field_first', '-print_format', 'json', str(file_path)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return classify_frame_field_order(json.loads(result.stdout).get('frames', []))
    except Exception:
        return None

def get_video_info(file_path: Path, warn: bool = True) -> VideoInfo:
    cmd = ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_streams', '-show_format', str(file_path)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        data = json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        raise Exception("ffprobe timeout - file may be corrupt")
    except json.JSONDecodeError:
        raise Exception("Failed to parse ffprobe output")
    except Exception as e:
        raise Exception(f"ffprobe failed: {e}")
    
    v_stream = next((s for s in data['streams'] if s['codec_type'] == 'video'), None)
    a_stream = next((s for s in data['streams'] if s['codec_type'] == 'audio'), None)
    if not v_stream:
        raise Exception("No video stream found")
    duration = float(data['format'].get('duration', 0))
    if duration == 0:
        raise Exception("Invalid or zero duration")
    fps_str = v_stream.get('avg_frame_rate', '30/1')
    try:
        fps = eval(fps_str)
    except:
        fps = 30.0
    fps_note = ''
    if v_stream.get('codec_name') == 'dvvideo':
        # DV's frame rate is fixed by its signal system, but raw DV and MKVs
        # built from raw DV (dvrescue) have no real container timing, so
        # ffprobe's rate fields can come out as 60000, 30000, hundreds of fps...
        # Take the rate from the DV frame header instead (falling back to frame
        # height for SD if the header can't be read).
        dv_height = int(v_stream.get('coded_height') or v_stream.get('height', 0))
        dv_system = read_dv_system(file_path)
        system_source = "DV frame header"
        if dv_system is None and dv_height in (480, 486, 576):
            dv_system = '625/50' if dv_height == 576 else '525/60'
            system_source = "frame height"
        if dv_system:
            nominal = dv_nominal_fps(dv_system, dv_height)
            if abs(fps - nominal) > 0.01:
                fps_note = (f"container reported {fps:.2f} fps — using DV system rate {nominal:.2f} fps "
                            f"({dv_system}, from {system_source})")
                fps = nominal
    total_frames = int(v_stream.get('nb_frames', 0))
    if total_frames == 0:
        total_frames = int(duration * fps)

    # Detect interlacing
    field_order = v_stream.get('field_order', 'progressive')
    interlaced = field_order not in ['progressive', 'unknown']
    field_order_source, field_order_detail = 'stream', field_order
    if 'field_order' not in v_stream or field_order == 'unknown':
        # Stream-level field order absent — fall back to frame metadata sampled across the file
        # rather than silently assuming progressive (GitHub issue #9).
        frame_order = detect_field_order_from_frames(file_path, duration)
        if frame_order is None:
            field_order_source, field_order_detail = 'none', ''
        else:
            field_order_source, field_order_detail = 'frames', frame_order
            interlaced = frame_order in ('tff', 'bff')

    # VFR detection: r_frame_rate (the stream's nominal/guessed rate) diverging
    # from avg_frame_rate (total_frames/duration) indicates the source is not CFR.
    r_fps = _parse_frame_rate(v_stream.get('r_frame_rate', '0/1'))
    is_vfr = r_fps > 0 and fps > 0 and abs(r_fps - fps) > 0.05
    if fps_note:
        # The container's rate fields were shown to be meaningless for this DV
        # source, so comparing them says nothing about VFR. The per-packet
        # duration check (detect_variable_packet_durations) still runs for
        # -v210/-ffv1 and catches real dropped or held frames.
        is_vfr = False

    # Use coded (full sample buffer) dimensions rather than display dimensions —
    # macOS FFmpeg 7.x+ honors QuickTime clap clean aperture atoms and reports
    # cropped display dimensions here, which understates the true frame size
    # for lossless preservation encoding.
    display_width = int(v_stream.get('width', 0))
    display_height = int(v_stream.get('height', 0))
    width = int(v_stream.get('coded_width') or display_width)
    height = int(v_stream.get('coded_height') or display_height)

    logger = logging.getLogger('video_transcoder')
    if warn and fps_note:
        logger.warning(f"{file_path.name}: DV frame rate misreported by container — {fps_note}")
    if warn and (width, height) != (display_width, display_height):
        logger.warning(f"{file_path.name}: coded dimensions {width}x{height} differ from "
                       f"reported display dimensions {display_width}x{display_height}")

    # Observed in practice (FFmpeg 8.1.2): clap crop doesn't show up as a
    # coded_width/width divergence at all — it's conveyed only through this
    # Frame Cropping side-data entry. This is the signal that actually matters.
    crop_side_data = next((sd for sd in v_stream.get('side_data_list', [])
                           if sd.get('side_data_type') == 'Frame Cropping'), None)
    if warn and crop_side_data:
        logger.warning(
            f"{file_path.name}: clean aperture crop present in source "
            f"(top={crop_side_data.get('crop_top', 0)} bottom={crop_side_data.get('crop_bottom', 0)} "
            f"left={crop_side_data.get('crop_left', 0)} right={crop_side_data.get('crop_right', 0)}) — "
            f"default preserves the full coded frame; pass --clean-aperture to honor the crop instead"
        )

    # major_brand='qt  ' identifies a genuine QuickTime file specifically — the mov
    # demuxer also handles plain MP4/M4A/3GP files under the same format_name bucket,
    # so format_name alone can't distinguish "actually QuickTime" from those.
    major_brand = data['format'].get('tags', {}).get('major_brand', '').strip()
    is_quicktime = major_brand == 'qt'

    return VideoInfo(
        width=width, height=height, duration=duration,
        fps=fps, dar=v_stream.get('display_aspect_ratio', '4:3'), has_audio=a_stream is not None,
        total_frames=total_frames, codec=v_stream.get('codec_name', 'unknown'),
        interlaced=interlaced, is_vfr=is_vfr, is_quicktime=is_quicktime,
        field_order_source=field_order_source, field_order_detail=field_order_detail,
        fps_note=fps_note
    )

def detect_video_standard(width: int, height: int, fps: float) -> VideoStandard:
    if width == 720 and height in [486, 480] and abs(fps - 29.97) < 0.1:
        return VideoStandard.NTSC
    if width == 720 and height == 576 and abs(fps - 25) < 0.1:
        return VideoStandard.PAL
    return VideoStandard.UNKNOWN

def calculate_scaling_params(width: int, height: int, dar: str, force_anamorphic: bool = False) -> str:
    if force_anamorphic and width == 720 and height in [480, 486, 576]:
        # Content digitized as 4:3 full frame but actually anamorphic (squeezed 16:9) —
        # detected DAR is unreliable here, so the caller overrides it explicitly.
        return "854:480"
    if height == 576 and width == 720:
        return "854:480" if dar == "16:9" else "640:480"
    elif height in [480, 486] and width == 720:
        return "854:480" if dar == "16:9" else "640:480"
    elif height == 480 and width == 352:
        # Half-D1 NTSC (352x480, SAR 20:11) — direct-to-disc DVD recorders.
        # Non-square pixels; DAR 4:3 displays at 640x480. Matches standard NTSC output.
        return "640:480"
    elif width == 1440 and height == 1080 and dar == "16:9":
        return "1280:720"
    elif width >= 1280 and height >= 720:
        if (width == 1920 and height == 1080) or (width == 1280 and height == 720):
            return "1280:720"
        elif width > 1920 or height > 1080:
            return "1280:720" if dar == "16:9" else "-2:720"
    return "trunc(iw/2)*2:trunc(ih/2)*2"

def get_bitrate_config(height: int) -> Dict[str, str]:
    return BITRATE_CONFIG['sd'] if height <= 576 else BITRATE_CONFIG['hd']

def calculate_output_fps(fps: float) -> float:
    is_pal = (abs(fps - 50) < 1 or abs(fps - 25) < 1)
    threshold = 25 if is_pal else 30
    return fps / 2 if fps > threshold else fps

def build_audio_filter_and_mapping(config: Config, info: VideoInfo) -> Tuple[List[str], List[str], str]:
    if not info.has_audio:
        return [], ["-an"], "No Audio"
    
    # Build ceiling filter string if requested.
    # Uses dynaudnorm with m=1 (max gain = 1.0, so it never boosts — only reduces)
    # targeting the specified peak level. This eliminates the slow level-riding
    # effect of default dynaudnorm settings: quiet passages are left completely
    # unchanged, and loud passages are reduced quickly (f=100ms frames, g=3
    # Gaussian window vs. the 500ms/31-frame defaults).
    clip_filter = ""
    clip_desc = ""
    if config.clip_ceiling is not None:
        clip_level = 10 ** (config.clip_ceiling / 20)
        clip_filter = f"dynaudnorm=p={clip_level:.4f}:m=1:f=100:g=3"
        clip_desc = f" [ceiling: {config.clip_ceiling:.0f} dBFS]"
    
    audio_mapping, audio_filter_args, audio_description = [], [], ""
    if config.audio_mode == 'stereo':
        audio_mapping = ["-map", config.audio_stream]
        if config.audio_pan_center:
            pan = "pan=stereo|c0=0.5*c0+0.5*c1|c1=0.5*c0+0.5*c1"
            af = f"{pan},{clip_filter}" if clip_filter else pan
            audio_filter_args = ["-af", af]
            audio_description = f"Stereo {config.audio_stream} (center-panned){clip_desc}"
        elif clip_filter:
            audio_filter_args = ["-af", clip_filter]
            audio_description = f"Stereo {config.audio_stream}{clip_desc}"
        else:
            audio_description = f"Stereo {config.audio_stream}"
    elif config.audio_mode == 'mono-duplicate':
        audio_mapping = ["-map", config.audio_stream]
        ch = f"c{config.audio_channel}"
        pan = f"pan=stereo|c0={ch}|c1={ch}"
        af = f"{pan},{clip_filter}" if clip_filter else pan
        audio_filter_args = ["-af", af]
        channel_label = "L" if config.audio_channel == 0 else "R" if config.audio_channel == 1 else f"c{config.audio_channel}"
        audio_description = f"Mono {config.audio_stream} ch{config.audio_channel} ({channel_label}, duplicated to L+R){clip_desc}"
    elif config.audio_mode == 'mono-merge':
        audio_mapping = []
        base_fc = "[0:a:0][0:a:1]amerge=inputs=2,pan=stereo|c0=0.5*c0+0.5*c1|c1=0.5*c0+0.5*c1"
        fc = f"{base_fc},{clip_filter}[aout]" if clip_filter else f"{base_fc}[aout]"
        audio_filter_args = ["-filter_complex", fc, "-map", "[aout]"]
        audio_description = f"Mono 0:a:0+0:a:1 (merged and center-panned){clip_desc}"
    return audio_mapping, audio_filter_args, audio_description

def validate_output(file_path: Path, timeout: int = VALIDATION_TIMEOUT) -> Tuple[bool, str]:
    cmd = ['ffmpeg', '-v', 'error', '-i', str(file_path), '-f', 'null', '-']
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (result.returncode == 0 and len(result.stderr) == 0), result.stderr
    except subprocess.TimeoutExpired:
        return False, "Validation timeout"
    except Exception as e:
        return False, str(e)

def probe_streams_summary(file_path: Path, timeout: int = 30) -> List[Tuple[str, str]]:
    """(codec_type, codec_name) for every stream in the file, in stream order —
    including non-AV streams like Matroska attachments (ffprobe reports these
    as codec_type='attachment'). Used to check that --trim-in/--trim-out's
    stream-copy-everything ('-map 0 -c copy') actually carried every stream
    through, not just audio/video."""
    cmd = ['ffprobe', '-v', 'error', '-show_entries', 'stream=codec_type,codec_name', '-of', 'json', str(file_path)]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    data = json.loads(result.stdout)
    return [(s.get('codec_type', '?'), s.get('codec_name', '?')) for s in data.get('streams', [])]

def validate_trim_output(source_path: Path, output_path: Path, requested_in: float, requested_out: float,
                          timeout: int = VALIDATION_TIMEOUT) -> Tuple[bool, List[str]]:
    """Three-tier check for a --trim-in/--trim-out stream-copy output:
      1. Structural — every stream in the source (video/audio/attachments/data)
         is present in the output, same codec, same order.
      2. Duration — output duration is within tolerance of the requested span.
         Stream-copy trims can only cut at keyframes (ffmpeg can't split mid-GOP
         without re-encoding), so for non-intra codecs the actual cut point may
         land a GOP away from what was requested — expected, not a failure, as
         long as it's within tolerance. FFV1 masters (GOP=1, all-intra) land
         exactly on the requested point.
      3. Full decode — every stream, via '-map 0' (not ffmpeg's default picks),
         decodes cleanly start to finish.
    Returns (ok, messages); messages covers all three tiers regardless of
    overall pass/fail, so the caller can log full detail either way.
    """
    messages: List[str] = []
    ok = True

    try:
        source_streams = probe_streams_summary(source_path)
        output_streams = probe_streams_summary(output_path)
    except Exception as e:
        return False, [f"Structural check FAILED: could not probe streams: {e}"]
    if source_streams != output_streams:
        ok = False
        messages.append(f"Structural check FAILED: source streams {source_streams} != output streams {output_streams}")
    else:
        messages.append(f"Structural check passed: {len(output_streams)} stream(s) match source {output_streams}")

    try:
        output_duration = get_video_info(output_path).duration
    except Exception as e:
        ok = False
        messages.append(f"Duration check FAILED: could not read output duration: {e}")
    else:
        requested_duration = requested_out - requested_in
        drift = output_duration - requested_duration
        tolerance = 15.0  # seconds — generous, to allow for a keyframe-boundary snap
        if abs(drift) > tolerance:
            ok = False
            messages.append(f"Duration check FAILED: output is {format_duration(output_duration)}, requested span "
                            f"was {format_duration(requested_duration)} (drift {drift:+.1f}s, tolerance {tolerance:.0f}s)")
        else:
            messages.append(f"Duration check passed: output is {format_duration(output_duration)}, requested span "
                            f"was {format_duration(requested_duration)} (drift {drift:+.1f}s — expected if a trim "
                            f"point fell mid-GOP and ffmpeg snapped to the nearest keyframe)")

    cmd = ['ffmpeg', '-v', 'error', '-i', str(output_path), '-map', '0', '-f', 'null', '-']
    result = None
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        decode_ok = result.returncode == 0 and len(result.stderr) == 0
    except subprocess.TimeoutExpired:
        decode_ok = False
    if not decode_ok:
        ok = False
        err = result.stderr.strip()[:500] if result is not None else "Validation timeout"
        messages.append(f"Full decode validation FAILED (-map 0, every stream): {err}")
    else:
        messages.append("Full decode validation passed: all streams (-map 0) decoded cleanly start to finish")

    return ok, messages

# v210 PAL policy — mirrors POLICY_V210_NTSC for 625-line/25 fps SD. SDI PAL is
# top field first; primaries and matrix tagged bt470bg, which MediaInfo reports as
# "BT.601 PAL" / "BT.470 System B/G" — the same tags vrecord writes on PAL captures
# (the bt470bg and smpte170m matrices are numerically identical BT.601).
# Added 2026-09-25, checked against a vrecord PAL VHS capture.
POLICY_V210_PAL = """\
<?xml version="1.0"?>
<policy type="and" name="VDG v210 MOV Master (PAL SD) — Apple TN2162 conformant">
  <description>10-bit Uncompressed v210 QuickTime MOV, PAL SD (720x576, 25.000fps, TFF).
Audio must be PCM 24-bit 48kHz. Channel count and track count are not constrained.
Asserts conformance with Apple TN2162 ("Uncompressed Y'CbCr Video in QuickTime
Files") for the colr atom (colour_primaries/transfer_characteristics/matrix_coefficients
rules below), the fiel atom (ScanType/ScanOrder rules below), and the pasp atom
(PixelAspectRatio rule below — UNVERIFIED, see comment on that rule). The clap
(clean aperture) atom is NOT asserted here: vdg's default (--clean-aperture not
passed) preserves the full coded frame with no display/coded size mismatch, so
there is nothing for a clap atom to declare — clap conformance only applies when
--clean-aperture is used, and isn't currently checked by this policy (see MANUAL.md).</description>
  <rule name="Container is MPEG-4" value="Format" tracktype="General" occurrence="*" operator="=">MPEG-4</rule>
  <rule name="Format profile is QuickTime" value="Format_Profile" tracktype="General" occurrence="*" operator="=">QuickTime</rule>
  <rule name="File extension is mov" value="FileExtension" tracktype="General" occurrence="*" operator="=">mov</rule>
  <rule name="Video codec is v210" value="CodecID" tracktype="Video" occurrence="*" operator="=">v210</rule>
  <rule name="Video width is 720" value="Width" tracktype="Video" occurrence="*" operator="=">720</rule>
  <rule name="Video height is 576" value="Height" tracktype="Video" occurrence="*" operator="=">576</rule>
  <rule name="Video frame rate is 25.000" value="FrameRate" tracktype="Video" occurrence="*" operator="=">25.000</rule>
  <rule name="Video standard is PAL" value="Standard" tracktype="Video" occurrence="*" operator="=">PAL</rule>
  <rule name="Chroma subsampling is 4:2:2" value="ChromaSubsampling" tracktype="Video" occurrence="*" operator="=">4:2:2</rule>
  <rule name="Bit depth is 10" value="BitDepth" tracktype="Video" occurrence="*" operator="=">10</rule>
  <rule name="Scan type is Interlaced" value="ScanType" tracktype="Video" occurrence="*" operator="=">Interlaced</rule>
  <rule name="Scan order is TFF" value="ScanOrder" tracktype="Video" occurrence="*" operator="=">TFF</rule>
  <rule name="Color primaries is BT.601 PAL" value="colour_primaries" tracktype="Video" occurrence="*" operator="=">BT.601 PAL</rule>
  <rule name="Transfer characteristics is BT.709" value="transfer_characteristics" tracktype="Video" occurrence="*" operator="=">BT.709</rule>
  <rule name="Matrix coefficients is BT.470 System B/G" value="matrix_coefficients" tracktype="Video" occurrence="*" operator="=">BT.470 System B/G</rule>
  <!-- TN2162 pasp atom (pixel aspect ratio). vdg encodes setsar=12/11 (vrecord's
       own recommended PAL 4:3 default, per vrecord's Config > Aspect Ratio
       tab). MediaConch has no three-state result, so this passes on ANY of
       vrecord's three user-configurable PAL 4:3 PAR values (720x576 D-1) —
       vdg's own output warns separately (Python-level, not MediaConch) when
       the actual value isn't vrecord's default. 2026-09-28: corrected from an
       earlier single-value 1.067 rule, which reflected a since-fixed
       setdar=4/3 bug that silently overrode setsar on every v210 output
       (see process_v210_output) rather than any legitimate PAR target. -->
  <policy type="or" name="Pixel aspect ratio is a vrecord-acceptable PAL 4:3 value">
    <rule name="PAR is 1.091 (12/11, vrecord default)" value="PixelAspectRatio" tracktype="Video" occurrence="*" operator="=">1.091</rule>
    <rule name="PAR is 1.067 (16/15)" value="PixelAspectRatio" tracktype="Video" occurrence="*" operator="=">1.067</rule>
    <rule name="PAR is 1.094 (128/117)" value="PixelAspectRatio" tracktype="Video" occurrence="*" operator="=">1.094</rule>
  </policy>
  <rule name="Audio format is PCM" value="Format" tracktype="Audio" occurrence="*" operator="=">PCM</rule>
  <rule name="Audio is 24-bit" value="BitDepth" tracktype="Audio" occurrence="*" operator="=">24</rule>
  <rule name="Audio sample rate is 48kHz" value="SamplingRate" tracktype="Audio" occurrence="*" operator="=">48000</rule>
  <rule name="Audio is little-endian" value="Format_Settings_Endianness" tracktype="Audio" occurrence="*" operator="=">Little</rule>
  <rule name="Audio is signed" value="Format_Settings_Sign" tracktype="Audio" occurrence="*" operator="=">Signed</rule>
</policy>
"""

def policy_without_audio_rules(policy_xml: str) -> str:
    """Remove every tracktype="Audio" rule from a MediaConch policy, plus any
    sub-policy left empty by that (e.g. FFV1's "Audio is PCM or FLAC" block).
    Used for sources with no audio track, where MediaConch otherwise fails every
    audio rule — confirmed with MediaConch 25.04 on a silent v210 output."""
    lines = [line for line in policy_xml.splitlines() if 'tracktype="Audio"' not in line]
    xml = "\n".join(lines) + "\n"
    return re.sub(r'\n[ \t]*<policy[^>]*>\s*</policy>', '', xml)

def policy_without_dimension_rules(policy_xml: str) -> str:
    """Remove the Width/Height rules from a MediaConch policy. Used for v210 with
    --clean-aperture, where the output is display-cropped (e.g. 704x480) by
    design and can never match the 720x486 NTSC master rule — every other rule
    still applies."""
    lines = [line for line in policy_xml.splitlines()
             if not re.search(r'value="(Width|Height)"', line)]
    return "\n".join(lines) + "\n"

def run_validation_command_with_spinner(cmd: List[str], description: str, log_file: Optional[Path] = None) -> Tuple[bool, str]:
    if log_file:
        try:
            with open(log_file, 'a') as f:
                f.write(f"\nCOMMAND ({description}): {shlex.join(str(c) for c in cmd)}\n")
        except Exception as e:
            logging.getLogger('video_transcoder').warning(f"Failed to write command to log: {e}")
    spinner = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏']
    result_container = {'result': None, 'done': False}
    def run_command():
        result = subprocess.run(cmd, capture_output=True, text=True)
        result_container['result'] = result
        result_container['done'] = True
    thread = threading.Thread(target=run_command)
    thread.daemon = True
    thread.start()
    # Drawn as a tqdm line (not raw stdout writes) so it coexists with the
    # "Total Progress" bar instead of leaving stale copies of it behind.
    idx = 0
    spin_line = tqdm(total=0, bar_format="    {desc}", desc=description, leave=False,
                     dynamic_ncols=True)
    while not result_container['done']:
        spin_line.set_description_str(f"{description} {spinner[idx % len(spinner)]}")
        time.sleep(0.1)
        idx += 1
    spin_line.close()
    thread.join()
    result = result_container['result']
    console_write(f"    {description} {'✓' if result.returncode == 0 else '✗'}")
    return (result.returncode == 0), result.stdout

def run_mediaconch_check(output_path: Path, policy_xml: str, policy_filename: str,
                         log_dir: Path, process_log: Path, keep_policy: bool,
                         has_audio: bool = True, skip_dimensions: bool = False) -> Tuple[bool, str]:
    """Write embedded policy XML to log_dir, run mediaconch against output_path,
    log the result to process_log, and optionally retain the policy file."""
    logger = logging.getLogger('video_transcoder')
    if shutil.which('mediaconch') is None:
        logger.warning("mediaconch not found — skipping policy conformance check")
        return True, "mediaconch not available — check skipped"
    policy_path = log_dir / policy_filename
    if not has_audio:
        policy_xml = policy_without_audio_rules(policy_xml)
    if skip_dimensions:
        policy_xml = policy_without_dimension_rules(policy_xml)
    try:
        policy_path.write_text(policy_xml, encoding='utf-8')
        cmd = ['mediaconch', f'--Policy={str(policy_path)}', str(output_path)]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        mc_output = result.stdout.strip()
        passed = result.returncode == 0 and 'pass' in mc_output.lower()
        with open(process_log, 'a') as log_f:
            log_f.write("\n" + "=" * 70 + "\n")
            log_f.write("MEDIACONCH POLICY CHECK\n")
            log_f.write("=" * 70 + "\n")
            log_f.write(f"Command: {shlex.join(cmd)}\n")
            omitted = ([] if has_audio else ["audio rules omitted — no audio stream in source"]) + \
                      (["width/height rules omitted — --clean-aperture output"] if skip_dimensions else [])
            log_f.write(f"Policy:  {policy_filename}{' (' + '; '.join(omitted) + ')' if omitted else ''}\n")
            log_f.write(f"File:    {output_path.name}\n")
            log_f.write(f"Result:  {'PASS' if passed else 'FAIL'}\n")
            log_f.write(f"Output:  {mc_output}\n")
            if result.stderr.strip():
                log_f.write(f"Errors:  {result.stderr.strip()}\n")
            log_f.write("=" * 70 + "\n\n")
        if passed:
            logger.info(f"  ✓ MediaConch policy check PASSED for {output_path.name}")
        else:
            logger.error(f"  ✗ MediaConch policy check FAILED for {output_path.name}")
            logger.error(f"    {mc_output}")
        return passed, mc_output
    except subprocess.TimeoutExpired:
        return False, "MediaConch check timeout"
    except Exception as e:
        return False, f"MediaConch check error: {e}"
    finally:
        if not keep_policy and policy_path.exists():
            try:
                policy_path.unlink()
            except Exception:
                pass
def validate_v210_lossless(source_path: Path, output_path: Path, process_log: Path,
                           log_dir: Path, keep_framemd5: bool, keep_mediaconch: bool,
                           video_standard: VideoStandard, clean_aperture: bool = False,
                           has_audio: bool = True) -> Tuple[bool, str]:
    logger = logging.getLogger('video_transcoder')
    try:
        temp_dir = output_path.parent / "temp_framemd5"
        temp_dir.mkdir(exist_ok=True)
        source_video_md5 = temp_dir / f"{source_path.stem}_source_video.framemd5"
        output_video_md5 = temp_dir / f"{output_path.stem}_output_video.framemd5"
        logger.info(f"Validating v210 lossless conversion for {source_path.name}...")
        with open(process_log, 'a') as log_f:
            log_f.write("\n" + "=" * 70 + "\n" + "FRAMEMD5 LOSSLESS VALIDATION\n" + "=" * 70 + "\n\n")

        # Source hash must use the same clean-aperture handling as the encode
        # command, or a divergence there (not an actual encoding problem) will
        # show up as a framemd5 mismatch.
        logger.info("  → Generating framemd5 for source video stream...")
        cmd_source_video = ['ffmpeg'] + clean_aperture_input_args(clean_aperture) + ['-i', str(source_path), '-map', '0:v:0', '-f', 'framemd5', str(source_video_md5)]
        success, _ = run_validation_command_with_spinner(cmd_source_video, "Hashing source video", process_log)
        if not success:
            return False, "Failed to generate source video framemd5"

        logger.info("  → Generating framemd5 for output video stream...")
        cmd_output_video = ['ffmpeg', '-i', str(output_path), '-map', '0:v:0', '-f', 'framemd5', str(output_video_md5)]
        success, _ = run_validation_command_with_spinner(cmd_output_video, "Hashing output video", process_log)
        if not success:
            return False, "Failed to generate output video framemd5"

        logger.info("  → Comparing video framemd5 checksums...")
        with open(source_video_md5, 'r') as f:
            source_video_hashes = [line.strip() for line in f if not line.startswith('#')]
        with open(output_video_md5, 'r') as f:
            output_video_hashes = [line.strip() for line in f if not line.startswith('#')]

        mismatches = [(i, s, o) for i, (s, o) in enumerate(zip(source_video_hashes, output_video_hashes)) if s != o]
        frame_count = len(source_video_hashes)

        if len(source_video_hashes) != len(output_video_hashes) or mismatches:
            mismatch_msg = (f"Video framemd5 mismatch: {len(source_video_hashes)} source frames "
                           f"vs {len(output_video_hashes)} output frames, {len(mismatches)} differing")
            with open(process_log, 'a') as log_f:
                log_f.write(f"ERROR: {mismatch_msg}\n")
                for i, src, out in mismatches:
                    log_f.write(f"  Frame {i}:\n    Source: {src}\n    Output: {out}\n")
            return False, mismatch_msg

        logger.info(f"  ✓ Video validation passed: {frame_count} frames match")

        if not has_audio:
            logger.info("  → No audio stream in source — video-only validation")
        else:
            logger.info("  → Generating hash for source audio stream(s)...")
        cmd_source_audio = ['ffmpeg', '-i', str(source_path), '-map', '0:a', '-f', 'streamhash', '-hash', 'md5', '-']
        if has_audio:
            success, source_audio_hash = run_validation_command_with_spinner(cmd_source_audio, "Hashing source audio", process_log)
        else:
            success, source_audio_hash = False, ""
        audio_passed = False
        source_audio_hash = source_audio_hash.strip() if success else ""
        output_audio_hash = ""
        if not success:
            with open(process_log, 'a') as log_f:
                log_f.write("Audio validation skipped: no audio stream in source\n" if not has_audio else
                            "Audio validation skipped: no audio stream or failed to hash source\n")
        else:
            cmd_output_audio = ['ffmpeg', '-i', str(output_path), '-map', '0:a', '-f', 'streamhash', '-hash', 'md5', '-']
            success, output_audio_hash = run_validation_command_with_spinner(cmd_output_audio, "Hashing output audio", process_log)
            output_audio_hash = output_audio_hash.strip()
            if not success:
                return False, "Failed to generate output audio hash"
            if source_audio_hash != output_audio_hash:
                mismatch_msg = f"Audio streamhash mismatch:\nSource: {source_audio_hash}\nOutput: {output_audio_hash}"
                with open(process_log, 'a') as log_f:
                    log_f.write(f"ERROR: {mismatch_msg}\n")
                return False, mismatch_msg
            audio_passed = True
            logger.info("  ✓ Audio validation passed: stream hashes match")

        # Always write digest to process log
        with open(process_log, 'a') as log_f:
            log_f.write("\nFRAMEMD5 DIGEST\n" + "-" * 40 + "\n")
            log_f.write(f"Frames compared:    {frame_count}\n")
            log_f.write(f"Video result:       PASS — all {frame_count} frames match\n")
            if not has_audio:
                log_f.write("Audio result:       N/A — no audio stream in source\n")
            if audio_passed:
                log_f.write(f"Audio source hash:  {source_audio_hash}\n")
                log_f.write(f"Audio output hash:  {output_audio_hash}\n")
                log_f.write(f"Audio result:       PASS\n")
            log_f.write("-" * 40 + "\n")
            log_f.write("\n" + "=" * 70 + "\nLOSSLESS VALIDATION: PASSED\n" + "=" * 70 + "\n\n")

        # Retain or delete framemd5 files
        if keep_framemd5:
            for md5_file in [source_video_md5, output_video_md5]:
                dest = log_dir / md5_file.name
                md5_file.rename(dest)
                logger.info(f"  → framemd5 retained: {dest.name}")
        else:
            for md5_file in [source_video_md5, output_video_md5]:
                try:
                    md5_file.unlink()
                except Exception:
                    pass
        try:
            temp_dir.rmdir()
        except Exception:
            pass

        logger.info(f"  ✓ v210 lossless validation PASSED for {source_path.name}")

        # MediaConch policy check — per video standard
        v210_policy = {VideoStandard.NTSC: (POLICY_V210_NTSC, "policy_v210_ntsc.xml"),
                       VideoStandard.PAL: (POLICY_V210_PAL, "policy_v210_pal.xml")}.get(video_standard)
        if v210_policy:
            if clean_aperture:
                # Display-cropped by request; flag it clearly rather than letting
                # the 720x486 rule quarantine every --clean-aperture v210.
                dims = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                                       '-show_entries', 'stream=width,height', '-of', 'csv=p=0:s=x',
                                       str(output_path)], capture_output=True, text=True).stdout.strip()
                standard_size = "720x486 NTSC" if video_standard == VideoStandard.NTSC else "720x576 PAL"
                logger.warning(f"  --clean-aperture: v210 output is display-cropped ({dims or 'non-standard size'}), "
                               f"not a standard {standard_size} master — MediaConch width/height rules skipped")
            mc_passed, mc_msg = run_mediaconch_check(
                output_path, v210_policy[0], v210_policy[1],
                log_dir, process_log, keep_mediaconch, has_audio, skip_dimensions=clean_aperture)
            if not mc_passed:
                return False, f"MediaConch policy check failed: {mc_msg}"
            if not clean_aperture:
                par_msg = par_default_mismatch(output_path, video_standard)
                if par_msg:
                    logger.warning(f"  {output_path.name}: {par_msg} — passes TN2162/MediaConch (vrecord-acceptable), "
                                   f"but doesn't match vrecord's own default for this standard")
        else:
            logger.info("  → MediaConch policy check skipped (no policy defined for this video standard)")

        return True, "Validation passed"
    except subprocess.TimeoutExpired:
        return False, "Validation timeout"
    except Exception as e:
        logger.error(f"Validation error: {e}")
        return False, f"Validation error: {e}"

def run_ffmpeg_with_progress(cmd: List[str], total_frames: int, description: str, log_file: Optional[Path] = None) -> Tuple[bool, str]:
    if total_frames <= 0:
        total_frames = 1
    if log_file:
        try:
            with open(log_file, 'a') as f:
                f.write(f"\nCOMMAND: {shlex.join(str(c) for c in cmd)}\n\n")
        except Exception as e:
            logging.getLogger('video_transcoder').warning(f"Failed to write command to log: {e}")
    pbar = tqdm(total=total_frames, desc=description, unit="fr", leave=False, dynamic_ncols=True, colour='cyan')
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, bufsize=1)
    frame_pattern = re.compile(r'frame=\s*(\d+)')
    full_output, last_frame = [], 0
    try:
        for line in process.stdout:
            full_output.append(line)
            match = frame_pattern.search(line)
            if match:
                current_frame = int(match.group(1))
                diff = current_frame - last_frame
                if diff > 0:
                    pbar.update(diff)
                    last_frame = current_frame
        process.wait()
    except Exception as e:
        process.kill()
        raise e
    finally:
        pbar.close()
    output_text = "".join(full_output)
    if log_file:
        try:
            with open(log_file, 'a') as f:
                f.write(output_text)
        except Exception as e:
            logging.getLogger('video_transcoder').warning(f"Failed to write log: {e}")
    return (process.returncode == 0), output_text

def generate_thumbnail(source_path: Path, output_path: Path, timestamp: float, scale_string: str, index: int, total: int, interlaced: bool, parity: int = -1, log_file: Optional[Path] = None) -> bool:
    # Only deinterlace thumbnails if source is interlaced
    # setsar=1 forces square output pixels — without it, ffmpeg's scale filter
    # recalculates SAR to preserve the *source's* DAR, silently undoing any
    # intentional aspect-ratio change (e.g. --force-anamorphic).
    if interlaced:
        vf_filter = f"bwdif=mode=0:parity={parity}:deint=all,scale={scale_string},setsar=1,format=rgb24"
    else:
        vf_filter = f"scale={scale_string},setsar=1,format=rgb24"
    
    # Use libopenjpeg encoder rather than the native jpeg2000 encoder.
    # The native encoder maps to sYCC color space at 9-bit regardless of input format.
    # libopenjpeg with rgb24 input produces standard 8-bit sRGB JP2 output.
    cmd = ["ffmpeg", "-y", "-ss", str(timestamp), "-i", str(source_path),
           "-vf", vf_filter,
           "-frames:v", "1", "-update", "1", "-c:v", "libopenjpeg", "-pix_fmt", "rgb24", str(output_path)]
    desc = f"Thumbnail {index}/{total}"
    success, output = run_ffmpeg_with_progress(cmd, 1, desc, log_file)
    return success

# Audio resync used in the separate H.264 audio mux step (GitHub issue #10).
AUDIO_ASYNC_FILTER = "aresample=async=1:min_hard_comp=0.100000:first_pts=0"

def add_audio_async_filter(audio_filter_args: List[str]) -> List[str]:
    """Add AUDIO_ASYNC_FILTER to the audio filter args from
    build_audio_filter_and_mapping(), preserving whatever is already there
    (pan, dynaudnorm ceiling, mono-merge filter_complex)."""
    if not audio_filter_args:
        return ["-af", AUDIO_ASYNC_FILTER]
    if audio_filter_args[0] == "-af":
        return ["-af", f"{AUDIO_ASYNC_FILTER},{audio_filter_args[1]}"] + audio_filter_args[2:]
    if audio_filter_args[0] == "-filter_complex" and audio_filter_args[1].endswith("[aout]"):
        fc = audio_filter_args[1][:-len("[aout]")] + f",{AUDIO_ASYNC_FILTER}[aout]"
        return ["-filter_complex", fc] + audio_filter_args[2:]
    return audio_filter_args

def cleanup_temp_files(prefix: Path):
    for ext in ["-0.log", "-0.log.mbtree"]:
        temp_file = Path(str(prefix) + ext)
        if temp_file.exists():
            try:
                temp_file.unlink()
            except Exception as e:
                logging.getLogger('video_transcoder').warning(f"Failed to delete {temp_file}: {e}")

def verify_derivatives_exist(output_paths: Dict[str, Path], thumbnail_prefix: str, num_thumbs: int = 4) -> bool:
    for output_path in output_paths.values():
        if output_path and not output_path.exists():
            return False
    if 'h264' in output_paths and output_paths['h264']:
        for i in range(1, num_thumbs + 1):
            thumb_path = output_paths['h264'].parent / f"{thumbnail_prefix}_thumb_{i}.jp2"
            if not thumb_path.exists():
                return False
    return True

def process_h264_output(source_path: Path, output_path: Path, info: VideoInfo, config: Config, process_log: Path, stats_log_prefix: Path, thumbnail_prefix: str) -> bool:
    try:
        out_fps = calculate_output_fps(info.fps)
        gop = math.ceil(out_fps * GOP_MULTIPLIER)
        scale_string = calculate_scaling_params(info.width, info.height, info.dar, config.force_anamorphic)
        bitrate_cfg = get_bitrate_config(info.height)
        
        # Build filter chain - only deinterlace if source is interlaced
        if info.interlaced:
            if config.force_scan == 'tff':
                parity = 0
            elif config.force_scan == 'bff':
                parity = 1
            else:
                parity = -1
            vf_chain = f"bwdif=mode=0:parity={parity}:deint=all,scale={scale_string},setsar=1,format=yuv420p"
        else:
            parity = -1
            vf_chain = f"scale={scale_string},setsar=1,format=yuv420p"
        
        audio_mapping, audio_filter_args, audio_description = build_audio_filter_and_mapping(config, info)
        with open(process_log, 'a') as log_f:
            log_f.write(f"Audio Configuration: {audio_description}\n")
            log_f.write(f"AAC Encoder: {config.aac_encoder}\n")
            log_f.write(f"Interlaced: {info.interlaced}\n")
        
        video_mapping = ["-map", "0:v:0"]
        video_codec_args = ["-c:v", "libx264", "-preset", "medium", "-profile:v", "main", "-fps_mode", "cfr",
                           "-r", f"{out_fps:.3f}", "-g", str(gop), "-sc_threshold", "0", "-vf", vf_chain,
                           "-b:v", bitrate_cfg['bitrate'], "-maxrate", bitrate_cfg['maxrate'], "-bufsize", bitrate_cfg['bufsize']]
        audio_codec_args = ["-c:a", config.aac_encoder, "-ac", "2", "-ar", "48000", "-b:a", "128k"] if info.has_audio else ["-an"]
        
        pass1_cmd = ["ffmpeg", "-y", "-i", str(source_path)] + video_mapping + video_codec_args + ["-pass", "1", "-passlogfile", str(stats_log_prefix), "-an", "-f", "mp4", os.devnull]
        success, _ = run_ffmpeg_with_progress(pass1_cmd, info.total_frames, f"H264 Pass 1: {source_path.name[:20]}", process_log)
        if not success:
            return False
        
        pass2_video_args = video_codec_args + ["-pass", "2", "-passlogfile", str(stats_log_prefix), "-tune", "film"]
        if not info.has_audio:
            pass2_cmd = ["ffmpeg", "-y", "-i", str(source_path)] + video_mapping + pass2_video_args + \
                        ["-an", "-movflags", "faststart", str(output_path)]
            success, _ = run_ffmpeg_with_progress(pass2_cmd, info.total_frames, f"H264 Pass 2: {source_path.name[:20]}", process_log)
            if not success:
                return False
        else:
            # GitHub issue #10: encoding video (slow x264) and audio (near-instant
            # AAC) in the same ffmpeg process silently dropped audio partway through
            # long files (e.g. ~35 min into long Premiere ProRes HQ exports) —
            # regardless of edit-list, resample or interleave fixes. So pass 2
            # encodes video only, to a temp file, and a separate step muxes that
            # (stream-copied) video with freshly decoded audio, where both streams
            # produce packets at comparable speed.
            temp_video = stats_log_prefix.parent / f"{output_path.stem}_video_tmp.mp4"
            pass2_cmd = ["ffmpeg", "-y", "-i", str(source_path)] + video_mapping + pass2_video_args + \
                        ["-an", str(temp_video)]
            success, _ = run_ffmpeg_with_progress(pass2_cmd, info.total_frames, f"H264 Pass 2: {source_path.name[:20]}", process_log)
            if not success:
                logging.getLogger('video_transcoder').error(
                    f"H264 pass 2 failed — audio mux skipped; partial video left for inspection: {temp_video}")
                return False
            mux_cmd = ["ffmpeg", "-y", "-i", str(source_path), "-i", str(temp_video), "-map", "1:v:0"]
            mux_cmd += audio_mapping + add_audio_async_filter(audio_filter_args)
            mux_cmd += ["-c:v", "copy"] + audio_codec_args + \
                       ["-max_interleave_delta", "0", "-movflags", "faststart", str(output_path)]
            success, _ = run_ffmpeg_with_progress(mux_cmd, info.total_frames, f"H264 Audio Mux: {source_path.name[:20]}", process_log)
            if not success:
                logging.getLogger('video_transcoder').error(
                    f"H264 audio mux failed — video-only encode left for inspection: {temp_video}")
                return False
            try:
                temp_video.unlink()
            except Exception as e:
                logging.getLogger('video_transcoder').warning(f"Failed to delete {temp_video}: {e}")
        
        if not config.skip_validation:
            is_valid, err_msg = validate_output(output_path)
            if not is_valid:
                logging.getLogger('video_transcoder').error(f"H264 validation failed: {err_msg}")
                return False
        
        return generate_thumbnail_set(source_path, output_path.parent, thumbnail_prefix, info, config,
                                      scale_string, parity, process_log)
    except Exception as e:
        logging.getLogger('video_transcoder').error(f"H264 processing error: {e}")
        return False

DESKTOP_REVIEW_SD_SIZE = (720, 540)
# Proportional 4:3 fit before pillarboxing to the 1920x1080 HD frame.
DESKTOP_REVIEW_HD_FIT = (1440, 1080)

def _desktop_review_vf_chain(info: VideoInfo, config: Config, target_width: int, target_height: int,
                             pillarbox: bool) -> str:
    """Build the bwdif+scale(+pad) filter chain for one desktop-review variant.
    Parity mirrors process_h264_output's logic: explicit from --force-scan,
    else -1 for ffmpeg/bwdif's own per-frame auto-detection."""
    if pillarbox:
        scale_part = (f"scale={target_width}:{target_height}:flags=lanczos,"
                      f"pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=black")
    else:
        scale_part = f"scale={target_width}:{target_height}:flags=lanczos"
    if info.interlaced:
        if config.force_scan == 'tff':
            parity = 0
        elif config.force_scan == 'bff':
            parity = 1
        else:
            parity = -1
        return f"bwdif=mode=0:parity={parity}:deint=all,{scale_part},setsar=1,format=yuv420p"
    return f"{scale_part},setsar=1,format=yuv420p"

def _encode_desktop_review_variant(source_path: Path, output_path: Path, info: VideoInfo, config: Config,
                                   process_log: Path, stats_log_prefix: Path, vf_chain: str, out_fps: float,
                                   bitrate_cfg: Dict[str, str], label: str) -> bool:
    """Two-pass H.264 encode for one desktop-review variant (SD or HD). Shares
    process_h264_output's separate video/audio-mux structure (GitHub issue #10
    — encoding slow x264 video and near-instant AAC audio in the same ffmpeg
    process silently dropped audio partway through long files)."""
    gop = math.ceil(out_fps * GOP_MULTIPLIER)
    audio_mapping, audio_filter_args, audio_description = build_audio_filter_and_mapping(config, info)
    with open(process_log, 'a') as log_f:
        log_f.write(f"[{label}] Audio Configuration: {audio_description}\n")
        log_f.write(f"[{label}] AAC Encoder: {config.aac_encoder}\n")
        log_f.write(f"[{label}] Filter chain: {vf_chain}\n")

    video_mapping = ["-map", "0:v:0"]
    video_codec_args = ["-c:v", "libx264", "-preset", "medium", "-profile:v", "high", "-fps_mode", "cfr",
                       "-r", f"{out_fps:.3f}", "-g", str(gop), "-sc_threshold", "0", "-vf", vf_chain,
                       "-b:v", bitrate_cfg['bitrate'], "-maxrate", bitrate_cfg['maxrate'], "-bufsize", bitrate_cfg['bufsize']]
    audio_codec_args = ["-c:a", config.aac_encoder, "-ac", "2", "-ar", "48000", "-b:a", "192k"] if info.has_audio else ["-an"]

    pass1_cmd = ["ffmpeg", "-y", "-i", str(source_path)] + video_mapping + video_codec_args + \
                ["-pass", "1", "-passlogfile", str(stats_log_prefix), "-an", "-f", "mp4", os.devnull]
    success, _ = run_ffmpeg_with_progress(pass1_cmd, info.total_frames, f"{label} Pass 1: {source_path.name[:20]}", process_log)
    if not success:
        return False

    pass2_video_args = video_codec_args + ["-pass", "2", "-passlogfile", str(stats_log_prefix), "-tune", "film"]
    if not info.has_audio:
        pass2_cmd = ["ffmpeg", "-y", "-i", str(source_path)] + video_mapping + pass2_video_args + \
                    ["-an", "-movflags", "faststart", str(output_path)]
        success, _ = run_ffmpeg_with_progress(pass2_cmd, info.total_frames, f"{label} Pass 2: {source_path.name[:20]}", process_log)
        if not success:
            return False
    else:
        temp_video = stats_log_prefix.parent / f"{output_path.stem}_video_tmp.mp4"
        pass2_cmd = ["ffmpeg", "-y", "-i", str(source_path)] + video_mapping + pass2_video_args + \
                    ["-an", str(temp_video)]
        success, _ = run_ffmpeg_with_progress(pass2_cmd, info.total_frames, f"{label} Pass 2: {source_path.name[:20]}", process_log)
        if not success:
            logging.getLogger('video_transcoder').error(
                f"{label} pass 2 failed — audio mux skipped; partial video left for inspection: {temp_video}")
            return False
        mux_cmd = ["ffmpeg", "-y", "-i", str(source_path), "-i", str(temp_video), "-map", "1:v:0"]
        mux_cmd += audio_mapping + add_audio_async_filter(audio_filter_args)
        mux_cmd += ["-c:v", "copy"] + audio_codec_args + \
                   ["-max_interleave_delta", "0", "-movflags", "faststart", str(output_path)]
        success, _ = run_ffmpeg_with_progress(mux_cmd, info.total_frames, f"{label} Audio Mux: {source_path.name[:20]}", process_log)
        if not success:
            logging.getLogger('video_transcoder').error(
                f"{label} audio mux failed — video-only encode left for inspection: {temp_video}")
            return False
        try:
            temp_video.unlink()
        except Exception as e:
            logging.getLogger('video_transcoder').warning(f"Failed to delete {temp_video}: {e}")

    if not config.skip_validation:
        is_valid, err_msg = validate_output(output_path)
        if not is_valid:
            logging.getLogger('video_transcoder').error(f"{label} validation failed: {err_msg}")
            return False
    return True

def process_desktop_review_output(source_path: Path, output_paths: Dict[str, Path], info: VideoInfo,
                                  video_standard: VideoStandard, config: Config, process_log: Path,
                                  sd_stats_prefix: Path, hd_stats_prefix: Path) -> bool:
    """Desktop review derivatives: a curator/artist/writer viewing copy for a
    meeting or presentation, not a preservation/access derivative. Produces:
      _ds — 720x540 square-pixel full-frame SD (720:540 == 4:3 exactly, no pad)
      _dh — 1920x1080 pillarboxed HD upconversion from a 4:3 source
    Output frame rate is fixed to the detected standard's rate (29.97p NTSC /
    25p PAL) rather than derived from the source fps, same convention as the
    v210 NTSC/PAL policy split. Caller must have already confirmed
    video_standard is NTSC or PAL (not UNKNOWN)."""
    out_fps = 29.970 if video_standard == VideoStandard.NTSC else 25.0

    sd_w, sd_h = DESKTOP_REVIEW_SD_SIZE
    sd_vf = _desktop_review_vf_chain(info, config, sd_w, sd_h, pillarbox=False)
    if not _encode_desktop_review_variant(source_path, output_paths['desktop_sd'], info, config,
                                          process_log, sd_stats_prefix, sd_vf, out_fps,
                                          BITRATE_CONFIG['desktop_sd'], "Desktop SD"):
        return False

    hd_fit_w, hd_fit_h = DESKTOP_REVIEW_HD_FIT
    hd_vf = _desktop_review_vf_chain(info, config, hd_fit_w, hd_fit_h, pillarbox=True)
    if not _encode_desktop_review_variant(source_path, output_paths['desktop_hd'], info, config,
                                          process_log, hd_stats_prefix, hd_vf, out_fps,
                                          BITRATE_CONFIG['desktop_hd'], "Desktop HD"):
        return False
    return True

def generate_thumbnail_set(source_path: Path, output_dir: Path, thumbnail_prefix: str, info: VideoInfo, config: Config,
                           scale_string: str, parity: int, process_log: Path) -> bool:
    """Generate the full set of JP2 thumbnails for one source: 4 at standard
    positions by default, or --thumbs N at random positions."""
    if config.thumb_count is not None:
        num_thumbs = config.thumb_count
        positions = sorted([random.uniform(0.05, 0.95) for _ in range(num_thumbs)])
    else:
        num_thumbs = len(THUMBNAIL_POSITIONS)
        positions = THUMBNAIL_POSITIONS
    time_points = [info.duration * pos for pos in positions]
    for idx, timestamp in enumerate(time_points, 1):
        thumb_path = output_dir / f"{thumbnail_prefix}_thumb_{idx}.jp2"
        success = generate_thumbnail(source_path, thumb_path, timestamp, scale_string, idx, len(time_points), info.interlaced, parity, process_log)
        if not success:
            return False
    return True

def process_thumbnails_only(source_path: Path, info: VideoInfo, config: Config, process_log: Path, thumbnail_prefix: str) -> bool:
    """Thumbnail-only mode: same scaling, deinterlacing and field order as the
    thumbnails generated alongside -h264 output, but no video encode."""
    try:
        scale_string = calculate_scaling_params(info.width, info.height, info.dar, config.force_anamorphic)
        if info.interlaced:
            parity = {'tff': 0, 'bff': 1}.get(config.force_scan, -1)
        else:
            parity = -1
        with open(process_log, 'a') as log_f:
            log_f.write(f"Interlaced: {info.interlaced}\n")
        return generate_thumbnail_set(source_path, config.output_dir, thumbnail_prefix, info, config,
                                      scale_string, parity, process_log)
    except Exception as e:
        logging.getLogger('video_transcoder').error(f"Thumbnail processing error: {e}")
        return False

def clean_aperture_input_args(clean_aperture: bool) -> List[str]:
    """Input-side ffmpeg args controlling QuickTime clean aperture (clap) handling.

    Default (clean_aperture=False) sets the generic decoder option apply_cropping=0,
    disabling automatic frame cropping (from the clap atom's Frame Cropping side data)
    so the full coded frame is preserved — required for true lossless v210/FFV1
    transcodes. Passing clean_aperture=True omits this, leaving apply_cropping at its
    default (1/enabled), so ffmpeg applies the clap crop and produces display-cropped
    output instead.

    NOTE: an earlier version of this used `-flags2 +ignorecrop`, which does NOT
    affect this container-derived crop (it only applies to codec-level SPS
    conformance-window cropping) — confirmed via MediaInfo on real test files
    that ignorecrop left 720x486 sources cropped to 704x480 either way.
    """
    return [] if clean_aperture else ["-apply_cropping", "0"]

def handle_failed_lossless_output(output_path: Path, keep_failed: bool) -> None:
    """On lossless validation failure, either delete the output or rename it
    with a _VALIDATION_FAILED suffix for inspection, per --keep-failed."""
    logger = logging.getLogger('video_transcoder')
    if not output_path.exists():
        return
    if keep_failed:
        failed_path = output_path.with_name(f"{output_path.stem}_VALIDATION_FAILED{output_path.suffix}")
        try:
            output_path.rename(failed_path)
            logger.warning(f"Retained failed output as {failed_path.name} (--keep-failed)")
        except Exception as e:
            logger.error(f"Failed to rename failed output {output_path.name}: {e}")
    else:
        try:
            output_path.unlink()
        except Exception:
            pass

def process_v210_output(source_path: Path, output_path: Path, info: VideoInfo, video_standard: VideoStandard,
                        process_log: Path, log_dir: Path, keep_framemd5: bool, keep_mediaconch: bool,
                        clean_aperture: bool = False, keep_failed: bool = False) -> bool:
    logger = logging.getLogger('video_transcoder')
    try:
        setfield = "bff" if video_standard == VideoStandard.NTSC else "tff"
        # vrecord's configurable per-standard PAR (Config > Aspect Ratio tab);
        # 10/11 (NTSC) / 12/11 (PAL) are vrecord's own recommended defaults.
        setsar = "10/11" if video_standard == VideoStandard.NTSC else "12/11"
        # Primaries/matrix per standard — previously smpte170m (NTSC) was written
        # for PAL too. Transfer stays bt709, matching vrecord masters.
        color_std = "smpte170m" if video_standard == VideoStandard.NTSC else "bt470bg"
        cmd = ["ffmpeg", "-y"] + clean_aperture_input_args(clean_aperture) + ["-i", str(source_path),
               "-movflags", "write_colr", "-c:v", "v210",
               "-color_primaries", color_std, "-color_trc", "bt709", "-colorspace", color_std,
               "-color_range", "mpeg", "-metadata:s:v:0", "encoder=Uncompressed 10-bit 4:2:2",
               # setdar=4/3 was REMOVED here (2026-09-28 bug fix): it silently
               # recalculated and overrode setsar to whatever value produces an
               # exact 4:3 DAR for the frame's actual pixel dimensions (9/10 for
               # 720x486 NTSC, 16/15 for 720x576 PAL) — discarding the intended
               # vrecord-matching setsar value above on every v210 file this
               # code has ever produced. setsar alone fully determines display
               # geometry; DAR is derived by any player from SAR + coded
               # dimensions, never an independently muxed value. Confirmed via
               # ffprobe: setsar alone gives SAR 10:11 / DAR 400:297 (NTSC);
               # with setdar=4/3 it silently became SAR 9:10 / DAR 4:3 instead.
               "-vf", f"setfield={setfield},setsar={setsar}",
               # 0:a? — optional, so sources with no audio track don't fail the encode
               "-c:a", "pcm_s24le", "-map", "0:v", "-map", "0:a?", "-f", "mov", str(output_path)]
        success, _ = run_ffmpeg_with_progress(cmd, info.total_frames, f"v210: {source_path.name[:20]}", process_log)
        if not success:
            return False
        logger.info(f"Starting framemd5 lossless validation for {source_path.name}")
        is_valid, validation_msg = validate_v210_lossless(
            source_path, output_path, process_log, log_dir,
            keep_framemd5, keep_mediaconch, video_standard, clean_aperture, info.has_audio)
        if not is_valid:
            logger.error(f"v210 lossless validation FAILED: {validation_msg}")
            handle_failed_lossless_output(output_path, keep_failed)
            return False
        return True
    except Exception as e:
        logger.error(f"v210 processing error: {e}")
        return False

def prores_interlace_args(info: VideoInfo, force_scan: Optional[str] = None) -> List[str]:
    """ffmpeg args so interlaced sources become interlaced ProRes. Without
    +ildct, prores_ks encodes every frame progressive and flags it so in the
    ProRes frame header, even when the container is labelled TFF/BFF (found
    testing DdD TFF material, 2026-09-25). prores_ks takes field dominance from
    each frame's flags, so setfield pins it whenever vdg's decision didn't come
    from the stream itself: --force-scan, or the frame-sampling fallback (where
    some frames — e.g. a damaged tape head — carry no field flags at all)."""
    if not info.interlaced:
        return []
    args = ["-flags", "+ildct"]
    if force_scan in ('tff', 'bff'):
        order = force_scan
    elif info.field_order_source == 'frames' and info.field_order_detail in ('tff', 'bff'):
        order = info.field_order_detail
    else:
        order = None
    if order:
        args += ["-vf", f"setfield={order}"]
    return args

def process_prores_output(source_path: Path, output_path: Path, info: VideoInfo, process_log: Path,
                          force_scan: Optional[str] = None) -> bool:
    try:
        cmd = ["ffmpeg", "-y", "-i", str(source_path), "-codec:v", "prores_ks", "-profile:v", "3",
               "-vtag", "apch"] + prores_interlace_args(info, force_scan) + [
               "-metadata:s", "encoder=Apple ProRes 422 HQ", "-vendor", "apl0",
               "-codec:a", "copy", "-map", "0:v", "-map", "0:a?", str(output_path)]
        success, _ = run_ffmpeg_with_progress(cmd, info.total_frames, f"ProRes: {source_path.name[:20]}", process_log)
        return success
    except Exception as e:
        logging.getLogger('video_transcoder').error(f"ProRes processing error: {e}")
        return False

def validate_ffv1_lossless(source_path: Path, output_path: Path, process_log: Path,
                           log_dir: Path, keep_framemd5: bool, keep_mediaconch: bool,
                           clean_aperture: bool = False, has_audio: bool = True) -> Tuple[bool, str]:
    """Framemd5 + audio streamhash validation for FFV1 output, with digest logging,
    optional framemd5 file retention, and MediaConch policy check."""
    logger = logging.getLogger('video_transcoder')
    try:
        temp_dir = output_path.parent / "temp_framemd5"
        temp_dir.mkdir(exist_ok=True)
        source_video_md5 = temp_dir / f"{source_path.stem}_source_video.framemd5"
        output_video_md5 = temp_dir / f"{output_path.stem}_output_video.framemd5"
        logger.info(f"Validating FFV1 lossless conversion for {source_path.name}...")
        with open(process_log, 'a') as log_f:
            log_f.write("\n" + "=" * 70 + "\n" + "FRAMEMD5 LOSSLESS VALIDATION (FFV1)\n" + "=" * 70 + "\n\n")

        # Source hash must use the same clean-aperture handling as the encode
        # command, or a divergence there (not an actual encoding problem) will
        # show up as a framemd5 mismatch.
        logger.info("  → Generating framemd5 for source video stream...")
        cmd_source_video = ['ffmpeg'] + clean_aperture_input_args(clean_aperture) + ['-i', str(source_path), '-map', '0:v:0', '-f', 'framemd5', str(source_video_md5)]
        success, _ = run_validation_command_with_spinner(cmd_source_video, "Hashing source video", process_log)
        if not success:
            return False, "Failed to generate source video framemd5"

        logger.info("  → Generating framemd5 for output video stream...")
        cmd_output_video = ['ffmpeg', '-i', str(output_path), '-map', '0:v:0', '-f', 'framemd5', str(output_video_md5)]
        success, _ = run_validation_command_with_spinner(cmd_output_video, "Hashing output video", process_log)
        if not success:
            return False, "Failed to generate output video framemd5"

        logger.info("  → Comparing video framemd5 checksums...")
        with open(source_video_md5, 'r') as f:
            source_video_hashes = [line.strip() for line in f if not line.startswith('#')]
        with open(output_video_md5, 'r') as f:
            output_video_hashes = [line.strip() for line in f if not line.startswith('#')]

        mismatches = [(i, s, o) for i, (s, o) in enumerate(zip(source_video_hashes, output_video_hashes)) if s != o]
        frame_count = len(source_video_hashes)

        if len(source_video_hashes) != len(output_video_hashes) or mismatches:
            mismatch_msg = (f"Video framemd5 mismatch: {len(source_video_hashes)} source frames "
                           f"vs {len(output_video_hashes)} output frames, {len(mismatches)} differing")
            with open(process_log, 'a') as log_f:
                log_f.write(f"ERROR: {mismatch_msg}\n")
                for i, src, out in mismatches:
                    log_f.write(f"  Frame {i}:\n    Source: {src}\n    Output: {out}\n")
            return False, mismatch_msg

        logger.info(f"  ✓ Video validation passed: {frame_count} frames match")

        if not has_audio:
            logger.info("  → No audio stream in source — video-only validation")
        else:
            logger.info("  → Generating hash for source audio stream(s)...")
        cmd_source_audio = ['ffmpeg', '-i', str(source_path), '-map', '0:a', '-f', 'streamhash', '-hash', 'md5', '-']
        if has_audio:
            success, source_audio_hash = run_validation_command_with_spinner(cmd_source_audio, "Hashing source audio", process_log)
        else:
            success, source_audio_hash = False, ""
        audio_passed = False
        source_audio_hash = source_audio_hash.strip() if success else ""
        output_audio_hash = ""
        if not success:
            with open(process_log, 'a') as log_f:
                log_f.write("Audio validation skipped: no audio stream in source\n" if not has_audio else
                            "Audio validation skipped: no audio stream or failed to hash source\n")
        else:
            cmd_output_audio = ['ffmpeg', '-i', str(output_path), '-map', '0:a', '-f', 'streamhash', '-hash', 'md5', '-']
            success, output_audio_hash = run_validation_command_with_spinner(cmd_output_audio, "Hashing output audio", process_log)
            output_audio_hash = output_audio_hash.strip()
            if not success:
                return False, "Failed to generate output audio hash"
            if source_audio_hash != output_audio_hash:
                mismatch_msg = f"Audio streamhash mismatch:\nSource: {source_audio_hash}\nOutput: {output_audio_hash}"
                with open(process_log, 'a') as log_f:
                    log_f.write(f"ERROR: {mismatch_msg}\n")
                return False, mismatch_msg
            audio_passed = True
            logger.info("  ✓ Audio validation passed: stream hashes match")

        # Always write digest to process log
        with open(process_log, 'a') as log_f:
            log_f.write("\nFRAMEMD5 DIGEST\n" + "-" * 40 + "\n")
            log_f.write(f"Frames compared:    {frame_count}\n")
            log_f.write(f"Video result:       PASS — all {frame_count} frames match\n")
            if not has_audio:
                log_f.write("Audio result:       N/A — no audio stream in source\n")
            if audio_passed:
                log_f.write(f"Audio source hash:  {source_audio_hash}\n")
                log_f.write(f"Audio output hash:  {output_audio_hash}\n")
                log_f.write(f"Audio result:       PASS\n")
            log_f.write("-" * 40 + "\n")
            log_f.write("\n" + "=" * 70 + "\nLOSSLESS VALIDATION: PASSED\n" + "=" * 70 + "\n\n")

        # Retain or delete framemd5 files
        if keep_framemd5:
            for md5_file in [source_video_md5, output_video_md5]:
                dest = log_dir / md5_file.name
                md5_file.rename(dest)
                logger.info(f"  → framemd5 retained: {dest.name}")
        else:
            for md5_file in [source_video_md5, output_video_md5]:
                try:
                    md5_file.unlink()
                except Exception:
                    pass
        try:
            temp_dir.rmdir()
        except Exception:
            pass

        logger.info(f"  ✓ FFV1 lossless validation PASSED for {source_path.name}")

        # MediaConch policy check
        mc_passed, mc_msg = run_mediaconch_check(
            output_path, POLICY_FFV1, "policy_ffv1.xml",
            log_dir, process_log, keep_mediaconch, has_audio)
        if not mc_passed:
            return False, f"MediaConch policy check failed: {mc_msg}"

        return True, "Validation passed"
    except subprocess.TimeoutExpired:
        return False, "Validation timeout"
    except Exception as e:
        logger.error(f"Validation error: {e}")
        return False, f"Validation error: {e}"

def process_ffv1_output(source_path: Path, output_path: Path, info: VideoInfo,
                        process_log: Path, log_dir: Path, keep_framemd5: bool, keep_mediaconch: bool,
                        clean_aperture: bool = False, keep_failed: bool = False) -> bool:
    """Encode source to FFV1 v3 in MKV with lossless framemd5 + audio hash validation.

    FFV1 parameters:
      -level 3      FFV1 version 3 — multithreading, per-slice CRCs, accepted by
                    most digital preservation repositories.
      -g 1          Keyframe every frame. Required for random access and error
                    recovery in archival use.
      -slices 16    Slice-based multithreading. 16 slices is appropriate for
                    Apple Silicon and modern x86.
      -slicecrc 1   Embeds a CRC in every slice header for per-slice error detection.
      Audio is copied without re-encoding to preserve the original PCM stream exactly.
      When the source is a genuine QuickTime file (major_brand 'qt  '), vendor_id
      is overridden to "Apple QuickTime" — without this, ffmpeg carries the source's
      own per-stream vendor_id tag straight through (observed as "KeyG" on this lab's
      QuickTime sources, a leftover from the original capture chain rather than
      anything meaningful about authorship). Left untouched for non-QuickTime
      sources (MXF, MPEG, etc.), where "Apple QuickTime" would be actively wrong.
    """
    logger = logging.getLogger('video_transcoder')
    try:
        vendor_metadata = ["-metadata:s:v:0", "vendor_id=Apple QuickTime"] if info.is_quicktime else []
        cmd = [
            "ffmpeg", "-y"] + clean_aperture_input_args(clean_aperture) + [
            "-i", str(source_path),
            "-map", "0:v", "-map", "0:a?",  # optional — sources with no audio track
            "-c:v", "ffv1", "-level", "3", "-g", "1", "-slices", "16", "-slicecrc", "1",
        ] + vendor_metadata + [
            "-c:a", "copy",
            str(output_path)
        ]
        success, _ = run_ffmpeg_with_progress(cmd, info.total_frames, f"FFV1: {source_path.name[:20]}", process_log)
        if not success:
            return False
        logger.info(f"Starting framemd5 lossless validation for {source_path.name}")
        is_valid, validation_msg = validate_ffv1_lossless(
            source_path, output_path, process_log, log_dir, keep_framemd5, keep_mediaconch, clean_aperture,
            info.has_audio)
        if not is_valid:
            logger.error(f"FFV1 lossless validation FAILED: {validation_msg}")
            handle_failed_lossless_output(output_path, keep_failed)
            return False
        return True
    except Exception as e:
        logger.error(f"FFV1 processing error: {e}")
        return False

ROLE_CODES = ('_pm', '_sh', '_sl', '_ds', '_dh')

def sanitize_filename(name: str) -> str:
    """Replace spaces with underscores in a filename stem."""
    return name.replace(' ', '_')

def strip_role_code(stem: str) -> str:
    """Strip a 3-character role code suffix (_pm, _sh, _sl) if present.
    If no recognized role code is found, return the stem unchanged."""
    for code in ROLE_CODES:
        if stem.endswith(code):
            return stem[:-3]
    return stem

def process_single_video(source_path: Path, config: Config, completed_set: Set[str], filename_disambiguation: Dict[str, str] = None) -> ProcessingResult:
    logger = logging.getLogger('video_transcoder')
    base_name, root_name = source_path.name, sanitize_filename(source_path.stem)
    base_stem = strip_role_code(root_name)
    disambig_suffix = (filename_disambiguation or {}).get(base_name)
    if disambig_suffix:
        # Same unique ID with multiple role codes (e.g. _pm.mov + _sh.mp4) would
        # otherwise collide on the same output filename — disambiguate with the
        # source extension, or role_code + extension if extension alone isn't
        # unique within the group (e.g. _pm.mov + _sh.mov).
        base_stem = f"{base_stem}_{disambig_suffix}"
    output_paths = {}
    if config.output_h264:
        output_paths['h264'] = config.output_dir / f"{base_stem}_sl.mp4"
    else:
        output_paths['h264'] = None
    if config.output_v210:
        output_paths['v210'] = config.output_dir / f"{root_name}.mov"
    else:
        output_paths['v210'] = None
    if config.output_prores:
        output_paths['prores'] = config.output_dir / f"{base_stem}_sh.mov"
    else:
        output_paths['prores'] = None
    if config.output_ffv1:
        output_paths['ffv1'] = config.output_dir / f"{base_stem}_pm.mkv"
    else:
        output_paths['ffv1'] = None
    if config.output_desktop_review:
        output_paths['desktop_sd'] = config.output_dir / f"{base_stem}_ds.mp4"
        output_paths['desktop_hd'] = config.output_dir / f"{base_stem}_dh.mp4"
    else:
        output_paths['desktop_sd'] = None
        output_paths['desktop_hd'] = None
    
    thumbnail_prefix = base_stem if (config.output_h264 or config.thumbs_only) else None
    num_thumbs = config.thumb_count if config.thumb_count is not None else len(THUMBNAIL_POSITIONS)
    derivatives_exist = verify_derivatives_exist(output_paths, thumbnail_prefix if thumbnail_prefix else "", num_thumbs)
    # Thumbnail-only runs always regenerate — they're meant to replace thumbnails
    # for files that have already been processed.
    if base_name in completed_set and derivatives_exist and not config.thumbs_only:
        return ProcessingResult(base_name, ProcessStatus.SKIPPED, "N/A", "Already processed", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    
    process_log = config.log_dir / f"{root_name}_process.log"
    stats_log_prefix = config.log_dir / f"stats_{root_name}"
    desktop_sd_stats_prefix = config.log_dir / f"stats_{root_name}_ds"
    desktop_hd_stats_prefix = config.log_dir / f"stats_{root_name}_dh"
    audio_status = "Unknown"
    
    try:
        # warn=False: get_file_info_for_display() already probed this file and
        # logged any dimension/clean-aperture warnings once during the initial
        # file listing — avoid printing the same warning a second time here.
        info = get_video_info(source_path, warn=False)
        audio_status = "Stereo" if info.has_audio else "No Audio"

        if config.output_v210 or config.output_ffv1:
            # The whole-file average check (info.is_vfr) catches gross rate
            # divergence; the packet-duration check catches a handful of
            # held/duplicated frames scattered through an otherwise ~CFR-average
            # file, which the average alone can miss.
            vfr_detected = info.is_vfr or detect_variable_packet_durations(source_path)
            if vfr_detected:
                logger.warning(f"Variable frame rate detected in {base_name} — quarantining for review "
                               f"(lossless roundtrip validation is unreliable on VFR sources)")
                with open(process_log, 'w') as log_f:
                    log_f.write(SCRIPT_TITLE + "\n" + SCRIPT_NAME + "\n" + SCRIPT_SEPARATOR + "\n" + SCRIPT_SEPARATOR + "\n\n")
                    log_f.write(f"Processing: {base_name}\n")
                    log_f.write("QUARANTINED: variable frame rate detected in source video stream — "
                                "transcode skipped, moved to QUARANTINE for investigation\n")
                if not config.dry_run:
                    shutil.move(str(source_path), str(config.quarantine_dir / base_name))
                return ProcessingResult(base_name, ProcessStatus.QUARANTINED, audio_status,
                                        "Quarantined: variable frame rate detected in source",
                                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

        # Apply FPS override if specified (before video standard detection)
        if config.force_fps is not None:
            logger.info(f"FPS override: {info.fps:.3f} -> {config.force_fps:.3f}")
            info.fps = config.force_fps
            info.total_frames = int(info.duration * info.fps)
        
        video_standard = detect_video_standard(info.width, info.height, info.fps)
        
        # Apply scan type override if specified
        if config.force_scan == 'progressive':
            info.interlaced = False
            logger.info(f"Scan override: forcing progressive (ignoring detected field order)")
        elif config.force_scan in ('tff', 'bff'):
            info.interlaced = True
            logger.info(f"Scan override: forcing {config.force_scan.upper()} field dominance")
        
        # Log interlacing status to terminal
        interlace_status = "interlaced" if info.interlaced else "progressive"
        if config.force_scan is None:
            if info.field_order_source == 'frames':
                interlace_status += (f", {info.field_order_detail.upper()}" if info.interlaced else "") + \
                    " — from frame metadata sampled across the file, no stream field order"
            elif info.field_order_source == 'none':
                interlace_status += " — assumed, no field order metadata"
                logger.warning(f"{base_name}: no field order in stream or frame metadata — treating as "
                               f"progressive. Use --force-scan tff/bff if the source is interlaced")
        logger.info(f"Source: {info.width}x{info.height} @ {info.fps:.2f}fps ({interlace_status})")
        mediainfo_summary = get_mediainfo_summary(source_path)
        if mediainfo_summary:
            logger.info(f"MediaInfo — {base_name}\n" + "\n".join(mediainfo_summary) + "\n")
            mi_scan = scan_type_disagreement(mediainfo_summary, info.interlaced) if config.force_scan is None else None
            if mi_scan:
                logger.warning(f"{base_name}: scan type disagreement — vdg is treating the source as "
                               f"{'interlaced' if info.interlaced else 'progressive'}, but MediaInfo reports {mi_scan}. "
                               f"Check the output, or rerun with --force-scan (tff/bff/progressive)")
        else:
            logger.warning(f"MediaInfo summary unavailable for {base_name}")
        
        # Thumbnail-only runs append, so the original transcode's process log
        # (commands, framemd5 digests, validation results) is never overwritten.
        with open(process_log, 'a' if config.thumbs_only else 'w') as log_f:
            if config.thumbs_only:
                log_f.write(f"\n{SCRIPT_SEPARATOR}\nTHUMBNAIL-ONLY RUN — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            log_f.write(SCRIPT_TITLE + "\n" + SCRIPT_NAME + "\n" + SCRIPT_SEPARATOR + "\n" + SCRIPT_SEPARATOR + "\n\n")
            log_f.write(f"Processing: {base_name}\nSource: {info.width}x{info.height} @ {info.fps:.2f}fps\n")
            log_f.write(f"Scan: {interlace_status}\n")
            if info.fps_note:
                log_f.write(f"Frame rate: {info.fps_note}\n")
            if mediainfo_summary:
                log_f.write("\nMediaInfo:\n" + "\n".join(mediainfo_summary) + "\n\n")
            log_f.write(f"Video Standard: {video_standard.value}\nOutput Formats: {'thumbnails only' if config.thumbs_only else ', '.join([k for k, v in output_paths.items() if v])}\n\n")
        
        if config.thumbs_only:
            logger.info(f"Generating {config.thumb_count} thumbnail(s) for {base_name}")
            if not process_thumbnails_only(source_path, info, config, process_log, thumbnail_prefix):
                raise Exception("Thumbnail generation failed")
            return ProcessingResult(base_name, ProcessStatus.SUCCESS, audio_status,
                                    f"Thumbnails generated ({config.thumb_count})",
                                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        if config.output_h264:
            logger.info(f"Encoding H.264 for {base_name}")
            if not process_h264_output(source_path, output_paths['h264'], info, config, process_log, stats_log_prefix, thumbnail_prefix):
                raise Exception("H.264 encoding failed")
        if config.output_v210:
            logger.info(f"Encoding v210 for {base_name}")
            if video_standard == VideoStandard.UNKNOWN:
                raise Exception("Cannot create v210 output: video is not NTSC or PAL standard")
            if not process_v210_output(source_path, output_paths['v210'], info, video_standard,
                                       process_log, config.log_dir, config.keep_framemd5, config.keep_mediaconch,
                                       config.clean_aperture, config.keep_failed):
                raise Exception("v210 transcode or validation failed")
        if config.output_prores:
            logger.info(f"Encoding ProRes for {base_name}")
            if not process_prores_output(source_path, output_paths['prores'], info, process_log, config.force_scan):
                raise Exception("ProRes encoding failed")
        if config.output_ffv1:
            logger.info(f"Encoding FFV1/MKV for {base_name}")
            if not process_ffv1_output(source_path, output_paths['ffv1'], info,
                                       process_log, config.log_dir, config.keep_framemd5, config.keep_mediaconch,
                                       config.clean_aperture, config.keep_failed):
                raise Exception("FFV1 transcode or validation failed")
        if config.output_desktop_review:
            logger.info(f"Encoding desktop review derivatives for {base_name}")
            if video_standard == VideoStandard.UNKNOWN:
                raise Exception("Cannot create desktop review output: video is not NTSC or PAL SD 4:3 standard")
            if not process_desktop_review_output(source_path, output_paths, info, video_standard, config,
                                                 process_log, desktop_sd_stats_prefix, desktop_hd_stats_prefix):
                raise Exception("Desktop review encoding failed")
        
        if config.move_finished and not config.dry_run:
            shutil.move(str(source_path), str(config.finished_dir / base_name))
        formats_created = [k for k, v in output_paths.items() if v]
        return ProcessingResult(base_name, ProcessStatus.SUCCESS, audio_status, f"Completed successfully ({', '.join(formats_created)})", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    except Exception as e:
        logger.error(f"Error processing {base_name}: {e}")
        for output_path in output_paths.values():
            if output_path and output_path.exists():
                try:
                    output_path.unlink()
                except Exception:
                    pass
        if config.output_h264 and thumbnail_prefix:
            for i in range(1, num_thumbs + 1):
                thumb_path = config.output_dir / f"{thumbnail_prefix}_thumb_{i}.jp2"
                if thumb_path.exists():
                    try:
                        thumb_path.unlink()
                    except Exception:
                        pass
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if config.output_v210 or config.output_ffv1:
            # Lossless transcode failed for any reason — quarantine the source
            # so it's visually segregated from untried files for review.
            if not config.dry_run:
                try:
                    if source_path.exists():
                        shutil.move(str(source_path), str(config.quarantine_dir / base_name))
                        logger.warning(f"Quarantined {base_name} for review after failure")
                except Exception as move_err:
                    logger.error(f"Failed to quarantine {base_name}: {move_err}")
            return ProcessingResult(base_name, ProcessStatus.QUARANTINED, audio_status, str(e), timestamp)
        return ProcessingResult(base_name, ProcessStatus.ERROR, audio_status, str(e), timestamp)
    finally:
        if config.output_h264:
            cleanup_temp_files(stats_log_prefix)
        if config.output_desktop_review:
            cleanup_temp_files(desktop_sd_stats_prefix)
            cleanup_temp_files(desktop_hd_stats_prefix)

def collect_video_files(source_dir: Path, finished_dir: Path) -> List[Path]:
    files = []
    for root, _, filenames in os.walk(source_dir):
        root_path = Path(root)
        if root_path.resolve() == finished_dir.resolve():
            continue
        for filename in filenames:
            if filename.startswith('.'):
                # Skip hidden files, including macOS AppleDouble resource-fork
                # sidecars (._foo.mov) that appear on non-native filesystems —
                # these match VIDEO_EXTENSIONS but aren't real media.
                continue
            if filename.lower().endswith(VIDEO_EXTENSIONS):
                files.append(root_path / filename)
    return sorted(files)

def print_file_list(files: List[Path], file_index_map: Dict[str, int] = None):
    print("\n" + "=" * 100 + f"\nFILES FOUND ({len(files)} total):\n" + "-" * 100)
    total_size, total_duration = 0, 0.0
    print(f"{'#':>4}  {'Filename':<50}  {'Size':>12}  {'Duration':>10}\n" + "-" * 100)
    for i, file_path in enumerate(files, 1):
        status = ""
        if file_index_map and file_path.name in file_index_map:
            status = " ✓"
        size, duration = get_file_info_for_display(file_path)
        total_size += size
        total_duration += duration
        display_name = file_path.name if len(file_path.name) <= 50 else file_path.name[:47] + "..."
        size_str = format_file_size(size) if size > 0 else "N/A"
        duration_str = format_duration(duration) if duration > 0 else "N/A"
        print(f"{i:4}. {display_name:<50}  {size_str:>12}  {duration_str:>10}{status}")
    print("-" * 100 + f"\n{'TOTAL:':<56}  {format_file_size(total_size):>12}  {format_duration(total_duration):>10}\n" + "=" * 100 + "\n")

def print_live_status(files: List[Path], session_completed: Set[str], stats: ProcessingStats, session_quarantined: Set[str] = None):
    session_quarantined = session_quarantined or set()
    status_lines = ["\n" + "=" * 70, f"STATUS UPDATE ({len(session_completed) + len(session_quarantined)}/{len(files)} completed)", "-" * 70]
    for i, file_path in enumerate(files):
        if file_path.name in session_quarantined:
            status_icon = f"{Colors.YELLOW}Q{Colors.RESET}"
        elif file_path.name in session_completed:
            status_icon = f"{Colors.GREEN}✓{Colors.RESET}"
        else:
            status_icon = "⋯"
        status_lines.append(f"{i+1:4}. {status_icon} {file_path.name}")
    status_lines.append("-" * 70)
    success_str = f"{Colors.GREEN}Success: {stats.success}{Colors.RESET}"
    skipped_str = f"{Colors.YELLOW}Skipped: {stats.skipped}{Colors.RESET}" if stats.skipped > 0 else f"Skipped: {stats.skipped}"
    quarantined_str = f"{Colors.YELLOW}Quarantined: {stats.quarantined}{Colors.RESET}" if stats.quarantined > 0 else f"Quarantined: {stats.quarantined}"
    error_str = f"{Colors.RED}Errors: {stats.error}{Colors.RESET}" if stats.error > 0 else f"Errors: {stats.error}"
    status_lines.append(f"{success_str} | {skipped_str} | {quarantined_str} | {error_str}")
    status_lines.append("=" * 70 + "\n")
    for line in status_lines:
        console_write(line)

def print_summary(stats: ProcessingStats, start_time: datetime):
    logger = logging.getLogger('video_transcoder')
    duration = datetime.now() - start_time
    print("\n" + "=" * 70 + f"\n{Colors.BOLD}PROCESSING SUMMARY{Colors.RESET}\n" + "=" * 70)
    print(f"Total files:      {stats.total}\n{Colors.GREEN}Successful:       {stats.success}{Colors.RESET}")
    skipped_line = f"{Colors.YELLOW}Skipped:          {stats.skipped}{Colors.RESET}" if stats.skipped > 0 else f"Skipped:          {stats.skipped}"
    quarantined_line = f"{Colors.YELLOW}Quarantined:      {stats.quarantined}{Colors.RESET}" if stats.quarantined > 0 else f"Quarantined:      {stats.quarantined}"
    error_line = f"{Colors.RED}Errors:           {stats.error}{Colors.RESET}" if stats.error > 0 else f"Errors:           {stats.error}"
    print(f"{skipped_line}\n{quarantined_line}\n{error_line}")
    print(f"Processing time:  {duration}\n" + "=" * 70)
    if stats.quarantined_files:
        print(f"\n{Colors.YELLOW}QUARANTINED FILES:{Colors.RESET}")
        for quarantined in stats.quarantined_files:
            print(f"  {Colors.YELLOW}Q{Colors.RESET} {quarantined}")
        print()
    if stats.failed_files:
        print(f"\n{Colors.RED}FAILED FILES:{Colors.RESET}")
        for failed in stats.failed_files:
            print(f"  {Colors.RED}✗{Colors.RESET} {failed}")
        print()
    # Same summary for the session log only — the terminal already has the
    # colored version printed above.
    file_only = {'file_only': True}
    logger.info("=" * 70 + "\nPROCESSING SUMMARY\n" + "=" * 70, extra=file_only)
    logger.info(f"Total files:      {stats.total}\nSuccessful:       {stats.success}\nSkipped:          {stats.skipped}\nQuarantined:      {stats.quarantined}\nErrors:           {stats.error}\nProcessing time:  {duration}\n" + "=" * 70, extra=file_only)
    if stats.quarantined_files:
        logger.warning("QUARANTINED FILES:", extra=file_only)
        for quarantined in stats.quarantined_files:
            logger.warning(f"  - {quarantined}", extra=file_only)
    if stats.failed_files:
        logger.error("FAILED FILES:", extra=file_only)
        for failed in stats.failed_files:
            logger.error(f"  - {failed}", extra=file_only)
    if stats.error == 0 and stats.quarantined == 0:
        logger.info("JOB COMPLETED SUCCESSFULLY - All files processed without errors")
    elif stats.error == 0:
        logger.warning(f"JOB COMPLETED WITH QUARANTINED FILES - {stats.success} succeeded, "
                       f"{stats.quarantined} quarantined for review")
    elif stats.success > 0:
        logger.warning(f"JOB COMPLETED WITH ERRORS - {stats.success} succeeded, {stats.error} failed")
    else:
        logger.error("JOB FAILED - No files were successfully processed")

def _original_role_code(stem: str) -> str:
    """Return the role code (without leading underscore) a stem ends with, or ''."""
    for code in ROLE_CODES:
        if stem.endswith(code):
            return code.lstrip('_')
    return ""

def compute_filename_disambiguation(files: List[Path]) -> Dict[str, str]:
    """Map each source filename to a disambiguating suffix to append to its
    base_stem, for files that share a base_stem (post role-code-strip) with
    another file — e.g. an _pm.mov and an _sh.mp4 for the same unique ID.

    Tries the source extension alone first (matches the common case: two
    different container formats for the same ID). If the extension alone
    isn't unique within the group — e.g. an _pm.mov and an _sh.mov sharing
    the same container — falls back to role_code + extension instead, which
    is guaranteed unique as long as the source files themselves aren't exact
    duplicates.
    """
    stem_groups: Dict[str, List[Path]] = {}
    for f in files:
        stem = strip_role_code(sanitize_filename(f.stem))
        stem_groups.setdefault(stem, []).append(f)

    disambiguation: Dict[str, str] = {}
    for stem, group in stem_groups.items():
        if len(group) < 2:
            continue
        ext_tags = [f.suffix.lstrip('.').lower() for f in group]
        if len(set(ext_tags)) == len(group):
            for f, ext in zip(group, ext_tags):
                disambiguation[f.name] = ext
        else:
            for f, ext in zip(group, ext_tags):
                role_code = _original_role_code(sanitize_filename(f.stem))
                disambiguation[f.name] = f"{role_code}_{ext}" if role_code else ext
    return disambiguation

def process_batch(config: Config) -> ProcessingStats:
    logger = logging.getLogger('video_transcoder')
    stats, start_time = ProcessingStats(), datetime.now()
    files = collect_video_files(config.source_dir, config.finished_dir)
    stats.total = len(files)
    if not files:
        logger.warning("No video files found to process")
        return stats
    print_file_list(files)
    completed_set = get_completed_files(config.csv_log)
    logger.info(f"Found {len(completed_set)} previously completed files")
    filename_disambiguation = compute_filename_disambiguation(files)
    if filename_disambiguation:
        affected_ids = len({strip_role_code(sanitize_filename(Path(name).stem)) for name in filename_disambiguation})
        logger.warning(f"Found {affected_ids} unique ID(s) with multiple role codes — "
                       f"disambiguating output filenames")
    session_completed, session_quarantined = set(), set()
    mode = "CLEANUP" if config.cleanup_only else "PROCESSING"
    print(f"--- Starting {mode} MODE ---\n")

    if config.workers > 1 and not config.cleanup_only:
        with ProcessPoolExecutor(max_workers=config.workers) as executor:
            futures = {executor.submit(process_single_video, f, config, completed_set, filename_disambiguation): f for f in files}
            with tqdm(total=len(files), unit="file", desc="Total Progress", position=0) as pbar:
                for future in as_completed(futures):
                    result = future.result()
                    if not config.thumbs_only:
                        log_to_csv(config.csv_log, result, config.dry_run)
                    if result.status == ProcessStatus.SUCCESS:
                        stats.success += 1
                        session_completed.add(result.source_file)
                    elif result.status == ProcessStatus.SKIPPED:
                        stats.skipped += 1
                        session_completed.add(result.source_file)
                    elif result.status == ProcessStatus.QUARANTINED:
                        stats.quarantined += 1
                        stats.quarantined_files.append(f"{result.source_file}: {result.message}")
                        session_quarantined.add(result.source_file)
                    else:
                        stats.error += 1
                        stats.failed_files.append(f"{result.source_file}: {result.message}")
                    pbar.set_postfix_str(f"✓ {len(session_completed)}/{len(files)} | ✗ {stats.error}")
                    pbar.update(1)
                    if len(session_completed) % 1 == 0 or result.status in (ProcessStatus.ERROR, ProcessStatus.QUARANTINED):
                        print_live_status(files, session_completed, stats, session_quarantined)
    else:
        with tqdm(total=len(files), unit="file", desc="Total Progress", position=0) as pbar:
            for file_path in files:
                result = process_single_video(file_path, config, completed_set, filename_disambiguation)
                if not config.thumbs_only:
                    log_to_csv(config.csv_log, result, config.dry_run)
                if result.status == ProcessStatus.SUCCESS:
                    stats.success += 1
                    session_completed.add(result.source_file)
                elif result.status == ProcessStatus.SKIPPED:
                    stats.skipped += 1
                    session_completed.add(result.source_file)
                elif result.status == ProcessStatus.QUARANTINED:
                    stats.quarantined += 1
                    stats.quarantined_files.append(f"{result.source_file}: {result.message}")
                    session_quarantined.add(result.source_file)
                else:
                    stats.error += 1
                    stats.failed_files.append(f"{result.source_file}: {result.message}")
                pbar.set_postfix_str(f"✓ {len(session_completed)}/{len(files)} | ✗ {stats.error}")
                pbar.update(1)
                print_live_status(files, session_completed, stats, session_quarantined)
    print_summary(stats, start_time)
    return stats

def parse_arguments() -> argparse.Namespace:
    class _HelpFormatter(argparse.ArgumentDefaultsHelpFormatter, argparse.RawDescriptionHelpFormatter):
        """Keeps the multi-line banner in --description intact while still showing
        per-argument default values (the two base formatters don't conflict)."""
        pass

    parser = argparse.ArgumentParser(
        description=f"{SCRIPT_TITLE}\n{SCRIPT_NAME}\n{SCRIPT_SEPARATOR}\n\nVideo Transcoding and Archival Pipeline",
        formatter_class=_HelpFormatter)
    parser.add_argument('--version', action='version', version=SCRIPT_NAME)
    parser.add_argument('--source-dir', type=str, default='/Users/mangelet/Desktop/In_Progress/1/source', help='Source directory containing video files')
    parser.add_argument('--output-dir', type=str, default='/Users/mangelet/Desktop/In_Progress/1/output', help='Output directory for processed files')
    parser.add_argument('--workers', type=int, default=1, help='Number of parallel workers (1 = sequential)')
    parser.add_argument('--cleanup-only', action='store_true', help='Only cleanup temporary files, do not process')
    parser.add_argument('--move-finished', action='store_true', default=True, help='Move source files to finished_sources after processing')
    parser.add_argument('--no-move-finished', dest='move_finished', action='store_false', help='Do not move source files after processing')
    parser.add_argument('--dry-run', action='store_true', help='Simulate processing without making changes')
    parser.add_argument('--skip-validation', action='store_true', help='Skip output file validation (faster but less safe)')
    parser.add_argument('--keep-framemd5', action='store_true',
                        help='Retain framemd5 files in process_logs/ after lossless validation instead of deleting them. '
                             'Applies to both -ffv1 and -v210 output. A digest summary is always written to the process '
                             'log regardless of this flag.')
    parser.add_argument('--keep-mediaconch', action='store_true',
                        help='Retain the MediaConch policy XML written to process_logs/ after conformance checks '
                             'instead of deleting it. One policy file per format per session.')
    parser.add_argument('--force-scan', type=str, choices=['progressive', 'tff', 'bff'], default=None,
                        help='Override scan type detection: progressive (skip deinterlace), tff (top field first), bff (bottom field first)')
    parser.add_argument('--force-fps', type=float, default=None,
                        help='Override detected frame rate (e.g. 29.97). Use when ffprobe misreads FPS from the container')
    parser.add_argument('--force-anamorphic', action='store_true',
                        help='Force 854x480 (16:9) scaling for SD sources mistagged as 4:3 despite being '
                             'anamorphic squeezed footage. Overrides detected DAR for H.264 scaling/thumbnails.')
    parser.add_argument('--clean-aperture', action='store_true',
                        help='Honor QuickTime clean aperture (clap) atom cropping during v210/FFV1 transcodes '
                             'instead of preserving the full coded frame. Default is off (coded dimensions, '
                             'full sample data) — only enable if you specifically want display-cropped output.')
    parser.add_argument('--keep-failed', action='store_true',
                        help='Retain v210/FFV1 output files that fail lossless validation (framemd5/streamhash/'
                             'MediaConch) instead of deleting them, renamed with a _VALIDATION_FAILED suffix for '
                             'inspection. Default is to delete failed output.')
    parser.add_argument('--thumbs', type=int, default=None,
                        help='Number of thumbnails to generate (default: 4 at standard positions, override uses random positions). '
                             'Pass --thumbs N with no format flag to generate thumbnails only (no video derivative).')
    parser.add_argument('--trim-in', type=str, default=None, metavar='TIMESTAMP',
                        help='Start point for --trim-out mode (HH:MM:SS[.ms] or seconds). Requires --trim-out and '
                             'no format flag: a standalone stream-copy trim of exactly one source file in '
                             '--source-dir, for recovering a digitization session that ran long (e.g. trailing '
                             'snow/black). Writes <stem>_trimmed<ext> to --output-dir; the source is never moved '
                             'or modified.')
    parser.add_argument('--trim-out', type=str, default=None, metavar='TIMESTAMP',
                        help='End point for --trim-in mode (HH:MM:SS[.ms] or seconds). See --trim-in.')
    parser.add_argument('-h264', action='store_true', help='Generate H.264 MP4 output with thumbnails (_sl.mp4)')
    parser.add_argument('-v210', action='store_true', help='Generate v210 uncompressed 10-bit 4:2:2 QuickTime output (.mov)')
    parser.add_argument('-prores', action='store_true', help='Generate ProRes 422 HQ QuickTime output (_sh.mov)')
    parser.add_argument('-ffv1', action='store_true', help='Generate FFV1 v3 lossless MKV output (_pm.mkv) with framemd5 validation')
    parser.add_argument('-desktop-review', action='store_true',
                        help='Generate desktop review derivatives for curator/artist/writer viewing copies: '
                             '720x540 square-pixel SD (_ds.mp4) and 1920x1080 pillarboxed HD upconversion (_dh.mp4). '
                             'Requires an NTSC or PAL SD 4:3 source.')
    audio_group = parser.add_argument_group('Audio Configuration (H.264 only)')
    audio_group.add_argument('--audio-stream', type=str, default='0:a:0', help='Audio stream to use (default: 0:a:0). Examples: 0:a:0, 0:a:1')
    audio_group.add_argument('--audio-mode', type=str, choices=['stereo', 'mono-duplicate', 'mono-merge'], default='stereo', help='Audio processing mode: stereo (default), mono-duplicate, mono-merge')
    audio_group.add_argument('--audio-pan-center', action='store_true', help='Pan/mix stereo channels to center (music+dialogue to both L+R)')
    audio_group.add_argument('--audio-channel', type=int, default=0,
                             help='Channel index to use with --audio-mode mono-duplicate. '
                                  '0 = left (default), 1 = right. The selected channel is duplicated to both L and R output channels.')
    audio_group.add_argument('--clip-ceiling', type=float, default=None,
                             help='Apply a peak ceiling to audio output at the specified level in dBFS (e.g. -10). '
                                  'Uses dynaudnorm with max gain = 1.0 so it only reduces, never boosts — '
                                  'quiet passages are left unchanged. Fast response (100ms frames, 3-frame window). '
                                  'Default: disabled.')
    return parser.parse_args()

def run_trim(config: Config, logger: logging.Logger) -> int:
    """Standalone --trim-in/--trim-out mode: a lossless stream-copy trim of the
    single file in --source-dir, for recovering a digitization session that ran
    long (e.g. trailing snow/black after the operator was pulled away). The
    source is never moved or modified — the user reviews the *_trimmed output
    and manually replaces the original once satisfied (see MANUAL.md)."""
    files = collect_video_files(config.source_dir, config.finished_dir)
    if len(files) == 0:
        raise ConfigError(f"--trim-in/--trim-out requires exactly one video file in {config.source_dir}, found none")
    if len(files) > 1:
        names = ', '.join(f.name for f in files[:10])
        more = '...' if len(files) > 10 else ''
        raise ConfigError(f"--trim-in/--trim-out requires exactly one video file in {config.source_dir}, "
                          f"found {len(files)}: {names}{more}")
    source_path = files[0]
    output_path = config.output_dir / f"{source_path.stem}_trimmed{source_path.suffix}"

    logger.info(f"Trim mode: {source_path.name} [{config.trim_in_str} -> {config.trim_out_str}] -> {output_path.name}")

    # Input-side -ss/-to (before -i): with stream copy, ffmpeg's demuxer snaps
    # these to the nearest keyframe rather than erroring out on a non-keyframe
    # start point (unlike output-side seeking, which some tools — and even
    # ffmpeg itself in some configurations — can refuse for the same reason).
    # -avoid_negative_ts make_zero rewrites timestamps so the trimmed file
    # starts clean at 0 rather than carrying the original file's offset.
    cmd = ['ffmpeg', '-ss', config.trim_in_str, '-to', config.trim_out_str, '-i', str(source_path),
           '-map', '0', '-c', 'copy', '-avoid_negative_ts', 'make_zero', str(output_path)]

    if config.dry_run:
        print(f"\n[DRY RUN] Would trim {source_path.name} -> {output_path.name}")
        print(f"[DRY RUN] Command: {shlex.join(cmd)}\n")
        logger.info(f"[DRY RUN] Trim command: {shlex.join(cmd)}")
        return 0

    process_log = config.log_dir / f"{source_path.stem}_trim.log"
    try:
        with open(process_log, 'w') as log_f:
            log_f.write(f"vdg trim -- {datetime.now().isoformat()}\n"
                        f"Source: {source_path}\nOutput: {output_path}\n"
                        f"Requested: {config.trim_in_str} -> {config.trim_out_str}\n\n")
    except Exception as e:
        logger.warning(f"Failed to initialize trim process log: {e}")

    try:
        source_fps = get_video_info(source_path).fps
    except Exception:
        source_fps = 30.0
    estimated_frames = max(int((config.trim_out_seconds - config.trim_in_seconds) * source_fps), 1)

    success, _ = run_ffmpeg_with_progress(cmd, estimated_frames, f"Trim: {source_path.name[:30]}", process_log)
    if not success:
        print(f"\n{Colors.RED}{Colors.BOLD}TRIM FAILED:{Colors.RESET} ffmpeg exited with an error -- see {process_log}\n")
        logger.error(f"Trim failed for {source_path.name} -- ffmpeg exited with an error, see {process_log}")
        if output_path.exists() and not config.keep_failed:
            output_path.unlink()
        return 1

    if config.skip_validation:
        logger.warning(f"Trim complete for {source_path.name} -- validation skipped (--skip-validation)")
        print(f"\n{Colors.YELLOW}Trim complete (validation skipped):{Colors.RESET} {output_path}\n"
             f"Review the file yourself, then manually rename it over the original and delete the original -- "
             f"vdg does not do this automatically.\n")
        return 0

    print("\nValidating trim output...")
    ok, messages = validate_trim_output(source_path, output_path, config.trim_in_seconds, config.trim_out_seconds)
    try:
        with open(process_log, 'a') as log_f:
            log_f.write("\nVALIDATION:\n" + "\n".join(messages) + "\n")
    except Exception as e:
        logger.warning(f"Failed to append validation results to trim log: {e}")
    for m in messages:
        logger.info(m)

    if not ok:
        print(f"\n{Colors.RED}{Colors.BOLD}TRIM VALIDATION FAILED{Colors.RESET} -- see {process_log}:")
        for m in messages:
            print(f"  - {m}")
        handle_failed_lossless_output(output_path, config.keep_failed)
        if config.keep_failed:
            print(f"\nOutput retained with a _VALIDATION_FAILED suffix for inspection (--keep-failed).\n")
        else:
            print(f"\nOutput deleted. Source file was never modified: {source_path}\n")
        return 1

    print(f"\n{Colors.GREEN}{Colors.BOLD}Trim complete and validated:{Colors.RESET} {output_path}")
    for m in messages:
        print(f"  - {m}")
    print(f"\n{Colors.BOLD}Manual review required{Colors.RESET} -- this is not automatic. Watch the trimmed file, "
         f"then rename it over the original and delete the original yourself once you're satisfied. The source "
         f"file was not moved or modified: {source_path}\n")
    logger.info(f"Trim complete and validated: {output_path}")
    return 0

def main():
    args = parse_arguments()
    print(f"\n{Colors.BOLD}{Colors.CYAN}{SCRIPT_TITLE}{Colors.RESET}")
    print(f"{Colors.BOLD}{SCRIPT_NAME}{Colors.RESET}")
    print(SCRIPT_SEPARATOR)
    print(SCRIPT_SEPARATOR + "\n")
    try:
        config = Config(args)
        logger = setup_logging(config.log_dir, config.dry_run)
        for notice in config.setup_notices:
            logger.warning(notice)
        if not check_dependencies():
            logger.error("Missing required dependencies. Exiting.")
            sys.exit(1)
        if not config.dry_run:
            check_disk_space(config.output_dir)
        logger.info("Starting video transcoding pipeline")
        if config.trim_mode:
            logger.info(f"Trim mode: {config.trim_in_str} -> {config.trim_out_str} — stream-copy trim of a single "
                        f"file, source left in place, not recorded in the resume CSV")
            exit_code = run_trim(config, logger)
            sys.exit(exit_code)
        if config.thumbs_only:
            logger.info(f"Thumbnail-only mode: {config.thumb_count} thumbnail(s) per file at random positions — "
                        f"no video derivatives, sources left in place, not recorded in the resume CSV")
        if config.output_h264:
            # AAC is only relevant to the H.264 path (v210/FFV1 audio is PCM copy,
            # not AAC-encoded) — logging it unconditionally is misleading on
            # lossless-only runs.
            logger.info(f"AAC encoder: {config.aac_encoder}")
        stats = process_batch(config)
        if stats.error > 0:
            sys.exit(1)
    except ConfigError as e:
        print(f"{Colors.RED}{Colors.BOLD}ERROR:{Colors.RESET} {Colors.RED}{e}{Colors.RESET}\n")
        sys.exit(2)
    except KeyboardInterrupt:
        print("\n\nProcessing interrupted by user")
        sys.exit(130)
    except Exception as e:
        logging.getLogger('video_transcoder').critical(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)

