"""The commentary queue's drain: who runs the queued transcriptions.

exportCommentary fills the queue and hands it back to the run to drain, so a run
that hands it nothing transcribes nothing however full the queue is. The width is
whisper's rather than the core count, one run already spanning the GPU or every
CPU thread.
"""

import pytest

from medialib.cli import ingest_movies_run as run_module
from medialib.lib import whisper as whisper_lib

pytestmark = pytest.mark.pure


@pytest.fixture
def ingested(monkeypatch):
    """One ingest with every phase stubbed out, reporting the width and the
    drain exportCommentary was handed."""
    def run():
        seen = {}
        for name in ("cleanup", "mkv_mux", "movies_into_subfolders",
                     "extras_into_subfolders", "update_tags"):
            monkeypatch.setattr(run_module.rules, name, lambda *a, **k: None)
        for name in ("rename_folders", "rename_movies"):
            monkeypatch.setattr(run_module.rules, name, lambda *a, **k: 0)
        monkeypatch.setattr(run_module.safety, "lower_case_extensions",
                            lambda *a, **k: None)
        for name in ("move_subs", "rename_subs", "download_subs"):
            monkeypatch.setattr(run_module.subtitlefiles, name,
                                lambda *a, **k: None)
        monkeypatch.setattr(run_module.tmdblookup, "tag_plex_ids",
                            lambda *a, **k: None)
        for name in ("_transcode_opus", "improve_main_movies",
                     "check_folders"):
            monkeypatch.setattr(run_module, name, lambda *a, **k: None)
        monkeypatch.setattr(run_module, "log", lambda *a, **k: None)

        def export_commentary(directory, read_track_info, is_bonus_folder,
                              rename, audio_stream_index, ram_root, whisper,
                              whisper_jobs, log, drain_queue, *rest):
            seen["jobs"] = whisper_jobs
            seen["drain"] = drain_queue

        monkeypatch.setattr(run_module.commentarytranscription,
                            "export_commentary", export_commentary)

        state = run_module.Run(script_dir="", ram_root="/ram", skips=None,
                               fragments_file="", whisper={"model": "m"},
                               ffsubsync_quality="yes")
        run_module._ingest(state, "/x", True)
        return seen
    return run


def test_the_queue_is_handed_a_drain_and_not_none(ingested):
    assert callable(ingested()["drain"])


def test_the_width_is_whisper_s_and_not_the_core_count(ingested):
    assert ingested()["jobs"] == whisper_lib.WHISPER_JOBS


def test_every_queued_record_reaches_the_transcription(monkeypatch):
    seen = []
    monkeypatch.setattr(run_module.commentarytranscription,
                        "transcribe_commentary",
                        lambda record, *a: seen.append((record, a)))
    state = run_module.Run(script_dir="", ram_root="/ram", skips=None,
                           fragments_file="", whisper={"model": "m"},
                           ffsubsync_quality="yes")
    # Width one, so the records run in this process and no worker is forked.
    run_module._drain_commentary(state, ["one", "two"], 1)
    assert [record for record, _args in seen] == ["one", "two"]


def test_the_transcription_is_handed_this_run_s_settings(monkeypatch):
    seen = []
    monkeypatch.setattr(run_module.commentarytranscription,
                        "transcribe_commentary",
                        lambda record, *a: seen.append(a))
    state = run_module.Run(script_dir="", ram_root="/ram", skips=None,
                           fragments_file="", whisper={"model": "m"},
                           ffsubsync_quality="no")
    run_module._drain_commentary(state, ["one"], 1)
    assert seen == [({"model": "m"}, run_module.rules.MAX_WHISPER_SYNC_OFFSET,
                     "no", "/ram", run_module.log)]


def test_an_abort_stops_the_drain_where_it_is(monkeypatch):
    seen = []
    monkeypatch.setattr(run_module.commentarytranscription,
                        "transcribe_commentary",
                        lambda record, *a: seen.append(record))
    monkeypatch.setattr(run_module.safety, "abort_requested", lambda: True)
    state = run_module.Run(script_dir="", ram_root="/ram", skips=None,
                           fragments_file="", whisper={}, ffsubsync_quality="")
    run_module._drain_commentary(state, ["one", "two"], 1)
    assert seen == []
