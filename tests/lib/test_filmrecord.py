"""A film's probe answers, kept while the file stays the same.

The probe is a real process - a script on PATH that writes down each time it
is asked - because what is pinned is how often the file is actually asked, and
that it is asked again whenever the file is not the one the answer came from.
"""

import os
import pickle

import pytest

from medialib.lib import filmrecord

pytestmark = pytest.mark.stubbed

ANSWER = '{"tracks": []}'


@pytest.fixture
def probe(tmp_path, monkeypatch):
    """A ``probe`` tool on PATH that prints ANSWER and notes each call, and
    the film it is asked about."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls"
    tool = bin_dir / "probe"
    tool.write_text("#!/bin/sh\necho x >> '%s'\nprintf '%%s' '%s'\n"
                    % (calls, ANSWER), encoding="ascii")
    tool.chmod(0o755)
    monkeypatch.setenv("PATH", "%s%s%s" % (bin_dir, os.pathsep,
                                           os.environ["PATH"]))
    film = tmp_path / "Film (1970).mkv"
    film.write_bytes(b"one film")

    class Probe:
        path = str(film)
        argv = ["probe", "-J", str(film)]

        @staticmethod
        def asked() -> int:
            try:
                return len(calls.read_text().splitlines())
            except FileNotFoundError:
                return 0

    return Probe()


def test_an_unchanged_film_is_asked_once(probe):
    first = filmrecord.output(probe.argv, probe.path)
    second = filmrecord.output(probe.argv, probe.path)
    assert first.stdout == second.stdout == ANSWER.encode()
    assert first.returncode == 0
    assert probe.asked() == 1


def test_another_probe_of_the_same_film_is_asked_on_its_own(probe):
    filmrecord.output(probe.argv, probe.path)
    filmrecord.output(["probe", "--other", probe.path], probe.path)
    assert probe.asked() == 2


def test_a_header_edited_in_place_with_its_time_put_back_is_asked_again(
        probe):
    """The edit mkvpropedit makes and the tagging hides: same size, same
    modification time. The change time is what still tells."""
    filmrecord.output(probe.argv, probe.path)
    seen = os.stat(probe.path)
    with open(probe.path, "r+b") as handle:
        handle.write(b"ONE")
    os.utime(probe.path, ns=(seen.st_atime_ns, seen.st_mtime_ns))
    filmrecord.output(probe.argv, probe.path)
    assert probe.asked() == 2


def test_a_copy_swapped_in_under_the_same_name_is_asked_again(probe):
    filmrecord.output(probe.argv, probe.path)
    copy = probe.path + ".new"
    with open(copy, "wb") as handle:
        handle.write(b"one film")
    os.replace(copy, probe.path)
    filmrecord.output(probe.argv, probe.path)
    assert probe.asked() == 2


def test_a_forgotten_film_is_asked_again(probe):
    filmrecord.output(probe.argv, probe.path)
    filmrecord.forget(probe.path)
    filmrecord.output(probe.argv, probe.path)
    assert probe.asked() == 2


def test_an_answer_read_while_the_film_changed_is_not_kept(probe, tmp_path):
    """A probe racing a write answers for neither version of the file."""
    tool = tmp_path / "bin" / "probe"
    tool.write_text("#!/bin/sh\necho x >> '%s'\necho more >> '%s'\n"
                    % (tmp_path / "calls", probe.path), encoding="ascii")
    filmrecord.output(probe.argv, probe.path)
    filmrecord.output(probe.argv, probe.path)
    assert probe.asked() == 2


def test_a_missing_tool_raises_and_keeps_nothing(probe):
    with pytest.raises(OSError):
        filmrecord.output(["no-such-probe-anywhere", probe.path], probe.path)
    assert filmrecord.of(probe.path).answers == {}


def test_a_missing_film_is_asked_every_time(probe):
    gone = probe.path + ".gone"
    filmrecord.output(["probe", gone], gone)
    filmrecord.output(["probe", gone], gone)
    assert probe.asked() == 2


def test_a_record_handed_to_another_process_answers_there(probe):
    """What a worker is handed: the record pickled, adopted, and read from
    without the film being asked again."""
    filmrecord.output(probe.argv, probe.path)
    handed = pickle.loads(pickle.dumps(filmrecord.of(probe.path)))
    filmrecord.reset()
    filmrecord.adopt(handed)
    answer = filmrecord.output(probe.argv, probe.path)
    assert answer.stdout == ANSWER.encode()
    assert probe.asked() == 1


def test_a_record_handed_over_for_a_changed_film_is_not_believed(probe):
    filmrecord.output(probe.argv, probe.path)
    handed = pickle.loads(pickle.dumps(filmrecord.of(probe.path)))
    filmrecord.reset()
    with open(probe.path, "ab") as handle:
        handle.write(b" and more")
    filmrecord.adopt(handed)
    filmrecord.output(probe.argv, probe.path)
    assert probe.asked() == 2


def test_the_record_of_a_changed_film_is_empty(probe):
    filmrecord.output(probe.argv, probe.path)
    assert filmrecord.of(probe.path).answers
    with open(probe.path, "ab") as handle:
        handle.write(b" and more")
    assert filmrecord.of(probe.path).answers == {}


def test_the_oldest_records_go_first(probe, monkeypatch, tmp_path):
    monkeypatch.setattr(filmrecord, "MAX_RECORDS", 2)
    films = []
    for number in range(3):
        film = tmp_path / ("film%d.mkv" % number)
        film.write_bytes(b"x")
        films.append(str(film))
        filmrecord.output(["probe", str(film)], str(film))
    assert filmrecord.of(films[0]).answers == {}
    assert filmrecord.of(films[2]).answers


def test_the_runner_shape_probes_its_last_argument(probe):
    filmrecord.run(probe.argv, stdout=None, stderr=None)
    filmrecord.output(probe.argv, probe.path)
    assert probe.asked() == 1
