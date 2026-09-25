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
    parse_dv_system,
    dv_nominal_fps,
    add_audio_async_filter,
    AUDIO_ASYNC_FILTER,
    VideoStandard,
    ROLE_CODES,
)


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
