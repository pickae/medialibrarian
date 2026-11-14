"""The white box for ingest-movies's lanes: a film is remuxed once - and only
once - everything it waits for is done, the subtitles fetched ahead are fetched
by the id TMDb would give, and the run hands the walk after the lanes only the
films the lanes did not get to."""

import os
import threading

import pytest

from medialib.cli import ingest_movies_lanes as lanes
from medialib.cli import ingest_movies_run as run_module
from medialib.lib import console, tmdblookup
from medialib.lib import whisper as whisper_lib

pytestmark = pytest.mark.fs


class TestTranscriptTracker:
    def _tracker(self):
        done = []
        return lanes.TranscriptTracker(done.append), done

    def test_a_film_is_done_once_its_last_record_is(self):
        tracker, done = self._tracker()
        tracker.record_queued("a1")
        tracker.record_queued("a2")
        tracker.film_queued("/films/A.mkv")
        tracker.record_done("a1")
        assert done == []
        tracker.record_done("a2")
        assert done == [lanes.key("/films/A.mkv")]

    def test_a_record_done_before_its_film_was_named_still_counts(self):
        tracker, done = self._tracker()
        tracker.record_queued("a1")
        tracker.record_done("a1")
        tracker.film_queued("/films/A.mkv")
        assert done == ["/films/A.mkv"]

    def test_a_film_with_nothing_to_transcribe_is_done_at_once(self):
        tracker, done = self._tracker()
        tracker.film_queued("/films/A.mkv")
        assert done == ["/films/A.mkv"]

    def test_each_film_claims_the_records_queued_since_the_last(self):
        tracker, done = self._tracker()
        tracker.record_queued("a1")
        tracker.film_queued("/films/A.mkv")
        tracker.record_queued("b1")
        tracker.film_queued("/films/B.mkv")
        tracker.record_done("b1")
        assert done == [lanes.key("/films/B.mkv")]


class TestRemuxLane:
    def _lane(self, films, transcripts=True, improve=None):
        improved = []
        said = []
        lane = lanes.RemuxLane(films, improve or improved.append,
                               lanes.FilmLocks(), transcripts, said.append)
        return lane, improved, said

    def test_a_film_waits_for_its_opus_and_its_transcripts(self):
        lane, improved, _said = self._lane(["/f/A.mkv", "/f/B.mkv"])
        lane.opus_done("/f/A.mkv")
        lane.transcripts_done("/f/B.mkv")
        lane.transcripts_done("/f/A.mkv")
        lane.finish()
        assert improved == ["/f/A.mkv"]
        assert lane.done == {lanes.key("/f/A.mkv")}

    def test_without_transcription_the_opus_is_all_it_waits_for(self):
        lane, improved, _said = self._lane(["/f/A.mkv"], transcripts=False)
        lane.opus_done("/f/A.mkv")
        lane.finish()
        assert improved == ["/f/A.mkv"]

    def test_a_film_is_remuxed_once_whichever_wait_ends_last(self):
        lane, improved, _said = self._lane(["/f/A.mkv"])
        lane.transcripts_done("/f/A.mkv")
        lane.opus_done("/f/A.mkv")
        lane.opus_done("/f/A.mkv")
        lane.transcripts_done("/f/A.mkv")
        lane.finish()
        assert improved == ["/f/A.mkv"]

    def test_a_film_that_is_not_a_main_movie_is_left_alone(self):
        lane, improved, _said = self._lane(["/f/A.mkv"], transcripts=False)
        lane.opus_done("/f/Featurettes/Clip.mkv")
        lane.opus_done(None)
        lane.finish()
        assert improved == []
        assert lane.done == set()

    def test_one_film_s_failure_is_said_and_not_the_lane_s(self):
        def improve(movie):
            if movie.endswith("A.mkv"):
                raise RuntimeError("broken")
            improved.append(movie)

        improved: list = []
        lane, _unused, said = self._lane(["/f/A.mkv", "/f/B.mkv"],
                                         transcripts=False, improve=improve)
        lane.opus_done("/f/A.mkv")
        lane.opus_done("/f/B.mkv")
        lane.finish()
        assert improved == ["/f/B.mkv"]
        assert any("could not improve /f/A.mkv: broken" in line
                   for line in said)
        assert lane.done == {lanes.key("/f/A.mkv"), lanes.key("/f/B.mkv")}

    def test_after_an_interrupt_nothing_more_is_started(self, monkeypatch):
        monkeypatch.setattr(lanes.safety, "abort_requested", lambda: True)
        lane, improved, _said = self._lane(["/f/A.mkv"], transcripts=False)
        lane.opus_done("/f/A.mkv")
        lane.finish()
        assert improved == []
        assert lane.done == set()

    def test_the_lane_holds_the_film_against_the_other_lanes(self):
        locks = lanes.FilmLocks()
        inside = threading.Event()
        release = threading.Event()

        def improve(_movie):
            inside.set()
            release.wait(5)

        lane = lanes.RemuxLane(["/f/A.mkv"], improve, locks, False,
                               lambda _line: None)
        lane.opus_done("/f/A.mkv")
        assert inside.wait(5)
        taken = threading.Event()

        def other():
            with locks.held("/f/./A.mkv"):
                taken.set()

        thread = threading.Thread(target=other)
        thread.start()
        assert not taken.wait(0.1)
        release.set()
        lane.finish()
        thread.join(5)
        assert taken.is_set()


class TestTheLaneRow:
    """What each lane has done, counted where the lane does it."""

    def test_the_remux_and_whisper_lanes_count_on_the_row(self):
        row = console.LaneRow(("opus", "whisper", "remux", "subs"))
        remux = lanes.RemuxLane(["/f/A.mkv"], lambda _film: None,
                                lanes.FilmLocks(), True, lambda _line: None,
                                row)
        tracker = lanes.TranscriptTracker(remux.transcripts_done, row)
        tracker.record_queued("/f/A track 2.mka")
        tracker.film_queued("/f/A.mkv")
        assert row.render() == "  whisper 0/1 · remux 0"
        tracker.record_done("/f/A track 2.mka")
        remux.opus_done("/f/A.mkv")
        remux.finish()
        assert row.render() == "  whisper 1/1 · remux 1"


class TestSubtitleLane:
    def _run(self, monkeypatch, tmp_path, tagged, films):
        def tag_plex_ids(directory, log, skip_log, dry_run=False,
                         tagged=None, **_kwargs):
            assert dry_run
            tagged.update(tagged_folders)
            return 0

        tagged_folders = tagged
        monkeypatch.setattr(lanes.tmdblookup, "tag_plex_ids", tag_plex_ids)
        asked = []
        said = []
        lane = lanes.SubtitleLane(str(tmp_path), films, asked.extend,
                                  lanes.FilmLocks(), said.append)
        lane.start()
        lane.finish()
        return asked, said

    def test_a_film_is_fetched_for_by_the_id_its_folder_would_get(
            self, monkeypatch, tmp_path):
        folder = tmp_path / "Movie (1999)"
        folder.mkdir()
        movie = str(folder / "Movie (1999).mkv")
        asked, _said = self._run(monkeypatch, tmp_path,
                                 {"{imdb-tt0000007}": [str(folder)]}, [movie])
        assert asked == [(movie, "tt0000007")]

    def test_one_whose_name_carries_its_id_needs_no_folder_answer(
            self, monkeypatch, tmp_path):
        movie = str(tmp_path / "F {imdb-tt0000009}" / "F {imdb-tt0000009}.mkv")
        asked, _said = self._run(monkeypatch, tmp_path, {}, [movie])
        assert asked == [(movie, "tt0000009")]

    def test_one_with_no_id_at_all_is_left_to_the_walk_after_the_tagging(
            self, monkeypatch, tmp_path):
        movie = str(tmp_path / "Unknown" / "Unknown.mkv")
        asked, said = self._run(monkeypatch, tmp_path, {}, [movie])
        assert asked == []
        # A lane still running when it is finished first says it waits for
        # it; how far the thread got by then is the scheduler's to decide.
        assert [line for line in said if not line.startswith("Waiting")] == []

    def test_a_lane_that_fails_says_so_when_it_is_finished(
            self, monkeypatch, tmp_path):
        def broken(*_args, **_kwargs):
            raise RuntimeError("no network")

        monkeypatch.setattr(lanes.tmdblookup, "tag_plex_ids", broken)
        said = []
        lane = lanes.SubtitleLane(str(tmp_path), [], lambda films: None,
                                  lanes.FilmLocks(), said.append)
        lane.start()
        lane.finish()
        # A lane still running when it is finished first says it waits for
        # it; how far the thread got by then is the scheduler's to decide.
        warnings = [line for line in said if not line.startswith("Waiting")]
        assert len(warnings) == 1 and "no network" in warnings[0]


class TestTheMiddleOfTheIngest:
    """The transcode, the transcription and the remux handed to the lanes."""

    def _stubbed(self, monkeypatch, tmp_path, device):
        films = [str(tmp_path / name / (name + ".mkv")) for name in ("A", "B")]
        order: list = []
        threads: dict = {}

        def transcode(state, root, jobs=None, on_done=None,
                      exit_on_abort=True, row=None):
            threads["opus"] = threading.current_thread()
            threads["opus_jobs"] = jobs
            for film in films:
                order.append("opus " + os.path.basename(film))
                on_done(film)

        def export_commentary(directory, *args, film_queued=None, **_kw):
            threads["whisper"] = threading.current_thread()
            for film in films[:1]:
                order.append("transcribed " + os.path.basename(film))
                film_queued(film)

        def improve(movie):
            order.append("remux " + os.path.basename(movie))

        walked = []

        def walk(state, root, skip=(), around=None):
            walked.extend(film for film in films
                          if lanes.key(film) not in skip)

        monkeypatch.setattr(run_module, "_transcode_opus", transcode)
        monkeypatch.setattr(run_module, "main_movies", lambda root: films)
        monkeypatch.setattr(run_module, "improve_main_movies", walk)
        monkeypatch.setattr(run_module.commentarytranscription,
                            "export_commentary", export_commentary)
        monkeypatch.setattr(run_module, "log", lambda *a, **k: None)
        state = run_module.Run(
            script_dir="", ram_root=str(tmp_path), skips=None,
            fragments_file="",
            whisper={"jobs": whisper_lib.WHISPER_JOBS, "device": device},
            whisper_said=[], ffsubsync_quality="yes",
            long_names=tmdblookup.LongNames(), unfixed_movies=[],
            disk_turns="", scratch_headroom=0)
        monkeypatch.setattr(state, "improve_main_movie", improve,
                            raising=False)
        run_module._films_in_lanes(state, str(tmp_path), True,
                                   lanes.FilmLocks())
        return order, walked, threads, state

    def test_a_film_transcribed_is_remuxed_by_the_lane_the_rest_after(
            self, monkeypatch, tmp_path):
        order, walked, _threads, state = self._stubbed(monkeypatch, tmp_path,
                                                       "cpu")
        assert "remux A.mkv" in order
        assert "remux B.mkv" not in order
        assert walked == [str(tmp_path / "B" / "B.mkv")]
        assert state.scratch_headroom == 0

    def test_with_whisper_on_the_gpu_the_transcode_runs_beside_it(
            self, monkeypatch, tmp_path):
        _order, _walked, threads, _state = self._stubbed(monkeypatch,
                                                         tmp_path, "cuda")
        assert threads["opus"] is not threads["whisper"]
        assert threads["opus_jobs"] == run_module._opus_jobs_beside_whisper()

    def test_with_whisper_on_the_cpu_the_transcode_goes_first(
            self, monkeypatch, tmp_path):
        order, _walked, threads, _state = self._stubbed(monkeypatch, tmp_path,
                                                        "cpu")
        assert threads["opus"] is threads["whisper"]
        assert order.index("opus B.mkv") < order.index("transcribed A.mkv")
        assert threads["opus_jobs"] is None


def test_an_interrupt_still_waits_for_the_lanes(monkeypatch, tmp_path):
    """Nothing is left half-written behind a run that was asked to stop."""
    finished = []

    class Lane:
        def __init__(self, *args, **kwargs):
            pass

        def opus_done(self, _movie):
            pass

        def finish(self):
            finished.append("remux")

    monkeypatch.setattr(run_module.lanes, "RemuxLane", Lane)
    monkeypatch.setattr(run_module, "main_movies", lambda root: [])

    def interrupted(*_args, **_kwargs):
        raise SystemExit(130)

    monkeypatch.setattr(run_module, "_transcode_opus", interrupted)
    monkeypatch.setattr(run_module, "log", lambda *a, **k: None)
    state = run_module.Run(whisper={"device": "cpu"})
    with pytest.raises(SystemExit):
        run_module._films_in_lanes(state, str(tmp_path), False,
                                   lanes.FilmLocks())
    assert finished == ["remux"]


class TestFilmTags:
    def test_the_dry_run_is_asked_once_for_every_lane(self, monkeypatch,
                                                     tmp_path):
        runs = []

        def tag_plex_ids(directory, log, skip_log, dry_run=False,
                         tagged=None, **_kwargs):
            runs.append(directory)
            tagged["{imdb-tt0000007}"] = [str(tmp_path / "Movie (1999)")]
            return 0

        monkeypatch.setattr(lanes.tmdblookup, "tag_plex_ids", tag_plex_ids)
        tags = lanes.FilmTags(str(tmp_path))
        movie = str(tmp_path / "Movie (1999)" / "Movie (1999).mkv")
        assert tags.tag_of(movie) == "{imdb-tt0000007}"
        assert tags.tag_of(str(tmp_path / "Other" / "Other.mkv")) == ""
        assert runs == [str(tmp_path)]


class TestTheRemuxAsksForTheFilmsLanguages:
    def _state(self, monkeypatch, tag_of):
        monkeypatch.setenv("tmdbApiKey", "k")
        asked = []
        answer = run_module.tmdbidentify.FilmLanguages("ja", ("ja",))

        def languages(tag):
            asked.append(tag)
            return answer
        monkeypatch.setattr(run_module.tmdbidentify, "film_languages",
                            languages)

        class Tags:
            def tag_of(self, movie):
                return tag_of(movie)
        return run_module.Run(script_dir="", film_tags=Tags()), asked, answer

    def test_by_the_tag_the_name_already_carries(self, monkeypatch):
        state, asked, answer = self._state(monkeypatch,
                                           lambda movie: "{tmdb-9}")
        assert state.film_languages(
            "/f/F {imdb-tt0000001}/F {imdb-tt0000001}.mkv") == answer
        assert asked == ["{imdb-tt0000001}"]

    def test_by_the_tag_the_tagging_would_give_it_otherwise(self,
                                                            monkeypatch):
        state, asked, answer = self._state(monkeypatch,
                                           lambda movie: "{tmdb-9}")
        assert state.film_languages("/f/F (2001)/F (2001).mkv") == answer
        assert asked == ["{tmdb-9}"]

    def test_not_at_all_for_a_film_nothing_names(self, monkeypatch):
        state, asked, _answer = self._state(monkeypatch, lambda movie: "")
        assert state.film_languages("/f/F/F.mkv") \
            == run_module.tmdbidentify.FilmLanguages()
        assert asked == []

    def test_a_failed_asking_keeps_the_old_rule(self, monkeypatch):
        def broken(movie):
            raise RuntimeError("no network")
        state, _asked, _answer = self._state(monkeypatch, broken)
        monkeypatch.setattr(run_module, "log", lambda line: None)
        assert state.film_languages("/f/F/F.mkv") \
            == run_module.tmdbidentify.FilmLanguages()
