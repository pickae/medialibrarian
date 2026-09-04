"""Which Dolby Vision job a film needs, and what the remux does with the result.

The primitives - reading the fields, the profile gates, the RPU probe, the
conversion - are `medialib/lib/dolbyvision.py` and have their own white box; what
is here is the DECISION assembled from them, and the verification that stands
between a remux and the original file.

Two jobs exist, and only two. A real dual-layer profile 7 is normalised to
single-layer 8.1, metadata only. A container that claims Dolby Vision its video
does not carry has that claim dropped instead - there is nothing to convert -
which leaves the file reporting what its bitstream really is. Everything else is
left exactly as it is.
"""

import pytest

from medialib.cli import ingest_movies as rules
from medialib.cli import ingest_movies_run as run_module

pytestmark = pytest.mark.pure

EMPTY = {"PROFILE": "", "SETTINGS": "", "HDR": "", "TRANSFER": "",
         "FPS_SPEC": "", "STREAM_SIZE": ""}


def _info(**fields):
    found = dict(EMPTY)
    found.update(fields)
    return found


def _tracks(video=1, audio=1):
    found = [rules.Track(id=str(n), type="video", codec="V_MPEGH/ISO/HEVC")
             for n in range(video)]
    found += [rules.Track(id=str(video + n), type="audio", codec="A_AC3",
                          channels="6", language="eng")
              for n in range(audio)]
    return found


@pytest.fixture
def decide(monkeypatch):
    """The decision, with the probes answering what the case says."""
    def run(info, has_rpu=True, has_dovi_tool=True, tracks=None, logs=None):
        monkeypatch.setattr(run_module.dolbyvision, "read_video_info",
                            lambda path: info)
        monkeypatch.setattr(run_module.dolbyvision, "stream_has_rpu",
                            lambda path: has_rpu)
        monkeypatch.setattr(run_module, "_has_tool",
                            lambda name: has_dovi_tool)
        if logs is not None:
            monkeypatch.setattr(run_module, "log", logs.append)
        state = run_module.Run(script_dir="")
        return state.decide_dolby_vision_job("/x/Film (2020).mkv",
                                             tracks or _tracks())
    return run


class TestTheConversion:
    """Only dual-layer profile 7 has anything to gain."""

    def test_a_real_profile_7_is_converted(self, decide):
        job = decide(_info(PROFILE="dvhe.07", SETTINGS="BL+EL+RPU",
                           FPS_SPEC="24000/1001"))
        assert job["action"] == "convert"
        assert job["wanted"] is True
        assert job["fps"] == "24000/1001"

    def test_profile_7_is_recognised_from_the_settings_alone(self, decide):
        """Some files carry no profile field at all."""
        job = decide(_info(SETTINGS="BL+EL+RPU", FPS_SPEC="24"))
        assert job["action"] == "convert"

    def test_a_joined_hdr_format_field_still_finds_it(self, decide):
        """mediainfo renders one field per HDR format joined with " / ", and
        nothing guarantees Dolby Vision is the first entry."""
        job = decide(_info(PROFILE="SMPTE ST 2086 / dvhe.07",
                           SETTINGS="BL+EL+RPU", FPS_SPEC="24"))
        assert job["action"] == "convert"

    def test_a_finished_conversion_is_left_alone(self, decide):
        """The converted file reports profile 8, so a fresh run over it finds
        nothing to do - which is what makes the phase idempotent."""
        job = decide(_info(PROFILE="dvhe.08", SETTINGS="BL+RPU",
                           FPS_SPEC="24"))
        assert job["wanted"] is False
        assert job["action"] == ""

    def test_profile_5_is_never_touched(self, decide):
        """Its base layer is not HDR10, so it cannot be converted without
        re-encoding."""
        job = decide(_info(PROFILE="dvhe.05", SETTINGS="BL+RPU",
                           FPS_SPEC="24"))
        assert job["wanted"] is False

    def test_a_file_with_no_dolby_vision_fields_at_all(self, decide):
        """HDR10, HDR10+ and SDR carry none."""
        job = decide(_info(TRANSFER="PQ", HDR="SMPTE ST 2086", FPS_SPEC="24"))
        assert job["wanted"] is False
        assert job["action"] == ""


class TestTheFalseClaim:
    """A container that advertises Dolby Vision the video does not carry: there
    is nothing to CONVERT, so the claim is dropped instead."""

    def test_a_claim_with_no_RPU_behind_it_is_stripped(self, decide):
        job = decide(_info(PROFILE="dvhe.07", SETTINGS="BL+EL+RPU",
                           TRANSFER="PQ", HDR="SMPTE ST 2086", FPS_SPEC="24"),
                     has_rpu=False)
        assert job["action"] == "strip"
        assert job["wanted"] is True

    def test_a_source_that_really_is_HDR_is_recorded_as_such(self, decide):
        """Which of the two outcomes the copy has - HDR only, or nothing at all
        - is the whole point of the strip, and the verification afterwards turns
        on it."""
        job = decide(_info(PROFILE="dvhe.07", SETTINGS="BL+EL+RPU",
                           TRANSFER="PQ", HDR="SMPTE ST 2086", FPS_SPEC="24"),
                     has_rpu=False)
        assert job["source_hdr"] is True

    def test_a_source_that_is_not_HDR_at_all(self, decide):
        """A 4K SDR BT.709 encode with a Dolby Vision claim stuck on it."""
        job = decide(_info(PROFILE="dvhe.07", SETTINGS="BL+EL+RPU",
                           TRANSFER="BT.709", FPS_SPEC="24"), has_rpu=False)
        assert job["action"] == "strip"
        assert job["source_hdr"] is False

    @pytest.mark.parametrize("profile", ["dvhe.05", "dvhe.08"])
    def test_the_claim_is_dropped_whatever_profile_it_named(self, decide,
                                                            profile):
        """Profile 5 is never CONVERTED and profile 8.1 is the very shape a
        finished conversion has - but a claim with no RPU behind it is false
        whichever it names, and false claims are dropped."""
        job = decide(_info(PROFILE=profile, SETTINGS="BL+RPU", FPS_SPEC="24"),
                     has_rpu=False)
        assert job["action"] == "strip"


class TestWhatStopsAJobBeingDone:
    """Each leaves the film exactly as it was, and says why."""

    def test_without_dovi_tool_neither_job_can_be_decided(self, decide):
        """The RPU probe is what tells a real Dolby Vision file from one that
        only claims it."""
        logs = []
        job = decide(_info(PROFILE="dvhe.07", SETTINGS="BL+EL+RPU",
                           FPS_SPEC="24"), has_dovi_tool=False, logs=logs)
        assert job["wanted"] is False
        assert any("dovi_tool not installed" in line for line in logs)

    def test_and_says_so_only_for_the_profile_with_something_to_miss(self,
                                                                    decide):
        logs = []
        decide(_info(PROFILE="dvhe.05", SETTINGS="BL+RPU", FPS_SPEC="24"),
               has_dovi_tool=False, logs=logs)
        assert not any("dovi_tool not installed" in line for line in logs)

    def test_no_frame_rate_reported(self, decide):
        """A raw HEVC elementary stream carries no timing of its own, so without
        one mkvmerge would fall back to 25 fps and desync every audio track."""
        logs = []
        job = decide(_info(PROFILE="dvhe.07", SETTINGS="BL+EL+RPU"), logs=logs)
        assert job["wanted"] is False
        assert any("needs a frame rate" in line for line in logs)

    def test_more_than_one_video_track(self, decide):
        """The remux drops ALL of file 0's video tracks and replaces one, so a
        second would simply be thrown away."""
        logs = []
        job = decide(_info(PROFILE="dvhe.07", SETTINGS="BL+EL+RPU",
                           FPS_SPEC="24"), tracks=_tracks(video=2), logs=logs)
        assert job["wanted"] is False
        assert any("exactly one video track" in line for line in logs)

    def test_the_warning_names_the_job_it_declined(self, decide):
        logs = []
        decide(_info(PROFILE="dvhe.07", SETTINGS="BL+EL+RPU", FPS_SPEC="24"),
               has_rpu=False, tracks=_tracks(video=2), logs=logs)
        assert any("dropping the false Dolby Vision claim" in line
                   for line in logs)


class TestTheVerification:
    """The work is checked BEFORE the original is touched: a result that does not
    hold up is thrown away and the source left exactly as it was - still a
    perfectly playable file, if a mislabelled one."""

    def _reject(self, monkeypatch, job, profile8=None, free=None):
        if profile8 is not None:
            monkeypatch.setattr(run_module.dolbyvision, "is_profile8",
                                lambda path: profile8)
        if free is not None:
            monkeypatch.setattr(run_module.dolbyvision,
                                "is_dolby_vision_free",
                                lambda path, hdr: free)
        state = run_module.Run(script_dir="")
        return state._dolby_vision_reject("/x/out.mkv", job, 0)

    def test_a_conversion_that_reports_profile_8_is_accepted(self,
                                                             monkeypatch):
        job = {"wanted": True, "action": "convert", "source_hdr": False}
        assert self._reject(monkeypatch, job,
                            profile8=(True, "dvhe.08")) == ""

    def test_one_that_does_not_is_rejected_and_says_what_it_saw(self,
                                                                monkeypatch):
        job = {"wanted": True, "action": "convert", "source_hdr": False}
        reason = self._reject(monkeypatch, job, profile8=(False, "dvhe.07"))
        assert "instead of 8" in reason and "dvhe.07" in reason

    def test_a_strip_that_still_claims_dolby_vision_is_rejected(self,
                                                                monkeypatch):
        """Which would mean the strip achieved nothing."""
        job = {"wanted": True, "action": "strip", "source_hdr": True}
        reason = self._reject(monkeypatch, job,
                              free=(False, ("dvhe.05", "BL+RPU", "1")))
        assert "still claims Dolby Vision" in reason

    def test_a_strip_that_came_out_less_HDR_than_it_went_in_is_rejected(
            self, monkeypatch):
        """It took real HDR10 metadata along with the false claim."""
        job = {"wanted": True, "action": "strip", "source_hdr": True}
        reason = self._reject(monkeypatch, job, free=(False, ("", "", "0")))
        assert reason == "remux lost the HDR metadata the source had"

    def test_a_clean_strip_is_accepted(self, monkeypatch):
        job = {"wanted": True, "action": "strip", "source_hdr": True}
        assert self._reject(monkeypatch, job, free=(True, ("", "", "1"))) == ""

    def test_nothing_is_verified_when_mkvmerge_itself_failed(self):
        """Its own failure path already leaves the original untouched."""
        state = run_module.Run(script_dir="")
        job = {"wanted": True, "action": "convert", "source_hdr": False}
        assert state._dolby_vision_reject("/x/out.mkv", job, 2) == ""

    def test_and_nothing_when_there_was_no_job(self):
        state = run_module.Run(script_dir="")
        assert state._dolby_vision_reject("/x/out.mkv", {"wanted": False}, 0) \
            == ""


class TestWhenThePreparationFails:
    """A dovi_tool that dies falls back to keeping the video exactly as it is -
    and skips the remux altogether when that was its only reason."""

    def test_a_status_of_zero_is_the_conversion_working(self, tmp_path,
                                                        monkeypatch):
        """The two preparations report a shell status, where 0 is success - and
        0 is falsy in Python, so reading them as booleans inverts every one of
        them."""
        movie = str(tmp_path / "Film (2020).mkv")
        open(movie, "w").close()
        monkeypatch.setattr(run_module, "log", lambda line: None)
        monkeypatch.setattr(run_module.dolbyvision, "convert_to_profile81",
                            lambda movie, out, **k: open(out, "w").close() or 0)
        state = run_module.Run(script_dir="")
        job = {"wanted": True, "action": "convert", "fps": "24",
               "source_hdr": False, "video_index": 0, "video_count": 1,
               "stream_size": "", "info": EMPTY}
        assert state._prepare_video(movie, str(tmp_path), job) != ""

    def test_a_failed_conversion_leaves_the_film_alone(self, tmp_path,
                                                       monkeypatch):
        movie = str(tmp_path / "Film (2020).mkv")
        open(movie, "w").close()
        logs = []
        monkeypatch.setattr(run_module, "log", logs.append)
        # A shell STATUS, so a failure is non-zero - `False` would read as the
        # 0 that means it worked.
        monkeypatch.setattr(run_module.dolbyvision, "convert_to_profile81",
                            lambda *a, **k: 1)
        monkeypatch.setattr(run_module.ramscratch, "ram_scratch_dir_for",
                            lambda *a, **k: (str(tmp_path), False, 0))
        monkeypatch.setattr(run_module.ramscratch, "add_exit_cleanup",
                            lambda paths: None)
        monkeypatch.setattr(run_module.ramscratch, "release_exit_cleanup",
                            lambda paths: None)
        called = []
        monkeypatch.setattr(run_module, "_mkvmerge",
                            lambda argv: called.append(argv) or (0, ""))

        state = run_module.Run(script_dir="")
        job = {"wanted": True, "action": "convert", "fps": "24",
               "source_hdr": False, "video_index": 0, "video_count": 1,
               "stream_size": "", "info": EMPTY}
        state.remux(movie, str(tmp_path / "Film (2020)"), _tracks(), [], job)

        assert called == []
        assert any("No other improvements needed" in line for line in logs)
