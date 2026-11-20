"""Pure unit tests for per-file vision profile selection (convert-video -p auto)."""

from __future__ import annotations

import io
import os
from contextlib import contextmanager
from pathlib import Path

import pytest

from medialib.cli import convert_video as cv
from medialib.cli import convert_video_run as cv_run
from medialib.cli import convert_video_startup as cv_startup
from medialib.lib import (
    aivideoprofile,
    llmclient,
    llmroster,
    llmtraits,
    modelmeter,
    safety,
    videograin,
)

pytestmark = pytest.mark.pure


class _FakeBound:
    """Minimal stand-in for a bound llmclient.Llm inside ``with llm.serving():``."""

    def __init__(self, *, name: str = "vision-local", model: str = "gemma3-12b",
                 sees_images: bool = True, answer: str = "",
                 error: Exception | None = None,
                 prompt_tokens: int = 1200, completion_tokens: int = 24) -> None:
        self.name = name
        self.model = model
        self._sees_images = sees_images
        self._answer = answer
        self._error = error
        self._prompt_tokens = prompt_tokens
        self._completion_tokens = completion_tokens
        self.calls: list[tuple[str, str, list[str]]] = []

    def sees_images(self) -> bool:
        return self._sees_images

    def ask(self, system: str, user: str, tally: modelmeter.Tally | None = None,
            *, images=()) -> str:
        self.calls.append((system, user, list(images)))
        if self._error is not None:
            raise self._error
        if tally is not None:
            tally.add(0.4, self._completion_tokens, self._prompt_tokens)
        return self._answer

    @contextmanager
    def serving(self, detail=None):
        yield self


def _fake_extractor(succeed_for: set[int] | None = None):
    """Frame extractor that creates tiny JPEG files without invoking ffmpeg."""
    seen: list[tuple[str, str, str]] = []

    def extract(source: str, timestamp: str, dest: str,
                decode_accel_args: str = "") -> bool:
        seen.append((source, timestamp, decode_accel_args))
        index = len(seen)
        if succeed_for is not None and index not in succeed_for:
            return False
        Path(dest).write_bytes(b"\xff\xd8\xff\xd9")
        return True

    return extract, seen


class TestSampleTimestamps:
    def test_default_samples_span_interior_avoiding_intros_and_credits(self):
        duration = 6000.0
        stamps = aivideoprofile.sample_timestamps(duration)
        assert 4 <= len(stamps) <= 6
        assert len(stamps) == aivideoprofile.DEFAULT_SAMPLES
        values = [float(s) for s in stamps]
        assert values == sorted(values)
        assert len(set(values)) == len(values)
        assert values[0] >= duration * 0.10
        assert values[-1] <= duration * 0.90

    @pytest.mark.parametrize("count", [4, 5, 6])
    def test_requested_sample_counts_are_strictly_increasing(self, count: int):
        stamps = aivideoprofile.sample_timestamps(1000.0, count)
        assert len(stamps) == count
        values = [float(s) for s in stamps]
        assert values[0] == pytest.approx(120.0)
        assert values[-1] == pytest.approx(880.0)
        assert all(a < b for a, b in zip(values[:-1], values[1:], strict=True))

    def test_single_sample_and_non_positive_inputs(self):
        assert aivideoprofile.sample_timestamps(100.0, 1) == ["50.000"]
        assert aivideoprofile.sample_timestamps(0.0) == []
        assert aivideoprofile.sample_timestamps(-10.0) == []
        assert aivideoprofile.sample_timestamps(100.0, 0) == []


class TestParseAnswerAndMapping:
    @pytest.mark.parametrize(
        ("kind", "expected_profile"),
        [
            ("live_action", "av1BluRay"),
            ("animation_2d", "av1Animation"),
            ("cgi_3d", "av1BluRay"),
            ("heavy_grain", "av1Grain"),
        ],
    )
    def test_valid_kinds_map_to_exact_convert_video_profiles(
        self, kind: str, expected_profile: str
    ):
        answer = '{"kind": "%s", "why": "clear visual match"}' % kind
        pick = aivideoprofile.parse_answer(answer)
        assert pick.profile == expected_profile
        assert pick.kind == kind
        assert pick.why == "clear visual match"
        assert pick.fallback is False

    def test_parses_fenced_and_case_varied_json(self):
        answer = (
            "Here is the classification:\n"
            "```json\n"
            '{"kind": "  Animation_2D ", "why": "flat cel shading  and ink outlines"}\n'
            "```"
        )
        pick = aivideoprofile.parse_answer(answer)
        assert pick.profile == "av1Animation"
        assert pick.kind == "animation_2d"
        assert pick.why == "flat cel shading and ink outlines"
        assert pick.fallback is False

    @pytest.mark.parametrize(
        "answer",
        [
            '{"kind": null, "why": "frames are mostly black"}',
            '{"kind": "stop_motion", "why": "claymation"}',
            '{"kind": 42, "why": "numeric"}',
            '{"why": "forgot kind"}',
            "not json at all",
            "",
        ],
    )
    def test_unsure_or_invalid_answers_fall_back_to_default_profile(
        self, answer: str
    ):
        pick = aivideoprofile.parse_answer(answer)
        assert pick.profile == aivideoprofile.DEFAULT_PROFILE
        assert pick.fallback is True
        assert pick.why

    def test_every_mapped_profile_is_real_svtav1_profile_with_expected_grain(self):
        targets = set(aivideoprofile.CONTENT_PROFILES.values()) | {
            aivideoprofile.DEFAULT_PROFILE
        }
        for name in targets:
            args = cv.profile_args(cv.VIDEO_PROFILES, name)
            assert cv.encoder_of(args) == "libsvtav1"
            assert cv.video_quality_flag(args) == "-crf"
        assert videograin.grain_default_for("av1Animation") == "0"
        assert videograin.grain_default_for("av1BluRay") == "probe"


class TestRosterTask:
    def test_video_profile_task_requires_vision_and_leans_light(self):
        task = llmroster.TASKS["video-profile"]
        assert task is llmroster.VIDEO_PROFILE
        assert task.requires == ("vision",)
        assert task.lean == "light"

    def test_roster_picks_light_vision_model_and_excludes_text_only(
        self, tmp_path, monkeypatch
    ):
        llmroster.forget()
        llmtraits.forget()
        monkeypatch.setattr(llmtraits, "_server_json", lambda *a, **k: None)
        for name in llmclient.SETTING_FIELDS.values():
            monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(llmroster.LEAN_VARIABLE, raising=False)
        models_file = tmp_path / "models.toml"
        models_file.write_text(
            '[models.text-local]\n'
            'url = "http://localhost:8081/v1"\n'
            'model = "qwen3-14b"\n'
            'traits = { vision = false, size = "small", speed = "fast", cheap = true }\n'
            '\n'
            '[models.claude]\n'
            'api = "anthropic"\n'
            'model = "claude-opus-5-5"\n'
            'traits = { vision = true, size = "large", speed = "slow", cost_out = 25 }\n'
            '\n'
            '[models.vision-local]\n'
            'url = "http://localhost:8082/v1"\n'
            'model = "gemma3-12b"\n'
            'traits = { vision = true, size = "small", speed = "fast", cheap = true }\n',
            encoding="utf-8",
        )
        monkeypatch.setenv(llmroster.MODELS_VARIABLE, str(models_file))
        roster = llmroster.load()
        ladder = roster.ladder(llmroster.VIDEO_PROFILE)
        assert [m.name for m in ladder] == ["vision-local", "claude"]


class TestClassifyAndMetering:
    def test_classify_logs_pick_records_meter_and_cleans_up_frames(self, tmp_path):
        bound = _FakeBound(
            answer='{"kind": "animation_2d", "why": "drawn outlines and cel shading"}',
            prompt_tokens=2000, completion_tokens=30,
        )
        extractor, seen = _fake_extractor()
        messages: list[str] = []
        run_spans = modelmeter.Spans()
        run_tally = modelmeter.Tally()
        meter_file = str(tmp_path / "profile.meter")

        pick = aivideoprofile.classify(
            "show.mkv", "Anime/show.mkv", 1440.0, str(tmp_path),
            models=[bound],
            meter_file=meter_file,
            run_spans=run_spans,
            run_tally=run_tally,
            extractor=extractor,
            log=messages.append,
        )

        assert pick.profile == "av1Animation"
        assert pick.kind == "animation_2d"
        assert pick.fallback is False
        assert len(seen) == aivideoprofile.DEFAULT_SAMPLES
        assert len(bound.calls) == 1
        assert len(bound.calls[0][2]) == aivideoprofile.DEFAULT_SAMPLES
        # Temporary frames directory removed after classification.
        assert not (tmp_path / "profile_frames").exists()
        # Meter recorded both in-memory and to disk.
        assert run_tally.requests == 1
        assert run_tally.read == 2000
        assert run_tally.tokens == 30
        assert os.path.isfile(meter_file)
        file_spans, file_tally = modelmeter.gathered(meter_file)
        assert file_spans.seconds >= 0.0
        assert file_tally.requests == 1
        assert file_tally.read == 2000
        assert file_tally.tokens == 30
        # Per-file log reports the model rate line and the profile + reason.
        joined = "\n".join(messages)
        assert "profile:" in joined
        assert "av1Animation" in joined
        assert "drawn outlines and cel shading" in joined
        assert "Anime/show.mkv" in joined

    def test_classify_falls_back_on_llm_error_and_cleans_up_frames(self, tmp_path):
        bound = _FakeBound(error=llmclient.LlmError("server timeout"))
        extractor, _seen = _fake_extractor()
        messages: list[str] = []

        pick = aivideoprofile.classify(
            "film.mkv", "film.mkv", 5400.0, str(tmp_path),
            models=[bound], extractor=extractor, log=messages.append,
        )

        assert pick.profile == aivideoprofile.DEFAULT_PROFILE
        assert pick.fallback is True
        assert not (tmp_path / "profile_frames").exists()
        joined = "\n".join(messages)
        assert "server timeout" in joined
        assert "default fallback" in joined

    def test_classify_falls_back_when_no_frames_extracted(self, tmp_path):
        bound = _FakeBound(answer='{"kind": "animation_2d", "why": "unused"}')
        extractor, _seen = _fake_extractor(succeed_for=set())
        messages: list[str] = []

        pick = aivideoprofile.classify(
            "broken.mkv", "broken.mkv", 1200.0, str(tmp_path),
            models=[bound], extractor=extractor, log=messages.append,
        )

        assert pick.profile == aivideoprofile.DEFAULT_PROFILE
        assert pick.fallback is True
        assert bound.calls == []
        assert not (tmp_path / "profile_frames").exists()

    def test_classify_skips_model_without_vision_capability(self, tmp_path):
        blind = _FakeBound(name="blind", sees_images=False)
        sighted = _FakeBound(
            name="sighted", sees_images=True,
            answer='{"kind": "heavy_grain", "why": "coarse 16mm grain"}',
        )
        extractor, _seen = _fake_extractor()
        messages: list[str] = []

        pick = aivideoprofile.classify(
            "scan.mkv", "scan.mkv", 3600.0, str(tmp_path),
            models=[blind, sighted], extractor=extractor, log=messages.append,
        )

        assert pick.profile == "av1Grain"
        assert pick.fallback is False
        assert blind.calls == []
        assert len(sighted.calls) == 1

    def test_extract_frames_stops_on_abort(self, tmp_path, monkeypatch):
        monkeypatch.setattr(safety, "abort_requested", lambda: True)
        extractor, seen = _fake_extractor()
        frames = aivideoprofile.extract_frames(
            "clip.mkv", 1000.0, str(tmp_path / "frames"), extractor=extractor,
        )
        assert frames == []
        assert seen == []


class TestConvertVideoIntegration:
    def test_resolve_video_profile_accepts_auto_and_keeps_strict_lookup(self):
        args, is_auto = cv.resolve_video_profile("auto")
        assert is_auto is True
        assert args == cv.profile_args(cv.video_profiles(), "av1BluRay")

        fixed_args, fixed_auto = cv.resolve_video_profile("av1Animation")
        assert fixed_auto is False
        assert fixed_args == cv.profile_args(cv.video_profiles(), "av1Animation")

        with pytest.raises(cv.UnknownProfile):
            cv.resolve_video_profile("nonexistentProfile")

    def test_startup_validate_accepts_auto_with_svtav1_options_and_refuses_e(self):
        video_args, is_auto = cv.resolve_video_profile("auto")
        base = cv.Settings(
            video_profile="auto", auto_video_profile=is_auto,
            audio_profile="opus", cores=8,
        )
        assert cv_startup._validate(base, video_args, "", "") == ""

        with_opts = cv.Settings(
            video_profile="auto", auto_video_profile=is_auto,
            audio_profile="opus", cores=8,
            quality="25", quality_given=True, fast_decode="1",
        )
        assert cv_startup._validate(with_opts, video_args, "12", "") == ""

        with_nvenc_e = cv.Settings(
            video_profile="auto", auto_video_profile=is_auto,
            audio_profile="opus", cores=8,
        )
        refusal = cv_startup._validate(with_nvenc_e, video_args, "", "2")
        assert refusal and "-e" in refusal

    def test_per_file_auto_profile_resettles_grain_without_mutating_run_settings(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(cv_run.pausecontrol, "wait_while_paused", lambda: None)
        monkeypatch.setattr(safety, "abort_requested", lambda: False)

        run_settings = cv.Settings(
            input_dir=str(tmp_path / "in"),
            output_dir=str(tmp_path / "out"),
            chunk_root=str(tmp_path / "chunks"),
            video_profile="auto",
            auto_video_profile=True,
            encoder="libsvtav1",
            svt_av1_hdr=True,
            grain_option="",
        )
        run = cv_run.Run(run_settings)
        run.profile_llm = _FakeBound()

        # 1) 2D animation -> av1Animation, skips grain probe (grain_level=0)
        monkeypatch.setattr(
            aivideoprofile, "classify",
            lambda *a, **kw: aivideoprofile.Pick("av1Animation", "animation_2d", "cel"),
        )
        anim_plan = cv_run.Plan("anime.mkv", cv.Settings(**run.settings.__dict__))
        anim_plan.duration = 1400.0
        run._settle_auto_profile(anim_plan)
        assert anim_plan.settings.video_profile == "av1Animation"
        assert anim_plan.settings.grain_probe_wanted is False
        assert anim_plan.settings.grain_level == "0"
        assert anim_plan.settings.grain_tune is False

        # 2) Heavy grain on SVT-AV1-HDR -> av1Grain with grain_tune=True, no probe
        monkeypatch.setattr(
            aivideoprofile, "classify",
            lambda *a, **kw: aivideoprofile.Pick("av1Grain", "heavy_grain", "16mm"),
        )
        grain_plan = cv_run.Plan("scan.mkv", cv.Settings(**run.settings.__dict__))
        grain_plan.duration = 5400.0
        run._settle_auto_profile(grain_plan)
        assert grain_plan.settings.video_profile == "av1Grain"
        assert grain_plan.settings.grain_tune is True
        assert grain_plan.settings.grain_probe_wanted is False

        # 3) Live action -> av1BluRay with grain_probe_wanted=True
        monkeypatch.setattr(
            aivideoprofile, "classify",
            lambda *a, **kw: aivideoprofile.Pick("av1BluRay", "live_action", "film"),
        )
        film_plan = cv_run.Plan("movie.mkv", cv.Settings(**run.settings.__dict__))
        film_plan.duration = 6000.0
        run._settle_auto_profile(film_plan)
        assert film_plan.settings.video_profile == "av1BluRay"
        assert film_plan.settings.grain_tune is False
        assert film_plan.settings.grain_probe_wanted is True

        # 4) Explicit -g off overrides the picked profile's default
        explicit_settings = cv.Settings(**run.settings.__dict__)
        explicit_settings.grain_option = "off"
        off_plan = cv_run.Plan("movie2.mkv", explicit_settings)
        off_plan.duration = 6000.0
        run._settle_auto_profile(off_plan)
        assert off_plan.settings.video_profile == "av1BluRay"
        assert off_plan.settings.grain_probe_wanted is False
        assert off_plan.settings.grain_level == "0"

        # Run-level settings were never mutated by any per-file pick.
        assert run.settings.video_profile == "auto"

    def test_run_footer_reports_profile_model_totals_only_when_used(self, monkeypatch):
        monkeypatch.setattr(cv_run.pausecontrol, "paused_seconds", lambda _now: 0)
        monkeypatch.setattr(cv_run.durationcheck, "report", lambda _out: None)

        run = cv_run.Run(cv.Settings())
        buf = io.StringIO()
        monkeypatch.setattr(cv_run.sys, "stderr", buf)
        run.footer()
        assert "Profile model:" not in buf.getvalue()

        run.profile_tally.add(2.5, 45, 3000)
        run.profile_spans.add(10.0, 12.5)
        buf = io.StringIO()
        monkeypatch.setattr(cv_run.sys, "stderr", buf)
        run.footer()
        out = buf.getvalue()
        assert "Profile model:" in out
        assert "profile:" in out
        assert "1 request" in out
