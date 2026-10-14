# `plex-letterboxd-sync`

Keeps what was watched, when, and how it was rated in step between a Plex
server's movie libraries and a Letterboxd account. Letterboxd takes nothing but
a file, so the two directions differ: Plex is changed directly, and what
Letterboxd lacks is written to a CSV for its importer.

    plex-letterboxd-sync [options] <export.zip|account>

| At a glance | |
| --- | --- |
| **Takes** | a Letterboxd data export, or an account name; `plexUrl` and `plexToken` |
| **Writes** | the import CSV and the state file ([below](#default-behavior)) |
| **Input** | marks films played, rates them and adds to the watchlist in Plex; `-p` previews instead |
| **Network** | the Plex server, plex.tv (the watchlist), Letterboxd's RSS feed when given an account, and TheMovieDB when `tmdbApiKey` is set |

## Default behavior

With no options, a run:

- reads what Letterboxd has: **everything** from an export zip (Settings > Data
  > Export), or the fifty or so most recent diary entries from the account's
  public RSS feed. The first run needs the export; the feed alone is refused
  until there is a state file
- reads the Plex server's movie libraries (`-s` names some) with each film's
  rating and the days it was played, from the server's play history; and the
  account's watchlist
- **Letterboxd → Plex:** marks played every unplayed Plex film found on
  Letterboxd, gives a Plex film with no rating Letterboxd's, and adds the films
  on Letterboxd's watchlist to Plex's
- **Plex → Letterboxd:** writes to `logs/letterboxd-import-<date>.csv` (`-o`),
  to upload at <https://letterboxd.com/import/>:
  - a diary entry for every Plex play Letterboxd does not have, marked as a
    rewatch when the film was played or logged before
  - Plex's rating, where Letterboxd has none
  - a film played in Plex with no play on record, as a film watched with no date

  and to `logs/letterboxd-watchlist-<date>.csv` the films on Plex's watchlist
  that Letterboxd has neither watched nor on its own, for the import on the
  Letterboxd watchlist page
- skips the Letterboxd side with `-l`
- remembers in `data/plex-letterboxd-state.json` (`-k`) what Letterboxd has,
  and every row it has written into a CSV; and keeps a copy of the Plex library
  beside it, so the next run reads only what changed

Use the export for the first run and the feed for the regular ones after it.

### Frequent runs

A run reads only what changed in Plex since the last one: films added,
updated, played or rated, the new plays, and the newest additions to the
watchlist. Once a week — or with `-f` — it reads everything again, which is
the only way a film removed from Plex, one marked unplayed, or one taken off
the watchlist is seen. With the feed, a run takes seconds, and needs no file
from Letterboxd.

### What it cannot do

- **The day a film was watched does not reach Plex.** Plex marks a film played
  as of the moment it is told to. It keeps no play for that in its history, so
  such a film is never sent back to Letterboxd with that date either.
- **A rating that differs on the two sides is left alone on both.** Neither
  side records when a rating was given, so neither can be said to be newer; the
  run lists them.
- **Nothing is removed.** A film unmarked, a diary entry deleted or a film
  taken off a watchlist on one side stays on the other. A film that is
  watched comes off both watchlists by itself.
- **The feed has no watchlist.** Letterboxd's watchlist is read from the
  export only.

### Rows sent and not uploaded yet

A row written into a CSV is not written again while it waits to be uploaded.
Once Letterboxd shows it — in the feed, or in an export — it is done with. A
full export made the day after the CSV or later that still lacks it means it
was never uploaded, and it is offered again in the next CSV. The watchlist CSV
works the same way.

The first CSVs can be long: every play in Plex's history that Letterboxd has no
diary entry for, and the whole of Plex's watchlist.

## Matching

Letterboxd's export names a film by its title and year only, so most films are
matched by title — through the same fold `ingest-movies` matches a folder
against TheMovieDB with. Accents, punctuation, a leading article, "Part II"
against "Part 2", "&" against "and" and a dropped subtitle all still match. The
evidence is asked strongest first: a TMDb or IMDb id both sides carry; the
title in the same year; the title a year apart; the title where one side has
no year; in the same year, one title being a part of the other (a franchise
written in front, a subtitle left off); and last, the title with one typo in
it. Two films with ids that differ are never matched however alike their
titles are, and a match weaker than an id has to hold both ways: a Plex film
only takes a Letterboxd film that would pick it back out of the whole library.

Where `tmdbApiKey` is set, TheMovieDB is asked for the ids of the Letterboxd
films a title does not settle — not found in Plex, found twice, or found only
by one of the weaker readings. TheMovieDB names a film only when it is certain,
and the id then finds a film Plex holds under another title altogether, or
rules out a reading that found the wrong one. A film it cannot name is
remembered, and not asked about again.

A Plex film that matches two Letterboxd films equally well is not marked; the
run lists it as a warning. A match that is not written identically is printed
with the Letterboxd spelling beside it, and `-v` says which rule made it.

---

**Options:** `plex-letterboxd-sync -h` lists every option and its default.
**Shared rules:** [file safety](../file-safety.md) ·
[missing tools](../requirements.md#missing-tools-are-refused-up-front) ·
[what a run prints](../output.md)
