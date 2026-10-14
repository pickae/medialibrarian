"""Which film in one list is a film from another: Plex's library against the
films a Letterboxd account has logged.

Neither side carries an id the other is sure to have. A Letterboxd export
names a film by its title and year alone, and a Plex film's ids are only there
when its agent found them - so the title is what most films are matched by, and
a title is written differently on the two sides for all the usual reasons: an
accent, a colon against a dash, "Part II" against "Part 2", a leading "The".
What counts as the same title is :mod:`medialib.lib.titlematch`, the same fold
the TMDb lookup matches a folder against a catalogue with.

The evidence is asked strongest first, and the first rung that answers at all
is the answer:

* an id both sides carry - TMDb's or IMDb's;
* a title whose keys meet, in the same year;
* the same title once folded, a year either side - a festival year written
  where the release year was meant (:data:`medialib.lib.tmdblookup.NEAR_YEARS`).
  Only the fold and not every key, because a year apart is already one
  disagreement: "Hound" and "The Hound" a year apart are two films;
* a title whose keys meet where one side has no year to compare;
* in the same year, one side's title being ONE PART of the other's - the same
  three shapes the TMDb lookup accepts a catalogue's name by: what stands
  before the first " - " or ": " of it, or after the last ("Sandfall" for
  "Sandfall: Part One", "Faithful" for "The Ember Saga: Faithful"); or the whole of it
  standing at the start or the end of the other, where it has three words
  or more besides its articles to anchor on ("Life of a Lantern" at the end
  of "Moss Company's Life of a Lantern"). Three and not the lookup's two, because
  the lookup asks this only of the films a search for the title came back
  with, and this is asked of a whole year of a library: "Copper Rain" ends
  "After the Copper Rain", and the two are different films;
* a title one slip of the hand away (:func:`titlematch.one_slip_apart`), in
  the same year. Asked last, because it is the one rung that says somebody
  wrote the title wrong, and never across a year, where "Spirits" and "Spirity"
  are a slip apart and two films.

Two things veto a pair on every rung but the first: both carrying an id of one
scheme that is not the same, and the two titles being written in different
scripts. The fold reads only Latin letters, so a Japanese title folds to the
handful of characters in it that look Latin - which meet a Japanese title
written the same way, and nothing in Latin letters that they should.

Two films answering on the same rung are two, and are not chosen between -
unless exactly one of them is written identically once folded. A pair that is
left is AMBIGUOUS, and what the caller does with that is the caller's to say.

And a match weaker than an id holds only both ways - see
:meth:`FilmIndex.find_from`.
"""

from __future__ import annotations

import functools
from typing import NamedTuple

from medialib.lib import titlematch
from medialib.lib.tmdblookup import NEAR_YEARS

__all__ = ["Film", "FilmIndex", "Found"]


class Film(NamedTuple):
    """One film as one side writes it."""

    title: str
    year: str = ""          # "" when the side does not say
    tmdb: str = ""
    imdb: str = ""
    # Other titles the same side holds the film under - Plex's original title.
    aliases: tuple = ()
    # What the side says about watching it, none of which matching asks: the
    # rating out of 10 (Letterboxd's half stars doubled, Plex's own scale),
    # 0 for none; the days it was watched, YYYY-MM-DD, oldest first; and the
    # day Letterboxd was first told it was watched at all, "" when not known.
    rating: int = 0
    dates: tuple = ()
    logged: str = ""

    @property
    def titles(self) -> tuple:
        return (self.title, *(alias for alias in self.aliases
                              if alias and alias != self.title))

    def __str__(self) -> str:
        return "%s (%s)" % (self.title, self.year) if self.year else self.title


class Found(NamedTuple):
    """What a lookup came back with: the one film it settled on, or the
    films it could not choose between, and which rung answered."""

    film: Film | None = None
    ambiguous: tuple = ()
    how: str = ""

    @property
    def matched(self) -> bool:
        return self.film is not None


def _year_gap(one: str, other: str) -> int | None:
    """How many years apart two films are, or None when either has no year."""
    if not (one.isdigit() and other.isdigit()):
        return None
    return abs(int(one) - int(other))


def _different_ids(one: Film, other: Film) -> bool:
    """Whether both films carry an id of one scheme and it is not the same -
    which makes them two films however alike their titles are."""
    return any(mine and theirs and mine != theirs
               for mine, theirs in ((one.tmdb, other.tmdb),
                                    (one.imdb, other.imdb)))


class _Title(NamedTuple):
    """One written title, with everything a comparison asks of it - worked
    out once, because a library is compared against every film it is not."""

    latin: bool
    fold: str
    keys: frozenset
    parts: frozenset        # the keys of its first and its last segment
    anchored: bool          # three words or more besides its articles


# How many words besides its articles a whole title needs to be recognised at
# the start or the end of another one.
_ANCHORED = 3


@functools.cache
def _read(title: str) -> _Title:
    head, _rest = titlematch.leading_segment(title)
    parts = frozenset().union(*(titlematch.title_keys(part)
                                for part in (head, titlematch.last_segment(title))
                                if part))
    fold = titlematch.normalize_title(title)
    return _Title(titlematch.in_latin_script(title), fold,
                  titlematch.title_keys(title), parts,
                  len([word for word in fold.split()
                       if word not in titlematch.ARTICLES]) >= _ANCHORED)


def _titles(film: Film) -> tuple:
    return tuple(_read(title) for title in film.titles if title)


def _edges(fold: str) -> list:
    """Every run of words a folded title starts or ends with, short of the
    whole of it, that would be anchored as a title of its own."""
    words = fold.split()
    runs = [" ".join(words[:cut]) for cut in range(1, len(words))] \
        + [" ".join(words[cut:]) for cut in range(1, len(words))]
    return [run for run in runs
            if len([word for word in run.split()
                    if word not in titlematch.ARTICLES]) >= _ANCHORED]


class FilmIndex:
    """A list of films, indexed to be asked "which of you is this one?"."""

    def __init__(self, films) -> None:
        self.films: list[Film] = list(films)
        self._titles = [_titles(film) for film in self.films]
        self._by_id: dict[str, list[int]] = {}
        self._by_key: dict[tuple, list[int]] = {}
        self._by_year: dict[str, list[int]] = {}
        # For the part-of-the-name rung, so it is looked up and not searched
        # for: the keys of each title's segments, each anchored title's fold
        # whole, and every anchored run of words a title starts or ends with.
        self._by_part: dict[tuple, list[int]] = {}
        self._by_fold: dict[tuple, list[int]] = {}
        self._by_edge: dict[tuple, list[int]] = {}
        # For the slip: a year's titles by the length of their fold.
        self._by_length: dict[tuple, list[int]] = {}
        for index, film in enumerate(self.films):
            for scheme, value in (("tmdb", film.tmdb), ("imdb", film.imdb)):
                if value:
                    self._by_id.setdefault("%s:%s" % (scheme, value),
                                           []).append(index)
            for key in _keys(self._titles[index]):
                self._by_key.setdefault(key, []).append(index)
            self._by_year.setdefault(film.year, []).append(index)
            for title in self._titles[index]:
                for key in title.parts:
                    self._by_part.setdefault((title.latin, key), []).append(index)
                if title.anchored:
                    self._by_fold.setdefault((title.latin, title.fold),
                                             []).append(index)
                for edge in _edges(title.fold):
                    self._by_edge.setdefault((title.latin, edge), []).append(index)
                self._by_length.setdefault((film.year, len(title.fold)),
                                           []).append(index)

    def __len__(self) -> int:
        return len(self.films)

    def find(self, film: Film) -> Found:
        """The film in this index that ``film`` is, by the strongest evidence
        that answers at all."""
        by_id = {index for scheme, value in (("tmdb", film.tmdb),
                                             ("imdb", film.imdb)) if value
                 for index in self._by_id.get("%s:%s" % (scheme, value), ())}
        if by_id:
            return self._settle(film, by_id, "id")

        mine = _titles(film)
        meeting = {index for key in _keys(mine)
                   for index in self._by_key.get(key, ())
                   if not _different_ids(film, self.films[index])}
        gaps = {index: _year_gap(film.year, self.films[index].year)
                for index in meeting}
        folds = _folds(mine)
        for how, wanted in (
                ("title", lambda index, gap: gap == 0),
                ("title, a year apart",
                 lambda index, gap: gap is not None and 0 < gap <= NEAR_YEARS
                 and bool(folds & _folds(self._titles[index]))),
                ("title, no year to compare", lambda index, gap: gap is None)):
            rung = {index for index, gap in gaps.items() if wanted(index, gap)}
            if rung:
                return self._settle(film, rung, how)

        part = self._one_part_of_the_name(film)
        if part:
            return self._settle(film, part, "title, one part of the name")
        return self._settle(film, self._misspelt(film), "title misspelt")

    def find_from(self, film: Film, origin: FilmIndex) -> Found:
        """:meth:`find`, where the film it settles on would pick ``film`` back
        out of ``origin`` - the list ``film`` comes from - or nothing.

        A reading weaker than an id is only as good as the films it was not
        asked to choose between. A library holding a 2020 film and a 2021 film
        under one title has the 2021 one matching the other side's 2021 film
        exactly, and the 2020 one matching it too, a year apart; asked from
        the other side, the 2021 film is the answer, and the 2020 one is not
        the film. Copies of one film come back together, as two films equally
        good, and each of them is it. Two films this side cannot choose
        between are left as they are, for the caller to answer.
        """
        found = self.find(film)
        if found.film is None or found.how == "id":
            return found
        back = origin.find(found.film)
        if back.film == film or film in back.ambiguous:
            return found
        return Found()

    def _one_part_of_the_name(self, film: Film) -> set:
        """The films from ``film``'s own year that one of its titles is one
        part of, or that are one part of it."""
        if not film.year.isdigit():
            return set()
        found: set = set()
        for title in _titles(film):
            script = title.latin
            for key in title.parts:
                found.update(self._by_key.get((script, key), ()))
            for key in title.keys:
                found.update(self._by_part.get((script, key), ()))
            if title.anchored:
                found.update(self._by_edge.get((script, title.fold), ()))
            for edge in _edges(title.fold):
                found.update(self._by_fold.get((script, edge), ()))
        return {index for index in found
                if self.films[index].year == film.year
                and not _different_ids(film, self.films[index])}

    def _misspelt(self, film: Film) -> set:
        """The films from ``film``'s own year a slip of the hand away.

        Only that year's titles within a character of this one's length are
        looked at - a slip changes the length by one at most. A film with no
        year is not asked about at all: with neither the year nor the spelling
        to vouch for it, a slip is too much of a guess. What counts as a slip
        is titlematch's rule, asked of the folds - which fold to themselves -
        and never across scripts, for the reason the keys are not.
        """
        if not film.year.isdigit():
            return set()
        found = set()
        for one in _titles(film):
            near = {index for length in range(len(one.fold) - 1, len(one.fold) + 2)
                    for index in self._by_length.get((film.year, length), ())}
            for index in near:
                if index not in found and not _different_ids(film, self.films[index]) \
                        and any(one.latin == theirs.latin
                                and titlematch.one_slip_apart(one.fold, theirs.fold)
                                for theirs in self._titles[index]):
                    found.add(index)
        return found

    def _settle(self, film: Film, indices: set, how: str) -> Found:
        """One film out of a rung's answers, or the ambiguity it leaves."""
        if not indices:
            return Found()
        if len(indices) > 1:
            mine = _folds(_titles(film))
            identical = [index for index in indices
                         if mine & _folds(self._titles[index])]
            if len(identical) != 1:
                return Found(ambiguous=tuple(self.films[index]
                                            for index in sorted(indices)), how=how)
            indices = set(identical)
        return Found(film=self.films[min(indices)], how=how)


def _keys(titles: tuple) -> frozenset:
    """Every key the titles fold to, each with the script it was written in."""
    return frozenset((title.latin, key) for title in titles for key in title.keys)


def _folds(titles: tuple) -> frozenset:
    """Each title folded whole, with the script it was written in - what
    "written identically" means here."""
    return frozenset((title.latin, title.fold) for title in titles if title.fold)
