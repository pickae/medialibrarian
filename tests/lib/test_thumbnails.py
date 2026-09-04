"""The white box for medialib/lib/thumbnails.py.

Which image next to a release becomes the cover, what the extraction and the
embedding reach for, and what a sidecar does to the cover a transcode already
pulled from the source. What is pinned here: the ladder over the tree, the
extraction and the embedding's dispatch and file dance through the shared tool
stub, the exact argv each tool call hands its tool, the branches the tuning
values fall into, and the exit status the last command of each function leaves
behind. """

import os

import pytest

from medialib.lib import thumbnails

pytestmark = pytest.mark.stubbed


class _Proc:
    def __init__(self, returncode=0):
        self.returncode = returncode


class _Run:
    """The command runner stand-in: per-tool canned results and per-tool write
    lists, consumed in call order, the argv of every call recorded."""

    def __init__(self, results=None, writes=None):
        self.results = results or {}
        self.writes = writes or {}
        self.calls = []
        self._rc_index = {}
        self._write_index = {}

    def __call__(self, argv, quiet=False):
        name = os.path.basename(str(argv[0]))
        self.calls.append(list(argv))
        rcs = self.results.get(name, [0])
        i = self._rc_index.get(name, 0)
        self._rc_index[name] = i + 1
        rc = rcs[min(i, len(rcs) - 1)]
        writes = self.writes.get(name, [])
        j = self._write_index.get(name, 0)
        if j < len(writes):
            with open(writes[j], "wb") as handle:
                handle.write(b"")
            self._write_index[name] = j + 1
        return _Proc(rc)

    def called(self, name):
        return [c for c in self.calls if os.path.basename(str(c[0])) == name]


class _Covers:
    """The cover writer stand-in: every (audio, cover) pair recorded, and a
    canned status per call.

    Embedding is a function call now rather than a subprocess, so what used to be
    an assertion on argv is an assertion on the call - the same two paths, one
    layer in.
    """

    def __init__(self, statuses=None):
        self.statuses = list(statuses or [])
        self.calls = []

    def __call__(self, audio, cover):
        self.calls.append((audio, cover))
        return self.statuses.pop(0) if self.statuses else 0


@pytest.fixture
def covers(monkeypatch):
    recorder = _Covers()
    monkeypatch.setattr(thumbnails.mutagentags, "embed_cover", recorder)
    return recorder


def _folder(base, names):
    """Empty files (a name may hold '/' to nest)."""
    for name in names:
        path = base / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()


def _file_with_size(base, name, size):
    path = base / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(b"\0" * size)


def _stub_bin(base, tools):
    """A PATH holding executable stand-ins for just the named tools."""
    binstub = base / "stubbin"
    binstub.mkdir(exist_ok=True)
    for tool in tools:
        (binstub / tool).write_text("#!/bin/sh\nexit 0\n")
        (binstub / tool).chmod(0o755)
    return str(binstub)


@pytest.fixture
def isolated_env(monkeypatch, tmp_path):
    """A PYTHON_BIN that is never run; the PATH is set per test."""
    monkeypatch.setenv("PYTHON_BIN", str(tmp_path / "python3"))
    monkeypatch.delenv("LOG_TIMESTAMPS", raising=False)
    return tmp_path


################################################################################
# chooseThumbnail: the ladder
################################################################################
def test_the_only_image_is_the_cover(tmp_path, isolated_env):
    _folder(tmp_path, ["artwork.jpg"])
    assert thumbnails.choose_thumbnail(str(tmp_path)) == str(tmp_path / "artwork.jpg")


def test_a_non_image_is_never_the_cover(tmp_path, isolated_env):
    _folder(tmp_path, ["artwork.png", "notes.txt"])
    assert thumbnails.choose_thumbnail(str(tmp_path)) == str(tmp_path / "artwork.png")


def test_a_folder_with_no_image_at_all(tmp_path, isolated_env):
    _folder(tmp_path, ["notes.txt", "booklet.pdf"])
    assert thumbnails.choose_thumbnail(str(tmp_path)) == ""


def test_the_ladder_rung_by_rung(tmp_path, isolated_env):
    cases = [
        (["artwork.jpg", "back.jpg"], "back.jpg"),
        (["back.jpg", "folder.jpg"], "folder.jpg"),
        (["folder.jpg", "inlay.jpg"], "inlay.jpg"),
        (["inlay.jpg", "cover.jpg"], "cover.jpg"),
        (["cover.jpg", "front.jpg"], "front.jpg"),
        (["front.jpg", "cover front.jpg"], "cover front.jpg"),
    ]
    for names, winner in cases:
        sub = tmp_path / "-".join(names)
        _folder(sub, names)
        assert thumbnails.choose_thumbnail(str(sub)) == str(sub / winner), names


def test_a_back_cover_does_not_win_the_cover_rung(tmp_path, isolated_env):
    _folder(tmp_path, ["back cover.jpg", "front.jpg"])
    assert thumbnails.choose_thumbnail(str(tmp_path)) == str(tmp_path / "front.jpg")


def test_every_image_extension_reaches_the_top(tmp_path, isolated_env):
    for ext in ("jpg", "png", "webp", "avif"):
        sub = tmp_path / ext
        _folder(sub, ["artwork.jpg", f"cover.{ext}"])
        assert thumbnails.choose_thumbnail(str(sub)) == str(sub / f"cover.{ext}")


def test_a_directory_named_like_an_image_is_not_a_cover(tmp_path, isolated_env):
    for ext in ("png", "webp", "avif"):
        (tmp_path / f"art.{ext}").mkdir()
    _folder(tmp_path, ["notes.txt"])
    assert thumbnails.choose_thumbnail(str(tmp_path)) == ""
    _folder(tmp_path, ["real.jpg"])
    assert thumbnails.choose_thumbnail(str(tmp_path)) == str(tmp_path / "real.jpg")


def test_a_link_wearing_an_image_name_is_not_a_cover(tmp_path, isolated_env):
    _folder(tmp_path, ["real.jpg"])
    (tmp_path / "link.jpg").symlink_to(tmp_path / "real.jpg")
    assert thumbnails.choose_thumbnail(str(tmp_path)) == str(tmp_path / "real.jpg")


def test_names_match_case_insensitively_the_way_find_does(tmp_path, isolated_env):
    _folder(tmp_path, ["BACK.jpg"])
    assert thumbnails.choose_thumbnail(str(tmp_path)) == str(tmp_path / "BACK.jpg")
    # the fold is the C library's: U+0130 lower-cases to a bare "i", not to
    # "i" plus a combining dot the way str.lower would
    assert thumbnails._iname("İstanbul.jpg", "*istanbul*")
    assert not thumbnails._iname("İstanbul.jpg", "*i̇stanbul*")


################################################################################
# extractThumbnail: which source the cover comes from
################################################################################
def test_a_scan_pdf_beats_a_booklet_pdf(tmp_path, isolated_env, monkeypatch):
    _folder(tmp_path, ["scan.pdf", "booklet.pdf"])
    run = _Run()
    monkeypatch.setenv("PATH", _stub_bin(tmp_path, ["pdftoppm"]))
    assert thumbnails.extract_thumbnail(str(tmp_path), "ram/track01", 100, False, run) == 0
    assert run.called("pdftoppm") == [
        ["pdftoppm", str(tmp_path / "scan.pdf"), "ram/track01", "-jpeg", "-rx", "100",
         "-ry", "100", "-f", "1", "-singlefile"]]


def test_a_booklet_pdf_without_a_scan(tmp_path, isolated_env, monkeypatch):
    _folder(tmp_path, ["booklet.pdf"])
    run = _Run()
    monkeypatch.setenv("PATH", _stub_bin(tmp_path, ["pdftoppm"]))
    thumbnails.extract_thumbnail(str(tmp_path), "ram/track01", 100, False, run)
    assert run.called("pdftoppm")[0][1] == str(tmp_path / "booklet.pdf")


def test_a_host_without_poppler_forgets_the_pdf_and_warns(tmp_path, isolated_env,
                                                          monkeypatch, capsys):
    _folder(tmp_path, ["booklet.pdf", "a.opus"])
    run = _Run()
    monkeypatch.setenv("PATH", _stub_bin(tmp_path, ["ffmpeg"]))
    status = thumbnails.extract_thumbnail(str(tmp_path), "ram/track01", 100, False, run)
    assert status == 0
    assert "pdftoppm not installed" in capsys.readouterr().err
    assert run.called("ffmpeg") == [
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
         "-i", str(tmp_path / "a.opus"), "-an", "-vcodec", "copy",
         "ram/track01.jpg"]]


def test_an_opus_cover_detours_over_mka_when_mkvtoolnix_is_there(tmp_path,
                                                                  isolated_env,
                                                                  monkeypatch):
    monkeypatch.chdir(tmp_path)
    _folder(tmp_path, ["a.opus"])
    (tmp_path / "ram").mkdir()
    run = _Run(writes={"mkvmerge": ["ram/track01.extract.mka"],
                       "mkvextract": ["ram/track01.jpg"]})
    monkeypatch.setenv("PATH", _stub_bin(tmp_path, ["pdftoppm", "mkvmerge", "mkvextract"]))
    assert thumbnails.extract_thumbnail(str(tmp_path), "ram/track01", 100, True, run) == 0
    assert run.called("mkvmerge") == [
        ["mkvmerge", "--quiet", "-o", "ram/track01.extract.mka",
         "--no-chapters", str(tmp_path / "a.opus")]]
    assert run.called("mkvextract") == [
        ["mkvextract", "--quiet", "ram/track01.extract.mka", "attachments",
         "1:ram/track01.jpg"]]
    assert not (tmp_path / "ram" / "track01.extract.mka").exists()
    assert (tmp_path / "ram" / "track01.jpg").exists()


def test_an_opus_cover_without_mkvtoolnix_goes_through_ffmpeg(tmp_path, isolated_env,
                                                              monkeypatch):
    _folder(tmp_path, ["a.opus"])
    run = _Run()
    monkeypatch.setenv("PATH", _stub_bin(tmp_path, ["pdftoppm", "ffmpeg"]))
    thumbnails.extract_thumbnail(str(tmp_path), "ram/track01", 100, False, run)
    assert run.called("ffmpeg")[0][-3:] == ["-vcodec", "copy", "ram/track01.jpg"]


def test_the_audio_sources_are_tried_opus_then_mp3_then_flac(tmp_path, isolated_env,
                                                             monkeypatch):
    for names, tool_input in ((["a.opus", "b.mp3", "c.flac"], "a.opus"),
                              (["b.mp3", "c.flac"], "b.mp3"),
                              (["c.flac"], "c.flac")):
        sub = tmp_path / names[0]
        _folder(sub, names)
        run = _Run()
        monkeypatch.setenv("PATH", _stub_bin(sub, ["pdftoppm", "ffmpeg"]))
        thumbnails.extract_thumbnail(str(sub), "ram/track01", 100, False, run)
        assert run.called("ffmpeg")[0][6] == str(sub / tool_input)


def test_neither_pdf_nor_audio_is_a_status_zero_and_no_call(tmp_path, isolated_env,
                                                            monkeypatch):
    _folder(tmp_path, ["notes.txt"])
    run = _Run()
    monkeypatch.setenv("PATH", _stub_bin(tmp_path, []))
    assert thumbnails.extract_thumbnail(str(tmp_path), "ram/track01", 100, False, run) == 0
    assert run.calls == []


def test_pdftoppm_s_own_status_is_the_functions(tmp_path, isolated_env, monkeypatch):
    _folder(tmp_path, ["booklet.pdf"])
    run = _Run(results={"pdftoppm": [3]})
    monkeypatch.setenv("PATH", _stub_bin(tmp_path, ["pdftoppm"]))
    assert thumbnails.extract_thumbnail(str(tmp_path), "ram/track01", 100, False, run) == 3


################################################################################
# embedThumbnail: get it to a sane size, then write it in
#
# The audio files are looked up next to the CURRENT directory, the way the
# shell version finds them after its caller has cd'd into the subfolder, so
# each case runs from inside its own folder.
################################################################################
def test_no_image_and_no_extraction_is_a_status_zero(tmp_path, isolated_env,
                                                     monkeypatch):
    d = tmp_path / "case"
    _folder(d, ["notes.txt"])
    (d / "ram").mkdir()
    monkeypatch.chdir(d)
    run = _Run()
    monkeypatch.setenv("PATH", _stub_bin(tmp_path, []))
    assert thumbnails.embed_thumbnail(
        str(d), "track01", 100, 90, "1024x1024", 100, False, "ram", "script",
        run) == 0
    assert run.calls == []


def test_a_small_jpg_is_copied_not_converted(tmp_path, isolated_env, monkeypatch):
    d = tmp_path / "case"
    _file_with_size(d, "art.jpg", 10)
    (d / "ram").mkdir()
    monkeypatch.chdir(d)
    run = _Run()
    monkeypatch.setenv("PATH", _stub_bin(tmp_path, []))
    assert thumbnails.embed_thumbnail(
        str(d), "track01", 100, 90, "1024x1024", 100, False, "ram", "script",
        run) == 0
    assert (d / "ram" / "track01.output.jpg").is_file()
    assert run.calls == []


def test_a_large_file_is_converted_with_the_tuning(tmp_path, isolated_env,
                                                   monkeypatch):
    d = tmp_path / "case"
    _file_with_size(d, "art.jpg", 100)
    (d / "ram").mkdir()
    monkeypatch.chdir(d)
    run = _Run(writes={"convert": ["ram/track01.output.jpg"]})
    monkeypatch.setenv("PATH", _stub_bin(tmp_path, ["convert"]))
    thumbnails.embed_thumbnail(
        str(d), "track01", 100, 90, "1024x1024", 100, False, "ram", "script", run)
    assert run.called("convert") == [
        ["convert", "-quiet", str(d / "art.jpg"), "-quality", "90",
         "-resize", "1024x1024>", "ram/track01.output.jpg"]]


def test_a_non_jpg_is_converted_however_small(tmp_path, isolated_env, monkeypatch):
    d = tmp_path / "case"
    _file_with_size(d, "art.png", 2)
    (d / "ram").mkdir()
    monkeypatch.chdir(d)
    run = _Run(writes={"convert": ["ram/track01.output.jpg"]})
    monkeypatch.setenv("PATH", _stub_bin(tmp_path, ["convert"]))
    thumbnails.embed_thumbnail(
        str(d), "track01", 100, 90, "1024x1024", 100, False, "ram", "script", run)
    assert run.called("convert")[0][2] == str(d / "art.png")


def test_the_jpg_check_is_case_sensitively_exact(tmp_path, isolated_env, monkeypatch):
    d = tmp_path / "case"
    _file_with_size(d, "art.JPG", 2)
    (d / "ram").mkdir()
    monkeypatch.chdir(d)
    run = _Run(writes={"convert": ["ram/track01.output.jpg"]})
    monkeypatch.setenv("PATH", _stub_bin(tmp_path, ["convert"]))
    thumbnails.embed_thumbnail(
        str(d), "track01", 100, 90, "1024x1024", 100, False, "ram", "script", run)
    assert run.called("convert")


def test_the_extraction_falls_through_when_no_image_was_chosen(tmp_path,
                                                                isolated_env,
                                                                monkeypatch):
    d = tmp_path / "case"
    _folder(d, ["a.opus"])
    (d / "ram").mkdir()
    monkeypatch.chdir(d)
    run = _Run(writes={"ffmpeg": ["ram/track01.jpg"]})
    monkeypatch.setenv("PATH", _stub_bin(tmp_path, ["pdftoppm", "ffmpeg"]))
    thumbnails.embed_thumbnail(
        str(d), "track01", 100, 90, "1024x1024", 100, False, "ram", "script", run)
    assert run.called("ffmpeg")[0][-1] == "ram/track01.jpg"


def test_an_opus_output_is_embedded_through_mutagen(tmp_path, isolated_env,
                                                    monkeypatch, covers):
    d = tmp_path / "case"
    _folder(d, ["art.jpg", "track01.opus"])
    (d / "ram").mkdir()
    monkeypatch.chdir(d)
    run = _Run()
    monkeypatch.setenv("PATH", _stub_bin(tmp_path, []))
    assert thumbnails.embed_thumbnail(
        str(d), "track01", 100, 90, "1024x1024", 100, False, "ram", "script",
        run) == 0
    assert covers.calls == [("track01.opus", "ram/track01.output.jpg")]
    assert not (d / "ram" / "track01.output.jpg").exists()


def test_two_outputs_in_one_run_never_share_a_temp_thumbnail(tmp_path,
                                                             isolated_env,
                                                             monkeypatch,
                                                             covers):
    """The path is derived from the OUTPUT name, so two workers of one parallel
    run cannot delete each other's in-flight cover.

    Asserted here rather than end to end: concatAudio's parallel run used to show
    it through the two python3 calls a PATH recorder saw, and embedding stopped
    being a subprocess at item 5.2.
    """
    d = tmp_path / "case"
    _folder(d, ["art.jpg", "Album One.opus", "Album Two.opus"])
    (d / "ram").mkdir()
    monkeypatch.chdir(d)
    monkeypatch.setenv("PATH", _stub_bin(tmp_path, []))
    for base in ("Album One", "Album Two"):
        thumbnails.embed_thumbnail(str(d), base, 100, 90, "1024x1024", 100,
                                   False, "ram", "script", _Run())
    temps = [cover for _audio, cover in covers.calls]
    assert len(temps) == 2
    assert len(set(temps)) == 2, temps


def test_a_flac_output_beats_an_mp3_in_the_dispatch(tmp_path, isolated_env,
                                                    monkeypatch, covers):
    d = tmp_path / "case"
    _folder(d, ["art.jpg", "track01.mp3", "track01.flac"])
    (d / "ram").mkdir()
    monkeypatch.chdir(d)
    run = _Run()
    monkeypatch.setenv("PATH", _stub_bin(tmp_path, []))
    thumbnails.embed_thumbnail(
        str(d), "track01", 100, 90, "1024x1024", 100, False, "ram", "script", run)
    assert covers.calls[0][0] == "track01.flac"
    assert not run.called("ffmpeg")


def test_an_mp3_output_is_rewritten_through_a_temp(tmp_path, isolated_env,
                                                   monkeypatch):
    d = tmp_path / "case"
    _folder(d, ["art.jpg", "track01.mp3"])
    (d / "ram").mkdir()
    monkeypatch.chdir(d)
    run = _Run(writes={"ffmpeg": ["ram/track01.temp.mp3"]})
    monkeypatch.setenv("PATH", _stub_bin(tmp_path, ["ffmpeg"]))
    assert thumbnails.embed_thumbnail(
        str(d), "track01", 100, 90, "1024x1024", 100, False, "ram", "script",
        run) == 0
    assert run.called("ffmpeg") == [
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
         "-i", "track01.mp3", "-i", "ram/track01.output.jpg",
         "-c", "copy", "-map", "0", "-map", "1", "ram/track01.temp.mp3"]]
    assert (d / "track01.mp3").is_file()
    assert not (d / "ram" / "track01.temp.mp3").exists()
    assert not (d / "ram" / "track01.output.jpg").exists()


def test_an_mp3_embed_the_tool_cannot_write_is_a_status_one(tmp_path,
                                                             isolated_env,
                                                             monkeypatch):
    d = tmp_path / "case"
    _folder(d, ["art.jpg", "track01.mp3"])
    (d / "ram").mkdir()
    monkeypatch.chdir(d)
    run = _Run()
    monkeypatch.setenv("PATH", _stub_bin(tmp_path, ["ffmpeg"]))
    assert thumbnails.embed_thumbnail(
        str(d), "track01", 100, 90, "1024x1024", 100, False, "ram", "script",
        run) == 1
    assert not (d / "track01.mp3").exists()


def test_an_m4b_output_gets_the_attached_pic_disposition(tmp_path, isolated_env,
                                                         monkeypatch):
    d = tmp_path / "case"
    _folder(d, ["art.jpg", "track01.m4b"])
    (d / "ram").mkdir()
    monkeypatch.chdir(d)
    run = _Run(writes={"ffmpeg": ["ram/track01.temp.m4b"]})
    monkeypatch.setenv("PATH", _stub_bin(tmp_path, ["ffmpeg"]))
    assert thumbnails.embed_thumbnail(
        str(d), "track01", 100, 90, "1024x1024", 100, False, "ram", "script",
        run) == 0
    assert run.called("ffmpeg")[0][-3:] == ["-disposition:v:0", "attached_pic",
                                            "ram/track01.temp.m4b"]
    assert (d / "track01.m4b").is_file()


def test_no_audio_file_is_a_status_zero(tmp_path, isolated_env, monkeypatch):
    d = tmp_path / "case"
    _folder(d, ["art.jpg"])
    (d / "ram").mkdir()
    monkeypatch.chdir(d)
    run = _Run()
    monkeypatch.setenv("PATH", _stub_bin(tmp_path, []))
    assert thumbnails.embed_thumbnail(
        str(d), "track01", 100, 90, "1024x1024", 100, False, "ram", "script",
        run) == 0


################################################################################
# applyCover: the sidecar overrides, then the embed decides
################################################################################
def test_the_webp_sidecar_beats_the_jpg_sidecar(tmp_path, isolated_env,
                                                monkeypatch):
    d = tmp_path / "case"
    _folder(d, ["in/track 01.webp", "in/track 01.jpg"])
    for sub in ("out", "tmp"):
        (d / sub).mkdir()
    monkeypatch.chdir(d)
    run = _Run()
    monkeypatch.setenv("PATH", _stub_bin(tmp_path, []))
    thumbnails.apply_cover(
        "track 01.mp3", "out/track 01.opus", 100, 90, "512x512",
        "in", "out", "tmp", "out/track 01.opus", "script", run)
    assert (d / "tmp" / "tempCover.jpg").is_file()


def test_no_sidecar_and_no_pulled_cover_does_nothing(tmp_path, isolated_env,
                                                     monkeypatch):
    d = tmp_path / "case"
    _folder(d, ["in/track 01.mp3"])
    for sub in ("out", "tmp"):
        (d / sub).mkdir()
    monkeypatch.chdir(d)
    run = _Run()
    monkeypatch.setenv("PATH", _stub_bin(tmp_path, []))
    assert thumbnails.apply_cover(
        "track 01.mp3", "out/track 01.opus", 100, 90, "512x512",
        "in", "out", "tmp", "out/track 01.opus", "script", run) == 0
    assert run.calls == []


def test_a_pulled_cover_is_embedded_even_without_a_sidecar(tmp_path,
                                                            isolated_env,
                                                            monkeypatch,
                                                            covers):
    d = tmp_path / "case"
    _file_with_size(d, "tmp/tempCover.jpg", 10)
    (d / "out").mkdir()
    monkeypatch.chdir(d)
    run = _Run()
    monkeypatch.setenv("PATH", _stub_bin(tmp_path, []))
    assert thumbnails.apply_cover(
        "track 01.mp3", "out/track 01.opus", 100, 90, "512x512",
        "in", "out", "tmp", "out/track 01.opus", "script", run) == 0
    assert covers.calls == [("out/track 01.opus", "tmp/cover.jpg")]


def test_a_large_sidecar_is_scaled_the_big_cover_way(tmp_path, isolated_env,
                                                     monkeypatch):
    d = tmp_path / "case"
    _file_with_size(d, "in/track 01.jpg", 100)
    for sub in ("out", "tmp"):
        (d / sub).mkdir()
    monkeypatch.chdir(d)
    run = _Run(writes={"convert": ["tmp/cover.jpg"]})
    monkeypatch.setenv("PATH", _stub_bin(tmp_path, ["convert"]))
    thumbnails.apply_cover(
        "track 01.mp3", "out/track 01.opus", 100, 90, "512x512",
        "in", "out", "tmp", "out/track 01.opus", "script", run)
    assert run.called("convert") == [
        ["convert", "tmp/tempCover.jpg", "-quality", "90",
         "-resize", "512x512>", "tmp/cover.jpg"]]


def test_a_sidecar_below_the_threshold_is_copied(tmp_path, isolated_env,
                                                 monkeypatch):
    d = tmp_path / "case"
    _file_with_size(d, "in/track 01.jpg", 10)
    for sub in ("out", "tmp"):
        (d / sub).mkdir()
    monkeypatch.chdir(d)
    run = _Run()
    monkeypatch.setenv("PATH", _stub_bin(tmp_path, []))
    thumbnails.apply_cover(
        "track 01.mp3", "out/track 01.opus", 100, 90, "512x512",
        "in", "out", "tmp", "out/track 01.opus", "script", run)
    assert (d / "tmp" / "cover.jpg").is_file()
    assert not run.called("convert")


def test_the_sidecar_copies_in_the_output_are_dropped_only_on_success(
        tmp_path, isolated_env, monkeypatch):
    for embeds, rc in (("yes", 0), ("no", 1)):
        d = tmp_path / embeds
        _folder(d, ["in/track 01.jpg", "out/track 01.jpg", "out/track 01.webp"])
        (d / "tmp").mkdir()
        monkeypatch.chdir(d)
        run = _Run()
        covers = _Covers([rc])
        monkeypatch.setattr(thumbnails.mutagentags, "embed_cover", covers)
        monkeypatch.setenv("PATH", _stub_bin(tmp_path, []))
        status = thumbnails.apply_cover(
            "track 01.mp3", "out/track 01.opus", 100, 90, "512x512",
            "in", "out", "tmp", "out/track 01.opus", "script", run)
        # the function's last command is an if without an else, so the
        # status is zero even when the embed refused
        assert status == 0
        if embeds == "yes":
            assert not (d / "out" / "track 01.jpg").exists()
            assert not (d / "out" / "track 01.webp").exists()
        else:
            assert (d / "out" / "track 01.jpg").is_file()
            assert (d / "out" / "track 01.webp").is_file()


def test_the_target_defaults_to_the_opus(tmp_path, isolated_env, monkeypatch,
                                         covers):
    d = tmp_path / "case"
    _file_with_size(d, "in/track 01.jpg", 10)
    for sub in ("out", "tmp"):
        (d / sub).mkdir()
    monkeypatch.chdir(d)
    run = _Run()
    monkeypatch.setenv("PATH", _stub_bin(tmp_path, []))
    thumbnails.apply_cover(
        "track 01.mp3", None, 100, 90, "512x512",
        "in", "out", "tmp", "out/track 01.opus", "script", run)
    assert covers.calls[0][0] == "out/track 01.opus"


################################################################################
# extractSourceCover: the cover already inside the file
################################################################################
def test_a_matroska_source_is_asked_for_its_attachment_first(tmp_path,
                                                              isolated_env,
                                                              monkeypatch):
    monkeypatch.chdir(tmp_path)
    _folder(tmp_path, ["src.mka"])
    run = _Run()
    monkeypatch.setenv("PATH", _stub_bin(tmp_path, ["mkvextract", "ffmpeg"]))
    assert thumbnails.extract_source_cover("src.mka", "tmp", run) == 0
    assert run.called("mkvextract") == [
        ["mkvextract", "src.mka", "attachments", "1:tmp/tempCover.jpg"]]
    assert not run.called("ffmpeg")


def test_a_matroska_source_whose_attachment_fails_falls_back_to_ffmpeg(
        tmp_path, isolated_env, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _folder(tmp_path, ["src.mkv"])
    run = _Run(results={"mkvextract": [1]})
    monkeypatch.setenv("PATH", _stub_bin(tmp_path, ["mkvextract", "ffmpeg"]))
    assert thumbnails.extract_source_cover("src.mkv", "tmp", run) == 0
    assert run.called("ffmpeg") == [
        ["ffmpeg", "-nostdin", "-i", "src.mkv", "-an", "-c:v", "copy",
         "tmp/tempCover.jpg"]]


def test_the_matroska_test_is_case_insensitive_the_shells_way(tmp_path,
                                                               isolated_env,
                                                               monkeypatch):
    monkeypatch.chdir(tmp_path)
    _folder(tmp_path, ["SRC.MKV"])
    run = _Run()
    monkeypatch.setenv("PATH", _stub_bin(tmp_path, ["mkvextract", "ffmpeg"]))
    thumbnails.extract_source_cover("SRC.MKV", "tmp", run)
    assert run.called("mkvextract")


def test_a_non_matroska_source_goes_straight_to_ffmpeg(tmp_path, isolated_env,
                                                       monkeypatch):
    monkeypatch.chdir(tmp_path)
    _folder(tmp_path, ["src.mp3"])
    run = _Run()
    monkeypatch.setenv("PATH", _stub_bin(tmp_path, ["mkvextract", "ffmpeg"]))
    assert thumbnails.extract_source_cover("src.mp3", "tmp", run) == 0
    assert run.called("ffmpeg")[0][3] == "src.mp3"
    assert not run.called("mkvextract")


def test_a_failed_extraction_is_still_a_status_zero(tmp_path, isolated_env,
                                                    monkeypatch):
    monkeypatch.chdir(tmp_path)
    _folder(tmp_path, ["src.mp3"])
    run = _Run(results={"ffmpeg": [1]})
    monkeypatch.setenv("PATH", _stub_bin(tmp_path, ["ffmpeg"]))
    assert thumbnails.extract_source_cover("src.mp3", "tmp", run) == 0