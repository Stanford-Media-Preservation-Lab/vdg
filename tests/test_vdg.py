"""
tests/test_vdg.py

Unit tests for vdg core logic.
Covers pure functions that do not require ffmpeg/ffprobe to be present.
"""

import pytest
from pathlib import Path

from vdg.cli import (
    sanitize_filename,
    strip_role_code,
    calculate_scaling_params,
    detect_video_standard,
    calculate_output_fps,
    get_bitrate_config,
    compute_filename_disambiguation,
    clean_aperture_input_args,
    collect_video_files,
    handle_failed_lossless_output,
    parse_ffmpeg_version,
    validate_directories,
    describe_missing_path,
    ConfigError,
    parse_mediainfo_text,
    format_mediainfo_summary,
    classify_frame_field_order,
    field_order_read_intervals,
    scan_type_disagreement,
    prores_interlace_args,
    VideoInfo,
    parse_dv_system,
    dv_nominal_fps,
    add_audio_async_filter,
    AUDIO_ASYNC_FILTER,
    policy_without_audio_rules,
    policy_without_dimension_rules,
    POLICY_FFV1,
    POLICY_V210_NTSC,
    POLICY_V210_PAL,
    VideoStandard,
    ROLE_CODES,
    _desktop_review_vf_chain,
    DESKTOP_REVIEW_SD_SIZE,
    DESKTOP_REVIEW_HD_FIT,
    BITRATE_CONFIG,
    par_default_mismatch,
    V210_PAR_NTSC_DEFAULT,
    V210_PAR_NTSC_ACCEPTABLE,
    V210_PAR_PAL_DEFAULT,
    V210_PAR_PAL_ACCEPTABLE,
    parse_trim_timestamp,
    probe_streams_summary,
    validate_trim_output,
    Config,
)
from unittest.mock import patch, MagicMock
import types
import argparse


# ---------------------------------------------------------------------------
# sanitize_filename
# ---------------------------------------------------------------------------

class TestSanitizeFilename:
    def test_spaces_replaced_with_underscores(self):
        assert sanitize_filename("my file name") == "my_file_name"

    def test_no_spaces_unchanged(self):
        assert sanitize_filename("qp616km4412_pm") == "qp616km4412_pm"

    def test_multiple_spaces(self):
        assert sanitize_filename("vendor  delivery  file") == "vendor__delivery__file"

    def test_empty_string(self):
        assert sanitize_filename("") == ""

    def test_leading_trailing_spaces(self):
        assert sanitize_filename(" file ") == "_file_"


# ---------------------------------------------------------------------------
# strip_role_code
# ---------------------------------------------------------------------------

class TestStripRoleCode:
    def test_strips_pm(self):
        assert strip_role_code("qp616km4412_pm") == "qp616km4412"

    def test_strips_sh(self):
        assert strip_role_code("qp616km4412_sh") == "qp616km4412"

    def test_strips_sl(self):
        assert strip_role_code("qp616km4412_sl") == "qp616km4412"

    def test_no_role_code_unchanged(self):
        assert strip_role_code("vendor_delivery") == "vendor_delivery"

    def test_partial_match_unchanged(self):
        # "_pm" must be a full suffix, not a mid-stem occurrence
        assert strip_role_code("compound_pm_extra") == "compound_pm_extra"

    def test_non_standard_suffix_unchanged(self):
        assert strip_role_code("file_master") == "file_master"

    def test_role_code_only(self):
        # Edge: stem is only the role code
        assert strip_role_code("_pm") == ""

    def test_all_role_codes_covered(self):
        # Ensure ROLE_CODES constant and strip_role_code stay in sync
        for code in ROLE_CODES:
            stem = f"testfile{code}"
            assert strip_role_code(stem) == "testfile"


# ---------------------------------------------------------------------------
# calculate_scaling_params
# ---------------------------------------------------------------------------

class TestCalculateScalingParams:
    def test_ntsc_4x3(self):
        assert calculate_scaling_params(720, 480, "4:3") == "640:480"

    def test_ntsc_16x9(self):
        assert calculate_scaling_params(720, 480, "16:9") == "854:480"

    def test_ntsc_486_4x3(self):
        assert calculate_scaling_params(720, 486, "4:3") == "640:480"

    def test_ntsc_486_16x9(self):
        assert calculate_scaling_params(720, 486, "16:9") == "854:480"

    def test_pal_4x3(self):
        assert calculate_scaling_params(720, 576, "4:3") == "640:480"

    def test_pal_16x9(self):
        assert calculate_scaling_params(720, 576, "16:9") == "854:480"

    def test_anamorphic_1440x1080(self):
        assert calculate_scaling_params(1440, 1080, "16:9") == "1280:720"

    def test_full_hd_1920x1080(self):
        assert calculate_scaling_params(1920, 1080, "16:9") == "1280:720"

    def test_native_720p(self):
        assert calculate_scaling_params(1280, 720, "16:9") == "1280:720"

    def test_ultra_hd_16x9(self):
        assert calculate_scaling_params(3840, 2160, "16:9") == "1280:720"

    def test_ultra_hd_non_169(self):
        result = calculate_scaling_params(3840, 2160, "4:3")
        assert result == "-2:720"

    def test_unknown_dimensions_passthrough(self):
        # Non-standard dimensions should return mod-2 safe passthrough
        result = calculate_scaling_params(800, 600, "4:3")
        assert result == "trunc(iw/2)*2:trunc(ih/2)*2"


# ---------------------------------------------------------------------------
# detect_video_standard
# ---------------------------------------------------------------------------

class TestDetectVideoStandard:
    def test_ntsc_480(self):
        assert detect_video_standard(720, 480, 29.97) == VideoStandard.NTSC

    def test_ntsc_486(self):
        assert detect_video_standard(720, 486, 29.97) == VideoStandard.NTSC

    def test_pal(self):
        assert detect_video_standard(720, 576, 25.0) == VideoStandard.PAL

    def test_unknown_hd(self):
        assert detect_video_standard(1920, 1080, 29.97) == VideoStandard.UNKNOWN

    def test_ntsc_fps_tolerance(self):
        # 30.0 is within the 0.1 tolerance and is deliberately treated as NTSC:
        # ffprobe can misreport a true 29.97 SD source as 30.0, and v210 output
        # should still be possible. Genuine 30p SD doesn't come off tape — it only
        # appears in born-digital/acquired media, where the v210 MediaConch policy
        # (29.970fps) then fails and flags the file for review.
        assert detect_video_standard(720, 480, 30.0) == VideoStandard.NTSC

    def test_ntsc_outside_tolerance(self):
        assert detect_video_standard(720, 480, 25.0) == VideoStandard.UNKNOWN

    def test_pal_fps_tolerance(self):
        assert detect_video_standard(720, 576, 25.0) == VideoStandard.PAL


# ---------------------------------------------------------------------------
# calculate_output_fps
# ---------------------------------------------------------------------------

class TestCalculateOutputFps:
    def test_ntsc_passthrough(self):
        assert calculate_output_fps(29.97) == pytest.approx(29.97)

    def test_pal_passthrough(self):
        assert calculate_output_fps(25.0) == pytest.approx(25.0)

    def test_60i_halved(self):
        assert calculate_output_fps(60.0) == pytest.approx(30.0)

    def test_50i_halved(self):
        assert calculate_output_fps(50.0) == pytest.approx(25.0)

    def test_59_94_halved(self):
        assert calculate_output_fps(59.94) == pytest.approx(29.97)


# ---------------------------------------------------------------------------
# get_bitrate_config
# ---------------------------------------------------------------------------

class TestGetBitrateConfig:
    def test_sd_profile(self):
        cfg = get_bitrate_config(480)
        assert cfg['bitrate'] == '1000k'
        assert cfg['maxrate'] == '1200k'
        assert cfg['bufsize'] == '2000k'

    def test_sd_576(self):
        cfg = get_bitrate_config(576)
        assert cfg['bitrate'] == '1000k'

    def test_hd_profile(self):
        cfg = get_bitrate_config(720)
        assert cfg['bitrate'] == '2800k'
        assert cfg['maxrate'] == '2900k'
        assert cfg['bufsize'] == '5800k'

    def test_hd_1080(self):
        cfg = get_bitrate_config(1080)
        assert cfg['bitrate'] == '2800k'


# ---------------------------------------------------------------------------
# calculate_scaling_params — force_anamorphic override
# ---------------------------------------------------------------------------

class TestCalculateScalingParamsForceAnamorphic:
    def test_force_overrides_4x3_dar(self):
        # Mistagged 4:3 that is actually anamorphic squeezed 16:9 footage
        assert calculate_scaling_params(720, 480, "4:3", force_anamorphic=True) == "854:480"

    def test_force_486_height(self):
        assert calculate_scaling_params(720, 486, "4:3", force_anamorphic=True) == "854:480"

    def test_force_pal_576_height(self):
        assert calculate_scaling_params(720, 576, "4:3", force_anamorphic=True) == "854:480"

    def test_force_false_uses_detected_dar(self):
        assert calculate_scaling_params(720, 480, "4:3", force_anamorphic=False) == "640:480"

    def test_force_ignored_for_hd_dimensions(self):
        # Flag only applies to SD (720-wide) sources
        result = calculate_scaling_params(1920, 1080, "4:3", force_anamorphic=True)
        assert result != "854:480"


# ---------------------------------------------------------------------------
# compute_filename_disambiguation
# ---------------------------------------------------------------------------

class TestComputeFilenameDisambiguation:
    def test_same_id_different_extensions(self):
        files = [
            Path("wb824kh0844_At_Home_But_Not_At_Home_2020_pm.mov"),
            Path("wb824kh0844_At_Home_But_Not_At_Home_2020_sh.mp4"),
        ]
        result = compute_filename_disambiguation(files)
        assert result == {
            "wb824kh0844_At_Home_But_Not_At_Home_2020_pm.mov": "mov",
            "wb824kh0844_At_Home_But_Not_At_Home_2020_sh.mp4": "mp4",
        }

    def test_same_id_same_extension_falls_back_to_role_code(self):
        # Regression: bm994hd4640_pm.mov + bm994hd4640_sh.mov both .mov —
        # extension alone collided and silently overwrote one output.
        # Must fall back to role_code + extension for the whole group.
        files = [
            Path("bm994hd4640_pm.mov"),
            Path("bm994hd4640_sh.mov"),
            Path("bm994hd4640_sl.mp4"),
        ]
        result = compute_filename_disambiguation(files)
        assert result == {
            "bm994hd4640_pm.mov": "pm_mov",
            "bm994hd4640_sh.mov": "sh_mov",
            "bm994hd4640_sl.mp4": "sl_mp4",
        }
        # Confirm every disambiguated stem is actually unique.
        assert len(set(result.values())) == 3

    def test_unique_stems_do_not_collide(self):
        files = [
            Path("file_one_pm.mov"),
            Path("file_two_sh.mp4"),
        ]
        assert compute_filename_disambiguation(files) == {}

    def test_no_role_code_no_collision(self):
        files = [Path("vendor_delivery.mov")]
        assert compute_filename_disambiguation(files) == {}

    def test_empty_file_list(self):
        assert compute_filename_disambiguation([]) == {}


# ---------------------------------------------------------------------------
# clean_aperture_input_args
# ---------------------------------------------------------------------------

class TestCleanApertureInputArgs:
    def test_default_disables_clap_crop(self):
        assert clean_aperture_input_args(False) == ["-apply_cropping", "0"]

    def test_clean_aperture_true_omits_flag(self):
        assert clean_aperture_input_args(True) == []


# ---------------------------------------------------------------------------
# collect_video_files — hidden-file / AppleDouble filtering
# ---------------------------------------------------------------------------

class TestCollectVideoFiles:
    def test_excludes_appledouble_sidecar(self, tmp_path):
        (tmp_path / "real_pm.mov").touch()
        (tmp_path / "._real_pm.mov").touch()
        files = collect_video_files(tmp_path, tmp_path / "finished_sources")
        assert [f.name for f in files] == ["real_pm.mov"]

    def test_excludes_other_hidden_files(self, tmp_path):
        (tmp_path / "real_pm.mov").touch()
        (tmp_path / ".DS_Store").touch()
        files = collect_video_files(tmp_path, tmp_path / "finished_sources")
        assert [f.name for f in files] == ["real_pm.mov"]

    def test_excludes_finished_dir_contents(self, tmp_path):
        finished = tmp_path / "finished_sources"
        finished.mkdir()
        (tmp_path / "real_pm.mov").touch()
        (finished / "already_done_pm.mov").touch()
        files = collect_video_files(tmp_path, finished)
        assert [f.name for f in files] == ["real_pm.mov"]


# ---------------------------------------------------------------------------
# handle_failed_lossless_output — --keep-failed behavior
# ---------------------------------------------------------------------------

class TestHandleFailedLosslessOutput:
    def test_deletes_by_default(self, tmp_path):
        output_path = tmp_path / "failed_pm.mkv"
        output_path.touch()
        handle_failed_lossless_output(output_path, keep_failed=False)
        assert not output_path.exists()

    def test_keep_failed_renames_with_suffix(self, tmp_path):
        output_path = tmp_path / "failed_pm.mkv"
        output_path.touch()
        handle_failed_lossless_output(output_path, keep_failed=True)
        assert not output_path.exists()
        assert (tmp_path / "failed_pm_VALIDATION_FAILED.mkv").exists()

    def test_missing_output_is_a_no_op(self, tmp_path):
        output_path = tmp_path / "does_not_exist_pm.mkv"
        # Should not raise even though the file was never created.
        handle_failed_lossless_output(output_path, keep_failed=True)
        handle_failed_lossless_output(output_path, keep_failed=False)


# ---------------------------------------------------------------------------
# parse_ffmpeg_version
# ---------------------------------------------------------------------------

class TestParseFfmpegVersion:
    def test_homebrew_release(self):
        out = "ffmpeg version 7.1.5 Copyright (c) 2000-2025 the FFmpeg developers\nbuilt with Apple clang"
        assert parse_ffmpeg_version(out) == ("7.1.5", 7)

    def test_ubuntu_package(self):
        out = "ffprobe version 6.1.1-3ubuntu5 Copyright (c) 2007-2023 the FFmpeg developers"
        assert parse_ffmpeg_version(out) == ("6.1.1-3ubuntu5", 6)

    def test_newer_major(self):
        assert parse_ffmpeg_version("ffmpeg version 9.0.2 Copyright")[1] == 9

    def test_n_prefixed_tag(self):
        assert parse_ffmpeg_version("ffmpeg version n7.1 Copyright") == ("n7.1", 7)

    def test_git_snapshot_has_no_major(self):
        assert parse_ffmpeg_version("ffmpeg version N-118000-gabc1234 Copyright") == ("N-118000-gabc1234", None)

    def test_unrecognized_output(self):
        assert parse_ffmpeg_version("") == (None, None)
        assert parse_ffmpeg_version("command not found") == (None, None)


# ---------------------------------------------------------------------------
# validate_directories / describe_missing_path
# ---------------------------------------------------------------------------

class TestValidateDirectories:
    def test_existing_dirs_ok(self, tmp_path):
        (tmp_path / "src").mkdir(); (tmp_path / "out").mkdir()
        assert validate_directories(tmp_path / "src", tmp_path / "out") == []

    def test_missing_source_raises(self, tmp_path):
        with pytest.raises(ConfigError, match="Source directory does not exist"):
            validate_directories(tmp_path / "nope" / "src", tmp_path)

    def test_source_is_file_raises(self, tmp_path):
        f = tmp_path / "file.mov"; f.write_text("x")
        with pytest.raises(ConfigError, match="not a directory"):
            validate_directories(f, tmp_path)

    def test_missing_output_leaf_is_created_with_notice(self, tmp_path):
        (tmp_path / "src").mkdir()
        notices = validate_directories(tmp_path / "src", tmp_path / "out")
        assert len(notices) == 1 and "creating it" in notices[0]

    def test_missing_output_parent_raises(self, tmp_path):
        (tmp_path / "src").mkdir()
        with pytest.raises(ConfigError, match="neither does its parent"):
            validate_directories(tmp_path / "src", tmp_path / "missing" / "out")

    def test_error_names_deepest_existing_folder(self, tmp_path):
        with pytest.raises(ConfigError, match=str(tmp_path)):
            validate_directories(tmp_path / "a" / "b", tmp_path)

class TestDescribeMissingPath:
    def test_unmounted_volume(self):
        msg = describe_missing_path(Path("/Volumes/NoSuchDrive_vdg_test/1/source"))
        assert "NoSuchDrive_vdg_test" in msg and "not mounted" in msg


# ---------------------------------------------------------------------------
# MediaInfo summary
# ---------------------------------------------------------------------------

MEDIAINFO_SAMPLE = """General
Complete name                            : src.mov
Format                                   : MPEG-4
Format profile                           : QuickTime
File size                                : 38.7 MiB
Duration                                 : 1 h 14 min
Overall bit rate                         : 81.2 Mb/s

Video
ID                                       : 1
Format                                   : AVC
Format profile                           : High@L5.1
Format settings                          : CABAC / 1 Ref Frames
Codec ID                                 : avc1
Bit rate                                 : 18.8 Mb/s
Width                                    : 3 840 pixels
Height                                   : 2 160 pixels
Display aspect ratio                     : 16:9
Frame rate mode                          : Constant
Frame rate                               : 30.000 FPS
Color space                              : YUV
Chroma subsampling                       : 4:2:0
Bit depth                                : 8 bits
Scan type                                : Progressive

Audio #1
Format                                   : PCM
Format settings                          : Little / Signed
Channel(s)                               : 2 channels
Sampling rate                            : 48.0 kHz
Bit depth                                : 24 bits

Audio #2
Format                                   : AAC LC
Channel(s)                               : 1 channel
Sampling rate                            : 48.0 kHz
"""

class TestMediainfoSummary:
    def test_parse_sections(self):
        sections = parse_mediainfo_text(MEDIAINFO_SAMPLE)
        assert [name for name, _ in sections] == ["General", "Video", "Audio #1", "Audio #2"]
        assert sections[1][1]["Format profile"] == "High@L5.1"

    def test_summary_fields(self):
        lines = format_mediainfo_summary(MEDIAINFO_SAMPLE)
        text = "\n".join(lines)
        assert lines[0].startswith("Container") and lines[0].endswith(": MPEG-4 / QuickTime")
        for expected in ["AVC", "High@L5.1", "CABAC / 1 Ref Frames", "avc1", "1 h 14 min", "18.8 Mb/s",
                         "3 840 pixels", "16:9", "Constant", "30.000 FPS", "4:2:0", "8 bits", "Progressive"]:
            assert expected in text
        assert lines[-2].endswith(": PCM, 2 channels, 48.0 kHz, 24 bits")
        assert lines[-1].endswith(": AAC LC, 1 channel, 48.0 kHz")

    def test_duration_from_general_when_video_lacks_it(self):
        assert any(l.startswith("Duration") and l.endswith("1 h 14 min")
                   for l in format_mediainfo_summary(MEDIAINFO_SAMPLE))

    def test_no_audio(self):
        lines = format_mediainfo_summary("General\nFormat : Matroska\n\nVideo\nFormat : FFV1\n")
        assert lines[-1].startswith("Audio") and lines[-1].endswith(": none")


# ---------------------------------------------------------------------------
# classify_frame_field_order (issue #9 — first-frame field order fallback)
# ---------------------------------------------------------------------------

class TestClassifyFrameFieldOrder:
    def _frames(self, *pairs):
        return [{"interlaced_frame": i, "top_field_first": t} for i, t in pairs]

    def test_bff_dv(self):
        assert classify_frame_field_order(self._frames(*[(1, 0)] * 5)) == "bff"

    def test_tff(self):
        assert classify_frame_field_order(self._frames(*[(1, 1)] * 5)) == "tff"

    def test_progressive(self):
        assert classify_frame_field_order(self._frames(*[(0, 0)] * 5)) == "progressive"

    def test_majority_vote(self):
        assert classify_frame_field_order(self._frames((1, 0), (1, 0), (1, 0), (0, 0), (0, 0))) == "bff"

    def test_no_frames(self):
        assert classify_frame_field_order([]) is None


# ---------------------------------------------------------------------------
# DV system / frame rate (misreported container rates on raw & dvrescue DV)
# ---------------------------------------------------------------------------

class TestDvSystem:
    def test_ntsc_header(self):
        assert parse_dv_system(bytes([0x1f, 0x07, 0x00, 0x3f, 0xf9])) == "525/60"

    def test_pal_header(self):
        assert parse_dv_system(bytes([0x1f, 0x07, 0x00, 0xbf, 0xf8])) == "625/50"

    def test_not_a_header_block(self):
        assert parse_dv_system(bytes([0x3f, 0x07, 0x00, 0x3f])) is None

    def test_too_short(self):
        assert parse_dv_system(b"") is None

    def test_nominal_rates(self):
        assert dv_nominal_fps("525/60", 480) == pytest.approx(29.97, abs=0.001)
        assert dv_nominal_fps("625/50", 576) == 25.0
        assert dv_nominal_fps("525/60", 1080) == pytest.approx(29.97, abs=0.001)
        assert dv_nominal_fps("525/60", 720) == pytest.approx(59.94, abs=0.001)
        assert dv_nominal_fps("625/50", 720) == 50.0


# ---------------------------------------------------------------------------
# add_audio_async_filter (issue #10 — separate H.264 audio mux)
# ---------------------------------------------------------------------------

class TestAddAudioAsyncFilter:
    def test_no_existing_filter(self):
        assert add_audio_async_filter([]) == ["-af", AUDIO_ASYNC_FILTER]

    def test_prepended_to_af_chain(self):
        assert add_audio_async_filter(["-af", "pan=stereo|c0=c0|c1=c0"]) == \
            ["-af", f"{AUDIO_ASYNC_FILTER},pan=stereo|c0=c0|c1=c0"]

    def test_appended_inside_filter_complex(self):
        out = add_audio_async_filter(["-filter_complex", "[0:a:0][0:a:1]amerge=inputs=2[aout]", "-map", "[aout]"])
        assert out == ["-filter_complex", f"[0:a:0][0:a:1]amerge=inputs=2,{AUDIO_ASYNC_FILTER}[aout]", "-map", "[aout]"]


# ---------------------------------------------------------------------------
# policy_without_audio_rules (MediaConch on sources with no audio track)
# ---------------------------------------------------------------------------

import xml.etree.ElementTree as ET

class TestPolicyWithoutAudioRules:
    @pytest.mark.parametrize("policy", [POLICY_FFV1, POLICY_V210_NTSC, POLICY_V210_PAL])
    def test_no_audio_rules_and_still_valid_xml(self, policy):
        stripped = policy_without_audio_rules(policy)
        root = ET.fromstring(stripped.encode())
        assert not [r for r in root.iter("rule") if r.get("tracktype") == "Audio"]
        video_rules = [r for r in ET.fromstring(policy.encode()).iter("rule") if r.get("tracktype") != "Audio"]
        assert len([r for r in root.iter("rule")]) == len(video_rules)

    def test_ffv1_empty_audio_subpolicy_removed(self):
        root = ET.fromstring(policy_without_audio_rules(POLICY_FFV1).encode())
        assert not [p for p in root.iter("policy") if len(list(p)) == 0]
        assert "Audio is PCM or FLAC" not in policy_without_audio_rules(POLICY_FFV1)

    def test_dimension_rules_removed_others_kept(self):
        stripped = policy_without_dimension_rules(POLICY_V210_NTSC)
        rules = list(ET.fromstring(stripped.encode()).iter("rule"))
        assert not [r for r in rules if r.get("value") in ("Width", "Height")]
        original = list(ET.fromstring(POLICY_V210_NTSC.encode()).iter("rule"))
        assert len(rules) == len(original) - 2


class TestFieldOrderSampling:
    def test_intervals_spread_across_file(self):
        spec = field_order_read_intervals(3600.0)
        assert spec == "360.000%+#5,1080.000%+#5,1800.000%+#5,2520.000%+#5,3240.000%+#5"

    def test_unknown_duration_reads_from_start(self):
        assert field_order_read_intervals(0) == "%+#5"

    def test_damaged_head_outvoted(self):
        head = [{"interlaced_frame": 0, "top_field_first": 0}] * 5
        body = [{"interlaced_frame": 1, "top_field_first": 0}] * 20
        assert classify_frame_field_order(head + body) == "bff"


class TestScanTypeDisagreement:
    LINES = ["Width                : 720 pixels", "Scan type            : Interlaced"]

    def test_flags_interlaced_treated_as_progressive(self):
        assert scan_type_disagreement(self.LINES, interlaced=False) == "Interlaced"

    def test_agreement_is_none(self):
        assert scan_type_disagreement(self.LINES, interlaced=True) is None

    def test_mbaff_ignored(self):
        assert scan_type_disagreement(["Scan type            : MBAFF"], interlaced=False) is None

    def test_missing_scan_type(self):
        assert scan_type_disagreement(["Width : 720 pixels"], interlaced=False) is None


class TestProresInterlaceArgs:
    def _info(self, interlaced, source="stream", detail="tt"):
        return VideoInfo(width=720, height=486, duration=10, fps=29.97, dar="4:3", has_audio=True,
                         total_frames=300, codec="ffv1", interlaced=interlaced, is_vfr=False,
                         is_quicktime=False, field_order_source=source, field_order_detail=detail)

    def test_progressive_untouched(self):
        assert prores_interlace_args(self._info(False)) == []

    def test_stream_field_order_relies_on_frame_flags(self):
        assert prores_interlace_args(self._info(True)) == ["-flags", "+ildct"]

    def test_frame_sampled_order_pinned(self):
        assert prores_interlace_args(self._info(True, "frames", "bff")) == ["-flags", "+ildct", "-vf", "setfield=bff"]

    def test_force_scan_wins(self):
        assert prores_interlace_args(self._info(True, "frames", "bff"), "tff") == ["-flags", "+ildct", "-vf", "setfield=tff"]


# ---------------------------------------------------------------------------
# --desktop-review: filter chain construction, suffixes, bitrate config
# ---------------------------------------------------------------------------

class TestDesktopReviewVfChain:
    def _info(self, interlaced, source="stream", detail="bt"):
        return VideoInfo(width=720, height=486, duration=10, fps=29.97, dar="4:3", has_audio=True,
                         total_frames=300, codec="ffv1", interlaced=interlaced, is_vfr=False,
                         is_quicktime=False, field_order_source=source, field_order_detail=detail)

    def _config(self, force_scan=None):
        return types.SimpleNamespace(force_scan=force_scan)

    def test_sd_progressive_no_bwdif(self):
        vf = _desktop_review_vf_chain(self._info(False), self._config(), 720, 540, pillarbox=False)
        assert "bwdif" not in vf
        assert "scale=720:540:flags=lanczos" in vf
        assert "pad=" not in vf

    def test_sd_interlaced_auto_parity(self):
        vf = _desktop_review_vf_chain(self._info(True), self._config(), 720, 540, pillarbox=False)
        assert "bwdif=mode=0:parity=-1:deint=all" in vf
        assert "scale=720:540:flags=lanczos" in vf

    def test_sd_interlaced_force_scan_tff(self):
        vf = _desktop_review_vf_chain(self._info(True), self._config(force_scan='tff'), 720, 540, pillarbox=False)
        assert "parity=0" in vf

    def test_sd_interlaced_force_scan_bff(self):
        vf = _desktop_review_vf_chain(self._info(True), self._config(force_scan='bff'), 720, 540, pillarbox=False)
        assert "parity=1" in vf

    def test_hd_pillarbox_present(self):
        vf = _desktop_review_vf_chain(self._info(True), self._config(), 1440, 1080, pillarbox=True)
        assert "scale=1440:1080:flags=lanczos" in vf
        assert "pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=black" in vf

    def test_sd_no_pillarbox(self):
        # SD target (720x540) is exactly 4:3 already — no pad should ever appear
        vf = _desktop_review_vf_chain(self._info(True), self._config(), 720, 540, pillarbox=False)
        assert "pad=" not in vf

    def test_always_square_pixel_yuv420p(self):
        vf = _desktop_review_vf_chain(self._info(True), self._config(), 720, 540, pillarbox=False)
        assert vf.endswith("setsar=1,format=yuv420p")


class TestDesktopReviewConstants:
    def test_sd_size_is_exact_4x3(self):
        w, h = DESKTOP_REVIEW_SD_SIZE
        assert w == 720 and h == 540
        assert w / h == pytest.approx(4 / 3)

    def test_hd_fit_is_exact_4x3(self):
        w, h = DESKTOP_REVIEW_HD_FIT
        assert w == 1440 and h == 1080
        assert w / h == pytest.approx(4 / 3)

    def test_role_codes_include_desktop_review(self):
        assert '_ds' in ROLE_CODES
        assert '_dh' in ROLE_CODES


class TestDesktopReviewBitrateConfig:
    def test_desktop_sd_present(self):
        assert 'desktop_sd' in BITRATE_CONFIG
        assert BITRATE_CONFIG['desktop_sd']['bitrate'] == '9000k'

    def test_desktop_hd_present(self):
        assert 'desktop_hd' in BITRATE_CONFIG
        assert BITRATE_CONFIG['desktop_hd']['bitrate'] == '18000k'

    def test_desktop_bitrates_higher_than_service_low(self):
        # Desktop review is a projector/meeting-room viewing copy, meant to
        # look better than the streaming _sl access copy, not just parse.
        sd_kbps = int(BITRATE_CONFIG['desktop_sd']['bitrate'].rstrip('k'))
        hd_kbps = int(BITRATE_CONFIG['desktop_hd']['bitrate'].rstrip('k'))
        sl_sd_kbps = int(BITRATE_CONFIG['sd']['bitrate'].rstrip('k'))
        sl_hd_kbps = int(BITRATE_CONFIG['hd']['bitrate'].rstrip('k'))
        assert sd_kbps > sl_sd_kbps
        assert hd_kbps > sl_hd_kbps


# ---------------------------------------------------------------------------
# TN2162 (pasp) rule presence in the v210 policies
# ---------------------------------------------------------------------------

class TestTN2162PixelAspectRatioRule:
    """PAR is a nested <policy type="or"> sub-policy (2026-09-28): MediaConch
    passes on any vrecord-acceptable value for the standard, since MediaConch
    itself has no three-state (default/acceptable/fail) result. The middle tier
    — acceptable but not vrecord's own default — is a separate Python-level
    warning, see TestParDefaultMismatch below."""

    def _par_sub_policy(self, policy_xml):
        root = ET.fromstring(policy_xml)
        sub_policies = [p for p in root.findall('policy') if p.get('type') == 'or']
        par_policies = [p for p in sub_policies
                        if any(r.get('value') == 'PixelAspectRatio' for r in p.findall('rule'))]
        assert len(par_policies) == 1, "expected exactly one PAR OR sub-policy"
        return par_policies[0]

    def test_ntsc_policy_has_par_or_subpolicy_with_four_values(self):
        sub = self._par_sub_policy(POLICY_V210_NTSC)
        values = {r.text for r in sub.findall('rule') if r.get('value') == 'PixelAspectRatio'}
        assert values == {'0.909', '0.889', '0.900', '0.912'}

    def test_ntsc_par_default_value_present(self):
        sub = self._par_sub_policy(POLICY_V210_NTSC)
        values = {r.text for r in sub.findall('rule')}
        assert f'{V210_PAR_NTSC_DEFAULT:.3f}' in values

    def test_pal_policy_has_par_or_subpolicy_with_three_values(self):
        sub = self._par_sub_policy(POLICY_V210_PAL)
        values = {r.text for r in sub.findall('rule') if r.get('value') == 'PixelAspectRatio'}
        assert values == {'1.091', '1.067', '1.094'}

    def test_pal_par_default_value_present(self):
        sub = self._par_sub_policy(POLICY_V210_PAL)
        values = {r.text for r in sub.findall('rule')}
        assert f'{V210_PAR_PAL_DEFAULT:.3f}' in values

    def test_both_policies_cite_tn2162_in_name(self):
        for policy in (POLICY_V210_NTSC, POLICY_V210_PAL):
            root = ET.fromstring(policy)
            assert 'TN2162' in root.get('name')

    def test_par_or_subpolicy_nested_inside_outer_and_policy(self):
        # Confirms the sub-policy is actually nested (same proven pattern as
        # POLICY_FFV1's "Audio is PCM or FLAC" sub-policy), not a sibling.
        for policy in (POLICY_V210_NTSC, POLICY_V210_PAL):
            root = ET.fromstring(policy)
            assert root.get('type') == 'and'
            direct_children = list(root)
            par_subpolicies = [c for c in direct_children if c.tag == 'policy' and c.get('type') == 'or'
                               and any(r.get('value') == 'PixelAspectRatio' for r in c.findall('rule'))]
            assert len(par_subpolicies) == 1


class TestParDefaultMismatch:
    """par_default_mismatch(): the Python-level warning for the middle tier —
    a vrecord-acceptable PAR that isn't vrecord's own default for the standard."""

    def _ffprobe_result(self, sar_text, returncode=0):
        result = MagicMock()
        result.returncode = returncode
        result.stdout = sar_text
        return result

    def test_default_ntsc_par_returns_none(self):
        with patch('vdg.cli.subprocess.run', return_value=self._ffprobe_result('10:11')):
            assert par_default_mismatch(Path('out.mov'), VideoStandard.NTSC) is None

    def test_non_default_acceptable_ntsc_par_warns(self):
        with patch('vdg.cli.subprocess.run', return_value=self._ffprobe_result('9:10')):
            msg = par_default_mismatch(Path('out.mov'), VideoStandard.NTSC)
        assert msg is not None
        assert '9/10' in msg
        assert '0.909' in msg  # cites the vrecord default it doesn't match

    def test_default_pal_par_returns_none(self):
        with patch('vdg.cli.subprocess.run', return_value=self._ffprobe_result('12:11')):
            assert par_default_mismatch(Path('out.mov'), VideoStandard.PAL) is None

    def test_non_default_acceptable_pal_par_warns(self):
        with patch('vdg.cli.subprocess.run', return_value=self._ffprobe_result('16:15')):
            msg = par_default_mismatch(Path('out.mov'), VideoStandard.PAL)
        assert msg is not None
        assert '16/15' in msg

    def test_par_outside_acceptable_set_returns_none(self):
        # Genuinely out-of-spec values are MediaConch's job (hard fail), not a
        # soft Python warning — this function only distinguishes among the
        # already-acceptable set.
        with patch('vdg.cli.subprocess.run', return_value=self._ffprobe_result('1:1')):
            assert par_default_mismatch(Path('out.mov'), VideoStandard.NTSC) is None

    def test_unparseable_ffprobe_output_returns_none(self):
        with patch('vdg.cli.subprocess.run', return_value=self._ffprobe_result('')):
            assert par_default_mismatch(Path('out.mov'), VideoStandard.NTSC) is None

    def test_ffprobe_failure_returns_none(self):
        with patch('vdg.cli.subprocess.run', return_value=self._ffprobe_result('10:11', returncode=1)):
            assert par_default_mismatch(Path('out.mov'), VideoStandard.NTSC) is None

    def test_unknown_video_standard_returns_none(self):
        with patch('vdg.cli.subprocess.run', return_value=self._ffprobe_result('9:10')):
            assert par_default_mismatch(Path('out.mov'), VideoStandard.UNKNOWN) is None


# ---------------------------------------------------------------------------
# parse_trim_timestamp
# ---------------------------------------------------------------------------

class TestParseTrimTimestamp:
    @pytest.mark.parametrize("value,expected", [
        ("01:16:55", 4615.0),
        ("76:55", 4615.0),          # MM:SS — leading unit isn't capped at 60
        ("00:00:00", 0.0),
        ("125", 125.0),
        ("125.5", 125.5),
        ("1:02:03.5", 3723.5),
        ("0:00:01", 1.0),
    ])
    def test_valid_formats(self, value, expected):
        assert parse_trim_timestamp(value) == pytest.approx(expected)

    @pytest.mark.parametrize("value", [
        "1:60:00",     # minutes out of range in HH:MM:SS
        "00:00:60",    # seconds out of range
        "abc",
        "1:2:3:4",     # too many fields
        "-5",
        "",
        "1:-5",        # negative seconds in MM:SS
    ])
    def test_invalid_formats_raise_value_error(self, value):
        with pytest.raises(ValueError):
            parse_trim_timestamp(value)


# ---------------------------------------------------------------------------
# Config: --trim-in/--trim-out standalone mode
# ---------------------------------------------------------------------------

def _trim_namespace(tmp_path, **overrides):
    """Minimal argparse.Namespace covering every Config field, defaulting to a
    valid trim-mode invocation. tmp_path/src and tmp_path/out are NOT created
    here — tests create what they need."""
    d = dict(
        source_dir=str(tmp_path / "src"), output_dir=str(tmp_path / "out"), workers=1,
        cleanup_only=False, move_finished=True, dry_run=True, skip_validation=False,
        h264=False, v210=False, prores=False, ffv1=False, desktop_review=False,
        audio_stream=0, audio_mode='stereo', audio_pan_center=False,
        force_scan=None, force_fps=None, thumbs=None, clip_ceiling=None,
        audio_channel=0, keep_framemd5=False, keep_mediaconch=False,
        clean_aperture=False, force_anamorphic=False, keep_failed=False,
        trim_in=None, trim_out=None,
    )
    d.update(overrides)
    return argparse.Namespace(**d)


class TestConfigTrimMode:
    def test_trim_alone_is_valid_and_sets_trim_mode(self, tmp_path):
        (tmp_path / "src").mkdir()
        config = Config(_trim_namespace(tmp_path, trim_in="00:00:00", trim_out="01:16:55"))
        assert config.trim_mode is True
        assert config.thumbs_only is False
        assert config.trim_in_seconds == pytest.approx(0.0)
        assert config.trim_out_seconds == pytest.approx(4615.0)
        # Trim mode never moves the source, same as thumbs_only.
        assert config.move_finished is False

    def test_trim_with_format_flag_raises(self, tmp_path):
        (tmp_path / "src").mkdir()
        with pytest.raises(ConfigError, match="standalone mode"):
            Config(_trim_namespace(tmp_path, trim_in="00:00:00", trim_out="00:10:00", h264=True))

    def test_trim_with_thumbs_raises(self, tmp_path):
        (tmp_path / "src").mkdir()
        with pytest.raises(ConfigError, match="standalone mode"):
            Config(_trim_namespace(tmp_path, trim_in="00:00:00", trim_out="00:10:00", thumbs=4))

    def test_trim_in_without_trim_out_raises(self, tmp_path):
        (tmp_path / "src").mkdir()
        with pytest.raises(ConfigError, match="must be given together"):
            Config(_trim_namespace(tmp_path, trim_in="00:00:00"))

    def test_trim_out_without_trim_in_raises(self, tmp_path):
        (tmp_path / "src").mkdir()
        with pytest.raises(ConfigError, match="must be given together"):
            Config(_trim_namespace(tmp_path, trim_out="00:10:00"))

    def test_trim_out_before_trim_in_raises(self, tmp_path):
        (tmp_path / "src").mkdir()
        with pytest.raises(ConfigError, match="must come after"):
            Config(_trim_namespace(tmp_path, trim_in="01:00:00", trim_out="00:30:00"))

    def test_malformed_trim_in_raises_config_error(self, tmp_path):
        (tmp_path / "src").mkdir()
        with pytest.raises(ConfigError, match="Invalid --trim-in"):
            Config(_trim_namespace(tmp_path, trim_in="not-a-time", trim_out="00:10:00"))

    def test_no_format_no_thumbs_no_trim_raises(self, tmp_path):
        (tmp_path / "src").mkdir()
        with pytest.raises(ConfigError, match="At least one output format"):
            Config(_trim_namespace(tmp_path))

    def test_trim_mode_does_not_create_finished_sources_dir(self, tmp_path):
        (tmp_path / "src").mkdir()
        Config(_trim_namespace(tmp_path, trim_in="00:00:00", trim_out="00:10:00"))
        assert not (tmp_path / "src" / "finished_sources").exists()


# ---------------------------------------------------------------------------
# probe_streams_summary / validate_trim_output
# ---------------------------------------------------------------------------

class TestValidateTrimOutput:
    """validate_trim_output()'s three tiers, with ffprobe/ffmpeg mocked out —
    these don't need real media, just to prove the pass/fail logic and
    messaging are correct given known probe/decode results."""

    def _streams_result(self, streams):
        result = MagicMock()
        result.stdout = __import__('json').dumps({"streams": streams})
        return result

    def test_probe_streams_summary_extracts_type_and_codec(self):
        streams = [
            {"codec_type": "video", "codec_name": "ffv1"},
            {"codec_type": "audio", "codec_name": "pcm_s24le"},
            {"codec_type": "attachment", "codec_name": "text"},
        ]
        with patch('vdg.cli.subprocess.run', return_value=self._streams_result(streams)):
            assert probe_streams_summary(Path("x.mkv")) == [
                ("video", "ffv1"), ("audio", "pcm_s24le"), ("attachment", "text"),
            ]

    def test_all_tiers_pass(self):
        streams = [("video", "ffv1"), ("audio", "pcm_s24le")]
        streams_dicts = [{"codec_type": t, "codec_name": c} for t, c in streams]
        decode_result = MagicMock(returncode=0, stderr="")

        def fake_run(cmd, **kwargs):
            if 'ffprobe' in cmd[0]:
                return self._streams_result(streams_dicts)
            return decode_result

        with patch('vdg.cli.subprocess.run', side_effect=fake_run), \
             patch('vdg.cli.get_video_info', return_value=types.SimpleNamespace(duration=10.0)):
            ok, messages = validate_trim_output(Path("src.mkv"), Path("out.mkv"), 0.0, 10.0)
        assert ok is True
        assert any("Structural check passed" in m for m in messages)
        assert any("Duration check passed" in m for m in messages)
        assert any("Full decode validation passed" in m for m in messages)

    def test_structural_mismatch_fails(self):
        source_dicts = [{"codec_type": "video", "codec_name": "ffv1"}, {"codec_type": "audio", "codec_name": "pcm_s24le"}]
        output_dicts = [{"codec_type": "video", "codec_name": "ffv1"}]  # dropped a stream
        calls = {"n": 0}

        def fake_run(cmd, **kwargs):
            calls["n"] += 1
            return self._streams_result(source_dicts if calls["n"] == 1 else output_dicts)

        with patch('vdg.cli.subprocess.run', side_effect=fake_run), \
             patch('vdg.cli.get_video_info', return_value=types.SimpleNamespace(duration=10.0)):
            ok, messages = validate_trim_output(Path("src.mkv"), Path("out.mkv"), 0.0, 10.0)
        assert ok is False
        assert any("Structural check FAILED" in m for m in messages)

    def test_duration_outside_tolerance_fails(self):
        streams_dicts = [{"codec_type": "video", "codec_name": "ffv1"}]

        def fake_run(cmd, **kwargs):
            if 'ffprobe' in cmd[0]:
                return self._streams_result(streams_dicts)
            return MagicMock(returncode=0, stderr="")

        with patch('vdg.cli.subprocess.run', side_effect=fake_run), \
             patch('vdg.cli.get_video_info', return_value=types.SimpleNamespace(duration=50.0)):
            # requested span 10s, actual output 50s — way outside the 15s tolerance
            ok, messages = validate_trim_output(Path("src.mkv"), Path("out.mkv"), 0.0, 10.0)
        assert ok is False
        assert any("Duration check FAILED" in m for m in messages)

    def test_decode_failure_fails(self):
        streams_dicts = [{"codec_type": "video", "codec_name": "ffv1"}]

        def fake_run(cmd, **kwargs):
            if 'ffprobe' in cmd[0]:
                return self._streams_result(streams_dicts)
            return MagicMock(returncode=1, stderr="Error decoding stream")

        with patch('vdg.cli.subprocess.run', side_effect=fake_run), \
             patch('vdg.cli.get_video_info', return_value=types.SimpleNamespace(duration=10.0)):
            ok, messages = validate_trim_output(Path("src.mkv"), Path("out.mkv"), 0.0, 10.0)
        assert ok is False
        assert any("Full decode validation FAILED" in m for m in messages)

    def test_decode_timeout_scales_with_requested_duration(self):
        """Regression test for a real production failure (video1, 2026-10):
        a 1-hour FFV1 trim's full decode validation failed with 'Validation
        timeout' because the decode subprocess used the flat
        VALIDATION_TIMEOUT (300s / 5 min) — sized for the much smaller/faster
        H.264 derivative check in validate_output(), not a full-length
        lossless master. The decode timeout must scale with the requested
        span instead of staying flat."""
        streams_dicts = [{"codec_type": "video", "codec_name": "ffv1"}]
        captured_kwargs = {}

        def fake_run(cmd, **kwargs):
            if 'ffprobe' in cmd[0]:
                return self._streams_result(streams_dicts)
            captured_kwargs.update(kwargs)
            return MagicMock(returncode=0, stderr="")

        with patch('vdg.cli.subprocess.run', side_effect=fake_run), \
             patch('vdg.cli.get_video_info', return_value=types.SimpleNamespace(duration=3600.0)):
            # 1-hour requested span, same as the real failure
            validate_trim_output(Path("src.mkv"), Path("out.mkv"), 0.0, 3600.0)

        # Flat VALIDATION_TIMEOUT (300s) would have been nowhere near enough
        # for a 3600s request — the scaled timeout must exceed it by a lot.
        from vdg.cli import VALIDATION_TIMEOUT
        assert captured_kwargs['timeout'] > VALIDATION_TIMEOUT
        assert captured_kwargs['timeout'] >= 3600 * 4  # at least the 1/4-real-time decode budget

    def test_decode_timeout_floor_for_short_trims(self):
        """A short requested span shouldn't shrink the decode timeout below
        the caller's own default/explicit timeout."""
        streams_dicts = [{"codec_type": "video", "codec_name": "ffv1"}]
        captured_kwargs = {}

        def fake_run(cmd, **kwargs):
            if 'ffprobe' in cmd[0]:
                return self._streams_result(streams_dicts)
            captured_kwargs.update(kwargs)
            return MagicMock(returncode=0, stderr="")

        from vdg.cli import VALIDATION_TIMEOUT
        with patch('vdg.cli.subprocess.run', side_effect=fake_run), \
             patch('vdg.cli.get_video_info', return_value=types.SimpleNamespace(duration=10.0)):
            validate_trim_output(Path("src.mkv"), Path("out.mkv"), 0.0, 10.0)

        assert captured_kwargs['timeout'] >= VALIDATION_TIMEOUT
