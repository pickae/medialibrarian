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
"""

from collections import Counter
from collections.abc import Sequence


def plurality_group_indices(mode: str, extensions: Sequence[str]) -> list[int]:
    """Return the indices of the siblings that form the group.

    ``mode`` is "files" or anything else, which means folders - matching the bash
    original, whose test is ``!= files``.
    """
    if mode != "files":
        return list(range(len(extensions)))

    lowered = [e.lower() for e in extensions]
    counts = Counter(lowered)

    # Walk the siblings in order, not the tally: first appearance settles a tie.
    best_ext: str | None = None
    best_count = 0
    for ext in lowered:
        if counts[ext] > best_count:
            best_count = counts[ext]
            best_ext = ext

    return [i for i, ext in enumerate(lowered) if ext == best_ext]
