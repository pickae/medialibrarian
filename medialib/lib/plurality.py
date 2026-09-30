"""Which of a folder's siblings take part in the collective name-cleaning pass.

In "folders" mode every item takes part: folders have no extension, so they always
form a single group.

In "files" mode only the plurality filetype does - the commonest extension among
the siblings. A lone odd file (a single ``cover.jpg`` among many ``.mp3`` files)
must neither be grouped with the plurality nor, by not sharing their common
leading or trailing text, stop that text being stripped from them.

Extensions are tallied case-insensitively, so ``.MP3`` and ``.mp3`` are one
filetype. A dotless filename yields an empty extension, which is a filetype of its
own rather than an error.

A tie goes to the filetype that appears FIRST among the siblings. That is a
stated rule rather than whatever a hash table's order happens to produce, so it
answers the same on every host.

`find-gaps` asks the same question a level up: which KIND of file - video,
audio, document - a folder is mostly of, so that a folder of .mp3 and .m4a
tracks is one run and its .jpg cover is not part of it.
"""

from collections import Counter
from collections.abc import Sequence

from medialib.lib import enums


def plurality_group_indices(mode: str, extensions: Sequence[str]) -> list[int]:
    """Return the indices of the siblings that form the group.

    ``mode`` is "files" or anything else, which means folders.
    """
    if mode != "files":
        return list(range(len(extensions)))
    return _plurality([e.lower() for e in extensions])


def plurality_kind_indices(names: Sequence[str]) -> list[int]:
    """The indices of the files of the commonest KIND (:data:`enums.FILE_KINDS`)
    - the videos of a season with its .srt and .nfo files beside them, the
    tracks of a book whatever mix of .mp3 and .m4a they were ripped to.

    The same tie rule as the extension group. A file with no extension is a
    kind of its own.
    """
    return _plurality([enums.file_kind(name) for name in names])


def _plurality(keys: Sequence[str]) -> list[int]:
    """The indices holding the commonest key, a tie going to the first seen."""
    counts = Counter(keys)

    # Walk the siblings in order, not the tally: first appearance settles a tie.
    best: str | None = None
    best_count = 0
    for key in keys:
        if counts[key] > best_count:
            best_count = counts[key]
            best = key

    return [i for i, key in enumerate(keys) if key == best]
