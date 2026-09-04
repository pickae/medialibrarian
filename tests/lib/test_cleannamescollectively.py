"""Tests for medialib.lib.cleannamescollectively - the collective cleaner.

These pin the pure engine on its own - the crop-aware suffix scan and its
files/folders guards, the bracket pairing and partner rules, and the
interior-passage pass.
"""

import pytest

from medialib.lib import cleannamescollectively as cnc

pytestmark = pytest.mark.pure


def coll(names, mode="", lens=None):
    return cnc.clean_names_collectively(names, mode=mode, input_lens=lens)


# --- affix stripping, end to end ---------------------------------------------

@pytest.mark.parametrize(
    "names, expected",
    [
        # identical names -> all empty: a zero-length remainder must yield
        # empty strings rather than a negative substring length
        (["suffix.extension", "suffix.extension"], ["", ""]),
        # affixes cut at the letter/digit boundary: the digit run is what
        # differs, so "suf" and "fix.extension" both go
        (["suf1fix.extension", "suf2fix.extension"], ["1", "2"]),
        # an all-letters common affix with no class change is never cut:
        # "last"/"lost"/"list" share "l" and "st", both mid-word
        (["last", "lost", "list"], ["last", "lost", "list"]),
        # a purely numeric common prefix is not stripped
        (["11 Title", "12 Title"], ["11", "12"]),
        # a mix of an empty element and a value has no common affix to remove
        (["", "01"], ["", "01"]),
        # the shared trailing separator+word goes; the words themselves,
        # pure letters, are never cut into
        (["Alpha - Show", "Beta - Show", "Gamma - Show"], ["Alpha", "Beta", "Gamma"]),
        # a mixed token is cut where its class changes: the surrounding
        # single letters go, the digit run stays
        (["a16z", "a17z"], ["16", "17"]),
        # a common leading separator+word, per separator kind
        (["Alpha_Show", "Beta_Show", "Gamma_Show"], ["Alpha", "Beta", "Gamma"]),
        (["Alpha.Show", "Beta.Show", "Gamma.Show"], ["Alpha", "Beta", "Gamma"]),
        (["Alpha-Show", "Beta-Show", "Gamma-Show"], ["Alpha", "Beta", "Gamma"]),
        (["Show_Alpha", "Show_Beta", "Show_Gamma"], ["Alpha", "Beta", "Gamma"]),
        (["Show.1", "Show.2", "Show.3"], ["1", "2", "3"]),
        # a leading number stays whole when the shared affix hangs off it
        (["12_end", "13_end"], ["12", "13"]),
        # a single name is its own affix and comes back empty
        (["only one"], [""]),
        # no input, no output
        ([], []),
    ],
)
def test_affixes(names, expected):
    assert coll(names) == expected


def test_input_is_not_modified():
    names = ["Alpha - Show", "Beta - Show"]
    snapshot = list(names)
    coll(names)
    assert names == snapshot


# --- the crop-aware suffix ----------------------------------------------------

@pytest.mark.parametrize(
    "names, expected",
    [
        # a suffix cropped short on one name: each loses only what it carries
        (["1abc", "2ab", "3abc"], ["1", "2", "3"]),
        # case-insensitive, no cropping
        (["1ABC", "2abc", "3AbC"], ["1", "2", "3"]),
        # case-insensitive AND cropped
        (["1ABC", "2aB", "3AbC"], ["1", "2", "3"]),
        # a fully shared suffix, the classic case under the crop-aware scan
        (["1abc", "2abc", "3abc"], ["1", "2", "3"]),
        # a divergence that is NOT a crop blocks everything: the differing
        # d/c sits between the common "ab" and the edge
        (["1abd", "2abc", "3abc"], ["1abd", "2abc", "3abc"]),
        # legacy is permissive: "di" carries the first two characters of
        # "die", so "die" is the shared suffix and both names come back empty
        (["die", "di"], ["", ""]),
        # the permissive crop can reach past a divergent middle: " des k" is
        # carried by "mmmm des k" in full and cropped to " des" on "nn des"
        (["mmmm des k", "nn des"], ["mmmm", "nn"]),
        # a non-monotonic crop is permissive too: the shorter name carrying
        # less is fine without the files guards
        (["m des k", "nnnnn des"], ["m", "nnnnn"]),
        # the shared "album " prefix goes; the stray trailing "o" of "two" is not
        # a crop of "one", so nothing is stripped from the right
        (["album one", "album two"], ["one", "two"]),
    ],
)
def test_crop_aware_suffix_legacy(names, expected):
    assert coll(names) == expected


def test_crop_aware_scan_direct():
    # the whole first name can be the suffix when the other carries its start
    assert cnc._crop_aware_common_affix(["die", "di"], "", []) == "die"
    # the candidate with the best smallest-carried portion wins over the
    # merely longer one: " des k" (both carry >= 4) beats "des k" (one carries 3)
    assert cnc._crop_aware_common_affix(
        ["mmmm des k", "nn des"], "", []) == " des k"
    # nothing shared at all
    assert cnc._crop_aware_common_affix(["alpha", "beta"], "", []) == ""
    assert cnc._crop_aware_common_affix([], "", []) == ""


# --- the files/folders partial-crop guards -----------------------------------

@pytest.mark.parametrize(
    "names, mode, lens, expected",
    [
        # WORD GUARD: a cropped suffix that is a single word is not stripped
        # in files mode ("die"/"di" would mangle to ""/"e")
        (["die", "di"], "files", [4, 3], ["die", "di"]),
        # ... while the permissive legacy path still strips it
        (["die", "di"], "", None, ["", ""]),
        # a crop spanning >= 2 tokens is safe when the input lengths describe
        # a real fixed-width crop: 10 and 9, the space re-adds to 10
        (["mmmm des k", "nn des"], "files", [10, 9], ["mmmm", "nn"]),
        # SAME-WIDTH GUARD: 10 vs 6 differ by 4 but only one space was
        # cropped, so it is not a genuine fixed-width truncation
        (["mmmm des k", "nn des"], "files", [10, 6], ["mmmm des k", "nn des"]),
        # TRUNCATION-CONSISTENCY GUARD: the shorter input carries MORE of the
        # candidate than the longer one - impossible for a real end-crop
        (["m des k", "nnnnn des"], "files", [7, 9], ["m des k", "nnnnn des"]),
        # ... and the same shape with no mode is permissive
        (["m des k", "nnnnn des"], "", None, ["m", "nnnnn"]),
        # FOLDERS never get partial crops: the cropped "abc"/"ab" is left
        (["1abc", "2ab", "3abc"], "folders", [4, 3, 4], ["1abc", "2ab", "3abc"]),
        # ... and files mode blocks the same single-word crop by its word guard
        (["1abc", "2ab", "3abc"], "files", [4, 3, 4], ["1abc", "2ab", "3abc"]),
        # a fully shared suffix is accepted in every mode: no crop, no guards
        (["1abc", "2abc", "3abc"], "folders", [4, 4, 4], ["1", "2", "3"]),
        (["1abc", "2abc", "3abc"], "files", [4, 4, 4], ["1", "2", "3"]),
        # the same-width crop makes the bracket partner pass even: comic
        # volumes, each of the two bracket pairs goes as a whole
        (["(1946-1948) (Verlag 2011)", "(2011)", "(Verlag 2015)"],
         "files", [25, 6, 13],
         ["1946-1948 Verlag 2011", "2011", "Verlag 2015"]),
        # a missing length defaults to 0, as bash's ${lens[i]:-0} does - and a
        # zero-width source cannot be reconciled to the other's width, so
        # the same-width guard rejects the crop
        (["mmmm des k", "nn des"], "files", [10], ["mmmm des k", "nn des"]),
    ],
)
def test_mode_guards(names, mode, lens, expected):
    assert coll(names, mode=mode, lens=lens) == expected


def test_word_guard_direct():
    assert cnc._count_tokens("die") == 1
    assert cnc._count_tokens("des k") == 2
    assert cnc._count_tokens("a 12 b") == 3
    assert cnc._count_tokens("") == 0
    assert cnc._count_tokens("   ") == 0


def test_monotonic_guard_direct():
    # a shorter source carrying more of the candidate is a violation
    assert not cnc._monotonic_ok([6, 4], [7, 9])
    assert cnc._monotonic_ok([4, 6], [7, 9])
    assert cnc._monotonic_ok([6, 4], [9, 7])
    # equal lengths never violate
    assert cnc._monotonic_ok([1, 9], [5, 5])


def test_same_width_guard_direct():
    # 40 and 39(+1 trimmed space) both reach 40
    assert cnc._same_width_ok(" des k", [6, 4], [10, 9])
    # 10 vs 6(+1): no common length
    assert not cnc._same_width_ok(" des k", [6, 4], [10, 6])
    # already equal
    assert cnc._same_width_ok(" des k", [5, 5], [10, 10])


def test_crop_boundary_guard_direct():
    # the character before the crop is a letter of the same class: overlap
    assert not cnc._crop_boundary_ok(["album one", "album two"], "one", [3, 1])
    # a separator before the crop in every carrying name: a genuine crop
    assert cnc._crop_boundary_ok(["mmmm des k", "nn des"], " des k", [6, 4])
    # a separator-leading candidate has no boundary class to guard
    assert cnc._crop_boundary_ok(["die", "di"], " e", [1, 0])


# --- the bracket rules --------------------------------------------------------

def test_collect_brackets():
    assert cnc._collect_brackets("([x]a[x])") == "(" + "[]" + "[]" + ")"
    assert cnc._collect_brackets("no brackets here") == ""
    assert cnc._collect_brackets("(a(b)c)") == "((" + "))"
    assert cnc._collect_brackets("a[b]{c}<d>") == "[]" + "{}" + "<>"


def test_brackets_balanced():
    assert cnc.brackets_balanced("")
    assert cnc.brackets_balanced("()")
    assert cnc.brackets_balanced("([])")
    assert cnc.brackets_balanced("()()")
    assert cnc.brackets_balanced("(())")
    assert not cnc.brackets_balanced(")")
    assert not cnc.brackets_balanced("((")
    assert not cnc.brackets_balanced("([)]")
    # the pairs are matched by TYPE: a "(" cannot cancel a "]"
    mixed = "(" + "["
    assert not cnc.brackets_balanced(mixed)
    assert cnc.brackets_balanced("[]")


def test_bracket_pairs():
    # the inner pair completes first, the outer last
    assert cnc._bracket_pairs("(a(b)c)") == [(2, 4), (0, 6)]
    assert cnc._bracket_pairs("()()") == [(0, 1), (2, 3)]
    # a name that merely contains ")(" has no pair there
    assert cnc._bracket_pairs("9)(1") == []
    # "([)]": the square pair matches; the outer "(" pairs with nothing
    assert cnc._bracket_pairs("([)]") == [(1, 3)]
    # a closer whose top is the wrong opener does not pop it
    assert cnc._bracket_pairs("([)") == []


@pytest.mark.parametrize(
    "names, expected",
    [
        # a matched pair: prefix "(" cancels suffix ")", so both go
        (["(10a10)", "(10b10)"], ["a", "b"]),
        # mismatched: prefix "(" and suffix "(" don't cancel, so every
        # stripped bracket is kept (re-appended)
        (["(10a10(", "(10b10("], ["(a(", "(b("]),
        # a lone closing bracket in the suffix with no opening partner is
        # kept; the leading word still goes
        (["Show 1)", "Show 2)"], ["1)", "2)"]),
        # a fully balanced group living entirely in the prefix is safe to go
        (["(intro) 1", "(intro) 2"], ["1", "2"]),
        # multiple mixed types that cancel across the affixes all go
        (["([x]a[x])", "([x]b[x])"], ["a", "b"]),
        # the same shapes with the trailing ")" missing: the affix brackets
        # are unbalanced, so every one of them is kept
        (["([x]a[x]", "([x]b[x]"], ["[][]", "([]b[][]"]),
        # type-mismatched: prefix "(" cannot cancel suffix "]"
        (["(10a10]", "(10b10]"], ["(a]", "(b]"]),
        # NESTED: the leading "(" IS closed by the trailing ")" - one pair,
        # both ends in the affixes, so it goes as one
        (["(a(b)c)", "(a(d)c)"], ["b", "d"]),
        # CONSECUTIVE: the affix brackets belong to two different pairs;
        # each pair goes as a whole, taking its partner with it
        (["(ab) (cd)", "(ef) (gh)"], ["ab", "ef gh"]),
        # only a genuinely matched pair is ever broken up
        (["9)(1", "8)(2"], ["9)(1", "8)(2"]),
    ],
)
def test_affix_brackets(names, expected):
    assert coll(names) == expected


def test_drop_orphaned_partners_direct():
    items = ["ab) (cd", "ef) (gh"]
    originals = ["(ab) (cd)", "(ef) (gh)"]
    cnc._drop_orphaned_bracket_partners(items, originals, 1)
    assert items == ["ab cd", "ef gh"]
    # nothing orphaned: the retained range holds both ends of every pair
    items = ["(x)"]
    cnc._drop_orphaned_bracket_partners(items, ["(x)"], 0)
    assert items == ["(x)"]


# --- the interior passage -----------------------------------------------------

@pytest.mark.parametrize(
    "names, expected",
    [
        # an aligned run of >= 3 delimited on both sides is removed; the
        # consumed delimiter leaves a single space, per separator kind
        (["P abc Q", "R abc S"], ["P Q", "R S"]),
        (["P-abc-Q", "R-abc-S"], ["P Q", "R S"]),
        (["P_abc_Q", "R_abc_S"], ["P Q", "R S"]),
        (["9.abc.1", "8.abc.2"], ["9 1", "8 2"]),
        (["9(abc)1", "8(abc)2"], ["9 1", "8 2"]),
        # the removable token can be a number
        (["P 123 Q", "R 123 S"], ["P Q", "R S"]),
        # the block may span multiple tokens
        (["1 the show 2", "3 the show 4"], ["1 2", "3 4"]),
        (["zz the big show qq", "yy the big show ww"], ["zz qq", "yy ww"]),
        (["A-key 12 word-Q", "B-key 12 word-R"], ["A Q", "B R"]),
        # two separate delimited middles both go in one pass
        (["1 abc 2 def 3", "X abc Y def Z"], ["1 2 3", "X Y Z"]),
        # three names sharing one delimited middle
        (["A xyz B", "C xyz D", "E xyz F"], ["A B", "C D", "E F"]),
        # the numbers keep ALL their digits: the leading "3" of 10903 stays
        # with its number, the consumed dashes collapse to one space
        (["10903-deu-x", "10963-deu-y"], ["10903 x", "10963 y"]),
    ],
)
def test_interior_removed(names, expected):
    assert coll(names) == expected


@pytest.mark.parametrize(
    "names, expected",
    [
        # glued to letters on both sides: would cut into a word
        (["PabcQ", "RabcS"], ["PabcQ", "RabcS"]),
        # a plain class change is not a separator
        (["1abcQ2", "XabcRY"], ["1abcQ2", "XabcRY"]),
        # a digit run glued to a longer number: never bite into a number
        (["10903abcQ", "10963abcR"], ["10903abcQ", "10963abcR"]),
        # 2 chars is below the minimum of 3
        (["Pa Q", "Ra S"], ["Pa Q", "Ra S"]),
        # a delimiter on only one side is not enough
        (["9-abcQ", "8-abcR"], ["9-abcQ", "8-abcR"]),
        (["9abc-Q", "8abc-R"], ["9abc-Q", "8abc-R"]),
        # the same text at different offsets is not aligned
        (["abcX", "YabcZ"], ["abcX", "YabcZ"]),
        # a run reaching the shortest name's end is a suffix, not a middle
        (["Pabc", "Rabc"], ["Pabc", "Rabc"]),
        # a boundary that is a separator in some names but a letter in
        # others is not a clean delimiter in every name
        (["Xdiey", "Xdi z"], ["Xdiey", "Xdi z"]),
    ],
)
def test_interior_kept(names, expected):
    assert coll(names) == expected


@pytest.mark.parametrize(
    "names, expected",
    [
        # the run's "(" is closed AFTER the divergence: the "(" stays, only
        # the cropped "abc " goes - never the half-stripped "9x)1"
        (["9(abc x)1", "8(abc y)2"], ["9(x)1", "8(y)2"]),
        # symmetric: the ")" stays, the block is cropped on the right
        (["P (x abc) Q", "R (y abc) S"], ["P (x) Q", "R (y) S"]),
        # both edges straddle at once: each keeps its bracket
        (["P (abc D ghi) Q", "R (abc E ghi) S"], ["P (D) Q", "R (E) S"]),
        # nesting is respected: both pairs of "((...))" survive whole
        (["P ((abc x)) Q", "R ((abc y)) S"], ["P ((x)) Q", "R ((y)) S"]),
        # every recognised bracket type behaves the same
        (["1 [abc x] 2", "3 [abc y] 4"], ["1 [x] 2", "3 [y] 4"]),
        (["1 {abc x} 2", "3 {abc y} 4"], ["1 {x} 2", "3 {y} 4"]),
        (["1 <abc x> 2", "3 <abc y> 4"], ["1 <x> 2", "3 <y> 4"]),
        # the partner may sit at different offsets in each name, including
        # beyond the compared length of the shortest name
        (["1 (abc x) 2", "3 (abc yyyy) 4"], ["1 (x) 2", "3 (yyyy) 4"]),
        # cropping may cost the removal: only "a " is left, below the minimum
        (["9(a x)1", "8(a y)2"], ["9(a x)1", "8(a y)2"]),
        # only MATCHED brackets are protected: a lone "(" pairs with nothing
        (["9(abc x1", "8(abc y2"], ["9 x1", "8 y2"]),
        # the replacement space is dropped when the retained text is
        # separated anyway: the character outside the block is a separator
        # in every name
        (["1- abc x", "2. abc y"], ["1-x", "2.y"]),
    ],
)
def test_interior_brackets(names, expected):
    assert coll(names) == expected


def test_remove_common_middle_direct():
    items = ["1 abc 2 def 3", "X abc Y def Z"]
    cnc._remove_common_middle(items)
    assert items == ["1 2 3", "X Y Z"]
    # a single name has no "common" middle
    items = ["1 abc 2"]
    cnc._remove_common_middle(items)
    assert items == ["1 abc 2"]


# --- the roman-numeral prefix rule --------------------------------------------

def test_roman_prefix():
    # the shared " i" of "season i"/"season ii" counting is peeled off the
    # common prefix, so the numeral itself survives the prefix strip
    assert coll(["season i 1", "season ii 1"]) == ["i", "ii"]
    # with nothing after the numeral, the permissive crop-aware suffix takes
    # the whole longer name (the shorter one carries its leading part), so
    # both names come back empty
    assert coll(["season i", "season ii"]) == ["", ""]
    # where plain boundary alignment would keep the prefix "season i"
    # (no letter runs next to it in either name), the rule peels the " i"
    # first: the numeral survives on the side that carries it alone
    assert coll(["season i x", "season ii"]) == ["i x", "ii"]


# --- case insensitivity across the whole pass ---------------------------------

def test_case_insensitive():
    assert coll(["Show - INTRO", "show - main", "SHOW - outro"]) == [
        "INTRO", "main", "outro"]
    # the original (mixed-case) characters are what is kept
    assert coll(["Suf1Fix.X", "suf2fix.x"]) == ["1", "2"]

# --- the cases the bash white box pinned --------------------------------------
# Carried over whole, with its own descriptions as the ids: that white box was
# this module's specification, and this is what it said. The reasoning behind
# each rule is in the classes above; this is the ledger of the rules themselves.

COLLECTIVE_CASES = [
    pytest.param(
        ['suffix.extension', 'suffix.extension'],
        '',
        None,
        ['', ''],
        id='identical names -> all empty'),
    pytest.param(
        ['suf1fix.extension', 'suf2fix.extension'],
        '',
        None,
        ['1', '2'],
        id='affixes cut at letter/digit boundary'),
    pytest.param(
        ['last', 'lost', 'list'],
        '',
        None,
        ['last', 'lost', 'list'],
        id='pure-letter affixes not cut'),
    pytest.param(
        ['11 Title', '12 Title'],
        '',
        None,
        ['11', '12'],
        id='numeric prefix preserved'),
    pytest.param(
        ['', '01'],
        '',
        None,
        ['', '01'],
        id='empty element tolerated'),
    pytest.param(
        ['Alpha - Show', 'Beta - Show', 'Gamma - Show'],
        '',
        None,
        ['Alpha', 'Beta', 'Gamma'],
        id='common trailing word removed, words not cut'),
    pytest.param(
        ['a16z', 'a17z'],
        '',
        None,
        ['16', '17'],
        id='mixed token cut at class boundary'),
    pytest.param(
        ['1abc', '2ab', '3abc'],
        '',
        None,
        ['1', '2', '3'],
        id='cropped suffix stripped'),
    pytest.param(
        ['1ABC', '2abc', '3AbC'],
        '',
        None,
        ['1', '2', '3'],
        id='shared suffix stripped, case-insensitive'),
    pytest.param(
        ['1ABC', '2aB', '3AbC'],
        '',
        None,
        ['1', '2', '3'],
        id='cropped suffix stripped, case-insensitive'),
    pytest.param(
        ['1abc', '2abc', '3abc'],
        '',
        None,
        ['1', '2', '3'],
        id='uncropped shared suffix stripped'),
    pytest.param(
        ['1abd', '2abc', '3abc'],
        '',
        None,
        ['1abd', '2abc', '3abc'],
        id='conflicting tail blocks suffix strip'),
    pytest.param(
        ['(10a10)', '(10b10)'],
        '',
        None,
        ['a', 'b'],
        id='matched brackets stripped'),
    pytest.param(
        ['(10a10(', '(10b10('],
        '',
        None,
        ['(a(', '(b('],
        id='mismatched brackets re-appended'),
    pytest.param(
        ['Show 1)', 'Show 2)'],
        '',
        None,
        ['1)', '2)'],
        id='orphan closing bracket kept'),
    pytest.param(
        ['(intro) 1', '(intro) 2'],
        '',
        None,
        ['1', '2'],
        id='balanced bracket group in prefix removed'),
    pytest.param(
        ['([x]a[x])', '([x]b[x])'],
        '',
        None,
        ['a', 'b'],
        id='mixed matched bracket types stripped'),
    pytest.param(
        ['(10a10]', '(10b10]'],
        '',
        None,
        ['(a]', '(b]'],
        id='type-mismatched brackets re-appended'),
    pytest.param(
        ['(a(b)c)', '(a(d)c)'],
        '',
        None,
        ['b', 'd'],
        id='nested pair stripped as one'),
    pytest.param(
        ['(ab) (cd)', '(ef) (gh)'],
        '',
        None,
        ['ab', 'ef gh'],
        id='consecutive pairs each go'),
    pytest.param(
        ['9)(1', '8)(2'],
        '',
        None,
        ['9)(1', '8)(2'],
        id='unmatched )( left alone'),
    pytest.param(
        ['Alpha_Show', 'Beta_Show', 'Gamma_Show'],
        '',
        None,
        ['Alpha', 'Beta', 'Gamma'],
        id='common suffix stripped (underscore)'),
    pytest.param(
        ['Alpha.Show', 'Beta.Show', 'Gamma.Show'],
        '',
        None,
        ['Alpha', 'Beta', 'Gamma'],
        id='common suffix stripped (dot)'),
    pytest.param(
        ['Alpha-Show', 'Beta-Show', 'Gamma-Show'],
        '',
        None,
        ['Alpha', 'Beta', 'Gamma'],
        id='common suffix stripped (dash)'),
    pytest.param(
        ['Show_Alpha', 'Show_Beta', 'Show_Gamma'],
        '',
        None,
        ['Alpha', 'Beta', 'Gamma'],
        id='common prefix stripped (underscore)'),
    pytest.param(
        ['Show.1', 'Show.2', 'Show.3'],
        '',
        None,
        ['1', '2', '3'],
        id='common prefix stripped (dot)'),
    pytest.param(
        ['12_end', '13_end'],
        '',
        None,
        ['12', '13'],
        id='numeric prefix kept against underscore suffix'),
    pytest.param(
        ['P abc Q', 'R abc S'],
        '',
        None,
        ['P Q', 'R S'],
        id='space-delimited middle removed'),
    pytest.param(
        ['P-abc-Q', 'R-abc-S'],
        '',
        None,
        ['P Q', 'R S'],
        id='dash-delimited middle removed'),
    pytest.param(
        ['P_abc_Q', 'R_abc_S'],
        '',
        None,
        ['P Q', 'R S'],
        id='underscore-delimited middle removed'),
    pytest.param(
        ['9.abc.1', '8.abc.2'],
        '',
        None,
        ['9 1', '8 2'],
        id='dot-delimited middle removed'),
    pytest.param(
        ['9(abc)1', '8(abc)2'],
        '',
        None,
        ['9 1', '8 2'],
        id='bracket-delimited middle removed'),
    pytest.param(
        ['P 123 Q', 'R 123 S'],
        '',
        None,
        ['P Q', 'R S'],
        id='delimited number middle removed'),
    pytest.param(
        ['1 the show 2', '3 the show 4'],
        '',
        None,
        ['1 2', '3 4'],
        id='two-word delimited middle removed'),
    pytest.param(
        ['zz the big show qq', 'yy the big show ww'],
        '',
        None,
        ['zz qq', 'yy ww'],
        id='three-word delimited middle removed'),
    pytest.param(
        ['A-key 12 word-Q', 'B-key 12 word-R'],
        '',
        None,
        ['A Q', 'B R'],
        id='mixed-token dash middle removed'),
    pytest.param(
        ['1 abc 2 def 3', 'X abc Y def Z'],
        '',
        None,
        ['1 2 3', 'X Y Z'],
        id='multiple delimited middles removed'),
    pytest.param(
        ['A xyz B', 'C xyz D', 'E xyz F'],
        '',
        None,
        ['A B', 'C D', 'E F'],
        id='delimited middle removed across three'),
    pytest.param(
        ['10903-deu-x', '10963-deu-y'],
        '',
        None,
        ['10903 x', '10963 y'],
        id='delimited middle keeps whole numbers'),
    pytest.param(
        ['PabcQ', 'RabcS'],
        '',
        None,
        ['PabcQ', 'RabcS'],
        id='glued letter middle kept'),
    pytest.param(
        ['1abcQ2', 'XabcRY'],
        '',
        None,
        ['1abcQ2', 'XabcRY'],
        id='class-change-only middle kept'),
    pytest.param(
        ['10903abcQ', '10963abcR'],
        '',
        None,
        ['10903abcQ', '10963abcR'],
        id='glued number middle kept'),
    pytest.param(
        ['Pa Q', 'Ra S'],
        '',
        None,
        ['Pa Q', 'Ra S'],
        id='short middle fragment kept'),
    pytest.param(
        ['9-abcQ', '8-abcR'],
        '',
        None,
        ['9-abcQ', '8-abcR'],
        id='left-only delimiter middle kept'),
    pytest.param(
        ['9abc-Q', '8abc-R'],
        '',
        None,
        ['9abc-Q', '8abc-R'],
        id='right-only delimiter middle kept'),
    pytest.param(
        ['abcX', 'YabcZ'],
        '',
        None,
        ['abcX', 'YabcZ'],
        id='unaligned middle fragment kept'),
    pytest.param(
        ['Pabc', 'Rabc'],
        '',
        None,
        ['Pabc', 'Rabc'],
        id='end-touching run left to suffix pass'),
    pytest.param(
        ['9(abc x)1', '8(abc y)2'],
        '',
        None,
        ['9(x)1', '8(y)2'],
        id='straddling open bracket cropped out'),
    pytest.param(
        ['P (x abc) Q', 'R (y abc) S'],
        '',
        None,
        ['P (x) Q', 'R (y) S'],
        id='straddling close bracket cropped out'),
    pytest.param(
        ['P (abc D ghi) Q', 'R (abc E ghi) S'],
        '',
        None,
        ['P (D) Q', 'R (E) S'],
        id='both edges cropped to their brackets'),
    pytest.param(
        ['P ((abc x)) Q', 'R ((abc y)) S'],
        '',
        None,
        ['P ((x)) Q', 'R ((y)) S'],
        id='nested straddling brackets kept'),
    pytest.param(
        ['1 [abc x] 2', '3 [abc y] 4'],
        '',
        None,
        ['1 [x] 2', '3 [y] 4'],
        id='straddling bracket cropped ([])'),
    pytest.param(
        ['1 {abc x} 2', '3 {abc y} 4'],
        '',
        None,
        ['1 {x} 2', '3 {y} 4'],
        id='straddling bracket cropped ({})'),
    pytest.param(
        ['1 <abc x> 2', '3 <abc y> 4'],
        '',
        None,
        ['1 <x> 2', '3 <y> 4'],
        id='straddling bracket cropped (<>)'),
    pytest.param(
        ['1 (abc x) 2', '3 (abc yyyy) 4'],
        '',
        None,
        ['1 (x) 2', '3 (yyyy) 4'],
        id='partner offset differs per name'),
    pytest.param(
        ['9(a x)1', '8(a y)2'],
        '',
        None,
        ['9(a x)1', '8(a y)2'],
        id='cropped block too short -> kept'),
    pytest.param(
        ['9(abc x1', '8(abc y2'],
        '',
        None,
        ['9 x1', '8 y2'],
        id='unmatched bracket not protected'),
    pytest.param(
        ['1- abc x', '2. abc y'],
        '',
        None,
        ['1-x', '2.y'],
        id='no padding space next to a kept separator'),
    pytest.param(
        ['die', 'di'],
        'files',
        [4, 3],
        ['die', 'di'],
        id='files: single-word crop kept'),
    pytest.param(
        ['die', 'di'],
        '',
        None,
        ['', ''],
        id='legacy: single-word crop stripped'),
    pytest.param(
        ['mmmm des k', 'nn des'],
        'files',
        [10, 9],
        ['mmmm', 'nn'],
        id='files: multi-word same-width crop stripped'),
    pytest.param(
        ['mmmm des k', 'nn des'],
        'files',
        [10, 6],
        ['mmmm des k', 'nn des'],
        id='files: irreconcilable-length crop kept'),
    pytest.param(
        ['m des k', 'nnnnn des'],
        'files',
        [7, 9],
        ['m des k', 'nnnnn des'],
        id='files: non-monotonic crop kept'),
    pytest.param(
        ['m des k', 'nnnnn des'],
        '',
        None,
        ['m', 'nnnnn'],
        id='legacy: non-monotonic crop stripped'),
    pytest.param(
        ['1abc', '2ab', '3abc'],
        'folders',
        [4, 3, 4],
        ['1abc', '2ab', '3abc'],
        id='folders: no partial crop'),
    pytest.param(
        ['1abc', '2ab', '3abc'],
        'files',
        [4, 3, 4],
        ['1abc', '2ab', '3abc'],
        id='files: single-word crop kept (three)'),
    pytest.param(
        ['Xdiey', 'Xdi z'],
        '',
        None,
        ['Xdiey', 'Xdi z'],
        id='interior partial-word run kept'),
    pytest.param(
        ['(1946-1948) (Verlag 2011)', '(2011)', '(Verlag 2015)'],
        'files',
        [25, 6, 13],
        ['1946-1948 Verlag 2011', '2011', 'Verlag 2015'],
        id='files: consecutive bracket pairs each go'),
]


@pytest.mark.parametrize("names,mode,lens,expected", COLLECTIVE_CASES)
def test_the_case_the_bash_white_box_pinned(names, mode, lens, expected):
    assert coll(names, mode=mode, lens=lens) == expected
