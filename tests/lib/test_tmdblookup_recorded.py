"""The matching ladder against the real catalogue, on the folders a real
library has.

Everything in `test_tmdblookup.py` stands TMDb in with a catalogue written for
the case: the film answers to the titles the case says it answers to, and the
rule is asked one question at a time. That is the right shape for a rule and it
cannot answer the other question - whether the real service, asked the way this
module asks it, hands back something the rule settles. TMDb's own alternative
titles, which countries it recorded a release in, what its search puts first for
a common word, and how many of a silent film's many spellings it actually
carries are all facts about the service, and no fixture written here can
discover them.

So these cases are recorded rather than written. `REGEN=1` runs them against the
live service and commits three things: the layout that went in, the layout that
came out, and every request and response in between. A plain run replays the
recording, which makes it a fast, offline, deterministic case that is
nevertheless about real data - and re-recording is how a drift on TMDb's side
becomes a diff to read rather than a library quietly renamed wrong.

**These are the one place in the suite that names real films**, and on purpose:
the point is the catalogue, and an invented title is not in it. They are all
silent-era work long in the public domain, chosen because the era is where the
matching is hardest - a film with a dozen spellings across four languages, a
production year and a release year that disagree, restorations fifty years apart
that a library keeps side by side, and titles short enough to collide with
everything made since. Every other case in the suite uses invented names and
should stay that way.

The `.tree` fixtures are not typed. `REGEN=1` writes them from the real layouts,
the way `clean-folder-structure -s` writes its own, and `_CASES` below is their
specification - which the first case here asserts they still match, so the two
cannot drift.

**Replaying needs no key and no network**, which is why these are an ordinary
part of the default run rather than an opt-in tier like `media`: CI has no TMDb
key and does not need one, and a question the recording cannot answer fails the
case rather than going online. Only `REGEN=1` talks to the service, and it
refuses to run without a key rather than recording a file of nulls.

Before anything has been recorded the cases SKIP - a branch where nobody has
run REGEN yet is not a defect, and reddening a run that has no key to fix it
with helps nobody. A recording with holes in it FAILS, because that is a
committed fixture gone missing. The layouts that went IN are committed and are
asserted either way, so this file never goes entirely quiet.
"""

from __future__ import annotations

import json
import os

import pytest

from medialib.lib import tmdblookup
from medialib.lib.safety import SkipLog
from tests import blackbox, treefiles

pytestmark = pytest.mark.fs

_FIXTURES = blackbox.DATA / "movieMatching"
_CASSETTE = _FIXTURES / "tmdb.json"
_REGEN = bool(os.environ.get("REGEN", ""))

# How to record: on a host with network and a key,
#
#     REGEN=1 tmdbApiKey=... pytest tests/lib/test_tmdblookup_recorded.py
#
# and read the diff. A fixture that "needs" regenerating to go green is a bug
# until the diff says otherwise - the same rule the recorded command pages under
# `tests/data/cliContract` are kept by.
_HOW_TO_RECORD = (
    "REGEN=1 tmdbApiKey=<key> pytest tests/lib/test_tmdblookup_recorded.py")

# Replaying needs neither, so the recorded cases are an ordinary part of the
# default run wherever the recording is on disk - which is the point of
# recording rather than gating: CI has no key and does not need one.
_NOT_RECORDED_YET = (
    "these cases have not been recorded yet. They replay a recording of the "
    "live service, and nothing here has one:\n    " + _HOW_TO_RECORD
    + "\non a host with network, curl and GNU `tree`. What it writes is "
      "committed, and every run after that is offline.")


# Each case is a layout a library really has, named for what makes it hard. The
# films are real and the mess around them is the mess a disk accumulates: what a
# catalogue is asked here is exactly what it would be asked on a Saturday.
_CASES = {
    # The baseline, and the one that must stay cheap: a folder already spelled
    # the way the catalogue spells it, costing one search and one detail.
    "plainFolder": [
        "The Kid (1921)/The Kid (1921).mkv",
    ],
    # One letter wrong in the folder and the file, which no fold reaches. The
    # typo rung is built for exactly this and never gets the chance: TMDb
    # answers "Caligary" with no results at all, year or no year, so nothing
    # comes back for it to recognise and the folder is reported unmatched.
    "oneLetterWrong": [
        "The Cabinet of Dr. Caligary (1920)/"
        "The Cabinet of Dr. Caligary (1920).mkv",
    ],
    # The German original where the catalogue's primary title is the English
    # one - the alternative-titles document is the only thing that can join them,
    # and it is fetched with the candidate rather than asked for separately.
    "theOriginalTitle": [
        "Das Cabinet des Dr. Caligari (1920)/"
        "Das Cabinet des Dr. Caligari (1920).mkv",
    ],
    # The franchise heading a library repeats on the folder above, with the
    # subtitle alone on the folder itself. Neither half names the film; together
    # they do.
    "theFolderAbove": [
        "Nosferatu/Eine Symphonie des Grauens (1922)/"
        "Eine Symphonie des Grauens (1922).mkv",
    ],
    # One film, four restorations, kept side by side the way a collector keeps
    # them. Every one of them is the same id and a different edition, and the
    # year on two of them is the restoration's rather than the film's.
    "severalEditions": [
        "Metropolis (1927)/Metropolis (1927) 2010 Restoration.mkv",
        "Metropolis (1927)/Metropolis (1927) 1984 Giorgio Moroder.mkv",
        "Metropolis (1927)/Metropolis (1927) 2001 Restoration 1080p.mkv",
        "Metropolis (1927)/Metropolis (1927).mkv",
    ],
    # Subtitles beside the film, in the three shapes a library has them: bare,
    # language-tagged, and forced. They follow the film onto its new name or
    # they stop being its subtitles.
    "subtitlesBeside": [
        "The General (1926)/The General (1926).mkv",
        "The General (1926)/The General (1926).srt",
        "The General (1926)/The General (1926).eng.srt",
        "The General (1926)/The General (1926).eng.forced.srt",
        "The General (1926)/The General (1926).ger.sdh.srt",
    ],
    # The filler a local library writes in front of a name, over a title short
    # enough that a search for it alone returns a century of other films.
    "fillerInFront": [
        "Movie - The General (1926)/Movie - The General (1926) 1080p.mkv",
    ],
    # A long film split across two discs, which must come back as one folder with
    # a stacking token and not as two films.
    "splitInParts": [
        "Intolerance (1916)/Intolerance (1916) - Part 1.mkv",
        "Intolerance (1916)/Intolerance (1916) - Part 2.mkv",
    ],
    # An accent the folder could not reach, and the same film's transliterated
    # spelling on the file - two readings of one title that must not read as two
    # films.
    "anAccentDropped": [
        "Haxan (1922)/Häxan (1922).mkv",
    ],
    # A transliteration, which is a whole different alphabet's worth of
    # disagreement: the catalogue may hold the film under the English title, the
    # transliterated one, or both.
    "aTransliteration": [
        "Bronenosets Potyomkin (1925)/Bronenosets Potyomkin (1925).mkv",
    ],
    # Punctuation a filesystem and a catalogue spell differently: an
    # abbreviation's point, a comma, and an exclamation mark.
    "punctuation": [
        "Sherlock Jr (1924)/Sherlock Jr (1924).mkv",
        "Steamboat Bill Jr (1928)/Steamboat Bill Jr (1928).mkv",
        "Safety Last (1923)/Safety Last (1923).mkv",
    ],
    # No year at all, on a title the catalogue holds in two languages. The year
    # is what most of the rule narrows on, and this is what is left when there
    # is none.
    "noYearAtAll": [
        "Le Voyage dans la Lune/Le Voyage dans la Lune.mkv",
    ],
    # A year a digit wrong on the folder, with the file carrying the right one -
    # the disagreement the year rung exists for.
    "aYearADigitWrong": [
        "Man with a Movie Camera (1939)/Man with a Movie Camera (1929).mkv",
    ],
    # A title every decade since has reused. The rule must either settle on the
    # silent one from the year or name nothing; what it must not do is tag the
    # folder with a talkie's id.
    "aTitleReusedSince": [
        "The Phantom of the Opera (1925)/The Phantom of the Opera (1925).mkv",
    ],
    # Bonus material beside the feature, which is not a film and must not be
    # asked about or renamed as one.
    "bonusBeside": [
        "The Gold Rush (1925)/The Gold Rush (1925).mkv",
        "The Gold Rush (1925)/Behind The Scenes/A restoration diary.mkv",
        "The Gold Rush (1925)/Trailers/Re-release trailer.mkv",
    ],
}


# --- the recording -----------------------------------------------------------

def _key(url: str, params) -> str:
    """One request as a line of text, the API key left out of it.

    The key is the one part of a request that must never reach a committed
    fixture, and leaving it out costs nothing: it is the same value on every
    call, so it tells two requests apart no better than the host name does.
    """
    rest = ["%s=%s" % pair for pair in params if pair[0] != "api_key"]
    return url + "?" + "&".join(rest)


def _replay(recording):
    """A stand-in for `_curl` that answers out of ``recording``.

    A question the recording has no answer to fails the case rather than
    reaching the network: a replay that silently went online would be a
    different test on every host, and one that silently answered "no result"
    would report a matching change as a catalogue change.
    """
    def curl(url, params):
        key = _key(url, params)
        if key not in recording:
            raise AssertionError(
                "the recording has no answer for %s - the ladder asked "
                "something it did not ask when this was recorded, which is a "
                "change to re-record with REGEN=1 and read as a diff" % key)
        return recording[key]
    return curl


def _record(recording):
    """`_curl` itself, keeping every question and answer as it goes."""
    real = tmdblookup._curl

    def curl(url, params):
        body = real(url, params)
        recording[_key(url, params)] = body
        return body
    return curl


def _recorded() -> tuple:
    """(what the recording put on disk, what is missing from it).

    All three halves, because they are one artifact: the answers, the layout
    each case came out as, and what each case refused to name. A checkout has
    all of them or none.
    """
    wanted = [_CASSETTE]
    for name in _CASES:
        wanted += [_FIXTURES / ("%s after.tree" % name), _reports_of(name)]
    return ([path for path in wanted if path.exists()],
            [path for path in wanted if not path.exists()])


@pytest.fixture(scope="session")
def recording():
    """The committed answers, or the dict a REGEN run fills and writes.

    Nothing recorded at all SKIPS, and a recording with holes in it FAILS. The
    two states are different: the first is a branch where nobody has run REGEN
    yet, which is not a defect and should not redden a run that has no key to
    fix it with; the second is a committed fixture that has gone missing, which
    is exactly the thing this suite refuses to let pass quietly.

    Either way the layouts that went IN are still asserted - they are committed
    and need no recording - so this file never goes entirely silent.
    """
    if _REGEN:
        if not os.environ.get("tmdbApiKey"):
            pytest.fail(
                "REGEN=1 records against the live service and tmdbApiKey is "
                "not set. Without it every call fails and the recording would "
                "be a file of nulls, committed as though it were answers.")
        kept: dict = {}
        yield kept
        answered = [body for body in kept.values() if body]
        if not answered:
            pytest.fail(
                "REGEN=1 recorded %d request(s) and not one answer. Nothing "
                "has been written: curl reached nothing, or the key was "
                "refused." % len(kept))
        _FIXTURES.mkdir(parents=True, exist_ok=True)
        _CASSETTE.write_bytes(
            (json.dumps(dict(sorted(kept.items())), indent=1,
                        ensure_ascii=False) + "\n").encode("utf-8"))
        return
    here, missing = _recorded()
    if not here:
        pytest.skip(_NOT_RECORDED_YET)
    if missing:
        pytest.fail(
            "the recording is incomplete - %d of its %d files are missing, "
            "starting with %s. A recording is one artifact; re-record it "
            "whole:\n    %s"
            % (len(missing), len(here) + len(missing),
               missing[0].name, _HOW_TO_RECORD))
    yield json.loads(_CASSETTE.read_text(encoding="utf-8"))


def _tag(root, recording, monkeypatch):
    """The tagging pass over one case's folder, and the reports it filed.

    Through `monkeypatch` for both the stand-in and the working directory: a
    module attribute put back by hand is put back only when the case passes,
    and a case that failed would leave the next one talking to the network.
    """
    monkeypatch.setattr(tmdblookup, "_curl",
                        (_record if _REGEN else _replay)(recording))
    monkeypatch.chdir(root)
    ambiguous: list = []
    unmatched: list = []
    tmdblookup.tag_plex_ids(".", lambda _line: None, SkipLog(), recursive=True,
                            ambiguous=ambiguous, unmatched=unmatched)
    return {"ambiguous": sorted(reason for _p, reason, _n in ambiguous),
            "unmatched": sorted(unmatched)}


class _Case:
    """One case: what it is called, where it ran, and what it could not name."""

    def __init__(self, name, root, reports):
        self.name = name
        self.root = root
        self.reports = reports


@pytest.fixture(params=sorted(_CASES))
def case(request, recording, monkeypatch, tmp_path):
    """One case's layout, tagged - against the service under REGEN, out of the
    recording otherwise.

    Under REGEN the two `.tree` fixtures are written here, by `tree` itself
    rather than by a renderer of this file's own: the committed answer and the
    command's own `-s` artifacts are then the same bytes, which is the whole
    reason the format is what it is.
    """
    name = request.param
    root = tmp_path / name
    if _REGEN:
        for relative in _CASES[name]:
            full = root / relative
            full.parent.mkdir(parents=True, exist_ok=True)
            full.touch()
        _record_tree(name, "before", root)
    else:
        monkeypatch.setenv("tmdbApiKey", "apikey")
        treefiles.reconstruct(_FIXTURES / ("%s before.tree" % name), tmp_path)
    reports = _tag(root, recording, monkeypatch)
    if _REGEN:
        _record_tree(name, "after", root)
        _reports_of(name).write_bytes(
            (json.dumps(reports, indent=1, ensure_ascii=False) + "\n")
            .encode("utf-8"))
    return _Case(name, root, reports)


def _reports_of(name):
    return _FIXTURES / ("%s reports.json" % name)


def _files(entries) -> list:
    """The leaves of a listing - what `_CASES` names, a listing's folders being
    implied by the paths rather than written out."""
    return sorted(entry for entry in entries
                  if not any(other.startswith(entry + "/")
                             for other in entries))


def _record_tree(name: str, half: str, root) -> None:
    _FIXTURES.mkdir(parents=True, exist_ok=True)
    (_FIXTURES / ("%s %s.tree" % (name, half))).write_bytes(
        treefiles.snapshot(root).encode("utf-8"))


# --- what the cases claim ----------------------------------------------------

class TestTheLayoutsThatWentIn:
    """The half that needs no recording, and so is asserted on every host and
    in every run - including one that has never recorded anything."""

    @pytest.mark.parametrize("name", sorted(_CASES))
    def test_the_recorded_layout_is_the_one_that_went_in(self, name):
        """The `.tree` fixture and `_CASES` are one specification written twice,
        and a case where they disagree is testing a layout nobody wrote down."""
        recorded = treefiles.paths(_FIXTURES / ("%s before.tree" % name))
        assert _files(recorded) == sorted(_CASES[name])

    @pytest.mark.parametrize("name", sorted(_CASES))
    def test_and_it_rebuilds_as_the_layout_it_records(self, name, tmp_path):
        """The fixture is only a specification while it reconstructs: every
        case below starts by rebuilding one of these, and a rendering that
        parsed differently would quietly test a different library."""
        treefiles.reconstruct(_FIXTURES / ("%s before.tree" % name), tmp_path)
        assert treefiles.paths_on_disk(tmp_path / name) == treefiles.paths(
            _FIXTURES / ("%s before.tree" % name))


class TestTheRecordedLayouts:
    """Each case is a library that went in and a library that came out."""

    def test_the_library_it_left_is_the_recorded_one(self, case):
        assert treefiles.paths_on_disk(case.root) == treefiles.paths(
            _FIXTURES / ("%s after.tree" % case.name))

    def test_what_it_could_not_name_is_the_recorded_one(self, case):
        """The other half of the answer, and for these films the interesting
        half: a folder left alone is not a failure here, it is a fact about the
        catalogue, and a case that starts or stops refusing one is exactly what
        a recorded run is for."""
        assert case.reports == json.loads(
            _reports_of(case.name).read_text(encoding="utf-8"))

    def test_a_second_run_changes_nothing(self, case, recording, monkeypatch):
        """A tagged library is a fixed point. This is the claim that catches a
        rule that renames on every pass - which a library notices as a run that
        never finishes rather than as a wrong name."""
        settled = treefiles.paths_on_disk(case.root)
        _tag(case.root, recording, monkeypatch)
        assert treefiles.paths_on_disk(case.root) == settled
