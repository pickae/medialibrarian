# `ingest-movies`

Sorts loose movie files into per-movie subfolders (Plex layout), cleans
folder/movie/subtitle names, renames/downloads subtitles for six languages,
refreshes mkv tags, transcodes audio to Opus, transcribes commentary tracks,
and gives films without named chapters the ones their disc had.

Subtitles are downloaded after the films are tagged with their IMDb ids, and a
tagged film is searched for by its id, so only a subtitle catalogued under that
very film is taken — never one for a remake or another film of the same name. A
downloaded subtitle is kept only when it lines up with the film's speech at one
offset far better than at any other, however far that offset is from where it
started; a subtitle that fits nowhere in particular belongs to some other film
or cut and is thrown away, to be tried again on the next run.

A track counts as a commentary when its mkv commentary flag says so *or* when its
name does — in any of the supported languages, so a German disc's
"Audiokommentar" is picked up as readily as an "Audio Commentary". Commentary is
transcribed **in the language it is spoken in**: English gets an English
subtitle, another supported language gets the native one *and* an English
translation, anything else only the translation.

As the last work on each film it remuxes an improved copy in **one** `mkvmerge`
call, so the film is remuxed once and only one output exists at a time:

- Lossless tracks that were converted to Opus are swapped in place, keeping
  index, language, name and flags.
- Excessive audio and image-subtitle tracks are dropped. Commentary **audio** is
  exempt from the excessive-language rule, so a foreign-language commentary is
  never dropped from the film its subtitles were just made for.
- Commentary transcripts are appended, each tagged with its own language.
- **Dual-layer Dolby Vision profile 7 is normalised to single-layer profile
  8.1**, so DV survives on hardware that cannot read the enhancement layer. This
  is metadata only — the video is **never re-encoded** — and profile 5, profile
  8.x, HDR10, HDR10+ and SDR are left untouched.
- **A Dolby Vision claim the video does not back up is dropped**, so the file
  stops lying about itself: what the container reports is only a claim, so every
  DV file's stream is probed for an RPU that is really there, and a hollow claim
  is stripped, leaving the file reporting what its bitstream really is. A file
  whose RPU is really there is only checked, never rewritten, and a copy that
  came out less HDR than the source is thrown away and the original left alone.

The original is preserved as `<name> (old).mkv`, so nothing is lost and a rerun
is a no-op — which is also what makes the Dolby Vision work a one-time job per
film: a folder that already carries a `(old)` backup is skipped whole.

Last, once each film is the file it is going to stay, the folder and everything
in it are renamed — renamed only, in place — into the three names Plex reads:

- `{imdb-ttXXXXXXX}` on the folder and on every file, so the match is forced.
- `{edition-...}` on a folder's second, third and fourth *version* of one film,
  which Plex collapses into a single film with a named picker. A version named
  the way a release names itself (`Your Film (2020) colorized.mkv`) becomes
  `Your Film (2020) {imdb-tt0000000} {edition-Colorized}.mkv`.
- A split film's `Part1` / `cd1` token stays **last**, because that is where
  Plex's scanner reads it — a tag appended after it would stop the parts
  stacking at all.

Every sidecar follows its own film, including a commentary transcript whose name
extends the film's. The `(old)` backup keeps its own name, and running the
ingest again changes nothing.

Before anything is looked up, a folder whose **year is not written where a
reader can see it** is renamed onto one where it is: a separator standing where
the space before the bracket should be (`The Movie-(1999)`), a run of spaces
standing there instead of one, or a year that kept only one of its two brackets.
The first and the last are not years to Plex's scanner either; the middle one
leaves the title carrying a trailing space into every lookup. The name is put
right on the disk, so the folders no catalogue can name are fixed along with the
ones it can.

A film is named when **one** candidate carries its title — primary, original or
any alternative — and was released in the folder's year *in some country*, which
is what keeps a festival premiere and a release a year later from being two
different films. "Carries its title" is not a string compare. All of these are
one title said differently, and the readings **compose**, so a name that went
wrong in several ways at once is still found:

- an accent dropped, or written out the way a language writes it when it cannot
  be reached — `ä` reads as `a` and as `ae`, `ø` as `o` and as `oe`;
- a dash, apostrophe, comma or point written as a space, or as nothing at all,
  which is also how an abbreviation with its points meets one without;
- a roman numeral or a spelled-out one for an arabic one — `Part II`, `Part
  Two` and `Part 2` are one number — and the word that only *labels* a number
  dropped beside it, so `The Hills Beyond 2` meets `The Hills Beyond Part 2`
  and `Falkenauge 2. Teil`;
- a trailing `1` dropped altogether — the first of a series is written
  `Nordwind I`, `Nordwind 1` and `Nordwind` and meant identically. Only the
  first at the end: a trailing `2` is the sequel. A number with title on
  **both** sides of it may go at any value — `Nordwind 2: Der Sturm` is also
  written `Nordwind Der Sturm`, and what surrounds the number still says which
  film it is;
- the name a sequel has of its own, which nobody says out loud: `Missing Since
  2` is the film a catalogue holds as `Missing Since 2: The Beginning`, and a
  catalogue that runs a market's title on after the film's own is the same
  thing from the other side. Only after a series **number**, and only where a
  separator introduces it — a title with no number in it is only its name, and
  what follows one with no introduction is what a library writes after a title:
  a source, a resolution, a language, an extension. A year in what is cut comes
  back with the title, so `Falkenauge 2 (1985)` and `Falkenauge 2 (1999)` stay
  two films;
- a possessive `'s` that one side has and the other does not;
- an article gone from anywhere in the name, or several of them, or moved to
  the end the way a catalogue that sorts by the first real word moves it;
- `&` written out, in any of the six languages;
- upper case, lower case, or any mixture;
- a letter from the wrong keyboard — a Cyrillic or Greek letter that is the
  same *shape* as a Latin one reads as the Latin one, so a name that looks
  identical on screen is treated as identical.

One more thing a name can carry that a catalogue never does: **what kind of
thing the file is.** A `Movie`, `Film` or `Documentary` written in front of a
title, into the middle of it or on to the end of it says the same nothing in
each position, so `Looking for Group Documentary` is the film catalogued as
`Looking for Group`. In front of the title it comes off on sight — nothing
standing before the title is the title; further in it has to have a title in
front of it to be a label at all.

And one more that no reading of a *title* could reach, because the name is not
one title: a folder called `Falcons Forever aka Nordwind Rising (1998)`, where
whoever named it could not choose between the film's names and wrote both.
Nothing was ever released under that, so it is taken apart at the `aka` — in
any of the spellings a keyboard makes it, `AKA`, `a.k.a.`, `a/k/a`, `also known
as`, and the bracket a library holds the second name at arm's length in — and
the titles it holds are asked about **left to right**, the order they were
written in, until one answers. Whichever does names the film, and the folder is
written under that one alone. Only where the marker plainly holds two titles
apart: `Nakamura Lane` keeps its name, and so does a film actually called `Aka
Manto`.

A film nothing is found under is looked for again under the spellings a library
adds and a catalogue does not — a "Movie"/"Special" in front of the name, the
"Documentary" written into it, a franchise repeated on every film in it, its
numerals as figures. And under the one it *leaves out*: a library that keeps a
franchise in a folder of its own writes half the title on each, so the folder
above is read as the first half and the two together are asked about as one
title. A tail that is only a number is never asked about on its own — a
catalogue asked for `Part 2` answers with every film ever released in halves.

Where that leaves several candidates or none, the film's own length decides:
the folder is tagged only if exactly one candidate's runtime fits what is on
the disk and every other is ruled out, which is also what lets a folder whose
year is off by one be named at all.

Which results are looked at in the first place is its own question. Only the
first handful are worth a document each, and TMDb answers *most popular* first
— so a common word of a title fills the page with films that merely contain it.
The ones already carrying the folder's title are read before the rest, and the
rest in order of **how near their names are** — two things, read in this order:

- **does one name begin the other.** Two names that run together from the left
  until one of them stops is the shape of a subtitle, of a market's title run
  on after the film's own, and of a label somebody added: `Sunfall Reckoning`
  against `Sunfall Reckoning Redux`. Scattered words that happen to coincide
  are not that shape however many of them there are;
- **how much of the two names is the same name** — the words they share against
  the words they do not. A result carrying more of the folder's words is
  nearer, and a result saying less besides them is nearer: `Hard Border` is a
  better use of a request than `Wad: surviving on the border of water and land`,
  and both carry the whole of a folder called `Border`.

The first is a shape and the second a count, so where they disagree the shape
is the one read and neither needs a weight invented for it. A film reached only
by one of its other titles can never be in the group above — nothing the search
said about it matches — so its own name being near is the only thing that can
spend a request on it. Popularity is the catalogue's ordering and carries no
opinion about which film this folder holds, so it is what breaks a tie and
nothing more.

Before the length is asked, three narrowings settle most of what used to reach
it, because candidates are not equally likely just because they all survived:

- **A candidate with no IMDb id was never a possible answer.** What is being
  asked for is an id; one that has none can only stand beside a candidate that
  has one and make the pair look uncertain.
- **A title as written beats one reached through a reading, and a film's own
  name beats one of the names it is also known by.** Where any candidate folds
  to exactly what the folder is called, only those are considered — a folder
  called `1815` is not left unnamed because some other film also answers to it
  through a dropped article. And a catalogue's alternative titles hold every
  market's poster name, so one of them is bound to be somebody else's whole
  title: a film whose own name this is outweighs one carrying it down a list.
  A translation is not weakened by that — the catalogue leads with the title of
  the asking language, so a Swedish film catalogued in English carries the
  English name among its own.
- **A re-release does not answer for the film it was timed to.** A 1976 film
  back in cinemas in 2018 lists 2018 among its years; where another candidate
  was *first* released around the wanted year, the older one is set aside.
  Around, not exactly on — a premiere and a general release a year apart are
  the case the year rule exists for.

All three narrow and never widen: where every candidate is equally strong — or
equally useless — they all stand and the length decides as before. A folder of named editions
offers no length — which of the cuts the catalogue's one runtime is for is
exactly what is not known — so it falls back to the year alone.

Once a film is named, **TMDb's own spelling of whichever title matched is what
the folder and every file in it are written under** — including a film found
only by reading the folder above it as the first half of its title, which is
then written out in full — the one spelling of the
several that is known to be right. Whichever title matched, and not the
catalogue's primary one: a French film found under its French name stays French,
because the English title it is also catalogued under folds to something the
folder never said.

That is also what settles most of the folders no id can otherwise reach. A file
whose name says the folder's film in another hand is respelled onto the folder's
own rather than reported as a second film; a `Part 1` written with the number
held off becomes the `Part1` Plex stacks on; a `(1)`, `(copy)` or `- Copy` that a
file manager left is taken off, unless doing so would put two files under one
name, which is a real duplicate and is still reported.

Where the names genuinely cannot be made to agree — a second feature in the
folder, parts that each have a title of their own — **the id still goes on**,
provided every id already written in the folder is the same id. Nothing else
moves. One id that disagrees stops it: two ids in a folder is how a sequel ends
up filed under the film before it, and that is a mistake to be shown rather than
a spelling to be tidied.

One kind of disagreement the names themselves can never settle: a folder whose
files are the **same film named in another language**. `Die Blaue Stunde` and
`L'Ora Blu` share not a syllable with each other, and TMDb lists both as titles
of the film the folder matched. Where every file
in a folder is accounted for that way, the folder takes TMDb's spelling and the
id, and **every file keeps its own name** — which of a film's languages a file
is named in is a thing you chose, and a lookup is no reason to overwrite it. All
that is added is the id, so Plex knows they are one film. One file the catalogue
cannot account for and the folder is left alone as before: a second feature is
still a second feature.

Nothing is renamed for those folders, so no other list would mention them — they
get one of their own, `ingest-movies-othertitles-<folder>.txt`, naming each file
and the catalogued title it answered to. It is written on real runs as well as
dry ones, because it records what *happened*.

Such a folder **stays on the lists** either way, marked `id applied, names still
to settle`. The id is no longer missing; the names still are, and whether they
really could not be settled is exactly the thing worth looking at — an id going
on quietly is what would keep the folder off the list it belongs on.

**A language kept as an edition** is the other half of that. A library that
cannot mux every language into one file keeps one file per language and writes
the language after the year. Once the catalogue confirms the film, that suffix
becomes an `{edition-}` tag and the title in front of it takes the folder's
spelling — Plex then collapses them into one film with a named picker, and the
language survives in the tag. A file whose title is the *only* thing
distinguishing it keeps its name, because there is no tag for the language to
survive in.

And **when nothing answers to the folder's own name, its files are asked under
theirs**: a library holding a film under two languages often names the folder in
a third, and the files are then the only names a catalogue has ever heard of.
Every film in the folder must answer, and all of them to the same id — a folder
whose films answer to two ids is a folder holding two films.

**`-t` does only this phase** — no conversion, no remux, no downloads, no
transcription — and does it as a **dry run** unless `-w` is given, printing
every rename it would make. It walks the whole tree, so it can be pointed at a
library; a full ingest reads the one level it always did. Nothing is renamed
for a film TMDb could not name, nor for a folder holding a film that is not its
own. Instead each folder given gets these lists, in the script directory's
`logs/movie-tagging/` folder:

| List | What is in it | Written |
|---|---|---|
| `ingest-movies-unmatched-<folder>.tsv` | films TMDb could not name — fill in their ids and feed the file back with `-i` | always |
| `ingest-movies-ambiguous-<folder>.txt` | folders holding more than one film, which no id can settle — give the files names that say which release each one is | always |
| `ingest-movies-othertitles-<folder>.txt` | folders holding one film under several of its titles (above) | always |
| `ingest-movies-renames-<folder>.txt` | every rename the run would make | dry run only |
| `ingest-movies-nearmisses-<folder>.txt` | for each folder in the first two: what TMDb was asked, what it offered, and why each offer was refused | dry run only |

A folder is on the unmatched list or the ambiguous one, never both. Under each
name, the ambiguous list says what it *reads as* once the spelling is folded
away: two lines that read the same are a reading the matching does not have
yet — worth reporting; two that read differently are two different films. Read
the near-miss list before trusting either: a page of candidates that are
plainly the film means the matching is too narrow; a page of films that merely
share a word means it is working.

**A film kept twice is warned about.** When two separate folders carry the
same id, whether it was already on them or the run gives it to them, `-t`
names both at the end of the run rather than in a list: side by side, in
different corners of the library, or in two of the folders given (two disks,
say). That is nearly always one film kept twice by accident, so which copy to
keep is left to you. The several files inside one folder are its versions and
are never counted.

A line of the unmatched list, filled in:

```
Your Film (1975)	tt0000002
```

An id is read however you have it to hand (`tt0000002`, `imdb-tt0000002`, a bare
TMDb number), answers before the network is asked, and stays in the file so the
lookup you did by hand is not lost. Requests to TMDb are paced to ten a second.

**Where TMDb cannot name a film, `-t` asks a local copy of IMDb's own title
lists** - the romanisations its search does not find, and the films it states
no runtime for, which is what a folder with no year is settled on. The lists
are fetched into the checkout's `data/imdb` the first time (about 750 MB),
refreshed when they are a month old, and read with `duckdb`; without it the
pass runs on TMDb alone. IMDb offers them for personal, non-commercial use
only. A full ingest never asks them.

Feeding that list back is **`-i`**, the same tagging phase going on what you
filled in — and a **dry run** too, unless `-w` is given. It prints what your ids
would rename and writes nothing: not the lists, which are `-t`'s to write, and
not the file you are still filling in. `-iw` carries the renames out and brings
the file up to date.

**An id you filled in is used, not checked.** TMDb is not asked to agree with a
lookup you did by hand, because it is the one that failed to name the film in the
first place. A row you left **blank** is not asked about again either, for that
same reason — the run says how many there were, once, at the end, and nothing
per film. A row you filled in that names no folder in the library is an error
naming that row, and the run carries on to the next. And a `-i` file that
**cannot be read stops the run**: counted as no ids at all, a path typed wrong
would throw away every id in the real file and put the whole library back
through the lookups that already failed on it.

**`-w` on its own is refused.** Without `-t`, `-i` or `-s` there is no dry run
to carry out, and the run it would otherwise start is a full ingest. `-a` and
`-c` refuse it as well: the transcription and the chapter lookup are not dry
runs, so there is nothing for `-w` to carry out.

**`-s` does only the subtitles.** The given folders are walked the way `-t`
walks them, and every `<movie>.xx.srt` already beside a film is put to the test
a downloaded subtitle is kept by: its best alignment with the film's speech has
to stand out from every other. Commentary transcripts are never tested — they
are named after their track — and neither are forced subtitles or languages
outside the six. It is a **dry run** unless `-w` is given: each subtitle is
tested on a copy, the run says which would be kept and which thrown out, and
the ones out of step are listed in `logs/ingest-movies-subtitles-<folder>.txt`.
`-sw` syncs the ones in step, deletes the ones that are not, and then downloads,
syncs and tests every subtitle still missing, exactly as a full ingest does — so
one thrown out is fetched again in the same run. A subtitle ffsubsync cannot
align at all is left alone either way, since it may be one that cannot be
fetched again. Run it after `-tw`, so that films are searched for by their ids.

**`-c` does only the chapters.** Every tagged film under the given folders that
has no chapters, or only numbered ones (`Chapter 01`, `Kapitel 3`, a bare
number or time), is looked up in the read-only
[ChapterDB archive](https://chapterdb.plex.tv/): chapter times read off DVDs and
Blu-rays, mostly with the names from the disc's menu. The full ingest does the
same lookup right after the tagging, since a film is looked up by the title
its folder now carries — an untagged folder is never looked up.

- **A set must be the disc the film was ripped from.** It has to run as long as
  the film to within **two seconds**, which rules out every other cut and every
  PAL transfer. It also has to have at least three chapters, start at the
  start, be in order, and reach past the middle of the film.
- **Names win, but only for the same marks.** A film with no chapters gets the
  best named set, or a numbered one when that is all there is. A film with
  numbered chapters gets them **replaced** only by a named set with the same
  number of chapters, each starting within a second of the film's own: the
  same marks, now with names. Numbered chapters at other times — a different
  authoring, or marks a muxer put every five minutes — are kept, since there
  is no point dropping marks the film already has. A film whose chapters have
  names is not looked up at all. Among sets that fit, English names win, then
  the set more people confirmed.
- **Written in place, never remuxed.** `mkvpropedit --chapters` replaces every
  chapter the film had, so it never ends up carrying two sets.

It writes as it goes: there is no dry run, and no `-w`. The archive takes no
new sets, so a recent film will not be in it; if it stops answering, the rest of the run does
without it. Needs `curl`.

**`-a` does only the audio commentary phase.** The given folders are walked the way
`-t` walks them, and every commentary track that does not already have its
transcript beside the film is transcribed next to it — in the language it is
spoken in, on the GPU when whisper can use it, on every CPU thread otherwise —
and nothing else: no subtitle downloading, no renaming of titles, no
conversion, no remux, no tagging. The transcripts stay beside the films rather
than muxed into them, and a rerun over the same library finds every commentary
already transcribed and does nothing.

A transcript already beside a film is left alone, unless it is too small to be
the film's real transcript: a real one runs to the order of a kilobyte per
minute of film, and a sidecar under half of that is the one an older run wrote
forcing a non-English commentary through the English model. That one is
discarded and the commentary is transcribed for real.

A transcript an older run wrote numbered for the track it then stood under,
when the film's tracks have since moved and that number no longer names this
commentary, is renumbered to the track's current number rather than
transcribed again — where the rest of the name still says which commentary it
is. The srt itself is left untouched, and the renumbered sidecar is judged by
the size rule like any other.

**Several folders may be given**, and each is worked through in full before the
next is started, in the order they were typed. Each leaves lists of its own,
named after it — `ingest-movies-unmatched-Films.tsv`,
`ingest-movies-ambiguous-Films.txt` — so two libraries' worklists never land in
one file. An `-i` list you named yourself is the exception: it is read for every
folder and, under `-iw`, written back once, holding them all. Two folders of the same name are
refused before anything is touched, since their lists would be written to one
path.
