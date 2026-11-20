"""Tests for asking a language model to search the web for a book's cover -
medialib.lib.aicover.

The model is a stand-in that records what it was told and answers with
image addresses; the download is a stand-in that writes an image or does
not. What is pinned: the model is told the folder, the tags, the output name,
what the book probably is and what a good cover is; only a model that can
search is asked; its answer is read leniently, and the first address that
downloads as an image is the cover; and what it was asked is counted.
"""

import contextlib
import os
import pathlib

import pytest

from medialib.lib import aicover, llmclient, modelmeter, onlinecover

pytestmark = pytest.mark.pure

_JPEG = b"\xff\xd8\xff" + b"\0" * 4000


class _Model:
    """A model that can search, answering ``answer`` - or raising it - and,
    where it ``sees``, answering ``look`` when it is shown pictures."""

    def __init__(self, answer, can_search=True, sees=False, look=""):
        self.answer = answer
        self.can_search = can_search
        self.asked = []
        self.shown = []
        self.purpose = ""
        self.name = "stand-in"
        self.model = "stand-in"
        self.vision = "auto"
        self.sees = sees
        self.look = look

    def searching(self, purpose=""):
        self.purpose = purpose
        return self if self.can_search else None

    @contextlib.contextmanager
    def serving(self, log):
        yield self

    def sees_images(self):
        return self.sees

    def ask(self, system, prompt, tally=None, images=()):
        if images:
            self.shown.append((prompt, [pathlib.Path(p).read_bytes() for p in images]))
            if tally is not None:
                tally.add(1.0, 10)
            return self.look
        self.asked.append((system, prompt))
        if isinstance(self.answer, Exception):
            raise self.answer
        if tally is not None:
            tally.add(2.0, 40, 900)
        return self.answer


def _lookers(model):
    """The model as the look's ladder, where it sees; no ladder where not."""
    return [model] if model.sees else []


def _book(tmp_path):
    folder = tmp_path / "Ann Mirelle" / "The Quiet Orchard (Unabridged)"
    (folder / "CD1").mkdir(parents=True)
    (folder / "CD1" / "01 Opening.mp3").write_bytes(b"x" * 10)
    (folder / "CD1" / "02 The Gate.mp3").write_bytes(b"x" * 10)
    (folder / "notes.txt").write_text("read by a cast of four")
    return folder


def _tags(path):
    return {"album": "The Quiet Orchard", "artist": "Ann Mirelle",
            "title": os.path.basename(path), "language": "deu"}


def _download(good):
    tried = []

    def download(url, dest):
        tried.append(url)
        if url not in good:
            return False
        with open(dest, "wb") as handle:
            handle.write(_JPEG)
        return True
    download.tried = tried
    return download


def _tracks(folder):
    return sorted(str(p) for p in folder.rglob("*.mp3"))


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv(aicover.SWITCH_VARIABLE, "on")


def test_the_model_is_told_everything_the_run_knows(tmp_path):
    folder = _book(tmp_path)
    # The tags are the stand-in's, not ffprobe's.
    subject = onlinecover.subject_for(folder.name, 5 * 3600,
                                      tag_titles=["The Quiet Orchard"],
                                      tag_creators=["Ann Mirelle"])
    text = aicover.prompt(str(folder), "The Quiet Orchard", _tracks(folder),
                          subject, _tags)
    assert "The Quiet Orchard (Unabridged)" in text
    assert "Ann Mirelle" in text
    assert "CD1/01 Opening.mp3" in text and "notes.txt" in text
    assert "language: deu" in text
    assert "joined into one file named: The Quiet Orchard" in text
    assert "probably an audiobook, because of a length of 5:00:00" in text
    assert "not certainly" in text
    assert "audible" in text


def test_the_preferences_are_in_the_system_prompt():
    for wanted in ("own language", "audiobook edition", "High resolution",
                   "Any image format", "worse than none"):
        assert wanted in aicover.SYSTEM


def test_a_long_folder_is_listed_in_part(tmp_path, monkeypatch):
    monkeypatch.setattr(aicover, "MAX_LISTED_FILES", 2)
    folder = _book(tmp_path)
    assert aicover._listing(str(folder))[-1] == "... and 1 more file(s)"


def test_the_answer_is_read_leniently():
    answer = ('Here you go:\n```json\n{"images": [{"url": "https://a.test/x.jpg"},'
              ' "https://b.test/y.png", {"url": "file:///etc/passwd"}, 7]}\n```')
    assert [c["url"] for c in aicover.candidates(answer)] == [
        "https://a.test/x.jpg", "https://b.test/y.png"]
    assert aicover.candidates("nothing found") == []
    assert aicover.candidates('{"images": []}') == []


def test_the_first_address_that_is_an_image_is_the_cover(tmp_path):
    folder = _book(tmp_path)
    model = _Model('{"images": [{"url": "https://a.test/page"}, '
                   '{"url": "https://b.test/cover.jpg", "title": "Orchard"}]}')
    download = _download({"https://b.test/cover.jpg"})
    meter = tmp_path / "meter"
    image = aicover.book_cover(str(folder), "The Quiet Orchard", _tracks(folder),
                               5 * 3600, str(tmp_path / "out"), searchers=[model],
                               lookers=_lookers(model),
                               meter_file=str(meter), download=download,
                               probe=_tags)
    assert image == str(tmp_path / "out.jpg")
    assert download.tried == ["https://a.test/page", "https://b.test/cover.jpg"]
    assert model.purpose == "cover"
    spans, tally = modelmeter.gathered(str(meter))
    assert (tally.requests, tally.tokens, tally.read) == (1, 40, 900)


def test_nothing_that_downloads_is_no_cover(tmp_path):
    folder = _book(tmp_path)
    model = _Model('{"images": [{"url": "https://a.test/x.jpg"}]}')
    assert aicover.book_cover(str(folder), "x", _tracks(folder), 0,
                              str(tmp_path / "out"), searchers=[model], lookers=_lookers(model),
                              download=_download(set()), probe=_tags) == ""


def test_a_model_that_cannot_search_is_not_asked(tmp_path):
    folder = _book(tmp_path)
    model = _Model('{"images": ["https://a.test/x.jpg"]}', can_search=False)
    assert aicover.book_cover(str(folder), "x", _tracks(folder), 0,
                              str(tmp_path / "out"), searchers=[model], lookers=_lookers(model),
                              download=_download({"https://a.test/x.jpg"}),
                              probe=_tags) == ""
    assert model.asked == []


def test_the_switch_keeps_the_model_unasked(tmp_path, monkeypatch):
    monkeypatch.setenv(aicover.SWITCH_VARIABLE, "off")
    folder = _book(tmp_path)
    model = _Model('{"images": ["https://a.test/x.jpg"]}')
    assert aicover.book_cover(str(folder), "x", _tracks(folder), 0,
                              str(tmp_path / "out"), searchers=[model], lookers=_lookers(model),
                              probe=_tags) == ""
    assert model.asked == []


def test_no_model_set_up_is_no_cover(tmp_path, monkeypatch):
    for name in (llmclient.API_VARIABLE, llmclient.URL_VARIABLE,
                 llmclient.HARNESS_VARIABLE):
        monkeypatch.delenv(name, raising=False)
    folder = _book(tmp_path)
    assert aicover.book_cover(str(folder), "x", _tracks(folder), 0,
                              str(tmp_path / "out"), probe=_tags) == ""


def test_a_model_that_fails_is_no_cover_and_is_still_counted(tmp_path):
    folder = _book(tmp_path)
    model = _Model(llmclient.LlmError("no answer"))
    meter = tmp_path / "meter"
    assert aicover.book_cover(str(folder), "x", _tracks(folder), 0,
                              str(tmp_path / "out"), searchers=[model], lookers=_lookers(model),
                              meter_file=str(meter), probe=_tags) == ""
    assert modelmeter.gathered(str(meter))[1].requests == 0


_THREE = ('{"images": [{"url": "https://a.test/1.jpg", "title": "Orchard Live"},'
          ' {"url": "https://b.test/2.jpg", "title": "The Quiet Orchard",'
          ' "edition": "audiobook"}, {"url": "https://c.test/3.jpg"}]}')


def _all():
    """A download that writes each address's own picture."""
    def download(url, dest):
        with open(dest, "wb") as handle:
            handle.write(_JPEG + url.encode())
        return True
    return download


def _as_is(path, dest):
    return path


def _cover(tmp_path, model, **kwargs):
    folder = _book(tmp_path)
    return aicover.book_cover(str(folder), "The Quiet Orchard", _tracks(folder),
                              5 * 3600, str(tmp_path / "out"), searchers=[model],
                              lookers=_lookers(model),
                              download=_all(), probe=_tags, prepare=_as_is,
                              **kwargs)


class TestTheLook:
    """A model that sees is shown the candidates and picks one, or none."""

    def test_the_model_picks_among_the_pictures(self, tmp_path):
        model = _Model(_THREE, sees=True,
                       look='{"choice": 2, "why": "the title is printed on it"}')
        meter = tmp_path / "meter"
        image = _cover(tmp_path, model, meter_file=str(meter))
        assert pathlib.Path(image).read_bytes().endswith(b"https://b.test/2.jpg")
        prompt, pictures = model.shown[0]
        assert len(pictures) == 3
        assert "2. from b.test - title: The Quiet Orchard, edition: audiobook" in prompt
        assert "probably an audiobook" in prompt
        # The search and the look, each recorded for the run.
        assert modelmeter.gathered(str(meter))[1].requests == 2
        assert sorted(os.listdir(tmp_path)) == ["Ann Mirelle", "meter", "out.jpg"]

    def test_none_plausible_is_no_cover_and_leaves_nothing(self, tmp_path):
        model = _Model(_THREE, sees=True,
                       look='{"choice": null, "why": "all three are banners"}')
        assert _cover(tmp_path, model) == ""
        assert sorted(os.listdir(tmp_path)) == ["Ann Mirelle"]

    @pytest.mark.parametrize("look", ["I cannot tell", '{"choice": 9}'])
    def test_an_answer_that_is_no_choice_takes_the_first(self, tmp_path, look):
        model = _Model(_THREE, sees=True, look=look)
        image = _cover(tmp_path, model)
        assert pathlib.Path(image).read_bytes().endswith(b"https://a.test/1.jpg")

    def test_a_model_that_does_not_see_is_not_shown_them(self, tmp_path):
        model = _Model(_THREE, sees=False, look='{"choice": 2}')
        image = _cover(tmp_path, model)
        assert pathlib.Path(image).read_bytes().endswith(b"https://a.test/1.jpg")
        assert model.shown == []

    def test_no_more_than_a_few_are_shown(self, tmp_path, monkeypatch):
        monkeypatch.setattr(aicover, "MAX_LOOKED_AT", 2)
        model = _Model(_THREE, sees=True, look='{"choice": 1}')
        _cover(tmp_path, model)
        assert len(model.shown[0][1]) == 2


def _episode(tmp_path, name="20240105 Flying in Winter.opus"):
    folder = tmp_path / "Podcasts" / "Kite Talk"
    folder.mkdir(parents=True, exist_ok=True)
    track = folder / name
    track.write_bytes(b"x" * 10)
    return track


def _episode_tags(path):
    return {"album": "Kite Talk", "artist": "Ann Mirelle",
            "title": "Flying in Winter", "language": "eng"}


class TestTrackCover:
    """Per-track (podcast episode) covers: search plus mandatory vision look."""

    def test_episode_prompt_includes_show_title_and_date_from_filename(self, tmp_path):
        track = _episode(tmp_path, "20240105 Flying in Winter.opus")
        subject = onlinecover.track_subject(str(track), 45 * 60,
                                            tags=_episode_tags(str(track)))
        text = aicover.track_prompt(str(track), subject, _episode_tags)
        assert "Show: Kite Talk; Ann Mirelle" in text
        assert "Episode title: Flying in Winter" in text
        assert "Date: 2024-01-05" in text
        assert "Parent folder: Kite Talk" in text
        assert "File: 20240105 Flying in Winter.opus" in text
        assert "probably a podcast or a podcast episode" in text

    def test_episode_prompt_uses_parent_folder_and_date_tag_when_undated(self, tmp_path):
        track = _episode(tmp_path, "Flying in Winter.opus")
        tags = {"artist": "Ann Mirelle", "title": "Flying in Winter",
                "date": "2024-03-19"}
        subject = onlinecover.track_subject(str(track), 45 * 60, tags=tags)
        text = aicover.track_prompt(str(track), subject, lambda _p: tags)
        assert "Show: Ann Mirelle; Kite Talk" in text
        assert "Episode title: Flying in Winter" in text
        assert "Date: 2024-03-19" in text

    def test_vision_look_accepts_returns_accepted_image_and_records_meter(self, tmp_path):
        track = _episode(tmp_path)
        searcher = _Model(_THREE, can_search=True, sees=False)
        looker = _Model("", can_search=False, sees=True,
                        look='{"choice": 2, "why": "episode artwork matches"}')
        meter = tmp_path / "meter"
        image = aicover.track_cover(
            str(track), 45 * 60, str(tmp_path / "out"),
            searchers=[searcher], lookers=[looker],
            meter_file=str(meter), download=_all(),
            probe=_episode_tags, prepare=_as_is)
        assert image == str(tmp_path / "out.jpg")
        assert pathlib.Path(image).read_bytes().endswith(b"https://b.test/2.jpg")
        assert len(searcher.asked) == 1
        assert len(looker.shown) == 1
        _spans, tally = modelmeter.gathered(str(meter))
        assert (tally.requests, tally.tokens, tally.read) == (2, 50, 900)
        assert sorted(os.listdir(tmp_path)) == ["Podcasts", "meter", "out.jpg"]

    @pytest.mark.parametrize("look", [
        '{"choice": null, "why": "none is this episode"}',
        "I cannot tell",
        '{"choice": 9}',
        '{"choice": true}',
    ])
    def test_vision_look_rejects_or_gives_no_valid_choice_returns_no_cover(
            self, tmp_path, look):
        track = _episode(tmp_path)
        model = _Model(_THREE, can_search=True, sees=True, look=look)
        image = aicover.track_cover(
            str(track), 45 * 60, str(tmp_path / "out"),
            searchers=[model], lookers=[model],
            download=_all(), probe=_episode_tags, prepare=_as_is)
        assert image == ""
        assert sorted(os.listdir(tmp_path)) == ["Podcasts"]

    def test_no_vision_model_available_skips_search_and_returns_no_cover(self, tmp_path):
        track = _episode(tmp_path)
        searcher = _Model(_THREE, can_search=True, sees=False)
        image = aicover.track_cover(
            str(track), 45 * 60, str(tmp_path / "out"),
            searchers=[searcher], lookers=[],
            download=_all(), probe=_episode_tags, prepare=_as_is)
        assert image == ""
        assert searcher.asked == []

    def test_no_vision_model_in_roster_skips_search(self, tmp_path, monkeypatch):
        track = _episode(tmp_path)
        searcher = _Model(_THREE, can_search=True, sees=False)
        monkeypatch.setattr(aicover.llmroster, "optional",
                            lambda task: ([], "no vision model"))
        image = aicover.track_cover(
            str(track), 45 * 60, str(tmp_path / "out"),
            searchers=[searcher], lookers=None,
            download=_all(), probe=_episode_tags, prepare=_as_is)
        assert image == ""
        assert searcher.asked == []

    def test_looker_that_does_not_see_at_runtime_returns_no_cover(self, tmp_path):
        track = _episode(tmp_path)
        searcher = _Model(_THREE, can_search=True, sees=False)
        blind = _Model("", can_search=False, sees=False, look='{"choice": 1}')
        image = aicover.track_cover(
            str(track), 45 * 60, str(tmp_path / "out"),
            searchers=[searcher], lookers=[blind],
            download=_all(), probe=_episode_tags, prepare=_as_is)
        assert image == ""
        assert blind.shown == []
        assert sorted(os.listdir(tmp_path)) == ["Podcasts"]

    def test_cover_search_llm_off_skips_quietly(self, tmp_path, monkeypatch):
        monkeypatch.setenv(aicover.SWITCH_VARIABLE, "off")
        track = _episode(tmp_path)
        model = _Model(_THREE, can_search=True, sees=True, look='{"choice": 1}')
        meter = tmp_path / "meter"
        image = aicover.track_cover(
            str(track), 45 * 60, str(tmp_path / "out"),
            searchers=[model], lookers=[model],
            meter_file=str(meter), download=_all(),
            probe=_episode_tags, prepare=_as_is)
        assert image == ""
        assert model.asked == []
        assert model.shown == []


class TestFolderCoverLook:
    """Looking at a folder's unnamed image files to pick the front cover."""

    @pytest.fixture(autouse=True)
    def _enable_look(self, monkeypatch):
        monkeypatch.setenv(aicover.LOOK_SWITCH_VARIABLE, "on")

    def _images(self, tmp_path, names=("scan01.jpg", "cd.jpg", "booklet3.jpg")):
        folder = tmp_path / "The Quiet Orchard"
        folder.mkdir()
        paths = []
        for at, name in enumerate(names, start=1):
            path = folder / name
            path.write_bytes(_JPEG + bytes([at]))
            paths.append(str(path))
        return folder, paths

    def test_prompt_lists_folder_name_and_images_with_sizes(self, tmp_path):
        folder, paths = self._images(tmp_path)
        text = aicover.folder_look_prompt(str(folder), paths)
        assert "The Quiet Orchard" in text
        assert "1. scan01.jpg" in text
        assert "2. cd.jpg" in text
        assert "3. booklet3.jpg" in text
        assert '{"choice":' in text

    def test_caps_images_at_max_folder_looked_at(self, tmp_path, monkeypatch):
        monkeypatch.setattr(aicover, "MAX_FOLDER_LOOKED_AT", 2)
        folder, paths = self._images(tmp_path, ("a.jpg", "b.jpg", "c.jpg", "d.jpg"))
        model = _Model("", sees=True, look='{"choice": 2, "why": "second"}')
        picked = aicover.choose_folder_cover(
            str(folder), paths, lookers=[model], prepare=_as_is)
        assert picked == paths[1]
        assert len(model.shown[0][1]) == 2
        assert "3. c.jpg" not in model.shown[0][0]

    def test_prepared_temp_files_live_in_scratch_and_are_cleaned_up(self, tmp_path):
        folder, paths = self._images(tmp_path, ("scan01.jpg", "scan02.jpg"))
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        written = []

        def prepare(src, dest):
            written.append(dest)
            pathlib.Path(dest).write_bytes(pathlib.Path(src).read_bytes())
            return dest

        model = _Model("", sees=True, look='{"choice": 2, "why": "front"}')
        picked = aicover.choose_folder_cover(
            str(folder), paths, lookers=[model], scratch_dir=str(scratch),
            prepare=prepare)
        assert picked == paths[1]
        assert len(written) == 2
        # Prepared files went into scratch (never the input folder) and were removed.
        assert all(pathlib.Path(p).parent.parent == scratch for p in written)
        assert all(not pathlib.Path(p).exists() for p in written)
        assert list(scratch.iterdir()) == []
        assert sorted(p.name for p in folder.iterdir()) == ["scan01.jpg", "scan02.jpg"]

    def test_skips_non_vision_model_and_uses_next_in_ladder(self, tmp_path):
        folder, paths = self._images(tmp_path, ("scan01.jpg", "scan02.jpg"))
        blind = _Model("", sees=False, look='{"choice": 1}')
        sighted = _Model("", sees=True, look='{"choice": 2, "why": "cover"}')
        picked = aicover.choose_folder_cover(
            str(folder), paths, lookers=[blind, sighted], prepare=_as_is)
        assert picked == paths[1]
        assert blind.shown == []
        assert len(sighted.shown) == 1

    def test_llm_error_returns_empty_without_raising(self, tmp_path):
        folder, paths = self._images(tmp_path, ("scan01.jpg", "scan02.jpg"))
        meter = tmp_path / "meter"

        class _Failing(_Model):
            def ask(self, system, prompt, tally=None, images=()):
                raise llmclient.LlmError("server down")

        picked = aicover.choose_folder_cover(
            str(folder), paths, lookers=[_Failing("", sees=True)],
            meter_file=str(meter), prepare=_as_is)
        assert picked == ""
        assert modelmeter.gathered(str(meter))[1].requests == 0

