"""cue-to-chapters's library half: a cue sheet read as chapters.

The two time formats are what this is about. A cue sheet counts in CD FRAMES -
75 to the second, because the format inherits the disc's sector clock - and a
chapter file counts in H:MM:SS.mmm. Flat milliseconds are what the two are
carried between, and every constant here is one of the factors that gets them
there and back.
"""

from medialib.lib.cleannamesindividually import clean_names_individually

MS_PER_SECOND = 1000
SECONDS_PER_MINUTE = 60
MS_PER_MINUTE = 60000
MS_PER_HOUR = 3600000

# A cue sheet's third INDEX field is FRAMES rather than hundredths: 75 per
# second, one frame = 1/75 s = 13.33 ms, and the field runs 00..74.
CUE_FRAMES_PER_SECOND = 75

# How many characters off the end of an "INDEX 01 MM:SS:FF" line are handed to
# time_from_cue_string. The time field itself is 8, and the two extra characters
# are slack for the longer field a >1000-minute file would carry; the parse drops
# whatever prefix comes along with it.
CUE_TIME_TAIL_WIDTH = 10

# One chapter is a CHAPTERnn= line plus its CHAPTERnnNAME= line.
LINES_PER_CHAPTER = 2


def time_row(number: int, length: int) -> str:
    """``timeRow``: "CHAPTER01=00:00:00.000" for a chapter at <length> ms."""
    hours = length // MS_PER_HOUR
    minutes = length % MS_PER_HOUR // MS_PER_MINUTE
    seconds = length % MS_PER_HOUR % MS_PER_MINUTE // MS_PER_SECOND
    milliseconds = length % MS_PER_HOUR % MS_PER_MINUTE % MS_PER_SECOND
    return "CHAPTER%02d=%02d:%02d:%02d.%03d" % (number, hours, minutes, seconds,
                                                milliseconds)


def name_row(number: int, name: str) -> str:
    """``nameRow``: "CHAPTER01NAME=Intro"."""
    return "CHAPTER%02dNAME=%s" % (number, name)


def time_from_cue_string(text: str) -> int:
    """``timeFromCueString``: a cue "MM:SS:FF" time as flat milliseconds.

    The input is the tail of an INDEX line, so it arrives with whatever came
    along in front of the time; whitespace is collapsed and everything up to the
    first space is dropped, which is what leaves the time field alone.
    """
    formatted = text.replace("\r", "")
    formatted = " ".join(formatted.split())
    # Everything up to the FIRST space, when there is one. A tail that is all
    # time and no prefix - which is what a >99-minute field leaves once the ten
    # characters are taken - has none, and bash's ${x#* } leaves such a string
    # alone rather than emptying it.
    head, space, tail = formatted.partition(" ")
    formatted = tail if space else head

    if formatted in ("00:00:00", "0:00:00", "0:0:00"):
        return 0

    minutes = formatted.rpartition(":")[0].rpartition(":")[0]
    seconds = formatted.partition(":")[2].rpartition(":")[0]
    frames = formatted.partition(":")[2].partition(":")[2]

    # The frames are worth frames * 1000 / 75, rounded to the nearest
    # millisecond. Integer arithmetic rounds by adding half a divisor to the
    # numerator first, which is what the 37 is: 75/2 truncated. Exact rather than
    # approximate here, because (f * 1000) mod 75 is always 0, 25 or 50 for f in
    # 0..74, so it can never land in the gap where a truncated half and a real
    # 37.5 would disagree.
    half_frame = CUE_FRAMES_PER_SECOND // 2
    return (int(minutes, 10) * SECONDS_PER_MINUTE * MS_PER_SECOND
            + int(seconds, 10) * MS_PER_SECOND
            + (int(frames, 10) * MS_PER_SECOND + half_frame)
            // CUE_FRAMES_PER_SECOND)


def _lines(cue_file: str) -> list[str]:
    """The cue's lines, carriage returns dropped and leading space stripped."""
    with open(cue_file, encoding="utf-8", errors="surrogateescape") as handle:
        return [line.replace("\r", "").lstrip() for line in
                handle.read().split("\n")]


def _describes_one_file(lines: list[str]) -> bool:
    """Whether this cue describes chapters of ONE joined audio file.

    A cue listing several separate audio files, each starting its single track at
    00:00:00 - the classic "one FILE per track" rip - carries no chapter
    positions relative to a joined file: every track is time 0 of its own. There
    is nothing to turn into chapters, so the caller is told to ignore it rather
    than handed a useless single 00:00:00 entry.

    A FILE whose only track is a data/CD-ROM MODE track is not an audio file and
    does not count towards the tally.
    """
    audio_files = 0
    non_zero_index = False
    file_has_audio = False
    in_audio_track = False

    for line in lines:
        if line.startswith("FILE "):
            file_has_audio = False
        elif line.startswith("TRACK "):
            if "AUDIO" in line:
                in_audio_track = True
                if not file_has_audio:
                    file_has_audio = True
                    audio_files += 1
            else:
                in_audio_track = False
        elif line.startswith("INDEX 01"):
            if in_audio_track and time_from_cue_string(
                    line[-CUE_TIME_TAIL_WIDTH:]) != 0:
                non_zero_index = True

    return not (audio_files > 1 and not non_zero_index)


def chapters_from_cue(cue_file: str) -> list[str]:
    """``chaptersFromCue``: the OGM chapter lines a cue sheet describes.

    Empty for a cue that describes no chapters of one joined file. Everything is
    gated on being inside an AUDIO track, so a data track's title - and the
    album header TITLE that precedes the first TRACK - never becomes a chapter.
    """
    lines = _lines(cue_file)
    if not _describes_one_file(lines):
        return []

    chapters: list[str] = []
    counter = 1
    start_time = 0
    title = ""
    have_title = False
    in_audio_track = False

    for line in lines:
        if line.startswith("TRACK "):
            in_audio_track = "AUDIO" in line
            continue
        if not in_audio_track:
            continue

        if line.startswith("TITLE "):
            title = line[len("TITLE "):]
            have_title = True
        elif "INDEX 01" in line:
            start_time = time_from_cue_string(line[-CUE_TIME_TAIL_WIDTH:])

        # Within a track the cue order is TRACK -> TITLE -> INDEX, so a chapter
        # is emitted once its title is known and either a real start time was
        # read or it is the first chapter, which legitimately starts at 0.
        if have_title and (start_time != 0 or counter <= 1):
            chapters.append(time_row(counter, start_time))
            chapters.append(name_row(counter,
                                     clean_names_individually(title)[1]))
            start_time = 0
            have_title = False
            title = ""
            counter += 1

    return chapters


def write_chapters_from_cue(cue_file: str, chapter_file: str) -> None:
    """``writeChaptersFromCue``: the chapter list, in the OGM layout mkvmerge is
    given. A cue that describes no chapters leaves the file empty rather than
    absent, because the caller's next step is to read it."""
    chapters = chapters_from_cue(cue_file)
    with open(chapter_file, "w", encoding="utf-8",
              errors="surrogateescape") as handle:
        if chapters:
            handle.write("\n".join(chapters) + "\n")
