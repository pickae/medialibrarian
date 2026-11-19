"""How transcribe-audio gathers its prepared inputs into whisper calls."""

from __future__ import annotations

import os
import sys

import pytest

from medialib.cli import transcribe_audio
from medialib.lib import whisper


def _ready(relative, for_whisper=None):
    return (relative, for_whisper or "/in/" + relative, "/ram/" + relative)


def _labels_and_members(prepared, size):
    return [(label, [m[0] for m in members])
            for (label, members), _bytes in transcribe_audio._groups(
                iter(prepared), size)]


class TestGroupSize:
    def test_the_files_are_spread_over_the_workers(self):
        assert transcribe_audio.group_size(5, 2) == 3
        assert transcribe_audio.group_size(5, 1) == 5

    def test_it_is_capped(self):
        assert (transcribe_audio.group_size(100, 1)
                == whisper.WHISPER_GROUP_FILES)

    def test_it_is_never_zero(self):
        assert transcribe_audio.group_size(0, 2) == 1


class TestGroups:
    def test_full_groups_and_a_remainder(self):
        prepared = [(_ready("e%d.mp3" % n), 10) for n in range(5)]
        groups = _labels_and_members(prepared, 2)
        assert [members for _label, members in groups] == [
            ["e0.mp3", "e1.mp3"], ["e2.mp3", "e3.mp3"], ["e4.mp3"]]

    def test_a_group_is_labelled_by_its_first_input(self):
        prepared = [(_ready("a.mp3"), 1), (_ready("b.mp3"), 1)]
        assert _labels_and_members(prepared, 4)[0][0] == "a.mp3 (+1 more)"

    def test_a_single_input_is_labelled_by_itself(self):
        assert _labels_and_members([(_ready("a.mp3"), 1)], 4)[0][0] == "a.mp3"

    def test_a_large_source_goes_on_at_once(self):
        prepared = [(_ready("film.mkv"), whisper.WHISPER_GROUP_BYTES),
                    (_ready("a.mp3"), 1), (_ready("b.mp3"), 1)]
        groups = _labels_and_members(prepared, 8)
        assert [members for _l, members in groups] == [
            ["film.mkv"], ["a.mp3", "b.mp3"]]

    def test_a_name_already_in_the_group_starts_the_next(self):
        prepared = [(_ready("a/Episode.mp3"), 1), (_ready("b/episode.opus"), 1),
                    (_ready("c/other.mp3"), 1)]
        groups = _labels_and_members(prepared, 8)
        assert [members for _l, members in groups] == [
            ["a/Episode.mp3"], ["b/episode.opus", "c/other.mp3"]]


# A whisper stand-in: logs one line per start, writes a transcript per input -
# named after it, into --output_dir - and passes over an input called
# "nowhisper" when it has others, exiting 0 the way the real one does.
_FAKE_WHISPER = r'''
import os, sys
args = sys.argv[1:]
inputs = []
for a in args:
    if a.startswith("--"):
        break
    inputs.append(a)
out = args[args.index("--output_dir") + 1]
fmt = args[args.index("--output_format") + 1]
with open(os.environ["FAKE_WHISPER_LOG"], "a") as log:
    log.write(" ".join(inputs) + "\n")
for path in inputs:
    if "nowhisper" in path and len(inputs) > 1:
        continue
    stem = os.path.splitext(os.path.basename(path))[0]
    with open(os.path.join(out, stem + "." + fmt), "w") as handle:
        handle.write("stub transcript\n")
'''


class TestOneProcessForAGroup:
    @pytest.fixture
    def state(self, tmp_path, monkeypatch):
        fake = tmp_path / "fake_whisper.py"
        fake.write_text(_FAKE_WHISPER)
        calls = tmp_path / "calls"
        calls.write_text("")
        monkeypatch.setenv("FAKE_WHISPER_LOG", str(calls))
        monkeypatch.setattr(whisper, "WHISPER_COMMAND",
                            (sys.executable, str(fake)))
        source, out, ram, counter = (tmp_path / n for n in
                                     ("in", "out", "ram", "counter"))
        for folder in (source, out, ram, counter):
            folder.mkdir()
        model = {"modelMulti": "base", "device": "cpu", "computeType": "int8",
                 "threads": "1"}
        run = transcribe_audio.Run(str(source), str(out), "txt", model,
                                   str(ram), str(counter), 0)
        run.calls = calls
        return run

    def _group(self, state, names):
        members = []
        for name in names:
            path = os.path.join(state.input_dir, name)
            open(path, "w").close()
            members.append(state._prepare(name))
        return next(transcribe_audio._groups(
            ((m, 1) for m in members), len(members)))[0]

    def _starts(self, state):
        return state.calls.read_text().splitlines()

    def test_n_files_start_whisper_once(self, state):
        names = ["ep%d.mp3" % n for n in range(5)]
        state.transcribe_group(self._group(state, names))
        assert len(self._starts(state)) == 1
        for name in names:
            assert os.path.isfile(os.path.join(
                state.output_dir, name[:-4] + ".txt"))
        assert not state.any_failed()
        assert os.listdir(state.ram_root) == []

    def test_a_missed_file_is_retried_alone(self, state):
        state.transcribe_group(self._group(
            state, ["one.mp3", "nowhisper.mp3", "three.mp3"]))
        starts = self._starts(state)
        assert len(starts) == 2, starts
        assert starts[1].endswith("nowhisper.mp3"), starts
        assert " " not in starts[1].strip(), starts
        for stem in ("one", "nowhisper", "three"):
            assert os.path.isfile(os.path.join(state.output_dir,
                                               stem + ".txt"))
        assert not state.any_failed()
        assert os.listdir(state.ram_root) == []


class TestLyrics:
    @pytest.fixture
    def run(self, tmp_path, monkeypatch):
        fake = tmp_path / "fake_whisper.py"
        fake.write_text(_FAKE_WHISPER)
        calls = tmp_path / "calls"
        calls.write_text("")
        monkeypatch.setenv("FAKE_WHISPER_LOG", str(calls))
        monkeypatch.setattr(whisper, "WHISPER_COMMAND",
                            (sys.executable, str(fake)))
        source, out, ram, counter = (tmp_path / n for n in
                                     ("in", "out", "ram", "counter"))
        for folder in (source, out, ram, counter):
            folder.mkdir()
        model = {"modelMulti": "base", "device": "cpu", "computeType": "int8",
                 "threads": "1"}
        embedded = []

        def embed(target, transcript, tmp_dir):
            embedded.append((os.path.basename(target), transcript))
            return 0
        monkeypatch.setattr(transcribe_audio.transcripttags,
                            "embed_transcript", embed)
        state = transcribe_audio.Run(str(source), str(out), "srt", model,
                                     str(ram), str(counter), 0, lyrics=True)
        state.embedded = embedded
        return state

    def test_an_opus_input_gets_a_copy_in_the_output(self, run):
        for name in ("a.opus", "b.mp3"):
            open(os.path.join(run.input_dir, name), "w").close()
        for name in ("a.opus", "b.mp3"):
            run.transcribe_one(run._prepare(name))
        assert os.path.isfile(os.path.join(run.output_dir, "a.opus"))
        assert not os.path.exists(os.path.join(run.output_dir, "b.mp3"))
        assert [e[0] for e in run.embedded] == ["a.opus.part.opus"]

    def test_a_transcript_without_its_copy_is_done_again(self, run):
        open(os.path.join(run.input_dir, "a.opus"), "w").close()
        open(os.path.join(run.output_dir, "a.srt"), "w").write("x")
        assert run._prepare("a.opus") is not None
