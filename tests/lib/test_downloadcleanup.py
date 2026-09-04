"""The white box for medialib/lib/downloadcleanup.py.

What a finished download leaves beside an episode and what happens to it. What
is pinned here: which sidecars go, the sweep and the prune, the exact argv each
remux hands mkvmerge and mkvpropedit, the glob edges the stem can fall into,
and the safety record a lowercasing collision makes. """

import os

import pytest

from medialib.lib import downloadcleanup
from medialib.lib.safety import SkipLog

pytestmark = pytest.mark.stubbed


class _Proc:
    def __init__(self, returncode=0):
        self.returncode = returncode


class _Run:
    """The command runner stand-in: per-tool canned results, consumed in call
    order, the argv of every call recorded, and a write that stands for the file
    the tool produces (the remuxed .mkv)."""

    def __init__(self, results):
        self.results = results
        self.calls = []

    def __call__(self, argv):
        name = argv[0]
        self.calls.append(list(argv))
        queue = self.results.get(name, [])
        if queue:
            returncode, write = queue.pop(0)
            if write is not None:
                with open(write, "wb") as handle:
                    handle.write(b"")
        else:
            returncode = 0
        return _Proc(returncode)


def _write(directory, name, text="x"):
    with open(os.path.join(directory, name), "w", encoding="utf-8") as handle:
        handle.write(text)


class TestDownloadCleanupTools:
    def test_audio_needs_nothing(self):
        assert downloadcleanup.download_cleanup_tools("audio") == ""

    def test_video_is_mkvtoolnix(self):
        assert downloadcleanup.download_cleanup_tools("video") == \
            "mkvmerge mkvpropedit"

    def test_anything_else_is_audio(self):
        assert downloadcleanup.download_cleanup_tools("videoX") == ""
        assert downloadcleanup.download_cleanup_tools("") == ""


class TestCleanDownloadedFile:
    def test_audio_sidecars_go_cover_and_neighbour_stay(self, tmp_path):
        folder = tmp_path / "AI" / "latent space"
        folder.mkdir(parents=True)
        _write(folder, "20260607 Episode.opus")
        _write(folder, "20260607 Episode.jpg")
        _write(folder, "20260607 Episode.description")
        _write(folder, "20260607 Episode.info.json")
        _write(folder, "20260607 Episode.en.srt")
        _write(folder, "folder.jpg")
        _write(folder, "20260601 Older.opus")
        _write(folder, "20260601 Older.jpg")

        path = str(folder / "20260607 Episode.opus")
        cleaned, sidecars, remuxed = downloadcleanup.clean_downloaded_file(path)

        assert (cleaned, sidecars, remuxed) == (path, 4, False)
        assert (folder / "20260607 Episode.opus").is_file()
        assert not (folder / "20260607 Episode.jpg").exists()
        assert not (folder / "20260607 Episode.description").exists()
        assert not (folder / "20260607 Episode.info.json").exists()
        assert not (folder / "20260607 Episode.en.srt").exists()
        # the folder's cover shares no name with an episode; the other episode's
        # sidecars belong to that episode
        assert (folder / "folder.jpg").is_file()
        assert (folder / "20260601 Older.opus").is_file()
        assert (folder / "20260601 Older.jpg").is_file()

    def test_uppercase_extension_is_lowercased_first(self, tmp_path):
        folder = tmp_path
        _write(folder, "Shouty.OPUS")
        _write(folder, "Shouty.jpg")

        cleaned, sidecars, remuxed = downloadcleanup.clean_downloaded_file(
            str(folder / "Shouty.OPUS"))

        assert cleaned == str(folder / "Shouty.opus")
        assert (folder / "Shouty.opus").is_file()
        assert not (folder / "Shouty.OPUS").exists()
        assert not (folder / "Shouty.jpg").exists()
        assert (sidecars, remuxed) == (1, False)

    def test_lowercasing_collision_keeps_the_uppercase_and_records_a_skip(
            self, tmp_path):
        folder = tmp_path
        _write(folder, "File.JPG")
        _write(folder, "File.jpg")
        skips = SkipLog()

        cleaned, sidecars, remuxed = downloadcleanup.clean_downloaded_file(
            str(folder / "File.JPG"), skip_log=skips)

        # the lowercased name was taken, so the rename is refused: the episode
        # keeps its uppercase name, and the pre-existing File.jpg is a sidecar of
        # the same stem - so it goes
        assert cleaned == str(folder / "File.JPG")
        assert (folder / "File.JPG").is_file()
        assert not (folder / "File.jpg").exists()
        assert sidecars == 1
        assert skips.report() == [
            "Safety: skipped 1 rename(s) to avoid overwrite",
            "Safety skip details:",
            f"  {folder / 'File.JPG'} -> {folder / 'File.jpg'}",
        ]

    def test_a_file_that_is_gone_is_not_an_error(self, tmp_path):
        cleaned, sidecars, remuxed = downloadcleanup.clean_downloaded_file(
            str(tmp_path / "never existed.opus"))
        assert (cleaned, sidecars, remuxed) == (
            str(tmp_path / "never existed.opus"), 0, False)

    def test_a_matroska_is_never_remuxed(self, tmp_path):
        folder = tmp_path
        _write(folder, "episode.mkv")
        _write(folder, "episode.jpg")

        cleaned, sidecars, remuxed = downloadcleanup.clean_downloaded_file(
            str(folder / "episode.mkv"))

        assert (cleaned, sidecars, remuxed) == (str(folder / "episode.mkv"), 1, False)
        assert (folder / "episode.mkv").is_file()

    def test_an_audio_kept_extension_is_never_remuxed(self, tmp_path):
        folder = tmp_path
        _write(folder, "episode.mp3")

        cleaned, sidecars, remuxed = downloadcleanup.clean_downloaded_file(
            str(folder / "episode.mp3"))

        assert remuxed is False
        assert (folder / "episode.mp3").is_file()

    def test_remux_success_replaces_the_source_and_attaches(self, tmp_path):
        folder = tmp_path
        source = str(folder / "episode.mp4")
        target = str(folder / "episode.mkv")
        _write(folder, "episode.mp4")
        _write(folder, "episode.description")
        _write(folder, "episode.info.json")
        _write(folder, "episode.en.srt")
        run = _Run({
            "mkvmerge": [(0, target)],
            "mkvpropedit": [(0, None), (0, None)],
        })

        cleaned, sidecars, remuxed = downloadcleanup.clean_downloaded_file(
            source, run=run)

        assert (cleaned, sidecars, remuxed) == (target, 3, True)
        assert not (folder / "episode.mp4").exists()
        assert (folder / "episode.mkv").is_file()
        # the description and the json were attached into the Matroska, so their
        # loose copies go; the subtitle was muxed in, so its converted copy goes
        assert not (folder / "episode.description").exists()
        assert not (folder / "episode.info.json").exists()
        assert not (folder / "episode.en.srt").exists()
        assert run.calls[0] == [
            "mkvmerge", "--quiet", "-o", target, "--", source]
        assert run.calls[1:] == [
            ["mkvpropedit", target, "--add-attachment", str(folder / "episode.description")],
            ["mkvpropedit", target, "--add-attachment", str(folder / "episode.info.json")],
        ]

    def test_remux_failure_keeps_the_source_and_drops_a_partial_target(self, tmp_path):
        folder = tmp_path
        source = str(folder / "episode.mp4")
        target = str(folder / "episode.mkv")
        _write(folder, "episode.mp4")
        run = _Run({"mkvmerge": [(1, target)]})

        cleaned, sidecars, remuxed = downloadcleanup.clean_downloaded_file(
            source, run=run)

        assert (cleaned, sidecars, remuxed) == (source, 0, False)
        assert (folder / "episode.mp4").is_file()
        assert not (folder / "episode.mkv").exists()

    def test_remux_failure_without_a_partial_keeps_the_source(self, tmp_path):
        folder = tmp_path
        source = str(folder / "episode.mp4")
        _write(folder, "episode.mp4")
        run = _Run({"mkvmerge": [(1, None)]})

        cleaned, sidecars, remuxed = downloadcleanup.clean_downloaded_file(
            source, run=run)

        assert remuxed is False
        assert (folder / "episode.mp4").is_file()

    def test_a_remux_target_that_is_already_there_is_left_alone(self, tmp_path):
        folder = tmp_path
        source = str(folder / "episode.mp4")
        _write(folder, "episode.mp4")
        _write(folder, "episode.mkv")
        run = _Run({})

        cleaned, sidecars, remuxed = downloadcleanup.clean_downloaded_file(
            source, run=run)

        assert remuxed is False
        assert not any(call[0] == "mkvmerge" for call in run.calls)
        # the episode is the source; the pre-existing .mkv is not a sidecar
        assert (folder / "episode.mp4").is_file()
        assert (folder / "episode.mkv").is_file()

    def test_sidecars_are_matched_on_the_stem_not_the_whole_name(self, tmp_path):
        # the stem carries the date and the spaces; a cover that shares the date
        # but not the episode's name is not a sidecar
        folder = tmp_path
        _write(folder, "20260607 Episode.opus")
        _write(folder, "20260607.jpg")
        _write(folder, "20260607 Episode.jpg")

        cleaned, sidecars, remuxed = downloadcleanup.clean_downloaded_file(
            str(folder / "20260607 Episode.opus"))

        assert sidecars == 1
        assert (folder / "20260607.jpg").is_file()
        assert not (folder / "20260607 Episode.jpg").exists()

    def test_a_subtitle_language_between_the_stem_and_the_extension(self, tmp_path):
        folder = tmp_path
        _write(folder, "Episode.opus")
        _write(folder, "Episode.en.srt")
        _write(folder, "Episode.fr.vtt")
        _write(folder, "Episode.de.ass")
        _write(folder, "Episode.other.txt")

        cleaned, sidecars, remuxed = downloadcleanup.clean_downloaded_file(
            str(folder / "Episode.opus"))

        assert sidecars == 3
        assert not (folder / "Episode.en.srt").exists()
        assert not (folder / "Episode.fr.vtt").exists()
        assert not (folder / "Episode.de.ass").exists()
        assert (folder / "Episode.other.txt").is_file()

    def test_a_plain_subtitle_extension_is_a_sidecar_by_the_loop(self, tmp_path):
        # Episode.srt has no language between stem and extension, so the
        # "<stem>.<ext>" loop finds it - not the subtitle find
        folder = tmp_path
        _write(folder, "Episode.opus")
        _write(folder, "Episode.srt")

        cleaned, sidecars, remuxed = downloadcleanup.clean_downloaded_file(
            str(folder / "Episode.opus"))

        assert sidecars == 1
        assert not (folder / "Episode.srt").exists()

    def test_a_stem_with_glob_characters_is_matched_like_find(self, tmp_path):
        # the stem "[1]" is a character class to a glob: the subtitle find names
        # "File[1].*.srt", which find -name (and fnmatch) read as File + one of
        # {1} + . + star + .srt. A sibling that the class reaches is removed the
        # way the find would.
        folder = tmp_path
        _write(folder, "File[1].opus")
        _write(folder, "File1.en.srt")

        cleaned, sidecars, remuxed = downloadcleanup.clean_downloaded_file(
            str(folder / "File[1].opus"))

        assert sidecars == 1
        assert not (folder / "File1.en.srt").exists()

    def test_nested_episode(self, tmp_path):
        sub = tmp_path / "sub"
        sub.mkdir()
        _write(sub, "Episode.opus")
        _write(sub, "Episode.jpg")
        _write(sub, "Episode.en.srt")

        cleaned, sidecars, remuxed = downloadcleanup.clean_downloaded_file(
            str(sub / "Episode.opus"))

        assert cleaned == str(sub / "Episode.opus")
        assert sidecars == 2
        assert not (sub / "Episode.jpg").exists()
        assert not (sub / "Episode.en.srt").exists()


class TestSweepPartialDownloads:
    def test_every_leftover_wherever_it_sits(self, tmp_path):
        a = tmp_path / "AI" / "latent space"
        b = tmp_path / "AI" / "other"
        a.mkdir(parents=True)
        b.mkdir(parents=True)
        _write(a, "Something.f251.part")
        _write(a, "Something.partial")
        _write(b, "Else.ytdl")
        _write(b, "Else.temp")
        _write(b, "Else.concat")
        _write(b, "Else-Frag12")
        _write(b, "Real.opus")

        swept = downloadcleanup.sweep_partial_downloads(str(tmp_path))

        assert swept == 6
        assert not (a / "Something.f251.part").exists()
        assert not (a / "Something.partial").exists()
        assert not (b / "Else.ytdl").exists()
        assert not (b / "Else.temp").exists()
        assert not (b / "Else.concat").exists()
        assert not (b / "Else-Frag12").exists()
        assert (b / "Real.opus").is_file()

    def test_a_fragment_without_a_digit_is_not_swept(self, tmp_path):
        _write(tmp_path, "Else-Frag")
        _write(tmp_path, "Else-Fragx12")
        _write(tmp_path, "Else-Frag0")

        swept = downloadcleanup.sweep_partial_downloads(str(tmp_path))

        assert swept == 1
        assert (tmp_path / "Else-Frag").is_file()
        assert (tmp_path / "Else-Fragx12").is_file()
        assert not (tmp_path / "Else-Frag0").exists()

    def test_a_symlink_to_a_partial_is_not_swept(self, tmp_path):
        _write(tmp_path, "Else.ytdl")
        os.symlink(tmp_path / "Else.ytdl", tmp_path / "link.ytdl")

        swept = downloadcleanup.sweep_partial_downloads(str(tmp_path))

        # the real file goes; the link is not -type f, so it is left (now broken)
        assert swept == 1
        assert not (tmp_path / "Else.ytdl").exists()
        assert os.path.islink(tmp_path / "link.ytdl")

    def test_not_a_directory(self, tmp_path):
        assert downloadcleanup.sweep_partial_downloads(str(tmp_path / "nope")) == 0


class TestPruneEmptyFolders:
    def test_depth_first_the_root_is_kept(self, tmp_path):
        (tmp_path / "Gone" / "deeper").mkdir(parents=True)
        (tmp_path / "AI" / "other").mkdir(parents=True)
        _write(tmp_path / "AI" / "other", "Real.opus")

        pruned = downloadcleanup.prune_empty_folders(str(tmp_path))

        assert pruned == 2
        assert not (tmp_path / "Gone" / "deeper").exists()
        assert not (tmp_path / "Gone").exists()
        assert (tmp_path / "AI" / "other").is_dir()
        assert tmp_path.is_dir()

    def test_the_root_is_kept_even_once_it_is_itself_empty(self, tmp_path):
        """The case the sibling above cannot make: there the root still holds
        content, so it could not be pruned whatever the walk did.

        This is the whole reason the walk excludes the root. A caller pointed at a
        mistyped or not-yet-filled path would otherwise have it removed from under
        them, and the next step then fails on a missing directory rather than on
        the empty one it was given.
        """
        (tmp_path / "Gone" / "deeper").mkdir(parents=True)

        pruned = downloadcleanup.prune_empty_folders(str(tmp_path))

        assert pruned == 2
        assert list(tmp_path.iterdir()) == []
        assert tmp_path.is_dir()

    def test_a_hidden_entry_counts_as_content(self, tmp_path):
        folder = tmp_path / "Gone"
        folder.mkdir()
        _write(folder, ".hidden")

        pruned = downloadcleanup.prune_empty_folders(str(tmp_path))

        assert pruned == 0
        assert folder.is_dir()

    def test_not_a_directory(self, tmp_path):
        assert downloadcleanup.prune_empty_folders(str(tmp_path / "nope")) == 0