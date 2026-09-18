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

from medialib.cli import ingest_movies as rules
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
                ffmpeg_rc="0", discard_existing=None,
                same_commentary_name=rules._same_commentary):
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
                             WHISPER, 2, logs.append, drain, "10", quality,
                             discard_existing, same_commentary_name)
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

    @pytest.mark.parametrize("kept", [
        "movie (old).mkv",
        "movie {imdb-tt0120737} {edition-Colorized} (old).mkv"])
    def test_and_so_is_the_copy_an_improvement_kept(self, w, kept):
        """Its commentary is the same audio as its living sibling's, which is
        already being transcribed. The suffix comes last, after every tag, so a
        copy of an already-tagged film is passed over just the same."""
        root = w.tmp_path / "root"
        root.mkdir()
        (root / kept).touch()
        ram = w.tmp_path / "ram"
        ram.mkdir()
        logs, records = _run_export(
            w, str(root), str(ram),
            {os.path.splitext(kept)[0]: [("Commentary", "true", "audio",
                                          "eng")]},
            _detected("English"), "0 0")
        assert records == []
        assert w.calls() == []

    def test_a_transcript_cut_at_another_place_is_still_this_track_s(self, w):
        """The resume check asks after the TRACK, not after today's name.

        Anything that changes the length of a film's name - an id tag added, a
        folder renamed - moves the place a long track name is cut at, and a
        check for the exact name would not see the transcript sitting right
        there: the whole transcription would be spent again to write a second
        copy of it under the new name.
        """
        root = w.tmp_path / "root"
        root.mkdir()
        (root / "movie.mkv").touch()
        ram = w.tmp_path / "ram"
        ram.mkdir()
        name = "Commentary by " + "Someone " * 40
        tracks = {"movie": [(name, "true", "audio", "eng")]}

        # what a run whose film was named something shorter left behind: the
        # same track, cut ten characters earlier than today's run would cut it
        stem = ct.commentary_stem("movie", 0, name)
        (root / (stem[:-10] + ".en.srt")).touch()

        logs, records = _run_export(w, str(root), str(ram), tracks,
                                    _detected("English"), "0 0")
        assert records == []
        assert w.calls() == []

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

    def test_a_sidecar_the_caller_judges_too_small_is_discarded(self, w):
        """The existing sidecar is the resume check's other answer: removed, and
        the commentary is transcribed for real rather than skipped."""
        root = w.tmp_path / "root"
        root.mkdir()
        (root / "movie.mkv").touch()
        sidecar = root / "movie 0 Commentary.en.srt"
        sidecar.write_bytes(b"x" * 100)
        ram = w.tmp_path / "ram"
        ram.mkdir()
        tracks = {"movie": [("Commentary", "true", "audio", "eng")]}

        discarded = []
        logs, records = _run_export(
            w, str(root), str(ram), tracks, _detected("English"), "0 0",
            discard_existing=lambda prefix, movie: (discarded.append(prefix),
                                                    True)[1])
        assert len(discarded) == 1
        assert discarded[0].endswith("movie 0 ")
        assert not sidecar.exists()
        assert len(records) == 1
        assert any("Discarding" in line for line in logs)

    def test_and_a_sidecar_the_caller_keeps_is_not_touched(self, w):
        """The existing sidecar is the resume: asked about, and left alone -
        nothing extracted, nothing queued."""
        root = w.tmp_path / "root"
        root.mkdir()
        (root / "movie.mkv").touch()
        sidecar = root / "movie 0 Commentary.en.srt"
        sidecar.write_bytes(b"y" * 100000)
        ram = w.tmp_path / "ram"
        ram.mkdir()
        tracks = {"movie": [("Commentary", "true", "audio", "eng")]}

        asked = []
        logs, records = _run_export(
            w, str(root), str(ram), tracks, _detected("English"), "0 0",
            discard_existing=lambda prefix, movie: (asked.append(prefix),
                                                    False)[1])
        assert len(asked) == 1
        assert asked[0].endswith("movie 0 ")
        assert sidecar.exists()
        assert records == []


class TestStaleNumber:
    """A transcript an older run numbered for a track that no longer stands
    where it numbered it: the resume check finds nothing under the track's own
    number, and the near miss is that a second transcription is spent on a
    transcript that is beside the film. Renumbered to the track's own number,
    the file is what the check skips on - and is judged by the size rule like
    any other sidecar once renumbered."""

    TRACKS = {"movie": [("Video", "false", "video", ""),
                        ("Main", "false", "audio", "eng"),
                        ("Commentary", "true", "audio", "eng")]}

    def _fixture(self, w, tracks=None):
        root = w.tmp_path / "root"
        root.mkdir()
        (root / "movie.mkv").touch()
        ram = w.tmp_path / "ram"
        ram.mkdir()
        return root, ram, tracks or self.TRACKS

    def test_a_transcript_numbered_for_a_stale_track_is_renumbered(self, w):
        """The track's own number is what no cut may reach, and the one thing
        a rerun may not have kept: the film's track layout changed under the
        name, so the transcript is renumbered rather than transcribed again."""
        root, ram, tracks = self._fixture(w)
        stale = root / "movie 1 Commentary.en.srt"
        stale.write_bytes(b"1\n00:00:00,000 --> 00:00:01,000\nstale\n")

        logs, records = _run_export(
            w, str(root), str(ram), tracks, _detected("English"), "0 0")

        # renumbered to the track's own number, the rest of the name as
        # written, and the srt itself untouched
        assert not stale.exists()
        renumbered = root / "movie 2 Commentary.en.srt"
        assert renumbered.read_bytes() == \
            b"1\n00:00:00,000 --> 00:00:01,000\nstale\n"
        # and nothing extracted, detected or transcribed on top of it
        assert records == []
        assert w.calls() == []
        assert sum("Renumbered" in line for line in logs) == 1

    def test_and_a_name_an_older_run_cut_short_is_the_same_commentary(self, w):
        """Not an identical name: a name an older run cut at a different place
        still points at the commentary, where nothing else of the film's it
        could be of."""
        root, ram, tracks = self._fixture(
            w, {"movie": [("Video", "false", "video", ""),
                          ("Main", "false", "audio", "eng"),
                          ("Director Commentary by John Smith", "true",
                           "audio", "eng")]})
        stale = root / "movie 1 Director Commentary by John.en.srt"
        stale.write_bytes(b"cut short\n")

        logs, records = _run_export(
            w, str(root), str(ram), tracks, _detected("English"), "0 0")

        assert not stale.exists()
        assert (root / "movie 2 Director Commentary by John.en.srt").is_file()
        assert records == []

    def test_but_not_the_one_the_film_s_other_commentary_also_fits(self, w):
        """A name cut back to a point two of the film's commentaries both fit
        points at no particular one, which is what the number there is for:
        neither track renumbers it, and the film's commentaries are
        transcribed for real."""
        tracks = {"movie": [("Video", "false", "video", ""),
                            ("Main", "false", "audio", "eng"),
                            ("Audio Commentary", "true", "audio", "eng"),
                            ("Audio Commentary German", "true", "audio",
                             "ger")]}
        root, ram, _ = self._fixture(w)
        # cut to a point both commentaries' names still start with, so it fits
        # two of them and says no
        stale = root / "movie 1 Audio Comment.en.srt"
        stale.write_bytes(b"ambiguous\n")

        _logs, records = _run_export(
            w, str(root), str(ram), tracks, _detected("English"),
            "0 0 0 0", ffmpeg_rc="0 0 0")

        # the stub is still beside the film, untouched by either track
        assert stale.exists()
        assert stale.read_bytes() == b"ambiguous\n"
        # and both commentaries were transcribed for real
        assert len(records) == 3

    def test_a_stale_number_of_the_other_commentary_is_its_own(self, w):
        """A transcript that fits exactly ONE of the film's commentaries is
        that commentary's whatever number it stands under: renumbered by the
        track it is of, and not reached for by the one it is not."""
        tracks = {"movie": [("Video", "false", "video", ""),
                            ("Main", "false", "audio", "eng"),
                            ("Audio Commentary", "true", "audio", "eng"),
                            ("Audio Commentary German", "true", "audio",
                             "ger")]}
        root, ram, _ = self._fixture(w)
        stale = root / "movie 1 Audio Commentary German.en.srt"
        stale.write_bytes(b"german\n")

        logs, records = _run_export(
            w, str(root), str(ram), tracks, _detected("English"),
            "0 0", ffmpeg_rc="0 0")

        # the German commentary's track renumbered it, and skipped
        assert not stale.exists()
        renumbered = root / "movie 3 Audio Commentary German.en.srt"
        assert renumbered.read_bytes() == b"german\n"
        # the other commentary has no transcript, and was transcribed
        assert len(records) == 1
        assert records[0].split("\x1f")[1].endswith(
            "movie 2 Audio Commentary.en.srt")
        assert sum("Renumbered" in line for line in logs) == 1

    def test_a_transcript_an_older_version_named_without_a_language(self, w):
        """One written before the language went into the name carries no
        suffix at all, and is renumbered the same way."""
        root, ram, tracks = self._fixture(w)
        stale = root / "movie 1 Commentary.srt"
        stale.write_bytes(b"old style\n")

        logs, records = _run_export(
            w, str(root), str(ram), tracks, _detected("English"), "0 0")

        assert not stale.exists()
        assert (root / "movie 2 Commentary.srt").read_bytes() \
            == b"old style\n"
        assert records == []

    def test_but_a_stale_sidecar_the_caller_judges_too_small_is_discarded(
            self, w):
        """Renumbered, the sidecar is asked the same questions the check asks:
        one too small to be the film's real transcript is discarded at the
        track's own number, and the commentary is transcribed for real."""
        root, ram, tracks = self._fixture(w)
        stale = root / "movie 1 Commentary.en.srt"
        stale.write_bytes(b"x" * 100)

        discarded = []
        logs, records = _run_export(
            w, str(root), str(ram), tracks, _detected("English"), "0 0",
            ffmpeg_rc="0 0",
            discard_existing=lambda prefix, movie: (discarded.append(prefix),
                                                     True)[1])
        assert len(discarded) == 1
        assert discarded[0].endswith("movie 2 ")
        assert not stale.exists()
        assert len(records) == 1
        assert any("Discarding" in line for line in logs)

    def test_and_a_stale_sidecar_the_caller_keeps_is_renumbered_not_touched(
            self, w):
        """The size rule says keep, and the rest says the same: renumbered to
        the track's own number and left alone - nothing extracted, nothing
        queued."""
        root, ram, tracks = self._fixture(w)
        stale = root / "movie 1 Commentary.en.srt"
        stale.write_bytes(b"y" * 100000)

        asked = []
        logs, records = _run_export(
            w, str(root), str(ram), tracks, _detected("English"), "0 0",
            discard_existing=lambda prefix, movie: (asked.append(prefix),
                                                     False)[1])
        assert len(asked) == 1
        assert asked[0].endswith("movie 2 ")
        assert not stale.exists()
        renumbered = root / "movie 2 Commentary.en.srt"
        assert renumbered.read_bytes() == b"y" * 100000
        assert records == []

    def test_an_opus_numbered_for_a_stale_track_is_not_renumbered(self, w):
        """This phase writes the .srt, and the .opus an older run may have
        left is judged by the pass that owns it: a stale-numbered one counts
        for nothing here, and is left exactly where it stands."""
        root, ram, tracks = self._fixture(w)
        stale = root / "movie 1 Commentary.opus"
        stale.write_bytes(b"\x01opus\x80")

        logs, records = _run_export(
            w, str(root), str(ram), tracks, _detected("English"), "0 0",
            ffmpeg_rc="0 0")

        assert stale.exists()
        assert stale.read_bytes() == b"\x01opus\x80"
        assert not (root / "movie 2 Commentary.opus").exists()
        assert len(records) == 1
        assert not any("Renumbered" in line for line in logs)

    def test_two_stale_copies_of_one_transcript_keep_the_one_written(self, w):
        """Two copies under one name are one transcript twice over: the number
        goes to one of them, and the other is dropped rather than left to
        disagree about which copy is the transcript."""
        root, ram, tracks = self._fixture(w)
        (root / "movie 1 Commentary.en.srt").write_bytes(b"first\n")
        (root / "movie 0 Commentary.en.srt").write_bytes(b"second\n")

        logs, records = _run_export(
            w, str(root), str(ram), tracks, _detected("English"), "0 0")

        assert records == []
        assert sorted(p.name for p in root.iterdir()
                      if p.name.endswith(".srt")) == ["movie 2 Commentary.en.srt"]
        assert sum("Renumbered" in line for line in logs) == 1
        assert sum("dropping a second transcript" in line for line in logs) \
            == 1

    def test_a_stale_number_of_another_film_in_the_same_folder(self, w):
        """A film whose name is the front of this one's name sits in the same
        folder, and its transcript must not read as this film's: the number
        has to be a number, and the film is the stem no cut may reach."""
        tracks = {"movie": self.TRACKS["movie"],
                  "movie extended": self.TRACKS["movie"]}
        root, ram, _ = self._fixture(w, tracks)
        (root / "movie extended.mkv").touch()
        # the other film's transcript, under its own current number
        other = root / "movie extended 2 Commentary.en.srt"
        other.write_bytes(b"theirs\n")
        # and this film's, under a stale one
        stale = root / "movie 1 Commentary.en.srt"
        stale.write_bytes(b"ours\n")

        logs, records = _run_export(
            w, str(root), str(ram), tracks, _detected("English"), "0 0")

        assert records == []
        assert other.read_bytes() == b"theirs\n"
        assert not stale.exists()
        assert (root / "movie 2 Commentary.en.srt").read_bytes() == b"ours\n"


class TestStem:
    """The name every output of one commentary track hangs off."""

    def test_the_cut_takes_the_file_name_and_not_the_path(self):
        """The limit is one file name's, so a film in a deeply nested folder
        keeps as much of its track's name as one in a shallow folder."""
        name = "Commentary by " + "Someone " * 40
        shallow = ct.commentary_stem("./Film", 3, name)
        deep = ct.commentary_stem("./a/rather/deeper/place/Film", 3, name)
        assert os.path.basename(shallow) == os.path.basename(deep)
        assert len(os.path.basename(deep)) == ct.COMMENTARY_STEM_MAX_BYTES
        assert os.path.dirname(deep) == "./a/rather/deeper/place"

    def test_the_cut_counts_bytes(self):
        """An accented name spends two of them on a letter that a count of
        characters spends one on."""
        stem = ct.commentary_stem("Film", 3, "Kommentar von " + "é" * 300)
        assert len(stem.encode("utf-8")) <= ct.COMMENTARY_STEM_MAX_BYTES
        assert len(stem) < ct.COMMENTARY_STEM_MAX_BYTES

    def test_a_name_that_fits_is_left_whole(self):
        assert ct.commentary_stem("./Film (2001)", 2, "Director") == \
            "./Film (2001) 2 Director"

    def test_the_prefix_is_what_no_cut_may_reach(self):
        """The film and the track number say which track a file is of, whatever
        was left of the name after the cut."""
        name = "Commentary by " + "Someone " * 40
        prefix = ct.commentary_prefix("./Film", 3)
        assert prefix == "./Film 3 "
        assert ct.commentary_stem("./Film", 3, name).startswith(prefix)


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