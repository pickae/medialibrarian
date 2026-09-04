"""The collective pass: strip what leads and trails every name in a group.

Sibling names share a leading and a trailing run, and this pass removes the
longest common one, case-insensitively, from all of them - "Show - Intro",
"Show - Main" and "Show - Outro" come back as "Intro", "Main" and "Outro". In
memory, in order, no files:

1. the longest common prefix, exact (filenames are only ever truncated at
   the end, so prefixes are never cropped);
2. the longest common suffix, crop-aware: a sibling may carry the whole
   suffix or only an inner portion of it, the outer edge having been
   chopped off, and the mode ("files"/"folders") vetoes partial crops;
3. both affixes aligned back to a word/number boundary, so a cut never
   splits a word or a number;
4. the affix brackets checked as a pair: a bracket goes only with its
   matching partner, and a stripped bracket takes the partner of its real
   pair with it (consecutive pairs each go as a whole);
5. the aligned common middle passage still sitting in every name, removed
   only where it is delimited and never breaks a bracket pair.

Pure string in / string out: no files, no tools.
"""

from medialib.lib.affix import affix_char_class, align_affix_to_run_boundary
from medialib.lib.enums import BRACKET_CLOSE, BRACKET_OPEN

__all__ = [
    "brackets_balanced",
    "clean_names_collectively",
]

# The flat "any bracket" set, used purely to decide WHETHER a character is
# a bracket; the pairing is the OPEN/CLOSE pairs matched by index.
_BRACKETS = BRACKET_OPEN + BRACKET_CLOSE

# Minimum length (in characters) an aligned common middle fragment must
# reach before the interior-passage pass will remove it.
_MIN_MIDDLE = 3


def _collect_brackets(s: str) -> str:
    """Only the bracket characters of ``s``, in order, everything else dropped."""
    return "".join(ch for ch in s if ch in _BRACKETS)


def brackets_balanced(s: str) -> bool:
    """Whether a string of bracket characters is properly matched and nested.

    WHICH opener matches WHICH closer is the central OPEN/CLOSE pairs
    (matched by index), not a hardcoded table. An empty string is balanced.
    """
    stack: list[str] = []
    for ch in s:
        if ch in BRACKET_OPEN:
            stack.append(ch)
        elif ch in BRACKET_CLOSE:
            pos = BRACKET_CLOSE.index(ch)
            if not stack or stack[-1] != BRACKET_OPEN[pos]:
                return False
            stack.pop()
    return not stack


def _bracket_pairs(s: str) -> list[tuple[int, int]]:
    """The matched bracket pairs of ``s`` as (open index, close index) runs.

    Exactly the matching rules ``brackets_balanced`` applies, over the whole
    name; a bracket that cannot be matched forms no pair and is not reported,
    so callers leave such strays alone. A closer whose top of the stack is
    the wrong opener does not pop it - the opener stays for what follows.
    """
    pairs: list[tuple[int, int]] = []
    stack: list[int] = []
    for j, ch in enumerate(s):
        if ch in BRACKET_OPEN:
            stack.append(j)
        elif ch in BRACKET_CLOSE:
            if not stack:
                continue
            top = stack[-1]
            if s[top] != BRACKET_OPEN[BRACKET_CLOSE.index(ch)]:
                continue
            stack.pop()
            pairs.append((top, j))
    return pairs


def _drop_orphaned_bracket_partners(
    items: list[str], originals: list[str], prefix_len: int
) -> None:
    """Drop, from each stripped name, the partners of the brackets it lost.

    Only called once the affix strip has actually removed brackets. A pair
    with exactly one end inside an affix loses its other end too, so a
    consecutive pair "(a) (b)" ends up "a b", one whole pair at a time -
    exactly as the two names "(a)"/"(b)" would each lose their brackets if
    they sat apart. Partners are found by matching the brackets of the whole
    ORIGINAL name, so only a genuine matched pair is ever broken up.
    """
    for i in range(len(items)):
        s = originals[i]
        r = len(items[i])
        end = prefix_len + r
        drop: set[int] = set()
        for a, b in _bracket_pairs(s):
            a_in = prefix_len <= a < end
            b_in = prefix_len <= b < end
            if a_in == b_in:
                continue
            drop.add(a if a_in else b)
        if not drop:
            continue
        out = []
        for j in range(r):
            if j + prefix_len in drop:
                continue
            out.append(items[i][j])
        items[i] = "".join(out)


def _longest_affix_portion(name: str, affix: str) -> int:
    """How much of the trailing affix this name actually carries at its end.

    The length of the longest PREFIX of ``affix`` that is a suffix of
    ``name``; 0 when the name carries none of it.
    """
    top = min(len(affix), len(name))
    for k in range(top, 0, -1):
        if name[-k:] == affix[:k]:
            return k
    return 0


def _count_tokens(s: str) -> int:
    """The number of separator-delimited tokens (maximal letter/digit runs)."""
    count = 0
    prev = "O"
    for ch in s:
        cls = affix_char_class(ch)
        if cls in ("L", "D"):
            if prev == "O":
                count += 1
            prev = "X"
        else:
            prev = "O"
    return count


def _crop_boundary_ok(names: list[str], cand: str, portions: list[int]) -> bool:
    """Whether a partial match is a genuine outer-edge crop, not in-word overlap.

    A crop only cuts where the cut lands on a class change. If, for any name,
    the character just before the cut is the same class as the affix's
    leading character, the shared tail is a coincidental in-word overlap -
    the "album one"/"album two" stray "o" that would cut "two" down to "tw".
    """
    cand_cls = affix_char_class(cand[0])
    if cand_cls not in ("L", "D"):
        return True
    for m, cur in zip(names, portions, strict=True):
        if cur == 0 or cur >= len(m):
            continue
        if affix_char_class(m[len(m) - cur - 1]) == cand_cls:
            return False
    return True


def _monotonic_ok(portions: list[int], input_lens: list[int]) -> bool:
    """Whether the crops respect end-truncation, on the ORIGINAL input lengths.

    A genuine fixed-width truncation can only ever drop tail characters, so
    a shorter source name can never carry strictly more of the candidate
    than a longer one.
    """

    def lens_at(i: int) -> int:
        return input_lens[i] if i < len(input_lens) else 0

    for ci in range(len(portions)):
        for cj in range(len(portions)):
            if ci == cj:
                continue
            if lens_at(ci) < lens_at(cj) and portions[ci] > portions[cj]:
                return False
    return True


def _same_width_ok(cand: str, portions: list[int], input_lens: list[int]) -> bool:
    """Whether every input filename can be brought to one common length.

    Fixed-width truncation leaves equal lengths, save for trailing
    whitespace trimmed off the shorter-carrying names: each name spans the
    interval [inputLen, inputLen + the whitespace run following its carried
    portion], and the crop is same-width iff those intervals share a common
    length.
    """
    lower = 0
    upper = 0
    init = False
    for i in range(len(portions)):
        carried = portions[i]
        ws = 0
        while cand[carried + ws:carried + ws + 1] in (" ", "\t"):
            ws += 1
        lo = input_lens[i] if i < len(input_lens) else 0
        up = lo + ws
        if not init:
            lower, upper, init = lo, up, True
        else:
            if lo > lower:
                lower = lo
            if up < upper:
                upper = up
    return not lower > upper


def _crop_aware_common_affix(
    names: list[str], mode: str, input_lens: list[int]
) -> str:
    """The shared trailing affix, allowing outer-edge crops on some names.

    Names are expected already lower-cased by the caller. Among all candidate
    suffixes (every suffix of every name) that EVERY name carries at least one
    character of, the one that maximises the SMALLEST per-name carried
    portion wins, ties broken toward the longer affix - so a fully shared
    suffix always beats a coincidental edge overlap, and a genuine crop only
    reaches further. Partial crops are vetted by ``mode``: "folders" rejects
    them outright, "files" adds the word, truncation-consistency and
    same-width guards; the mode-less legacy behaviour is permissive.
    """
    if not names:
        return ""
    best = ""
    best_min = 0
    best_len = 0
    for n in names:
        for length in range(len(n), 0, -1):
            cand = n[-length:]
            cmin = length
            portions: list[int] = []
            for m in names:
                cur = _longest_affix_portion(m, cand)
                portions.append(cur)
                if cur < cmin:
                    cmin = cur
                if cmin == 0:
                    break
            if cmin == 0:
                continue

            if not _crop_boundary_ok(names, cand, portions):
                continue

            # The mode guards engage only on a REAL crop: at least one name
            # carries fewer than all the candidate's characters.
            if cmin < length and mode in ("folders", "files"):
                if mode == "folders":
                    continue
                if _count_tokens(cand) < 2:
                    continue
                if not _monotonic_ok(portions, input_lens):
                    continue
                if not _same_width_ok(cand, portions, input_lens):
                    continue

            if cmin > best_min or (cmin == best_min and length > best_len):
                best = cand
                best_min = cmin
                best_len = length
    return best


def _crop_middle_to_keep_bracket_pairs(
    names: list[str], rs: int, re: int
) -> tuple[int, int]:
    """Crop a middle block back so it never carries off one end of a pair.

    A block may take WHOLE bracket pairs with it, but when a matched pair
    straddles a block edge the edge moves past the bracket whose partner
    stays behind: an opener inside the block with its closer to the right
    pushes the left edge; a closer inside with its opener to the left pulls
    the right edge back. Every crop tightens the edges for the rest of the
    scan, so several straddling pairs (and several names) settle in one call.
    Pairing is per name over the whole name.
    """
    for name in names:
        for a, b in _bracket_pairs(name):
            a_in = rs <= a <= re
            b_in = rs <= b <= re
            if a_in == b_in:
                continue
            if a_in:
                if a + 1 > rs:
                    rs = a + 1
            else:
                if b - 1 < re:
                    re = b - 1
            if rs > re:
                return rs, re
    return rs, re


def _remove_common_middle(items: list[str]) -> None:
    """Remove every aligned common middle fragment from all names, in place.

    A fragment qualifies when it sits at the very same character index in
    every name, is at least ``_MIN_MIDDLE`` long, is interior (not touching
    index 0 nor the shortest name's end), and is delimited by a separator or
    bracket in EVERY name at both boundaries - the cut can never bite into a
    word or a number. A block that owns a separator at an edge is replaced by
    a single space, unless the retained text is separated anyway. Case-
    insensitive: compared on lower-cased copies, removed by index.
    """
    n = len(items)
    if n < 2:
        return
    low = [s.lower() for s in items]
    min_len = min(len(s) for s in low)
    if min_len < _MIN_MIDDLE:
        return

    remove = [0] * min_len

    run_start = -1
    for pos in range(min_len):
        ch = low[0][pos]
        agree = all(low[k][pos] == ch for k in range(1, n))
        if agree:
            if run_start < 0:
                run_start = pos
            if pos < min_len - 1:
                continue
            run_end = pos
        else:
            run_end = pos - 1
        if run_start >= 0:
            length = run_end - run_start + 1
            if run_start >= 1 and run_end <= min_len - 2 and length >= _MIN_MIDDLE:
                rs, re = run_start, run_end
                while rs <= re:
                    prs, pre = rs, re
                    # Retreat the left edge while the block's own edge character
                    # is not a separator in every name.
                    while rs <= re:
                        if affix_char_class(low[0][rs]) == "O":
                            break
                        ok = True
                        for k in range(n):
                            if affix_char_class(low[k][rs - 1]) != "O":
                                ok = False
                                break
                        if ok:
                            break
                        rs += 1
                    # ... and the right edge, symmetrically.
                    while re >= rs:
                        if affix_char_class(low[0][re]) == "O":
                            break
                        ok = True
                        for k in range(n):
                            if affix_char_class(low[k][re + 1]) != "O":
                                ok = False
                                break
                        if ok:
                            break
                        re -= 1
                    if re < rs:
                        break
                    rs, re = _crop_middle_to_keep_bracket_pairs(low, rs, re)
                    if rs == prs and re == pre:
                        break
                if (
                    rs >= 1
                    and re <= min_len - 2
                    and re >= rs
                    and re - rs + 1 >= _MIN_MIDDLE
                ):
                    # The block may own a separator at an edge; deleting it whole
                    # would glue the retained neighbours, so a single space is
                    # left in its place - unless the text outside the block is
                    # already separated in every name.
                    span_space = 0
                    if affix_char_class(low[0][rs]) == "O" or affix_char_class(low[0][re]) == "O":
                        span_space = 1
                    if span_space:
                        for nb in (rs - 1, re + 1):
                            sep_outside = True
                            for k in range(n):
                                if affix_char_class(low[k][nb]) != "O":
                                    sep_outside = False
                                    break
                            if sep_outside:
                                span_space = 0
                                break
                    for p in range(rs, re + 1):
                        remove[p] = 1
                    if span_space:
                        remove[rs] = 2
            run_start = -1

    for k in range(n):
        s = items[k]
        out = []
        for j in range(len(s)):
            if j < min_len and remove[j] == 2:
                out.append(" ")
                continue
            if j < min_len and remove[j] == 1:
                continue
            out.append(s[j])
        items[k] = "".join(out)


def clean_names_collectively(
    items: list[str], mode: str = "", input_lens: list[int] | None = None
) -> list[str]:
    """The names with their common leading and trailing affixes removed.

    ``mode`` is "files", "folders" or "" (the permissive legacy behaviour);
    ``input_lens`` are the ORIGINAL input filename lengths, aligned to the
    names, consulted only in "files" mode. A copy is returned; the input is
    not modified.
    """
    items = list(items)
    if input_lens is None:
        input_lens = []
    input_lens = list(input_lens)
    if not items:
        return []

    # Case insensitive: the common affixes are detected on a lower-cased copy
    # and stripped from the original names by length.
    lower = [s.lower() for s in items]

    # Longest common prefix, exact: prefixes are never cropped, so the classic
    # scan shrinks the first name until it prefixes every other.
    common_prefix = lower[0]
    for s in lower[1:]:
        while common_prefix and not s.startswith(common_prefix):
            common_prefix = common_prefix[:-1]
        if not common_prefix:
            break
    # Nor roman numerals counting i, ii, iii, iv (lower-cased here).
    if common_prefix.endswith(" i"):
        common_prefix = common_prefix[:-1]
    # Never cut inside a word or a number: pull the prefix back to the nearest
    # class boundary shared by every name.
    common_prefix = align_affix_to_run_boundary("prefix", common_prefix, lower)

    # Longest common suffix, crop-aware, then the symmetric boundary
    # protection.
    common_suffix = _crop_aware_common_affix(lower, mode, input_lens)
    common_suffix = align_affix_to_run_boundary("suffix", common_suffix, lower)

    # A bracket may only be stripped if its matching partner is stripped too:
    # read together (prefix left-to-right, then suffix) the affix brackets
    # must form a balanced string, or every stripped bracket is kept.
    pref_br = _collect_brackets(common_prefix)
    suf_br = _collect_brackets(common_suffix)
    keep_pref_br = keep_suf_br = ""
    if (pref_br or suf_br) and not brackets_balanced(pref_br + suf_br):
        keep_pref_br, keep_suf_br = pref_br, suf_br

    # Removal is by length, so the original (mixed-case) characters go too.
    # The prefix is fully shared, so every name loses the same length; the
    # suffix may have been cropped, so each name loses only the portion it
    # carries. Keep the pre-strip names: the bracket-partner pass below needs
    # the whole original to work out which bracket matches which.
    orig = list(items)
    if common_prefix:
        p = len(common_prefix)
        for i in range(len(items)):
            items[i] = keep_pref_br + items[i][p:]
    if common_suffix:
        for i in range(len(items)):
            ks = _longest_affix_portion(lower[i], common_suffix)
            length = len(items[i]) - ks
            # Identical names: the prefix may already have consumed the whole
            # string, which would make the length negative; clamp so such names
            # come back empty.
            if length < 0:
                length = 0
            items[i] = items[i][:length] + keep_suf_br

    # A stripped bracket may belong to a pair whose other end is NOT in the
    # other affix (consecutive rather than nested pairs); that partner goes
    # too. Skipped when the affix brackets were kept instead of stripped.
    if not keep_pref_br and not keep_suf_br and (pref_br or suf_br):
        _drop_orphaned_bracket_partners(items, orig, len(common_prefix))

    # Whatever common passage still sits in the middle of every name.
    _remove_common_middle(items)
    return items