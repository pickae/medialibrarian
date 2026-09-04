"""Tests for medialib.lib.commentarytranscription.

What is pinned here: the exact argv each whisper and sync run gets, the queue
records a track layout produces, and the on-disk state a run leaves - what a
commentary becomes, what RAM holds afterwards, what a rerun skips.

The tool coupling is the shared toolstub: ffprobe answers with a canned
duration, ffmpeg materialises the extract and the detection excerpt (both its
last argument), pipx prints the "Detected language" line on every call,
ffsubsync writes its log where --log-dir-path points. The track reader, the
bonus-folder guard and the rename sanitiser are the caller's helpers and are
stood in for, the way the bash test lifts them out of ingest-movies.

One shape the stub cannot fake: a transcription's srt lands in the run's own
output directory, named after the extract - "<movie> <index> <name>.srt" -
which the per-call write list cannot spell, as it is word separated. So the
export-driven cases run their transcriptions to failure (nothing needs to be
written) and the cases that need a file written call the worker directly with
a plain extract name.
"""

import os
import shutil
from types import SimpleNamespace

import pytest

from medialib.lib import commentarytranscription as ct
from tests import blackbox

pytestmark = pytest.mark.stubbed

_TOOLSTUB = blackbox.TOOLSTUB

# the stub itself is a bash script that reaches for these to answer its
# write and rc lists, so they ride along in the stub-only PATH
_PLUMBING = ("bash", "awk", "cat")

# the settled whisper, the way the bash test exports it
DEVICE = "cpu"
COMPUTE = "int8"
THREADS = "4"
MODEL = "base.en"          # English-only, so the two are told apart
MODEL_MULTI = "large-v3"
WHISPER = {"device": DEVICE, "computeType": COMPUTE, "model": MODEL,
           "modelMulti": MODEL_MULTI, "threads": THREADS}

# the fixture's commentary tracks: every route through commentaryLanguage
FILM_A = [
    ("MainAudio", "false", "audio", "eng"),
    ("DirectorCommentary", "true", "audio", "eng"),
    ("DutchCommentary", "true", "audio", "eng"),
    ("Audiokommentar", "true", "audio", "ger"),
    ("CommentaryForeign", "true", "audio", "und"),
    ("Regiekommentar", "false", "audio", "ger"),
]
FILM_B = [
    ("Video", "false", "video", ""),
    ("CommentaryTrackTwo", "true", "audio", "und"),
]


def _detected(name, probability="0.987654"):
    return "Detected language '{}' with probability {}\n".format(
        name, probability)


def _pipx_detect(ram):
    return ["pipx", "run", "whisper-ctranslate2",
            os.path.join(ram, "languageProbe.wav"), "--output_dir", ram,
            "--model", MODEL_MULTI, "--task", "transcribe",
            "--output_format", "txt", "--vad_filter", "True",
            "--compute_type", COMPUTE, "--device", DEVICE, "--threads", THREADS]


def _mka(ram, root, rel_base):
    """The extract path export builds: RAM mirroring the absolute disk path,
    so a track of a film's folder sits under that folder's mirror."""
    return "{}/{}/{}.mka".format(ram, os.path.realpath(root), rel_base)


def _srt_calls(calls, mka):
    """The recorded transcription runs whose input is this extract."""
    return [call for call in calls
            if call[:3] == ["pipx", "run", "whisper-ctranslate2"]
            and call[3] == mka and "--output_format" in call
            and call[call.index("--output_format") + 1] == "srt"]


def _flag(call, name):
    if name not in call:
        return None
    return call[call.index(name) + 1]


@pytest.fixture()
def w(tmp_path, monkeypatch):
    """A PATH holding only the named stubs and their plumbing, plus the knobs
    that decide what each tool prints and with which per-call status it exits.
    """
    bin_dir = tmp_path / "bin"
    out_dir = tmp_path / "out"
    state_dir = tmp_path / "state"
    for d in (bin_dir, out_dir, state_dir):
        d.mkdir()
    for tool in _PLUMBING:
        (bin_dir / tool).symlink_to(shutil.which(tool))
    record = tmp_path / "calls"

    def install(name):
        shutil.copyfile(_TOOLSTUB, str(bin_dir / name))
        os.chmod(str(bin_dir / name), 0o755)

    def say(name, text):
        (out_dir / name).write_text(text)

    def rc(name, codes):
        (out_dir / (name + ".rc")).write_text(codes + "\n")

    def writes(name, paths):
        (out_dir / (name + ".write")).write_text(" ".join(paths) + "\n")

    def calls():
        if not record.exists():
            return []
        return [line.rstrip("\n").split("\t")[1:]
                for line in record.read_text().splitlines() if line]

    def clear():
        if record.exists():
            record.unlink()

    # the cases need the order around the settle, not the wait
    monkeypatch.setattr(ct, "SYNC_SETTLE_SECONDS", 0)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("TOOLSTUB_LOG", str(record))
    monkeypatch.setenv("TOOLSTUB_OUT", str(out_dir))
    monkeypatch.setenv("TOOLSTUB_STATE", str(state_dir))
    return SimpleNamespace(install=install, say=say, rc=rc, writes=writes,
                           calls=calls, clear=clear, bin_dir=bin_dir,
                           tmp_path=tmp_path)


def _read_track_info(tracks):
    """The caller's track reader (the bash's readTrackInfo), over the fixture's
    per-movie track list, keyed by the movie's base name."""
    def read(file):
        rows = tracks[os.path.basename(file)[:-4]]
        names, codecs, channels, comments, types, langs = \
            ([], [], [], [], [], [])
        for name, comment, type_, lang in rows:
            names.append(name)
            codecs.append("AC3")
            channels.append("2")
            comments.append(comment)
            types.append(type_)
            langs.append(lang)
        return (names, codecs, channels, comments, types, langs)
    return read


def _is_bonus_folder(folder):
    return os.path.basename(folder) in {"Extras"}


def _audio_stream_index(track, types):
    return sum(1 for t in types[:track - 1] if "audio" in t)


def _leftover_mkas(ram):
    return [f for dirpath, _dirnames, filenames in os.walk(ram)
            for f in filenames if f.endswith(".mka")]


def _run_export(w, root, ram, tracks, detected, pipx_rc, quality="yes",
                ffmpeg_rc="0"):
    """The whole exportCommentary with its caller helpers stood in: build the
    queue and drain it one record at a time through transcribe_commentary."""
    logs = []
    queue_records = []

    def drain(records, queue):
        queue_records.extend(records)
        for record in records:
            ct.transcribe_commentary(record, WHISPER, "10", quality, ram,
                                     logs.append)

    w.install("ffprobe")
    w.install("ffmpeg")
    w.install("pipx")
    w.install("ffsubsync")
    w.say("ffprobe", "7200.0\n")
    w.rc("ffprobe", "0 0 0")
    w.rc("ffmpeg", ffmpeg_rc)
    w.writes("ffmpeg", ["$LAST"] * 16)
    w.say("pipx", detected)
    w.rc("pipx", pipx_rc)
    cwd = os.getcwd()
    try:
        ct.export_commentary(root, _read_track_info(tracks), _is_bonus_folder,
                             lambda name: name, _audio_stream_index, ram,
                             WHISPER, 2, logs.append, drain, "10", quality)
    finally:
        os.chdir(cwd)
    return logs, queue_records


class TestQueue:
    def _fixture(self, w):
        root = w.tmp_path / "root"
        (root / "FilmA2020").mkdir(parents=True)
        (root / "FilmA2020" / "FilmA2020.mkv").touch()
        (root / "FilmB2021").mkdir()
        (root / "FilmB2021" / "FilmB2021.mkv").touch()
        (root / "Extras").mkdir()
        (root / "Extras" / "Bonus.mkv").touch()
        ram = w.tmp_path / "ram"
        ram.mkdir()
        tracks = {
            "FilmA2020": FILM_A,
            "FilmB2021": FILM_B,
            "Bonus": [("Commentary", "true", "audio", "eng")],
        }
        return str(root), str(ram), tracks

    def test_one_run_per_wanted_subtitle_across_every_film(self, w):
        root, ram, tracks = self._fixture(w)

        # three tracks give whisper nothing but their excerpt to work with,
        # and the excerpt is cut from the MIDDLE of a feature-length track:
        # ffprobe says 7200 s, so it starts at (7200-120)/2
        logs, records = _run_export(
            w, root, ram, tracks, _detected("English"),
            "0 0 0 1 1 1 1 1 1 1 1 1",
            ffmpeg_rc="0 0 0 0 0 0 0 0 0")

        # the plain extract of a commentary track: mkvtools numbers the whole
        # matroska while ffmpeg numbers each type from zero, so the second
        # track of FilmA is 0:a:1, and the input is the path the walk spelled
        mkaA1 = _mka(ram, root, "FilmA2020/FilmA2020 1 DirectorCommentary")
        extract_a = [c for c in w.calls() if c[0] == "ffmpeg"
                     and c[-1] == mkaA1][0]
        assert extract_a == [
            "ffmpeg", "-y", "-loglevel", "error", "-nostats",
            "-i", "./FilmA2020/FilmA2020.mkv",
            "-vn", "-map", "0:a:1", "-acodec", "copy", mkaA1,
        ]
        # and its detection excerpt, cut from the middle of the extract
        excerpt_a = [c for c in w.calls() if c[0] == "ffmpeg"
                     and c[-1] == os.path.join(ram, "languageProbe.wav")
                     and mkaA1 in c][0]
        assert excerpt_a == [
            "ffmpeg", "-y", "-loglevel", "error", "-nostats", "-ss", "3540",
            "-t", "120", "-i", mkaA1,
            "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
            os.path.join(ram, "languageProbe.wav"),
        ]
        # and the probe itself, on the multilingual model
        detect_a = [c for c in w.calls() if c[0] == "pipx"
                    and c[3] == os.path.join(ram, "languageProbe.wav")][0]
        assert detect_a == _pipx_detect(ram)

        # the flat queue: one record per wanted subtitle, across both films -
        # and not one for the plain audio track, the video track or the bonus
        # folder's movie
        assert len(records) == 9
        expected = {
            ("transcribe", "en"): [
                "FilmA2020 1 DirectorCommentary.en.srt",
                "FilmA2020 4 CommentaryForeign.en.srt",
                "FilmB2021 1 CommentaryTrackTwo.en.srt"],
            ("transcribe", "nl"): ["FilmA2020 2 DutchCommentary.nl.srt"],
            ("translate", "nl"): ["FilmA2020 2 DutchCommentary.en.srt"],
            ("transcribe", "de"): [
                "FilmA2020 3 Audiokommentar.de.srt",
                "FilmA2020 5 Regiekommentar.de.srt"],
            ("translate", "de"): [
                "FilmA2020 3 Audiokommentar.en.srt",
                "FilmA2020 5 Regiekommentar.en.srt"],
        }
        for record in records:
            fields = record.split("\x1f")
            assert len(fields) == 5
            name = fields[1].rsplit("/", 1)[-1]
            key = (fields[2], fields[3])
            assert name in expected[key], (name, key)
            expected[key].remove(name)
        assert not any(entries for entries in expected.values())

        # every record carries the sibling list its extract shares, so the last
        # run of it can free the extract
        for record in records:
            mka, srt, _task, _lang, siblings = record.split("\x1f")
            assert srt in siblings

        # and the queue file is exactly those NUL terminated records
        with open(os.path.join(ram, "commentaryQueue"), "rb") as handle:
            data = handle.read()
        assert data == "".join(r + "\0" for r in records).encode("utf-8")

# each run got the right task, language and model: anything that is not
        # an English transcription goes to the multilingual model, and the
        # English initial prompt only fronts runs that OUTPUT English
        en = _srt_calls(w.calls(), _mka(
            ram, root, "FilmA2020/FilmA2020 1 DirectorCommentary"))
        assert len(en) == 1
        assert _flag(en[0], "--task") == "transcribe"
        assert _flag(en[0], "--language") == "en"
        assert _flag(en[0], "--model") == MODEL
        assert _flag(en[0], "--initial_prompt") == "Hello."

        dutch = _srt_calls(w.calls(), _mka(
            ram, root, "FilmA2020/FilmA2020 2 DutchCommentary"))
        assert len(dutch) == 2
        native = [c for c in dutch if _flag(c, "--task") == "transcribe"][0]
        assert _flag(native, "--language") == "nl"
        assert _flag(native, "--model") == MODEL_MULTI
        assert "--initial_prompt" not in native
        translated = [c for c in dutch if _flag(c, "--task") == "translate"][0]
        assert _flag(translated, "--language") == "nl"
        assert _flag(translated, "--model") == MODEL_MULTI
        assert _flag(translated, "--initial_prompt") == "Hello."

# whisper heard English in the "foreign" track, so it got the English
        # treatment: one plain transcription, English model, English prompt
        foreign = _srt_calls(w.calls(), _mka(
            ram, root, "FilmA2020/FilmA2020 4 CommentaryForeign"))
        assert len(foreign) == 1
        assert _flag(foreign[0], "--task") == "transcribe"
        assert _flag(foreign[0], "--language") == "en"
        assert _flag(foreign[0], "--model") == MODEL

        # the queue spans every film: one drain, after every extract
        assert "Transcribing 9 queued commentary subtitle(s) on 2 " \
            "worker(s)" in logs

        # the transcriptions failed, so the workers left every extract to the
        # sweep at the end of the export - and it swept
        assert _leftover_mkas(ram) == []
        for record in records:
            assert not os.path.isfile(
                os.path.join(root, record.split("\x1f")[1]))

        # and a rerun re-extracts and re-transcribes nothing at all: the
        # resume check skips a track that already has ANY output
        for record in records:
            open(os.path.join(root, record.split("\x1f")[1]), "w").close()
        before = sorted(str(p.relative_to(root)) for p in
                        (w.tmp_path / "root").rglob("*") if p.is_file())
        w.clear()
        logs2, records2 = _run_export(
            w, root, ram, tracks, _detected("English"),
            "0 0 0 1 1 1 1 1 1 1 1 1",
            ffmpeg_rc="0 0 0 0 0 0 0 0 0")
        assert records2 == []
        assert w.calls() == []
        after = sorted(str(p.relative_to(root)) for p in
                       (w.tmp_path / "root").rglob("*") if p.is_file())
        assert before == after

    def test_a_translation_gets_the_prompt_even_from_english(self, w):
        root = w.tmp_path / "root"
        root.mkdir()
        ram = w.tmp_path / "ram"
        ram.mkdir()
        mka = os.path.join(ram, "extract.mka")
        open(mka, "w").close()
        srt = str(root / "out.en.srt")
        record = "\x1f".join([mka, srt, "translate", "en", srt])
        w.install("pipx")
        w.install("ffsubsync")
        w.rc("pipx", "0")
        w.writes("pipx", ["${--output_dir}/extract.srt"])
        w.rc("ffsubsync", "0")
        w.say("ffsubsync", "synced fine\n")
        w.writes("ffsubsync", ["${--log-dir-path}/ffsubsync.log"])
        logs = []
        ct.transcribe_commentary(record, WHISPER, "10", "yes", str(ram),
                                 logs.append)
        call = _srt_calls(w.calls(), mka)[0]
        assert _flag(call, "--task") == "translate"
        assert _flag(call, "--language") == "en"
        assert _flag(call, "--model") == MODEL_MULTI
        assert _flag(call, "--initial_prompt") == "Hello."
        assert os.path.isfile(srt)
        # the finished transcript is aligned against the extract
        sync_call = [c for c in w.calls() if c[0] == "ffsubsync"][0]
        assert sync_call[1] == mka
        assert sync_call[2:5] == ["-i", srt, "-o"]
        assert _flag(sync_call, "--max-offset-seconds") == "10"


class TestState:
    def _direct(self, w, mka, srt, task, lang, siblings, pipx_rc,
                pipx_write="-", ffsubsync_rc="0", ffsubsync_out=
                "synced fine\n", quality="no"):
        ram = w.tmp_path / "ram"
        ram.mkdir()
        os.makedirs(os.path.dirname(mka), exist_ok=True)
        open(mka, "w").close()
        w.install("pipx")
        w.install("ffsubsync")
        w.rc("pipx", pipx_rc)
        if pipx_write:
            w.writes("pipx", [pipx_write])
        w.rc("ffsubsync", ffsubsync_rc)
        w.say("ffsubsync", ffsubsync_out)
        w.writes("ffsubsync", ["${--log-dir-path}/ffsubsync.log"])
        logs = []
        record = "\x1f".join([mka, srt, task, lang, siblings])
        ct.transcribe_commentary(record, WHISPER, "10", quality, str(ram),
                                 logs.append)
        return logs

    def test_an_english_transcription_runs_on_the_english_model(self, w):
        root = w.tmp_path / "root"
        root.mkdir()
        mka = str(w.tmp_path / "ram" / "extract.mka")
        srt = str(root / "out.en.srt")
        logs = self._direct(w, mka, srt, "transcribe", "en", srt, "0",
                            "${--output_dir}/extract.srt")
        call = _srt_calls(w.calls(), mka)[0]
        assert _flag(call, "--model") == MODEL
        assert _flag(call, "--language") == "en"
        assert _flag(call, "--initial_prompt") == "Hello."
        assert os.path.isfile(srt)
        assert not os.path.exists(mka)  # every sibling (itself) is on disk
        assert logs == ["Transcribing commentary (en, {}): {}".format(
            MODEL, os.path.basename(srt))]

    def test_and_so_does_its_translation_counterpart(self, w):
        root = w.tmp_path / "root"
        root.mkdir()
        mka = str(w.tmp_path / "ram" / "extract.mka")
        srt = str(root / "out.nl.srt")
        logs = self._direct(w, mka, srt, "transcribe", "nl", srt, "0",
                            "${--output_dir}/extract.srt")
        call = _srt_calls(w.calls(), mka)[0]
        assert _flag(call, "--model") == MODEL_MULTI
        assert "--initial_prompt" not in call
        assert os.path.isfile(srt)
        assert logs == ["Transcribing commentary (nl, {}): {}".format(
            MODEL_MULTI, os.path.basename(srt))]

    def test_a_sync_that_dies_discards_the_transcript(self, w):
        root = w.tmp_path / "root"
        root.mkdir()
        mka = str(w.tmp_path / "ram" / "extract.mka")
        srt = str(root / "out.en.srt")
        logs = self._direct(w, mka, srt, "transcribe", "en", srt, "0",
                            "${--output_dir}/extract.srt", ffsubsync_rc="1")
        assert "WARNING: transcript sync failed, discarding: {}".format(
            os.path.basename(srt)) in logs
        assert not os.path.isfile(srt)
        assert os.path.exists(mka)

    def test_a_low_quality_sync_is_discarded_too(self, w):
        root = w.tmp_path / "root"
        root.mkdir()
        mka = str(w.tmp_path / "ram" / "extract.mka")
        srt = str(root / "out.en.srt")
        logs = self._direct(
            w, mka, srt, "transcribe", "en", srt, "0",
            "${--output_dir}/extract.srt", ffsubsync_out=
            "low-quality alignment found\n", quality="yes")
        assert "WARNING: transcript sync rejected as low-quality, " \
            "discarding: {}".format(os.path.basename(srt)) in logs
        assert not os.path.isfile(srt)
        assert os.path.exists(mka)
        # the quality offset only goes on the sync call when quality is on
        sync_call = [c for c in w.calls() if c[0] == "ffsubsync"][0]
        assert "--skip-sync-on-low-quality" in sync_call
        assert _flag(sync_call, "--quality-max-offset-seconds") == "10"
        assert _flag(sync_call, "--max-offset-seconds") == "10"

    def test_without_quality_the_offset_flags_are_left_off(self, w):
        root = w.tmp_path / "root"
        root.mkdir()
        mka = str(w.tmp_path / "ram" / "extract.mka")
        srt = str(root / "out.en.srt")
        self._direct(w, mka, srt, "transcribe", "en", srt, "0",
                     "${--output_dir}/extract.srt")
        sync_call = [c for c in w.calls() if c[0] == "ffsubsync"][0]
        assert "--skip-sync-on-low-quality" not in sync_call
        assert "--quality-max-offset-seconds" not in sync_call
        assert os.path.isfile(srt)

    def test_a_transcription_that_writes_nothing_is_a_failure(self, w):
        root = w.tmp_path / "root"
        root.mkdir()
        mka = str(w.tmp_path / "ram" / "extract.mka")
        srt = str(root / "out.en.srt")
        logs = self._direct(w, mka, srt, "transcribe", "en", srt, "0",
                            pipx_write="-")
        assert "WARNING: transcription failed: {}".format(
            os.path.basename(srt)) in logs
        assert not os.path.isfile(srt)
        assert os.path.exists(mka)

    def test_and_so_is_one_that_fails_outright(self, w):
        root = w.tmp_path / "root"
        root.mkdir()
        mka = str(w.tmp_path / "ram" / "extract.mka")
        srt = str(root / "out.en.srt")
        logs = self._direct(w, mka, srt, "transcribe", "en", srt, "7")
        assert "WARNING: transcription failed: {}".format(
            os.path.basename(srt)) in logs
        assert not os.path.isfile(srt)
        # a failed run leaves its extract to the sweep
        assert os.path.exists(mka)
        # ... and the whisper run was never reached for with the wrong model
        call = _srt_calls(w.calls(), mka)
        assert len(call) == 1

    def test_a_record_with_no_siblings_frees_the_extract_anyway(self, w):
        root = w.tmp_path / "root"
        root.mkdir()
        mka = str(w.tmp_path / "ram" / "extract.mka")
        srt = str(root / "out.en.srt")
        logs = self._direct(w, mka, srt, "transcribe", "en", "", "7")
        assert any("WARNING: transcription failed" in line for line in logs)
        assert not os.path.exists(mka)

    def test_a_worker_that_finds_its_srt_already_there_does_nothing(self, w):
        root = w.tmp_path / "root"
        root.mkdir()
        ram = w.tmp_path / "ram"
        ram.mkdir()
        mka = str(ram / "extract.mka")
        srt = str(root / "out.en.srt")
        open(mka, "w").close()
        open(srt, "w").close()
        ct.transcribe_commentary(
            "\x1f".join([mka, srt, "transcribe", "en", srt]), WHISPER, "10",
            "no", str(ram), lambda line: None)
        assert w.calls() == []
        assert not os.path.exists(mka)

    def test_the_extract_is_freed_once_every_sibling_exists(self, w):
        root = w.tmp_path / "root"
        root.mkdir()
        ram = w.tmp_path / "ram"
        ram.mkdir()
        mka = str(ram / "extract.mka")
        native = str(root / "out.nl.srt")
        english = str(root / "out.en.srt")
        open(mka, "w").close()
        siblings = native + "\x1e" + english
        w.install("pipx")
        w.install("ffsubsync")
        w.rc("pipx", "0 0")
        w.writes("pipx", ["${--output_dir}/extract.srt",
                          "${--output_dir}/extract.srt"])
        w.rc("ffsubsync", "0 0")
        w.say("ffsubsync", "synced fine\n")
        w.writes("ffsubsync", ["${--log-dir-path}/ffsubsync.log",
                               "${--log-dir-path}/ffsubsync.log"])
        logs = []
        # the native transcript first: the translation is still missing, so
        # the extract stays
        ct.transcribe_commentary(
            "\x1f".join([mka, native, "transcribe", "nl", siblings]),
            WHISPER, "10", "no", str(ram), logs.append)
        assert os.path.isfile(native)
        assert os.path.exists(mka)
        # the translation last: every sibling is on disk, so the extract goes
        ct.transcribe_commentary(
            "\x1f".join([mka, english, "translate", "nl", siblings]),
            WHISPER, "10", "no", str(ram), logs.append)
        assert os.path.isfile(english)
        assert not os.path.exists(mka)


class TestExportEdges:
    def _one_track(self, w, track, detected, pipx_rc, ffmpeg_rc="0",
                   quality="yes"):
        root = w.tmp_path / "root"
        root.mkdir()
        (root / "movie.mkv").touch()
        ram = w.tmp_path / "ram"
        ram.mkdir()
        return _run_export(
            w, str(root), str(ram), {"movie": [track]}, detected, pipx_rc,
            quality=quality, ffmpeg_rc=ffmpeg_rc)

    def test_an_inconclusive_detection_assumes_english(self, w):
        logs, records = self._one_track(
            w, ("Commentary", "true", "audio", "und"),
            _detected("Dutch", "0.3"), "0 1", ffmpeg_rc="0 0")
        assert "WARNING: could not tell the language of commentary " \
            "track 0, assuming English: ./movie.mkv" in logs
        fields = records[0].split("\x1f")
        assert fields[2] == "transcribe"
        assert fields[3] == "en"
        assert fields[1].endswith("movie 0 Commentary.en.srt")

    def test_a_failed_probe_assumes_english(self, w):
        logs, records = self._one_track(
            w, ("Commentary", "true", "audio", "und"),
            _detected("Dutch"), "0", ffmpeg_rc="0 7")
        # the excerpt cannot be made, so whisper is never ASKED what language this is
        # (the queue's transcription run is a different question), and the
        # track falls back to English
        assert any("assuming English" in line for line in logs)
        assert not any(c[0] == "pipx" and c[3] == os.path.join(
            str(w.tmp_path / "ram"), "languageProbe.wav")
            for c in w.calls())
        fields = records[0].split("\x1f")
        assert fields[2] == "transcribe"
        assert fields[3] == "en"

    def test_a_failed_extract_is_skipped(self, w):
        logs, records = self._one_track(
            w, ("Commentary", "true", "audio", "eng"),
            _detected("English"), "0 0", ffmpeg_rc="7")
        # the extract failed, so nothing was queued ...
        assert records == []
        assert "WARNING: commentary extract failed (track 0): " \
            "./movie.mkv" in logs
        # ... and the failed extract is not left behind
        ram = w.tmp_path / "ram"
        assert _leftover_mkas(str(ram)) == []

    def test_and_so_is_a_bonus_folder(self, w):
        root = w.tmp_path / "root"
        (root / "Extras").mkdir(parents=True)
        (root / "Extras" / "Bonus.mkv").touch()
        ram = w.tmp_path / "ram"
        ram.mkdir()
        logs, records = _run_export(
            w, str(root), str(ram),
            {"Bonus": [("Commentary", "true", "audio", "eng")]},
            _detected("English"), "0 0")
        assert records == []
        assert w.calls() == []
        assert "Extras" not in "".join(logs)

    def test_the_extract_failure_leaves_nothing_in_ram(self, w):
        # a track whose extract fails and a track that succeeds: the sweep at
        # the end of the export leaves RAM clean either way. The walk names the
        # films in no promised order, so the failure is the LAST extract: the
        # two successful calls are one extract and its excerpt, whichever film
        # it reached first
        root = w.tmp_path / "root"
        root.mkdir()
        (root / "movie.mkv").touch()
        (root / "other.mkv").touch()
        ram = w.tmp_path / "ram"
        ram.mkdir()
        logs, records = _run_export(
            w, str(root), str(ram),
            {"movie": [("Commentary", "true", "audio", "eng")],
             "other": [("Commentary", "true", "audio", "eng")]},
            _detected("English"), "0 1", ffmpeg_rc="0 0 7")
        # the surviving film's extract was queued and its transcription ran (and
        # failed), and the sweep leaves RAM clean of both extracts
        assert len(records) == 1
        assert _leftover_mkas(str(ram)) == []


class TestDetect:
    def _detect(self, w, ffprobe_out, ffprobe_rc, ffmpeg_rc, detected):
        ram = w.tmp_path / "ram"
        ram.mkdir()
        mka = os.path.join(str(ram), "extract.mka")
        open(mka, "w").close()
        w.install("ffprobe")
        w.install("ffmpeg")
        w.install("pipx")
        w.say("ffprobe", ffprobe_out)
        w.rc("ffprobe", ffprobe_rc)
        w.rc("ffmpeg", ffmpeg_rc)
        w.writes("ffmpeg", ["$LAST"])
        w.say("pipx", detected)
        w.rc("pipx", "0")
        answer = ct.detect_commentary_language(mka, str(ram), WHISPER,
                                               lambda line: None)
        return answer, w.calls()

    def test_the_excerpt_is_cut_from_the_middle(self, w):
        answer, calls = self._detect(w, "7200.0\n", "0", "0",
                                     _detected("Dutch"))
        assert answer == "Dutch"
        assert calls[0][0] == "ffprobe"
        assert calls[1] == ["ffmpeg", "-y", "-loglevel", "error", "-nostats",
                            "-ss", "3540", "-t", "120", "-i",
                            os.path.join(str(w.tmp_path / "ram"),
                                         "extract.mka"),
                            "-vn", "-ac", "1", "-ar", "16000",
                            "-c:a", "pcm_s16le",
                            os.path.join(str(w.tmp_path / "ram"),
                                         "languageProbe.wav")]
        assert calls[2] == _pipx_detect(str(w.tmp_path / "ram"))

    def test_but_not_for_a_track_shorter_than_two_excerpts(self, w):
        _answer, calls = self._detect(w, "239.9\n", "0", "0",
                                      _detected("Dutch"))
        assert calls[1][6] == "0"

    def test_and_a_duration_the_arithmetic_cannot_read_is_zero(self, w):
        _answer, calls = self._detect(w, "abc\n", "0", "0",
                                      _detected("Dutch"))
        assert calls[1][6] == "0"

    def test_an_unreadable_track_is_zero_too(self, w):
        _answer, calls = self._detect(w, "", "7", "0", _detected("Dutch"))
        assert calls[1][6] == "0"

    def test_a_probability_below_half_is_no_answer(self, w):
        answer, _calls = self._detect(w, "7200.0\n", "0", "0",
                                      _detected("Dutch", "0.49999"))
        assert answer == ""

    def test_and_half_is_an_answer(self, w):
        answer, _calls = self._detect(w, "7200.0\n", "0", "0",
                                      _detected("Dutch", "0.5"))
        assert answer == "Dutch"

    def test_a_failed_probe_drops_its_output_too(self, w):
        ram = w.tmp_path / "ram"
        ram.mkdir()
        mka = os.path.join(str(ram), "extract.mka")
        open(mka, "w").close()
        w.install("ffprobe")
        w.install("ffmpeg")
        w.install("pipx")
        w.say("ffprobe", "7200.0\n")
        w.rc("ffprobe", "0")
        w.rc("ffmpeg", "0")
        w.writes("ffmpeg", ["$LAST"])
        w.say("pipx", _detected("Dutch"))
        w.rc("pipx", "1")
        assert ct.detect_commentary_language(mka, str(ram), WHISPER,
                                             lambda line: None) == ""

    def test_output_without_the_detection_line_is_no_answer(self, w):
        ram = w.tmp_path / "ram"
        ram.mkdir()
        mka = os.path.join(str(ram), "extract.mka")
        open(mka, "w").close()
        w.install("ffprobe")
        w.install("ffmpeg")
        w.install("pipx")
        w.say("ffprobe", "7200.0\n")
        w.rc("ffprobe", "0")
        w.rc("ffmpeg", "0")
        w.writes("ffmpeg", ["$LAST"])
        w.say("pipx", "1\n00:00:00,000 --> 00:00:02,000\nnothing here\n")
        w.rc("pipx", "0")
        assert ct.detect_commentary_language(mka, str(ram), WHISPER,
                                             lambda line: None) == ""


class TestCommentaryLanguage:
    def _lang(self, w, name, tag, detected=""):
        ram = w.tmp_path / "ram"
        ram.mkdir()
        mka = os.path.join(str(ram), "extract.mka")
        open(mka, "w").close()
        if detected:
            w.install("ffprobe")
            w.install("ffmpeg")
            w.install("pipx")
            w.say("ffprobe", "7200.0\n")
            w.rc("ffprobe", "0")
            w.rc("ffmpeg", "0")
            w.writes("ffmpeg", ["$LAST"])
            w.say("pipx", _detected(detected))
            w.rc("pipx", "0")
        logs = []
        return ct.commentary_language(name, tag, mka, str(ram), WHISPER,
                                      logs.append)

    def test_a_language_word_in_the_name(self, w):
        assert self._lang(w, "German Commentary", "eng") == ("de", "de")
        assert w.calls() == []

    def test_and_case_does_not_matter(self, w):
        assert self._lang(w, "GERMAN commentary", "eng") == ("de", "de")

    def test_the_name_wins_over_the_tag(self, w):
        assert self._lang(w, "Dutch Commentary", "ger") == ("nl", "nl")

    def test_a_real_tag(self, w):
        assert self._lang(w, "Audiokommentar", "ger") == ("de", "de")

    def test_but_not_the_english_default(self, w):
        # eng is what updateTags stamps on any commentary that says nothing,
        # so it is the default rather than evidence
        assert self._lang(w, "Audiokommentar", "eng", "Dutch") == ("nl", "nl")

    def test_a_tag_this_script_has_no_row_for_is_no_evidence(self, w):
        assert self._lang(w, "Commentary", "zzz", "Dutch") == ("nl", "nl")

    def test_whisper_answers_with_the_code_when_there_is_one(self, w):
        assert self._lang(w, "Commentary", "eng", "Dutch") == ("nl", "nl")

    def test_and_with_the_name_when_there_is_not(self, w):
        assert self._lang(w, "Commentary", "eng", "Japanese") == \
            ("Japanese", "")

    def test_an_inconclusive_whisper_is_no_answer_at_all(self, w):
        ram = w.tmp_path / "ram"
        ram.mkdir()
        mka = os.path.join(str(ram), "extract.mka")
        open(mka, "w").close()
        w.install("ffprobe")
        w.install("ffmpeg")
        w.install("pipx")
        w.say("ffprobe", "7200.0\n")
        w.rc("ffprobe", "0")
        w.rc("ffmpeg", "0")
        w.writes("ffmpeg", ["$LAST"])
        w.say("pipx", _detected("Dutch", "0.3"))
        w.rc("pipx", "0")
        assert ct.commentary_language("Commentary", "eng", mka, str(ram),
                                      WHISPER, lambda line: None) == ("", "")