"""Pure unit tests for RSS/site podcast ad-read detection, validation,
chapter merging, opt-in cutting, and ytdlp integration."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from medialib.cli import ytdlp
from medialib.lib import aichapters, aipodcastads, llmclient, llmroster, modelmeter

pytestmark = pytest.mark.pure


def _cues() -> list[aichapters.Cue]:
    return [
        aichapters.Cue(0, 30_000, "Welcome back to the show, today we talk compilers."),
        aichapters.Cue(60_000, 120_000, "This episode is brought to you by Acme Mattresses."),
        aichapters.Cue(120_000, 500_000, "Now back to our deep dive into SSA form."),
        aichapters.Cue(500_000, 560_000, "Support our Patreon for bonus episodes."),
        aichapters.Cue(560_000, 900_000, "Closing thoughts and listener questions."),
    ]


class _FakeBound:
    def __init__(self, replies: list[str], *, fail: bool = False):
        self.replies = list(replies)
        self.fail = fail
        self.calls: list[tuple[str, str]] = []

    def ask(self, system: str, user: str, tally=None) -> str:
        self.calls.append((system, user))
        if self.fail:
            raise llmclient.LlmError("server unreachable")
        if tally is not None:
            tally.add(0.4, 42, read=180)
        return self.replies.pop(0) if self.replies else '{"spans": []}'


class _FakeLlm:
    def __init__(self, replies: list[str], *, name: str = "fake-llm",
                 context_tokens: int = 32768, fail: bool = False):
        self.name = name
        self.context_tokens = context_tokens
        self.bound = _FakeBound(replies, fail=fail)

    def serving(self, _detail=None):
        bound = self.bound

        class _Ctx:
            def __enter__(self):
                return bound

            def __exit__(self, *exc):
                return False

        return _Ctx()


# --- roster & settings switches ---------------------------------------------

def test_podcast_ads_task_in_roster():
    assert "podcast-ads" in llmroster.TASKS
    task = llmroster.TASKS["podcast-ads"]
    assert task is llmroster.PODCAST_ADS
    assert task.prefers == ("reasoning",)
    assert task.lean == "balanced"
    assert task.requires == ()


def test_profile_eligibility():
    assert aipodcastads.eligible_profile("rssAudio")
    assert aipodcastads.eligible_profile("rssVideo")
    assert aipodcastads.eligible_profile("siteVideo")
    assert not aipodcastads.eligible_profile("youtubeAudio")
    assert not aipodcastads.eligible_profile("youtubeVideo")


def test_switches_default_and_overrides(monkeypatch):
    monkeypatch.delenv(aipodcastads.SWITCH_VARIABLE, raising=False)
    monkeypatch.delenv(aipodcastads.CUT_VARIABLE, raising=False)
    assert aipodcastads.enabled() is True
    assert aipodcastads.cut_enabled() is False

    monkeypatch.setenv(aipodcastads.SWITCH_VARIABLE, "off")
    assert aipodcastads.enabled() is False
    monkeypatch.setenv(aipodcastads.CUT_VARIABLE, "on")
    assert aipodcastads.cut_enabled() is True


# --- timestamp parsing, JSON parsing, strict span validation ----------------

@pytest.mark.parametrize("raw,expected", [
    (0, 0),
    (65, 65_000),
    (12.5, 12_500),
    ("90", 90_000),
    ("01:30", 90_000),
    ("00:05:10", 310_000),
    ("1:02:03.25", 3_723_250),
    ("00:01:02,500", 62_500),
])
def test_parse_timestamp_ms_valid(raw, expected):
    assert aipodcastads.parse_timestamp_ms(raw) == expected


@pytest.mark.parametrize("raw", [
    None, True, False, -5, float("nan"), float("inf"), "", "abc",
    "00:65:00", "00:05:60", "10:70",
])
def test_parse_timestamp_ms_invalid(raw):
    assert aipodcastads.parse_timestamp_ms(raw) is None


def test_parse_spans_accepts_wrapper_keys_and_fenced_json():
    fenced = 'Sure!\n```json\n{"spans": [{"start": 60, "end": 120, "kind": "sponsor"}]}\n```'
    assert aipodcastads.parse_spans(fenced) == [{"start": 60, "end": 120, "kind": "sponsor"}]
    ads_json = '{"ads": [{"start": "01:00", "end": "02:00", "kind": "ad"}]}'
    assert aipodcastads.parse_spans(ads_json) == [
        {"start": "01:00", "end": "02:00", "kind": "ad"}
    ]
    assert aipodcastads.parse_spans("not json at all") == []
    assert aipodcastads.parse_spans('{"other": 1}') == []


def test_validate_spans_bounds_kinds_and_sorting():
    raw = [
        {"start": "00:08:20", "end": "00:09:20", "kind": "self-promo"},
        {"start": 60, "end": 120, "kind": "Sponsor"},
        {"start": -5, "end": 30, "kind": "ad"},          # negative start -> dropped
        {"start": 200, "end": 200, "kind": "ad"},        # start == end -> dropped
        {"start": 210, "end": 212, "kind": "ad"},        # < 5s -> dropped
        {"start": 100, "end": 800, "kind": "ad"},        # > 600s -> dropped
        {"start": 850, "end": 950, "kind": "ad"},        # past duration -> dropped
        {"start": 300, "end": 330, "kind": "unknown"},   # unknown kind -> dropped
    ]
    spans, reason = aipodcastads.validate_spans(raw, duration_ms=900_000)
    assert reason == ""
    assert spans == [
        aipodcastads.AdSpan(60_000, 120_000, "sponsor"),
        aipodcastads.AdSpan(500_000, 560_000, "selfpromo"),
    ]


def test_validate_spans_rejects_overlaps():
    raw = [
        {"start": 60, "end": 140, "kind": "sponsor"},
        {"start": 120, "end": 180, "kind": "ad"},
    ]
    spans, reason = aipodcastads.validate_spans(raw, duration_ms=900_000)
    assert spans == []
    assert "overlapping" in reason


def test_validate_spans_rejects_over_fifty_percent_share():
    raw = [
        {"start": 0, "end": 260, "kind": "sponsor"},
        {"start": 300, "end": 560, "kind": "ad"},
    ]
    # 520s out of 1000s = 52% > 50%
    spans, reason = aipodcastads.validate_spans(raw, duration_ms=1_000_000)
    assert spans == []
    assert "52%" in reason


def test_validate_spans_allows_exact_fifty_percent_share():
    raw = [
        {"start": 0, "end": 250, "kind": "sponsor"},
        {"start": 300, "end": 550, "kind": "ad"},
    ]
    spans, reason = aipodcastads.validate_spans(raw, duration_ms=1_000_000)
    assert reason == ""
    assert len(spans) == 2


# --- chapter merging (default mode) -----------------------------------------

def _ogm(pairs: list[tuple[int, str]]) -> list[str]:
    return aichapters.chapter_rows(pairs)


def test_merge_chapters_midroll_preserves_and_resumes_real_chapter():
    existing = _ogm([(0, "Intro"), (300_000, "Main Topic"), (600_000, "Outro")])
    ads = [aipodcastads.AdSpan(400_000, 460_000, "sponsor")]
    merged = aipodcastads.merge_chapters(existing, ads, duration_ms=900_000)
    parsed = aipodcastads._parse_ogm_rows(merged)
    assert parsed == [
        (0, "Intro"),
        (300_000, "Main Topic"),
        (400_000, "[Sponsor]"),
        (460_000, "Main Topic (cont.)"),
        (600_000, "Outro"),
    ]
    assert aipodcastads.has_ad_chapters(merged)


def test_merge_chapters_preroll_and_postroll_and_adjacent_ads():
    existing = _ogm([(0, "Opening"), (300_000, "Deep Dive")])
    ads = [
        aipodcastads.AdSpan(0, 45_000, "ad"),
        aipodcastads.AdSpan(45_000, 90_000, "selfpromo"),
        aipodcastads.AdSpan(840_000, 900_000, "sponsor"),
    ]
    merged = aipodcastads.merge_chapters(existing, ads, duration_ms=900_000)
    parsed = aipodcastads._parse_ogm_rows(merged)
    assert parsed == [
        (0, "[Ad]"),
        (45_000, "[Self-promo]"),
        (90_000, "Opening"),
        (300_000, "Deep Dive"),
        (840_000, "[Sponsor]"),
    ]


def test_merge_chapters_when_episode_had_no_chapters():
    ads = [aipodcastads.AdSpan(120_000, 180_000, "sponsor")]
    merged = aipodcastads.merge_chapters([], ads, duration_ms=600_000)
    parsed = aipodcastads._parse_ogm_rows(merged)
    assert parsed == [
        (0, "Episode"),
        (120_000, "[Sponsor]"),
        (180_000, "Episode"),
    ]


# --- opt-in cut planning & safe execution -----------------------------------

def test_keep_segments_and_shift_chapters():
    ads = [
        aipodcastads.AdSpan(0, 30_000, "ad"),
        aipodcastads.AdSpan(300_000, 360_000, "sponsor"),
        aipodcastads.AdSpan(840_000, 900_000, "selfpromo"),
    ]
    kept = aipodcastads.keep_segments(ads, duration_ms=900_000)
    assert kept == [(30_000, 300_000), (360_000, 840_000)]

    existing = _ogm([(0, "Intro"), (150_000, "Part 1"), (330_000, "Part 2"), (600_000, "Part 3")])
    shifted = aipodcastads._parse_ogm_rows(aipodcastads.shift_chapters_for_cut(existing, kept))
    assert shifted == [
        (0, "Intro"),
        (120_000, "Part 1"),
        (270_000, "Part 2"),
        (510_000, "Part 3"),
    ]


def test_ffmpeg_cut_filter_audio_and_video():
    kept = [(0, 60_000), (120_000, 500_000)]
    audio_graph = aipodcastads.ffmpeg_cut_filter(kept, has_video=False)
    assert audio_graph == (
        "[0:a]atrim=start=0.000:end=60.000,asetpts=PTS-STARTPTS[a0];"
        "[0:a]atrim=start=120.000:end=500.000,asetpts=PTS-STARTPTS[a1];"
        "[a0][a1]concat=n=2:v=0:a=1[aout]"
    )
    audio_argv = aipodcastads.ffmpeg_cut_argv("in.opus", "out.opus", kept, has_video=False)
    assert "-filter_complex" in audio_argv
    assert audio_argv[audio_argv.index("-filter_complex") + 1] == audio_graph
    assert "[aout]" in audio_argv
    assert "[vout]" not in audio_argv

    video_graph = aipodcastads.ffmpeg_cut_filter(kept, has_video=True)
    assert "[0:v]trim=start=0.000:end=60.000,setpts=PTS-STARTPTS[v0]" in video_graph
    assert "concat=n=2:v=1:a=1[vout][aout]" in video_graph
    video_argv = aipodcastads.ffmpeg_cut_argv("in.mkv", "out.mkv", kept, has_video=True)
    assert "[vout]" in video_argv and "[aout]" in video_argv


def test_cut_ads_stages_and_atomically_replaces_or_preserves_on_failure(tmp_path):
    media = tmp_path / "ep.opus"
    media.write_bytes(b"original-audio")
    os.utime(media, (1_700_000_000, 1_700_000_000))
    ads = [aipodcastads.AdSpan(60_000, 120_000, "sponsor")]

    def fake_run_ok(argv):
        dst = argv[-1]
        with open(dst, "wb") as handle:
            handle.write(b"cut-audio")
        return SimpleNamespace(returncode=0)

    status = aipodcastads.cut_ads(
        str(media), ads, duration_ms=900_000, tmp_dir=str(tmp_path / "work"),
        run=fake_run_ok, duration_probe=lambda _p: 840_000,
    )
    assert status == 0
    assert media.read_bytes() == b"cut-audio"
    assert int(os.stat(media).st_mtime) == 1_700_000_000

    # Failure: mismatched output duration leaves target untouched.
    media.write_bytes(b"original-again")
    status_bad = aipodcastads.cut_ads(
        str(media), ads, duration_ms=900_000, tmp_dir=str(tmp_path / "work2"),
        run=fake_run_ok, duration_probe=lambda _p: 200_000,
    )
    assert status_bad != 0
    assert media.read_bytes() == b"original-again"

    # Refusal: >50% spans never invoke ffmpeg and leave target untouched.
    called = False

    def fail_if_called(_argv):
        nonlocal called
        called = True
        return SimpleNamespace(returncode=0)

    huge_ads = [
        aipodcastads.AdSpan(0, 300_000, "sponsor"),
        aipodcastads.AdSpan(350_000, 650_000, "ad"),
    ]
    assert aipodcastads.cut_ads(
        str(media), huge_ads, duration_ms=1_000_000, run=fail_if_called,
    ) != 0
    assert not called
    assert media.read_bytes() == b"original-again"


# --- detect_ads & process_episode gating, logging, modelmeter ---------------

def test_detect_ads_records_modelmeter_and_logs(monkeypatch, tmp_path):
    monkeypatch.setenv(aipodcastads.SWITCH_VARIABLE, "on")
    meter = str(tmp_path / "ads.meter")
    lines: list[str] = []
    llm = _FakeLlm(['{"spans": [{"start": "00:01:00", "end": "00:02:00", "kind": "sponsor"}]}'])

    spans, reason = aipodcastads.detect_ads(
        _cues(), 900_000, title="ep01.opus", models=[llm], meter_file=meter, log=lines.append
    )
    assert reason == ""
    assert spans == [aipodcastads.AdSpan(60_000, 120_000, "sponsor")]
    assert any("1 ad stretch(es) found" in line for line in lines)
    assert any("podcast-ads:" in line for line in lines)

    recorded_spans, tally = modelmeter.gathered(meter)
    assert tally.requests == 1
    assert tally.tokens == 42
    assert tally.read == 180
    assert recorded_spans.seconds >= 0.0


def test_detect_ads_handles_switch_off_no_model_and_malformed_json(monkeypatch):
    # Switch off
    monkeypatch.setenv(aipodcastads.SWITCH_VARIABLE, "off")
    llm = _FakeLlm(['{"spans": [{"start": 60, "end": 120, "kind": "sponsor"}]}'])
    spans, reason = aipodcastads.detect_ads(_cues(), 900_000, models=[llm], log=lambda _: None)
    assert spans == [] and reason == "disabled"
    assert llm.bound.calls == []

    # No model available in roster
    monkeypatch.setenv(aipodcastads.SWITCH_VARIABLE, "on")
    monkeypatch.setattr(llmroster, "optional", lambda _task: ([], "no models configured"))
    spans, reason = aipodcastads.detect_ads(_cues(), 900_000, models=None, log=lambda _: None)
    assert spans == [] and reason == "no models configured"

    # Malformed JSON from model
    bad_llm = _FakeLlm(["I could not parse this cleanly, sorry!"])
    messages: list[str] = []
    spans, reason = aipodcastads.detect_ads(
        _cues(), 900_000, models=[bad_llm], log=messages.append
    )
    assert spans == [] and reason == ""
    assert any("no ad reads found" in msg for msg in messages)


def test_process_episode_skips_youtube_and_missing_transcript(monkeypatch, tmp_path):
    monkeypatch.setenv(aipodcastads.SWITCH_VARIABLE, "on")
    monkeypatch.setenv(aipodcastads.CUT_VARIABLE, "off")
    media = tmp_path / "ep.opus"
    media.write_bytes(b"audio")
    srt = tmp_path / "ep.srt"
    srt.write_text("1\n00:01:00,000 --> 00:02:00,000\nSponsor message here.\n", encoding="utf-8")

    llm = _FakeLlm(['{"spans": [{"start": 60, "end": 120, "kind": "sponsor"}]}'])
    # YouTube profile uses SponsorBlock -> skipped without calling the model.
    assert aipodcastads.process_episode(
        str(media), "youtubeAudio", models=[llm],
        duration_probe=lambda _p: 900_000, chapter_probe=lambda _p: "",
    ) == "skipped"
    assert llm.bound.calls == []

    # Missing transcript -> skipped without calling the model.
    srt.unlink()
    assert aipodcastads.process_episode(
        str(media), "rssAudio", models=[llm],
        duration_probe=lambda _p: 900_000, chapter_probe=lambda _p: "",
    ) == "skipped"
    assert llm.bound.calls == []


def test_process_episode_marks_chapters_by_default_and_cuts_only_with_opt_in(monkeypatch, tmp_path):
    monkeypatch.setenv(aipodcastads.SWITCH_VARIABLE, "on")
    monkeypatch.setenv(aipodcastads.CUT_VARIABLE, "off")
    media = tmp_path / "ep.opus"
    media.write_bytes(b"audio")
    srt = tmp_path / "ep.srt"
    srt.write_text(
        "1\n00:00:00,000 --> 00:01:00,000\nIntro chat.\n\n"
        "2\n00:01:00,000 --> 00:02:00,000\nBrought to you by Sponsor.\n\n"
        "3\n00:02:00,000 --> 00:15:00,000\nMain interview.\n",
        encoding="utf-8",
    )

    written: list[list[str]] = []
    monkeypatch.setattr(
        aipodcastads.chapters,
        "write_chapters",
        lambda _path, rows, _tmp, **_kw: (written.append(list(rows)), 0)[1],
    )
    cut_calls: list[list[aipodcastads.AdSpan]] = []
    monkeypatch.setattr(
        aipodcastads,
        "cut_ads",
        lambda _path, spans, _dur, **_kw: (cut_calls.append(list(spans)), 0)[1],
    )

    llm_mark = _FakeLlm(['{"spans": [{"start": 60, "end": 120, "kind": "sponsor"}]}'])
    outcome = aipodcastads.process_episode(
        str(media), "rssAudio", models=[llm_mark],
        duration_probe=lambda _p: 900_000,
        chapter_probe=lambda _p: (
            "chapters.chapter.0.start_time=\"0.000000\"\n"
            "chapters.chapter.0.tags.title=\"Intro\"\n"
            "chapters.chapter.1.start_time=\"300.000000\"\n"
            "chapters.chapter.1.tags.title=\"Interview\"\n"
        ),
    )
    assert outcome == "marked"
    assert cut_calls == []
    assert len(written) == 1
    assert aipodcastads._parse_ogm_rows(written[0]) == [
        (0, "Intro"),
        (60_000, "[Sponsor]"),
        (120_000, "Intro (cont.)"),
        (300_000, "Interview"),
    ]

    # Idempotent in mark mode: episode already carrying ad chapters is skipped.
    llm_repeat = _FakeLlm(['{"spans": [{"start": 60, "end": 120, "kind": "sponsor"}]}'])
    assert aipodcastads.process_episode(
        str(media), "rssAudio", models=[llm_repeat],
        duration_probe=lambda _p: 900_000,
        chapter_probe=lambda _p: (
            'chapters.chapter.0.start_time="0.000000"\n'
            'chapters.chapter.0.tags.title="Intro"\n'
            'chapters.chapter.1.start_time="60.000000"\n'
            'chapters.chapter.1.tags.title="[Sponsor]"\n'
        ),
    ) == "skipped"
    assert llm_repeat.bound.calls == []

    # Opt-in cut mode: cuts instead of only marking.
    monkeypatch.setenv(aipodcastads.CUT_VARIABLE, "on")
    llm_cut = _FakeLlm(['{"spans": [{"start": 60, "end": 120, "kind": "sponsor"}]}'])
    outcome_cut = aipodcastads.process_episode(
        str(media), "rssAudio", models=[llm_cut],
        duration_probe=lambda _p: 900_000, chapter_probe=lambda _p: "",
    )
    assert outcome_cut == "cut"
    assert len(cut_calls) == 1
    assert cut_calls[0] == [aipodcastads.AdSpan(60_000, 120_000, "sponsor")]


def test_ytdlp_library_queue_checks_ads_only_for_eligible_profile(monkeypatch, tmp_path):
    monkeypatch.setenv(aipodcastads.SWITCH_VARIABLE, "on")
    status_dir = tmp_path / "status"
    status_dir.mkdir()
    staging_speech = tmp_path / "Staging" / "Speech"
    staging_music = tmp_path / "Staging" / "Music"
    (staging_speech / "ShowA").mkdir(parents=True)
    (staging_music / "ChanB").mkdir(parents=True)
    ingested = tmp_path / "Ingested"
    ingested.mkdir()

    def fake_run_command(name, argv, **_kw):
        if name == "convert-audio":
            only_subdir = argv[argv.index("-O") + 1]
            out_dir = ingested / only_subdir
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "ep1.opus").write_bytes(b"converted")
        return SimpleNamespace(returncode=0)

    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(ytdlp.commands, "run_command", fake_run_command)
    monkeypatch.setattr(
        aipodcastads,
        "process_episode",
        lambda path, profile, **_kw: (seen.append((os.path.basename(path), profile)), "marked")[1],
    )

    state = SimpleNamespace(
        status_dir=str(status_dir),
        script_dir=str(tmp_path),
        ingest={
            "root": str(ingested),
            "staging": {"Speech": str(staging_speech), "Music": str(staging_music)},
            "args": {"Speech": "-c -m -C", "Music": "-c -b 65 -C"},
        },
    )
    queue = ytdlp.LibraryQueue(state)  # type: ignore[arg-type]
    queue.waiting = {"Speech": [("ShowA", 1)], "Music": [("ChanB", 1)]}
    queue._convert("Speech")
    queue._convert("Music")

    assert seen == [("ep1.opus", "rssAudio")]
