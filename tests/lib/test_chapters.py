"""The white box for medialib/lib/chapters.py.

The concat list turned into OGM chapter rows (each chapter's title decided by
looking at the OTHER chapters, the offsets a running sum of per-file probe
durations), the rows written into the finished file, and the transcode side
reading a source's chapters back out and re-attaching them. What is pinned
here: the exact argv each tool call hands its tool, the branches the values
fall into, the probe-duration arithmetic the shell did with printf and 10#, and
the exit status each function leaves behind. """

import os
import pathlib
import subprocess

import pytest

from medialib.lib import chapters

pytestmark = pytest.mark.stubbed


class _Proc:
    def __init__(self, returncode=0):
        self.returncode = returncode


class _Tags:
    """The tag writer stand-in: every call recorded, and the chapter file read at
    the moment it is handed over, because the caller removes it right after.

    A recorder rather than an argv assertion, because embedding is a function
    call now and not a subprocess - the same three things are still checked (the
    file, the force flag, the title), one layer in.
    """

    def __init__(self, status=0):
        self.status = status
        self.calls = []
        self.chapter_contents = []

    def __call__(self, audio, chapter_file, title="", force=False, error=None):
        self.calls.append((audio, chapter_file, title, force))
        if chapter_file != "/dev/null" and os.path.isfile(chapter_file):
            with open(chapter_file, encoding="utf-8") as handle:
                self.chapter_contents.append(handle.read())
        return self.status


@pytest.fixture
def tags(monkeypatch):
    recorder = _Tags()
    monkeypatch.setattr(chapters.mutagentags, "embed_chapters", recorder)
    return recorder


class _Run:
    """The command runner stand-in: per-tool canned statuses, the argv of
    every call recorded, and the sidecar a call is handed - a mkvmerge
    --chapters file or an ffmpeg ffmetadata one - read at the moment it is
    handed it (the file is removed right after the call).

    An ffmpeg call creates its output, the way the real one does: a caller that
    checks whether the file it asked for is there would otherwise read every
    stubbed run as a failure.
    """

    def __init__(self, results=None):
        self.results = results or {}
        self.calls = []
        self.chapter_contents = []
        self.metadata_contents = []
        self._rc_index = {}

    def __call__(self, argv, may_fail=False):
        name = os.path.basename(str(argv[0]))
        self.calls.append(list(argv))
        for i, a in enumerate(argv):
            if str(a) == "--chapters":
                path = str(argv[i + 1])
                if path != "/dev/null" and os.path.isfile(path):
                    with open(path, encoding="utf-8") as handle:
                        self.chapter_contents.append(handle.read())
                break
        if name == "ffmpeg":
            inputs = [str(argv[i + 1]) for i, a in enumerate(argv)
                      if str(a) == "-i"]
            if "-map_metadata" in [str(a) for a in argv] and len(inputs) > 1 \
                    and os.path.isfile(inputs[1]):
                with open(inputs[1], encoding="utf-8") as handle:
                    self.metadata_contents.append(handle.read())
            open(str(argv[-1]), "w").close()
        rcs = self.results.get(name, [0])
        i = self._rc_index.get(name, 0)
        self._rc_index[name] = i + 1
        return _Proc(rcs[min(i, len(rcs) - 1)])

    def calls_to(self, name):
        return [call for call in self.calls
                if os.path.basename(str(call[0])) == name]


def _NO_CHAPTERS(_path):
    return ""


def _rows(names, durations=None):
    """The OGM rows for these file names, in this order, with these durations
    - the unit contract's chapters() helper, with the probe a table lookup."""
    entries = [f"file '/work/{n}'" for n in names]
    table = {f"/work/{n}": d for n, d in zip(
        names, durations or ["2.500"] * len(names), strict=True)}
    return chapters.chapters_from_files(entries, table.get, _NO_CHAPTERS)


def _name_of(lines, n):
    return lines[2 * n - 1].split("=", 1)[1]


def _time_of(lines, n):
    return lines[2 * n - 2].split("=", 1)[1]


# --- the probe duration to milliseconds ------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("", 0),
    ("N/A", 0),
    ("1.5xyz", 1500),
    ("  3.2 ", 3200),
    ("0", 0),
    ("0.000", 0),
    ("2.500", 2500),
    ("3600.000", 3600000),
    ("61.5", 61500),
    ("1234.567", 1234567),
    ("0.0004", 0),
    # 0.0005 sits on the half-millisecond boundary: the shell rounds the
    # 80-bit float it parses, which lands down here, not up at a millisecond
    ("0.0005", 0),
    ("0.0015", 1),
    ("0.0025", 2),
    ("4923.264500", 4923264),
    ("2.675", 2675),
    ("8.125", 8125),
    ("1e2", 100000),
    ("+7.25", 7250),
    (".5", 500),
    ("5.", 5000),
    ("99999999.999", 99999999999),
])
def test_duration_ms(text, expected):
    assert chapters._duration_ms(text) == expected


@pytest.mark.parametrize("text", ["inf", "nan", "-inf"])
def test_duration_ms_words_die(text):
    # A word is not a duration: it is an error that kills the run.
    with pytest.raises(ValueError):
        chapters._duration_ms(text)


@pytest.mark.parametrize("text, expected", [
    ("N/A", "0.000"),
    ("1.5xyz", "1.500"),
    ("  3.2 ", "3.200"),
    ("2.675", "2.675"),
    ("inf", "inf"),
    ("-inf", "-inf"),
    ("nan", "nan"),
    ("", "0.000"),
    # Hex floats are not parsed at all - ffprobe never prints one.
    ("0x1p3", "0.000"),
])
def test_printf_f3(text, expected):
    assert chapters._printf_f3(text) == expected


@pytest.mark.parametrize("text", ["N/A", "1.5xyz", "3.2 ", "-"])
def test_printf_f3_errors_the_way_the_shell_does(text, capsys):
    # The line names the file the value was refused in, then the argument
    # verbatim - the spaces in it included.
    chapters._printf_f3(text)
    assert capsys.readouterr().err == (
        f"{chapters._SOURCE}: printf: {text}: invalid number\n")


def test_the_error_names_this_module():
    assert chapters._SOURCE.endswith("medialib/lib/chapters.py")


@pytest.mark.parametrize("text", ["1.5", "  3.2", "0", "1e2", "inf", "2.675"])
def test_printf_f3_says_nothing_the_shell_would_not(text, capsys):
    chapters._printf_f3(text)
    assert capsys.readouterr().err == ""


# --- flat milliseconds to the OGM stamp -------------------------------------

@pytest.mark.parametrize("ms, expected", [
    (0, "00:00:00.000"),
    (2500, "00:00:02.500"),
    (59999, "00:00:59.999"),
    (60000, "00:01:00.000"),
    (3600000, "01:00:00.000"),
    (3661000, "01:01:01.000"),
    (35999999, "09:59:59.999"),
    (99999999999, "27777:46:39.999"),
    (-500, "00:00:00.-500"),
])
def test_time_stamp(ms, expected):
    assert chapters._time_stamp(ms) == expected


@pytest.mark.parametrize("a, b, expected", [
    (-500, 1000, (0, -500)),
    (-1000, 1000, (-1, 0)),
    (500, 1000, (0, 500)),
    (3700, 3600, (1, 100)),
])
def test_c_division_and_remainder(a, b, expected):
    assert (chapters._c_div(a, b), chapters._c_rem(a, b)) == expected


# --- the unit contract: chapter names and offsets ----------------------------

def test_plain_numbered_run():
    lines = _rows(["01 Intro.flac", "02 Main.flac", "03 Outro.flac"])
    assert len(lines) == 6
    assert lines[0] == "CHAPTER01=00:00:00.000"
    assert [_time_of(lines, n) for n in (1, 2, 3)] \
        == ["00:00:00.000", "00:00:02.500", "00:00:05.000"]
    assert [_name_of(lines, n) for n in (1, 2, 3)] == ["Intro", "Main", "Outro"]


def test_order_follows_the_list_not_the_disk():
    lines = _rows(["03 Outro.flac", "01 Intro.flac"])
    assert [_name_of(lines, n) for n in (1, 2)] == ["Outro", "Intro"]


def test_common_leading_title_is_dropped():
    lines = _rows(["Show - Intro.flac", "Show - Main.flac", "Show - Outro.flac"])
    assert [_name_of(lines, n) for n in (1, 2, 3)] == ["Intro", "Main", "Outro"]


def test_date_prefix_is_kept_and_shared_word_dropped():
    lines = _rows(["20260101 Episode One.opus", "20260102 Episode Two.opus"])
    assert [_name_of(lines, n) for n in (1, 2)] == ["20260101 One", "20260102 Two"]


def test_numbers_only_run_titles_by_number():
    lines = _rows(["01.flac", "02.flac"])
    assert [_name_of(lines, n) for n in (1, 2)] == ["01", "02"]


def test_hour_rollover():
    lines = _rows(["01 LONG one.flac", "02 LONG two.flac"],
                  durations=["3600.000", "3600.000"])
    assert _time_of(lines, 2) == "01:00:00.000"


def test_single_file_keeps_its_name():
    lines = _rows(["Just One.flac"])
    assert len(lines) == 2
    assert _time_of(lines, 1) == "00:00:00.000"
    assert _name_of(lines, 1) == "Just One"


def test_single_numbered_file_drops_its_number():
    assert _name_of(_rows(["01 Intro.flac"]), 1) == "Intro"


def test_single_dated_file_keeps_its_date():
    assert _name_of(_rows(["20260101 Episode.opus"]), 1) == "20260101 Episode"


def test_single_numbers_only_file_titles_by_number():
    assert _name_of(_rows(["01.flac"]), 1) == "01"


# --- the entry the concat list carries ---------------------------------------

def test_every_file_word_is_stripped():
    # The shell strips "file " everywhere, not just in front: a name that
    # holds the word still lands where the shell lands.
    lines = chapters.chapters_from_files(
        ["file '/work/my file one.flac'"],
        {"/work/my one.flac": "2.500"}.get, _NO_CHAPTERS)
    assert _name_of(lines, 1) == "my one"


def test_an_apostrophe_in_a_name_reaches_its_probe():
    """The path keeps what lies between the surrounding quotes. Stripping the
    apostrophe was what made the probe miss the file and answer nothing: the
    chapter after it then started where this one did, and the merge dropped
    the zero-length one - a book missing a chapter for a name's sake."""
    lines = chapters.chapters_from_files(
        ["file '/work/one.mp3'", "file '/work/it's.mp3'",
         "file '/work/two.mp3'"],
        {"/work/one.mp3": "2.500", "/work/it's.mp3": "4.000",
         "/work/two.mp3": "1.000"}.get, _NO_CHAPTERS)
    assert _time_of(lines, 1) == "00:00:00.000"
    assert _time_of(lines, 2) == "00:00:02.500"
    assert _time_of(lines, 3) == "00:00:06.500"


def test_base_name_is_taken_after_the_last_slash():
    lines = chapters.chapters_from_files(
        ["file '/a/b/c.flac'"], {"/a/b/c.flac": "2.500"}.get, _NO_CHAPTERS)
    assert _name_of(lines, 1) == "c"


def test_last_extension_only_is_stripped():
    lines = chapters.chapters_from_files(
        ["file '/x/a.b.c.mp3'"], {"/x/a.b.c.mp3": "2.500"}.get, _NO_CHAPTERS)
    # the individual pass settles the dotted stem to its space form
    assert _name_of(lines, 1) == "a b c"


def test_a_blank_duration_is_zero():
    # a probe that answers nothing is zero, the way the shell's :-0 does
    lines = chapters.chapters_from_files(
        ["file '/a/one.flac'", "file '/a/two.flac'"],
        {"/a/one.flac": "2.500", "/a/two.flac": ""}.get, _NO_CHAPTERS)
    assert _time_of(lines, 1) == "00:00:00.000"
    assert _time_of(lines, 2) == "00:00:02.500"


# --- a file's own chapters ----------------------------------------------------

def _flat(*marks):
    """A flat -show_chapters stream for these (start seconds, title) marks; a
    title of None leaves the chapter untitled."""
    rows = []
    for index, (start, title) in enumerate(marks):
        rows.append(f'chapters.chapter.{index}.start_time="{start:.6f}"')
        if title is not None:
            rows.append(f'chapters.chapter.{index}.tags.title="{title}"')
    return "\n".join(rows) + "\n"


def _join(durations, flats):
    """chapters_from_files over these {path: seconds} files, in order, with
    {path: flat stream} as the chapter probe."""
    return chapters.chapters_from_files(
        [f"file '{path}'" for path in durations], durations.get,
        lambda path: flats.get(path, ""))


def test_inner_chapters_are_kept_and_shifted_by_the_files_before():
    lines = _join(
        {"/a/01 Part.m4a": "100.000", "/a/02 Part.m4a": "120.000"},
        {"/a/01 Part.m4a": _flat((0, "Prologue"), (60.5, "Opening")),
         "/a/02 Part.m4a": _flat((0, "Storm"), (30, "Harbour"),
                                 (90, "Home"))})
    assert lines == [
        "CHAPTER01=00:00:00.000", "CHAPTER01NAME=Prologue",
        "CHAPTER02=00:01:00.500", "CHAPTER02NAME=Opening",
        "CHAPTER03=00:01:40.000", "CHAPTER03NAME=Storm",
        "CHAPTER04=00:02:10.000", "CHAPTER04NAME=Harbour",
        "CHAPTER05=00:03:10.000", "CHAPTER05NAME=Home",
    ]


def test_inner_titles_are_cleaned_among_their_own_file_only():
    # The text common to one file's titles goes, but not text common across
    # files: each file is its own set.
    lines = _join(
        {"/a/one.opus": "100.000", "/a/two.opus": "100.000"},
        {"/a/one.opus": _flat((0, "01 - Book One - Arrival"),
                              (50, "02 - Book One - Departure")),
         "/a/two.opus": _flat((0, "01 - Book Two - Arrival"),
                              (50, "02 - Book Two - Return"))})
    names = [lines[i].split("=", 1)[1] for i in range(1, len(lines), 2)]
    assert names == ["Arrival", "Departure", "Arrival", "Return"]


def test_file_names_are_cleaned_among_the_unchaptered_as_before():
    # No file carries chapters: the rows are exactly what the names alone give.
    durations = {"/w/Book - 01.mp3": "2.500", "/w/Book - 02.mp3": "2.500"}
    assert _join(durations, {}) == chapters.chapters_from_files(
        [f"file '{p}'" for p in durations], durations.get, _NO_CHAPTERS)


def test_a_late_first_chapter_gets_a_mark_named_after_the_file():
    lines = _join(
        {"/a/02 Before.flac": "5.000", "/a/03 Third Leg.flac": "100.000"},
        {"/a/03 Third Leg.flac": _flat((10, "Dawn"), (60, "Dusk"))})
    assert lines[2:] == [
        "CHAPTER02=00:00:05.000", "CHAPTER02NAME=Third Leg",
        "CHAPTER03=00:00:15.000", "CHAPTER03NAME=Dawn",
        "CHAPTER04=00:01:05.000", "CHAPTER04NAME=Dusk"]


def test_a_lone_file_gets_no_mark_named_after_it():
    # The whole book is that one file: its chapters are all there is, the
    # first where it says it starts - and still cleaned among themselves.
    lines = _join(
        {"/a/03 Third Leg.flac": "100.000"},
        {"/a/03 Third Leg.flac": _flat((10, "01 - Dawn"),
                                          (60, "02 - Dusk"))})
    assert lines == ["CHAPTER01=00:00:10.000", "CHAPTER01NAME=Dawn",
                     "CHAPTER02=00:01:00.000", "CHAPTER02NAME=Dusk"]


def test_a_first_chapter_within_a_second_marks_the_start():
    lines = _join({"/a/x.flac": "100.000"},
                  {"/a/x.flac": _flat((0.4, "Dawn"), (60, "Dusk"))})
    assert lines == ["CHAPTER01=00:00:00.000", "CHAPTER01NAME=Dawn",
                     "CHAPTER02=00:01:00.000", "CHAPTER02NAME=Dusk"]


def test_a_single_chapter_counts_as_none():
    # One chapter is the file itself: the file's name titles it, as for a
    # file with no chapters at all.
    lines = _join({"/a/one.opus": "10.000", "/a/two.opus": "10.000"},
                  {"/a/one.opus": _flat((0, "Whole thing")),
                   "/a/two.opus": _flat((0, "Alpha"), (5, "Beta"))})
    assert lines == [
        "CHAPTER01=00:00:00.000", "CHAPTER01NAME=one",
        "CHAPTER02=00:00:10.000", "CHAPTER02NAME=Alpha",
        "CHAPTER03=00:00:15.000", "CHAPTER03NAME=Beta",
    ]


def test_a_lone_file_keeps_its_own_chapters():
    lines = _join({"/a/book.m4b": "600.000"},
                  {"/a/book.m4b": _flat((0, "Start"), (300, "Middle"))})
    assert lines == ["CHAPTER01=00:00:00.000", "CHAPTER01NAME=Start",
                     "CHAPTER02=00:05:00.000", "CHAPTER02NAME=Middle"]


def test_inner_marks_outside_the_file_or_out_of_order_are_dropped():
    # One that does not move forward, or lies at or past the file's end, is
    # dropped; an untitled one is "Chapter nn" before the cleaning.
    lines = _join({"/a/x.flac": "50.000"},
                  {"/a/x.flac": _flat((0, "Opening"), (20, None),
                                      (20, "Same"), (10, "Back"),
                                      (50, "End"), (70, "Past"))})
    assert [lines[0], lines[2]] == ["CHAPTER01=00:00:00.000",
                                    "CHAPTER02=00:00:20.000"]
    assert len(lines) == 4


# --- the flat chapter stream --------------------------------------------------

FLAT = (
    "chapters.chapter.0.id=8475894591796254271\n"
    'chapters.chapter.0.time_base="1/1000000000"\n'
    "chapters.chapter.0.start=2000000000\n"
    'chapters.chapter.0.start_time="2.000000"\n'
    "chapters.chapter.0.end=5500000000\n"
    'chapters.chapter.0.end_time="5.500000"\n'
    'chapters.chapter.0.tags.title="Two"\n'
    "chapters.chapter.1.id=-7099688264198272841\n"
    'chapters.chapter.1.time_base="1/1000000000"\n'
    "chapters.chapter.1.start=5500000000\n"
    'chapters.chapter.1.start_time="5.500000"\n'
    "chapters.chapter.1.end=8006508000\n"
    'chapters.chapter.1.end_time="8.006508"\n'
    'chapters.chapter.1.tags.title="Five Five"\n'
)


def test_flat_stream_to_ogm():
    assert chapters._chapters_from_flat(FLAT) == [
        "CHAPTER01=00:00:02.000",
        "CHAPTER01NAME=Two",
        "CHAPTER02=00:00:05.500",
        "CHAPTER02NAME=Five Five",
    ]


def test_flat_stream_without_a_title():
    lines = chapters._chapters_from_flat(
        'chapters.chapter.0.start_time="12.500000"\n')
    assert lines == ["CHAPTER01=00:00:12.500", "CHAPTER01NAME=Chapter 01"]


def test_flat_stream_ignores_the_distractors():
    text = (
        "chapters=2\n"
        "format_name=Matroska\n"
        'chapters.chapter.0.tags.language="en"\n'
        'chapters.other.0.start_time="9.0"\n'
        "no-equals-here\n"
        'chapters.chapter.0.start_time="3.000000"\n'
    )
    assert chapters._chapters_from_flat(text) == [
        "CHAPTER01=00:00:03.000",
        "CHAPTER01NAME=Chapter 01",
    ]


def test_flat_stream_gaps_and_order():
    # A chapter that has a title but no start time is skipped, and the title
    # may arrive before its start time.
    text = (
        'chapters.chapter.2.tags.title="Third"\n'
        'chapters.chapter.1.tags.title="Second"\n'
        'chapters.chapter.0.start_time="1.000000"\n'
        'chapters.chapter.2.start_time="7261.500000"\n'
    )
    assert chapters._chapters_from_flat(text) == [
        "CHAPTER01=00:00:01.000",
        "CHAPTER01NAME=Chapter 01",
        "CHAPTER03=02:01:01.500",
        "CHAPTER03NAME=Third",
    ]


def test_flat_stream_unquoted_and_unparseable_values():
    lines = chapters._chapters_from_flat(
        'chapters.chapter.0.start_time=2.0\n'
        'chapters.chapter.1.start_time=N/A\n')
    assert lines == [
        "CHAPTER01=00:00:02.000",
        "CHAPTER01NAME=Chapter 01",
        "CHAPTER02=00:00:00.000",
        "CHAPTER02NAME=Chapter 02",
    ]


def test_flat_stream_no_chapters():
    assert chapters._chapters_from_flat("chapters=0\n") == []


# --- extractChapters: the file and the status ---------------------------------

def test_extract_writes_the_rows_and_succeeds(tmp_path):
    out = str(tmp_path / "chapters.ogm")
    assert chapters.extract_chapters("src.opus", out, probe=lambda s: FLAT) == 0
    with open(out, encoding="utf-8") as handle:
        assert handle.read() == (
            "CHAPTER01=00:00:02.000\nCHAPTER01NAME=Two\n"
            "CHAPTER02=00:00:05.500\nCHAPTER02NAME=Five Five\n")


def test_extract_without_chapters_leaves_an_empty_file(tmp_path):
    out = str(tmp_path / "chapters.ogm")
    assert chapters.extract_chapters("src.opus", out, probe=lambda s: "") == 1
    with open(out, encoding="utf-8") as handle:
        assert handle.read() == ""


# --- embedChapters: the book's file found, and write_chapters with its title ---

_LINES = ["CHAPTER01=00:00:00.000", "CHAPTER01NAME=One",
          "CHAPTER02=00:00:01.000", "CHAPTER02NAME=Two"]


def _flat_of(lines):
    """What ffprobe's flat -show_chapters would say of a file holding these
    rows."""
    pairs = chapters._row_chapters(lines)
    return "".join('chapters.chapter.%d.start_time="%f"\n'
                   'chapters.chapter.%d.tags.title="%s"\n'
                   % (n, start / 1000, n, name)
                   for n, (start, name) in enumerate(pairs))


def _book(tmp_path, monkeypatch, name, duration="61.0"):
    (tmp_path / name).write_bytes(b"x")
    (tmp_path / "ram").mkdir(exist_ok=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(chapters, "_probe_duration", lambda path: duration)


class _CopyRun(_Run):
    """The runner whose ffmpeg writes a copy with something in it, as a real
    stream copy does."""

    def __call__(self, argv, may_fail=False):
        result = super().__call__(argv, may_fail)
        if os.path.basename(str(argv[0])) == "ffmpeg":
            with open(str(argv[-1]), "w") as handle:
                handle.write("copy")
        return result


def _reads_back(lines):
    """A probe that finds these rows in the staged copy, and none in the book."""
    return lambda path: _flat_of(lines) if "generated." in path else ""


def test_embed_opus_writes_the_rows_with_mutagen(tmp_path, capsys, monkeypatch,
                                                 tags):
    _book(tmp_path, monkeypatch, "song.opus")
    run = _Run()
    assert chapters.embed_chapters("song.opus", _LINES, "ram", run) == 0
    assert len(tags.calls) == 1
    audio, _chapter_file, title, force = tags.calls[0]
    assert (audio, title, force) == ("song.opus", "song", True)
    assert tags.chapter_contents == ["\n".join(_LINES) + "\n"]
    assert run.calls == []
    assert capsys.readouterr().err == ""


def test_embed_a_single_chapter_writes_the_title_alone(tmp_path, monkeypatch,
                                                      tags):
    _book(tmp_path, monkeypatch, "song.flac")
    assert chapters.embed_chapters("song.flac", ["CHAPTER01=00:00:00.000"],
                                   "ram", _Run()) == 0
    assert tags.calls[0][2] == "song"
    assert "CHAPTER" not in tags.chapter_contents[0]


def test_embed_chooses_the_file_in_its_own_order(tmp_path, monkeypatch, tags):
    for name in ("song.mp3", "song.flac"):
        _book(tmp_path, monkeypatch, name)
    _book(tmp_path, monkeypatch, "song.opus")
    chapters.embed_chapters("song.opus", [], "ram", _Run())
    assert tags.calls[0][0] == "song.opus"


@pytest.mark.parametrize("name", ["song.mp3", "song.m4b"])
def test_embed_a_stream_copy_keeps_the_audio_and_the_title(tmp_path,
                                                          monkeypatch, name):
    """One ffmpeg call, no Matroska: the chapters and the title come from the
    ffmetadata input, and only the audio from the book, so a cover stream the
    join carried cannot displace the chapter track."""
    _book(tmp_path, monkeypatch, name)
    run = _CopyRun()
    assert chapters.embed_chapters(name, _LINES, "ram", run,
                                   _reads_back(_LINES)) == 0
    assert [os.path.basename(str(c[0])) for c in run.calls] == ["ffmpeg"]
    argv = run.calls[0]
    assert argv[-1] == "ram/generated" + os.path.splitext(name)[1]
    assert argv[argv.index("-map") + 1] == "0:a"
    assert argv[argv.index("-map_metadata") + 1] == "1"
    assert argv[argv.index("-map_chapters") + 1] == "1"
    assert run.metadata_contents == [
        ";FFMETADATA1\n"
        "title=song\n"
        "[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=1000\ntitle=One\n"
        "[CHAPTER]\nTIMEBASE=1/1000\nSTART=1000\nEND=61000\ntitle=Two\n"]
    # Moved over the book; nothing left beside it or in the scratch.
    assert (tmp_path / name).read_bytes() == b"copy"
    assert list((tmp_path / "ram").iterdir()) == []


def test_embed_a_failed_copy_leaves_the_book_as_it_was(tmp_path, monkeypatch):
    _book(tmp_path, monkeypatch, "song.m4b")
    assert chapters.embed_chapters("song.m4b", _LINES, "ram",
                                   _CopyRun({"ffmpeg": [1]}),
                                   _reads_back(_LINES)) == 1
    assert (tmp_path / "song.m4b").read_bytes() == b"x"
    assert list((tmp_path / "ram").iterdir()) == []


def test_a_copy_holding_no_chapters_is_a_container_without_a_place_for_them(
        tmp_path, monkeypatch):
    _book(tmp_path, monkeypatch, "talk.aac")
    assert chapters.write_chapters("talk.aac", _LINES, "ram", _CopyRun(),
                                   lambda path: "") == chapters.CANNOT_HOLD
    assert (tmp_path / "talk.aac").read_bytes() == b"x"
    assert list((tmp_path / "ram").iterdir()) == []


def test_a_copy_holding_the_wrong_chapters_is_a_failure(tmp_path, monkeypatch):
    _book(tmp_path, monkeypatch, "song.mp3")
    assert chapters.write_chapters("song.mp3", _LINES, "ram", _CopyRun(),
                                   _reads_back(_LINES[:2])) == 1
    assert (tmp_path / "song.mp3").read_bytes() == b"x"


def test_embed_a_copy_that_came_out_short_is_not_put_over_the_book(
        tmp_path, monkeypatch):
    _book(tmp_path, monkeypatch, "song.mp3")
    monkeypatch.setattr(chapters, "_probe_duration",
                        lambda path: "30.0" if "generated." in path else "61.0")
    assert chapters.embed_chapters("song.mp3", _LINES, "ram", _CopyRun(),
                                   _reads_back(_LINES)) == 1
    assert (tmp_path / "song.mp3").read_bytes() == b"x"


def test_embed_with_no_book_there_does_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    run = _Run()
    assert chapters.embed_chapters("song.opus", _LINES, "ram", run) == 0
    assert run.calls == []


# --- writeChapters: in place, keeping the file's own tags -----------------------

def test_write_keeps_every_stream_and_tag_of_the_file(tmp_path, monkeypatch):
    _book(tmp_path, monkeypatch, "talk.mp3")
    run = _CopyRun()
    assert chapters.write_chapters("talk.mp3", _LINES, "ram", run,
                                   _reads_back(_LINES)) == 0
    argv = run.calls[0]
    assert argv[argv.index("-map") + 1] == "0"
    assert argv[argv.index("-map_metadata") + 1] == "0"


def test_write_matroska_in_place_with_mkvpropedit(tmp_path, monkeypatch):
    _book(tmp_path, monkeypatch, "talk.mka")
    run = _Run()
    assert chapters.write_chapters("talk.mka", _LINES, "ram", run,
                                   lambda path: _flat_of(_LINES)) == 0
    assert run.calls == [["mkvpropedit", "--quiet", "talk.mka", "--chapters",
                          "ram/generated.chapters.txt"]]
    assert run.chapter_contents == ["\n".join(_LINES) + "\n"]


@pytest.mark.parametrize("held, carries", [
    (_LINES, True),
    (_LINES[:2], False),
    (["CHAPTER01=00:00:00.000", "CHAPTER01NAME=One",
      "CHAPTER02=00:00:01.040", "CHAPTER02NAME=Two"], True),
    (["CHAPTER01=00:00:00.000", "CHAPTER01NAME=One",
      "CHAPTER02=00:00:01.100", "CHAPTER02NAME=Two"], False),
    (["CHAPTER01=00:00:00.000", "CHAPTER01NAME=One",
      "CHAPTER02=00:00:01.000", "CHAPTER02NAME=Other"], False),
])
def test_carries_chapters(held, carries):
    assert chapters.carries_chapters("x", _LINES,
                                     lambda path: _flat_of(held)) is carries


# --- attachChapters: the re-attach after an encode ------------------------------

def test_attach_reattaches_when_the_source_has_chapters(tmp_path, tags):
    status = chapters.attach_chapters("src.opus", "out.opus", str(tmp_path),
                                      "script", run=_Run(),
                                      probe=lambda s: FLAT)
    assert status == 0
    assert len(tags.calls) == 1
    audio, chapter_file, _title, force = tags.calls[0]
    assert audio == "out.opus"
    assert chapter_file == str(tmp_path / "chapters.ogm")
    assert force
    with open(tmp_path / "chapters.ogm", encoding="utf-8") as handle:
        assert handle.read().startswith("CHAPTER01=00:00:02.000\n")


def test_attach_without_chapters_embeds_nothing(tmp_path, tags):
    run = _Run()
    assert chapters.attach_chapters("src.opus", "out.opus", str(tmp_path),
                                    "script", run=run, probe=lambda s: "") == 0
    # Both, and not just the runner: embedding is not a subprocess, so "no
    # command ran" says nothing about whether a tag was written.
    assert run.calls == []
    assert tags.calls == []
    with open(tmp_path / "chapters.ogm", encoding="utf-8") as handle:
        assert handle.read() == ""


# --- attachChapters into an MP4: a chapter TRACK, not a tag ---------------------

def _staging_run(tmp_path, write=b"muxed", rc=0):
    """A runner that behaves the way the real ffmpeg does here: it writes the
    staging file the call names.

    Without that the move afterwards has nothing to move, and a case would pass
    for the wrong reason.
    """
    class _Muxer(_Run):
        def __call__(self, argv, may_fail=False):
            super().__call__(argv, may_fail)
            if write is not None:
                pathlib.Path(argv[-1]).write_bytes(write)
            return subprocess.CompletedProcess(argv, rc)

    return _Muxer()


def test_an_mp4_target_gets_a_chapter_track_and_no_tag(tmp_path, tags):
    """MP4 has nowhere to keep a CHAPTERnn row, so mutagen must not be reached
    for this half at all - it would write a tag no player reads and report
    success."""
    target = tmp_path / "out.m4a"
    target.write_bytes(b"original")
    run = _staging_run(tmp_path)
    assert chapters.attach_chapters("src.flac", str(target), str(tmp_path),
                                    "script", run=run,
                                    probe=lambda s: FLAT) == 0
    assert tags.calls == []
    assert [os.path.basename(call[0]) for call in run.calls] == ["ffmpeg"]


def test_the_source_goes_in_as_a_second_input_only_to_be_read(tmp_path, tags):
    target = tmp_path / "out.m4a"
    target.write_bytes(b"original")
    run = _staging_run(tmp_path)
    chapters.attach_chapters("src.flac", str(target), str(tmp_path), "script",
                             run=run, probe=lambda s: FLAT)
    argv = run.calls[0]
    # The target first, the source second, and the chapters taken from input 1.
    assert argv[argv.index("-i") + 1] == str(target)
    assert argv[argv.index("-map_chapters") + 1] == "1"
    # The audio is COPIED: the encoder that just made it must not run again.
    assert argv[argv.index("-c") + 1] == "copy"
    assert argv[argv.index("-map") + 1] == "0:a:0"


def test_the_result_replaces_the_target(tmp_path, tags):
    """ffmpeg cannot read and write one file at once, so the pass stages and the
    staging file is then moved over the target."""
    target = tmp_path / "out.m4a"
    target.write_bytes(b"original")
    chapters.attach_chapters("src.flac", str(target), str(tmp_path), "script",
                             run=_staging_run(tmp_path), probe=lambda s: FLAT)
    assert target.read_bytes() == b"muxed"


def test_a_pass_that_produced_nothing_leaves_the_audio_alone(tmp_path, tags):
    """Chapters missing is a metadata loss; a truncated output is the recording.
    A failed mux has to fail the way round that keeps the audio."""
    target = tmp_path / "out.m4a"
    target.write_bytes(b"original")
    run = _staging_run(tmp_path, write=None)
    assert chapters.attach_mp4_chapters("src.flac", str(target),
                                        str(tmp_path), run=run) == 1
    assert target.read_bytes() == b"original"


def test_an_empty_staging_file_counts_as_nothing_produced(tmp_path, tags):
    target = tmp_path / "out.m4a"
    target.write_bytes(b"original")
    run = _staging_run(tmp_path, write=b"")
    assert chapters.attach_mp4_chapters("src.flac", str(target),
                                        str(tmp_path), run=run) == 1
    assert target.read_bytes() == b"original"


def test_the_staging_file_keeps_the_targets_suffix(tmp_path, tags):
    """ffmpeg picks its muxer from the output name, so a staging file called
    something else would be written in the wrong container."""
    target = tmp_path / "out.m4b"
    target.write_bytes(b"original")
    run = _staging_run(tmp_path)
    chapters.attach_mp4_chapters("src.flac", str(target), str(tmp_path),
                                 run=run)
    assert run.calls[0][-1].endswith(".m4b")


@pytest.mark.parametrize("name", ["out.m4a", "out.M4A", "out.m4b", "out.mp4"])
def test_every_mp4_spelling_takes_this_route(tmp_path, tags, name):
    target = tmp_path / name
    target.write_bytes(b"original")
    chapters.attach_chapters("src.flac", str(target), str(tmp_path), "script",
                             run=_staging_run(tmp_path), probe=lambda s: FLAT)
    assert tags.calls == []


@pytest.mark.parametrize("name", ["out.opus", "out.flac"])
def test_and_the_vorbis_comment_containers_still_do_not(tmp_path, tags, name):
    target = tmp_path / name
    run = _staging_run(tmp_path)
    chapters.attach_chapters("src.flac", str(target), str(tmp_path), "script",
                             run=run, probe=lambda s: FLAT)
    assert len(tags.calls) == 1
    assert run.calls == []
