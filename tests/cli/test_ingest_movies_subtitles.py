"""Whether this ffsubsync can REJECT a bad alignment, and who is told.

The flag that asks for the check is a recent ffsubsync addition, so the run
probes for it once and both sync call sites - the downloaded subtitle and the
whisper transcript - are handed the answer. An older ffsubsync given a flag it
does not know fails argparse and exits non-zero, which both call sites read as a
failed sync and answer by discarding a subtitle that was perfectly good.
"""

import pytest

from medialib.cli import ingest_movies_run as run_module
from medialib.lib import tmdblookup
from medialib.lib import whisper as whisper_lib

pytestmark = pytest.mark.pure


@pytest.fixture
def probe(monkeypatch):
    """The probe, against an ffsubsync whose help page the case writes."""
    def run(help_text=None, watchable=True, **asked):
        logs: list[str] = []
        monkeypatch.setattr(run_module.subtitlefiles, "can_measure_confidence",
                            lambda: watchable)
        monkeypatch.setattr(run_module, "log", logs.append)
        monkeypatch.setattr(run_module, "_has_tool",
                            lambda name: help_text is not None)
        monkeypatch.setattr(run_module, "_tool_help", lambda name: help_text)
        return run_module._settle_ffsubsync_quality(**asked), logs
    return run


_FLAG = "  --skip-sync-on-low-quality  reject bad alignments"


def test_a_run_that_puts_nothing_to_the_confidence_is_not_warned(probe):
    """-a: a transcript is judged by its own offset, never the confidence."""
    answer, logs = probe(_FLAG, watchable=False, judged="")
    assert answer == "yes"
    assert logs == []


def test_the_warning_names_what_the_run_puts_to_the_test(probe):
    _answer, logs = probe(_FLAG, watchable=False, judged="a subtitle")
    assert "so a subtitle is believed only within" in logs[0]


def test_a_caller_with_its_own_refusal_is_not_told_twice(probe):
    answer, logs = probe("  --max-offset-seconds SECONDS",
                         say_unchecked=False)
    assert answer == "no"
    assert logs == []


@pytest.fixture
def gate(monkeypatch):
    """The subtitle-work gate, against the tools the case says are missing."""
    def run(missing, **asked):
        logs: list[str] = []
        monkeypatch.delenv("SKIP_TOOL_PREFLIGHT", raising=False)
        monkeypatch.setattr(run_module, "log", logs.append)
        monkeypatch.setattr(run_module, "_has_tool",
                            lambda name: name not in missing)
        return run_module._settle_subtitle_work(**asked), logs
    return run


def test_the_full_ingest_is_told_what_still_runs(gate):
    settled, logs = gate(["ffsubsync", "pipx"])
    assert settled is False
    assert "Everything else" in " ".join(logs)
    assert logs[-1].strip() == ("To enable them: apt install pipx, then pipx "
                                "install ffsubsync.")


def test_the_commentary_run_is_not_promised_a_rest_it_does_not_have(gate):
    _settled, logs = gate(["ffsubsync"], commentary_only=True)
    assert "subtitle downloading" not in logs[0]
    assert "Everything else" not in " ".join(logs)
    assert logs[-1].strip() == "To enable it: pipx install ffsubsync."


def test_only_the_missing_step_is_named(gate):
    _settled, logs = gate(["pipx"])
    assert logs[-1].strip() == "To enable them: apt install pipx."


def test_an_ffsubsync_the_helper_can_watch_is_asked_for_its_confidence(probe):
    answer, logs = probe("  --skip-sync-on-low-quality  reject bad alignments")
    assert answer == "confidence"
    assert logs == []


def test_one_it_cannot_watch_falls_back_to_the_offset_limit_and_says_so(probe):
    answer, logs = probe("  --skip-sync-on-low-quality  reject bad alignments",
                         watchable=False)
    assert answer == "yes"
    assert any("cannot be asked how sure" in line for line in logs)


def test_an_older_ffsubsync_is_not_handed_a_flag_it_would_refuse(probe):
    answer, logs = probe("  --max-offset-seconds SECONDS")
    assert answer == "no"
    assert any("upgrade ffsubsync" in line for line in logs)


def test_no_ffsubsync_at_all_settles_the_same_way_and_says_nothing(probe):
    # The run has already said what a missing ffsubsync costs, once, and the
    # subtitle phases are off anyway.
    answer, logs = probe(None)
    assert answer == "no"
    assert logs == []


@pytest.fixture
def ingested(monkeypatch):
    """One ingest with every phase stubbed out, reporting what the two sync
    call sites were handed."""
    def run(quality="yes", subtitle_work=True):
        seen = {}
        for name in ("cleanup", "mkv_mux", "movies_into_subfolders",
                     "extras_into_subfolders", "update_tags"):
            monkeypatch.setattr(run_module.rules, name, lambda *a, **k: None)
        for name in ("rename_folders", "rename_movies"):
            monkeypatch.setattr(run_module.rules, name, lambda *a, **k: 0)
        monkeypatch.setattr(run_module.safety, "lower_case_extensions",
                            lambda *a, **k: None)
        for name in ("move_subs", "rename_subs"):
            monkeypatch.setattr(run_module.subtitlefiles, name,
                                lambda *a, **k: None)
        monkeypatch.setattr(run_module.tmdblookup, "tag_plex_ids",
                            lambda *a, **k: None)
        for name in ("_transcode_opus", "improve_main_movies",
                     "_start_research", "_research_again",
                     "_commentary_name_phase", "_chapter_phase",
                     "check_folders"):
            monkeypatch.setattr(run_module, name, lambda *a, **k: None)
        monkeypatch.setattr(run_module, "log", lambda *a, **k: None)

        def download_subs(directory, providers, max_offset,
                          max_quality_offset, ffsubsync_quality, log):
            seen["download"] = ffsubsync_quality

        def export_commentary(directory, *args, **_kw):
            seen["commentary"] = args[-1]

        monkeypatch.setattr(run_module.subtitlefiles, "download_subs",
                            download_subs)
        monkeypatch.setattr(run_module.commentarytranscription,
                            "export_commentary", export_commentary)

        state = run_module.Run(script_dir="", ram_root="", skips=None,
                               fragments_file="",
                               whisper={"jobs": whisper_lib.WHISPER_JOBS},
                               whisper_said=[], ffsubsync_quality=quality,
                               long_names=tmdblookup.LongNames(),
                               unfixed_movies=[])
        run_module._ingest(state, "/x", subtitle_work, "x")
        return seen
    return run


def test_both_sync_call_sites_are_handed_the_one_probed_answer(ingested):
    assert ingested(quality="no") == {"download": "no", "commentary": "no"}


def test_the_answer_is_the_probe_s_and_not_a_constant(ingested):
    assert ingested(quality="yes") == {"download": "yes", "commentary": "yes"}


def test_a_full_ingest_never_asks_for_the_imdb_lists(ingested, monkeypatch):
    """They are the tagging pass's: a full ingest is pointed at a download,
    and fetching most of a gigabyte to name it is not what it is for."""
    prepared = []
    monkeypatch.setattr(run_module.imdbdata, "prepare",
                        lambda *arguments: prepared.append(arguments) or "db")
    ingested()
    assert prepared == []

