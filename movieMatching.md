# The films TMDb matches badly, and what a second catalogue would have to be

`ingest-movies` asks TheMovieDB what a folder holds and writes the answer as an
`{imdb-...}` tag. For ordinary features that works, and the ladder in
`medialib/lib/tmdblookup.py` is mostly about the ways a *name* can disagree.
Eight kinds of film match far worse than the rest, and this is an account of
**why** each one does - which is not the same answer in each case, and in most of
them is not the catalogue's fault at all.

The short version: **five gates account for nearly all of it, three of them are
ours, and only one of the eight categories is really a coverage problem.** A
second provider is worth having, but it is the last thing to reach for, not the
first.

---

## The five gates

### Gate 1 - a folder with no year is never asked about at all

`read_folder` ([tmdblookup.py:136](medialib/lib/tmdblookup.py:136)) answers
empty strings for a name with no `(YYYY)` in it, and the walk
([tmdblookup.py:1478](medialib/lib/tmdblookup.py:1478)) does

```python
base, tag, title, year, edition = read_folder(folder.name)
if not base or not is_film_folder(folder):
    continue
```

That `continue` is silent. The folder is not looked up, not renamed, and **not
put in the unmatched report either** - so it does not appear in any of the five
reports a run writes. From the outside it looks like the catalogue had no
answer, when in fact nothing was ever asked. `noYearAtAll` in
`tests/data/movieMatching/` is the only case of the fifteen that sends no
request at all and files nothing under either report.

This is the single largest effect, and it lands hardest on exactly the
categories below: a standup special, a concert disc, a documentary and a silent
short are the four things most likely to arrive as a bare title with no year on
it.

### Gate 2 - only `/search/movie` is ever asked

`_search` ([tmdblookup.py:715](medialib/lib/tmdblookup.py:715)) hits
`/search/movie` and nothing else. Anything TMDb types as television is
unreachable, whatever it is called. That is most TV movies, a good share of
concert films, and nearly every anime special and OVA.

### Gate 3 - the year has to be nearly right, twice

The year is used twice: as a search *filter*
([tmdblookup.py:712](medialib/lib/tmdblookup.py:712)) and then as the narrowing
that settles a candidate, with `NEAR_YEARS = 1`
([tmdblookup.py:180](medialib/lib/tmdblookup.py:180)).

One year of slack is right for a feature, where the gap is a festival year
against a release year. It is too tight for:

* a concert recorded one year and released two or three later - the disc's
  spine usually carries the **recording** year;
* an anime film released in Japan one year and internationally one to three
  years later, where a library often carries whichever date its source named;
* a TV movie, where the air date and whatever TMDb recorded as a release date
  routinely differ;
* a silent film, where the production year, the premiere year and the year the
  surviving restoration is dated can span a decade, and different catalogues
  pick different ones.

`_settle_the_year` ([tmdblookup.py:295](medialib/lib/tmdblookup.py:295)) already
handles the case where a folder and its own files disagree. It does not help
when the folder and the *catalogue* disagree and the files all agree with the
folder.

### Gate 4 - eight candidates, ordered by popularity

`MAX_CANDIDATES = 8` ([tmdblookup.py:186](medialib/lib/tmdblookup.py:186)), and
`_worth_asking` ([tmdblookup.py:723](medialib/lib/tmdblookup.py:723)) already
does the important thing - it puts rows whose titles key-match ahead of TMDb's
popularity order, which is what its own docstring says it exists for. What it
cannot fix is a page that never contained the right film: TMDb returns 20 rows
per page and only the first page is read, so an obscure film with a common-word
title can be on page two and is then simply absent.

### Gate 5 - a misspelling reaches nothing, so the typo rung never runs

The one gate that is the catalogue's.

`_a_letter_wrong` ([tmdblookup.py:418](medialib/lib/tmdblookup.py:418)) reads a
folder spelled a letter wrong, and it costs no request of its own because it
does not make the near miss - it recognises one TMDb's own search already made.
That is the assumption, and for "The Cabinet of Dr. Caligary" it does not hold:
the search answers **zero results**, with the year and without it
(`oneLetterWrong` in `tests/data/movieMatching/tmdb.json`). Nothing comes back, so
there is no row for the rung to measure and the folder is reported unmatched.

TMDb's search tolerates less than a search box suggests, and the rung is only as
good as what reaches it. Whatever fixes this is a *second* query rather than a
better rule - which is the one place in this document where a provider question
is the honest answer to a naming problem.

---

## The eight categories

Ordered by how much of the problem is ours rather than the catalogue's.

### Documentaries - almost entirely ours, and mostly already fixed

TMDb's documentary coverage is good. The disagreement is naming, and the
existing fold already does most of it: `LABEL_WORDS`
([titlematch.py:340](medialib/lib/titlematch.py:340)) carries `documentary`,
`doku`, `dokumentation`, `docu`, `documentaire`, `documental` and
`documentario`, so a folder's "…​ Documentary" is dropped as the label it is.

What is left is Gate 1 (a documentary folder often has no year) and series-style
naming - a broadcaster's strand name in front of the film's own, which
`_last_segment` ([titlematch.py:916](medialib/lib/titlematch.py:916)) already
offers as a query. **Verdict: no new provider. Fix Gate 1.**

### German films - already handled better than the rest

This one is worth stating plainly because it is the category most likely to be
blamed and least likely to be at fault. `titlematch` folds umlauts both ways
(dropped as a transliteration drops them, *and* spelled out the way German
writes them when a keyboard cannot reach them), reads `und` as a conjunction
([titlematch.py:298](medialib/lib/titlematch.py:298)), carries the German
articles ([titlematch.py:310](medialib/lib/titlematch.py:310)), and reads `Teil`
and `Kapitel` as number labels
([titlematch.py:360](medialib/lib/titlematch.py:360)). A dash written as
nothing - which is how German compounds arrive - is one of the separator
readings.

TMDb's German coverage is genuinely good, and its `alternative_titles` document
carries the German release title for most international films.

What is left is real but narrow: a German **TV film** hits Gate 2, and the
ß/ss pair is a fold that exists but is worth a recorded case. **Verdict: no new
provider.**

### Indie films - a paging problem, not a coverage problem

TMDb's long tail is good; the trouble is Gate 4 and the fact that a small film
often shares its title with a large one. The fix is on our side: ask for more
than one page when the first page produced no key-match, or fall back to a
title-exact search. **Verdict: no new provider.** Raising `MAX_CANDIDATES`
alone does not help - the right film was not in the rows to begin with.

### Silent-era films - a naming problem and a year problem

TMDb's silent coverage is better than its reputation, but the era stresses
everything at once: a film with an original title, an export title, a modern
English title and a restoration title; a year that can mean four things; and
frequently no year on disk at all. Short titles also fall under
`MIN_TYPO_LENGTH = 6` ([titlematch.py:796](medialib/lib/titlematch.py:796)) and
so are refused the typo rung.

Gates 1 and 3 are most of it. What TMDb is genuinely weaker at is **shorts** -
a one-reel film may be absent, or present without an IMDb id, which
`_that_can_answer` ([tmdblookup.py:454](medialib/lib/tmdblookup.py:454)) then
sets aside. **Verdict: fix Gates 1 and 3 first; a second catalogue helps only
for shorts.**

### Standup comedy specials - Gates 1, 2 and 3 together

TMDb carries most streaming-era specials as movies, so coverage is not the
problem. Naming is: a library writes `Comedian - Title`, `Comedian: Title` or
`Title (Comedian)`, and TMDb has variously the bare title or the performer's
name glued on. `_last_segment` and `_with_the_folder_above`
([tmdblookup.py:1608](medialib/lib/tmdblookup.py:1608)) already offer both
readings, which is most of the answer.

Older specials - anything shot for television before streaming - hit Gate 2, and
very few of any era carry a year on disk. **Verdict: fix Gates 1 and 2.**

### TV movies - Gate 2, squarely

This is a structural miss rather than a matching one. A film TMDb types as
television cannot be found by a `/search/movie` call no matter how it is
spelled. **Verdict: `/search/tv` is the fix, and it is a small one** - the
shape of a TV result is close enough to a movie result that `_Candidate` needs
no new fields, though the IMDb id sits on the series rather than the episode and
the tag written today would need thought.

### Manga / anime films - naming, and a second catalogue would genuinely help

Two distinct problems.

*Naming*: an anime film has a Japanese title, a romanised title, an English
title, and often a franchise prefix with a numbered entry - all four in play at
once. The existing ladder handles more of this than it gets credit for
(`_last_segment`, `_arabic_spelling`, `_unlabelled`, and `NUMBER_LABELS`
carrying `episode`), and the recorded case for a numbered film with a label
written into the middle already passes.

*Coverage*: TMDb's alternative-titles document is thin for anime compared with
what the anime-specific databases carry, and its romanisations are inconsistent.
This is the one category where a second catalogue's **title list** is materially
better than TMDb's.

**Verdict: the strongest case for a second provider, used as a title source
rather than as an answer.**

### Music concert discs - the real coverage gap

TMDb is a film database. A concert Blu-ray is a music release that happens to be
on video, and a great many simply are not in TMDb at all - and of those that
are, a good share carry no IMDb id, because IMDb does not catalogue them either.
Since the whole pass exists to produce an IMDb id, **a film with no IMDb id
cannot be answered even when it is found**: `_matched`
([tmdblookup.py:686](medialib/lib/tmdblookup.py:686)) settles on it, says so,
and hands back an empty id, and the folder goes on to be asked about under the
wider spellings. The code already knows this and says it - what it means for
concert discs is that no amount of better matching helps.

Naming is also unlike everything else here: `Artist - Live at Venue (Year)`,
where the year is the performance and the "title" is a place. None of the
current readings are about that shape.

**Verdict: the one category that genuinely needs a different kind of catalogue,
and possibly a different kind of tag.**

---

## What a second provider would have to be

The good news is that the seam is already most of the way there.
`_Candidate` ([tmdblookup.py:194](medialib/lib/tmdblookup.py:194)) is
provider-neutral -

```python
years      frozenset   every year some country released it in
titles     frozenset   its titles, folded to keys
spellings  tuple       those same titles as written
own        frozenset   its own two titles alone, folded
runtime    float       minutes, or 0.0
imdb       str         the id, or ""
```

- and everything below `_worth_asking`
([tmdblookup.py:723](medialib/lib/tmdblookup.py:723)) takes `_Candidate`s and
knows nothing about where they came from. `titlematch` already says of itself
that nothing in it is about films.

So a provider is two functions: one that turns a query and a year into rows, and
one that turns a row into a `_Candidate`. What is *not* factored out today is
the transport (`_curl`, [tmdblookup.py:121](medialib/lib/tmdblookup.py:121), and
the TMDb host at [tmdblookup.py:28](medialib/lib/tmdblookup.py:28)) and the
rate limit, which is TMDb's published figure.

**The fallback ladder does not change shape.** A second provider is another rung
at the bottom of Ladder B - asked only where every spelling has already failed
against TMDb - so its cost is bounded by how often TMDb fails, which is exactly
the set of folders the unmatched report already lists.

### The candidates

| Source | Key | Gives | Best for | Against |
| --- | --- | --- | --- | --- |
| **IMDb datasets** | none | `title.basics` + `title.akas`: every title, every regional spelling, type, year, runtime, **and the IMDb id itself** | everything, and especially shorts, TV movies, silent films | a bulk download (hundreds of MB), refreshed daily; personal/non-commercial use only |
| **Wikidata** | none | IMDb id (P345), TMDb id (P4947), publication date, labels and aliases in every language | documentaries, silent films, German films, concerts | SPARQL, needs a User-Agent, uneven depth |
| **TheTVDB** | yes | TV movies and specials, air dates | TV movies | another key to hold |
| **AniList** | none | romaji / english / native titles plus synonyms | anime films | no IMDb id, so it can only be a *title* source |
| **Discogs** | token | music video releases, artist/venue/date | concert discs | not film-shaped; no IMDb id |
| **OMDb** | yes | a thin wrapper over IMDb | a quick second opinion | weak search, 1000 requests a day |

**The IMDb datasets are the striking option**, and worth spelling out: the
command's whole output is an IMDb id, so TMDb is being used as a lookup *into*
IMDb. A local copy of `title.basics.tsv.gz` and `title.akas.tsv.gz` removes the
middleman for exactly the hard cases - it has the shorts TMDb lacks, the
regional spellings its alternative-titles document is thin on, the `tvMovie` and
`tvSpecial` and `video` types Gate 2 hides, and no rate limit at all. It is also
the only option that costs nothing per query, which matters for a pass over a
whole library. Against it: it is a bulk download rather than an API, and its
terms are personal and non-commercial - which this is, but the constraint should
be recorded rather than assumed.

---

## What to do, in order

1. **Gate 1: stop skipping yearless folders silently.** At minimum report them;
   better, look them up without a year, which `_under`
   ([tmdblookup.py:378](medialib/lib/tmdblookup.py:378)) can already do - it
   re-searches with no year when a year-filtered search returns nothing. This is
   the largest single win and touches one `continue`.
2. **Gate 2: ask `/search/tv` as a rung below the movie search.** Fixes TV
   movies outright and helps standup and anime, and needs no provider
   abstraction.
3. **Gate 3: widen the year for the categories that need it**, or better, let
   the runtime tiebreak
   ([tmdblookup.py:916](medialib/lib/tmdblookup.py:916)) carry a wider year
   window when it has a measurement to work with - the tolerance is already
   there, and a length that fits is stronger evidence than a year that does.
4. **Gate 4: read a second page** when the first produced no key-match.
5. **Then, and only then, a provider seam** - IMDb datasets first, as a bottom
   rung, because it answers the most categories for the least new machinery.
6. **Concert discs may want a different answer entirely.** If IMDb has no id for
   a disc, no amount of matching produces one; that is a question about what tag
   to write, not about which catalogue to ask.

Steps 1-4 are all inside `tmdblookup.py`, need no new dependency, no new key and
no new network host, and between them cover seven of the eight categories.

## How to check any of this

Each claim above is a folder shape, and folder shapes are what
`tests/lib/test_tmdblookup_recorded.py` records. Adding a case there and running

```bash
REGEN=1 tmdbApiKey=<key> pytest tests/lib/test_tmdblookup_recorded.py
```

answers "does TMDb really fail on this?" with a recording rather than an
opinion - and the `reports.json` beside each case is where a refusal and its
reason are pinned, which is the half of the answer these categories are about.
