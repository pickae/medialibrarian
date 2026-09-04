"""Tests for medialib.lib.cleannamesindividually - the per-name cleaner.

These pin the pure engine on its own - the ordered prefix rules, the separator
normalisation, the bracketed number-range tightening, and the word-protected
fragment removal.
"""

import pytest

from medialib.lib import cleannamesindividually as cni

pytestmark = pytest.mark.pure


def clean(raw, fragments_file=None):
    return cni.clean_names_individually(raw, fragments_file)


# --- the prefix and the cleaned name, end to end ------------------------------

@pytest.mark.parametrize(
    "raw, prefix, name",
    [
        ("20240102 The_Title", "20240102", "The Title"),
        ("2024 Something", "2024", "Something"),
        ("05-12 Title", "05-12", "Title"),
        ("01", "01", ""),
        ("Just A Name", "", "Just A Name"),
        # the offset-1 rule strips the characters AROUND the number, so they
        # must be delimiters, never letters or digits
        ("(01) Foo", "01", "Foo"),
        ("(01)", "01", ""),
        ("a16z", "", "a16z"),
        ("a01", "", "a01"),
        ("x99y", "", "x99y"),
        ("S01 Title", "", "S01 Title"),
        ("My_Show", "", "My Show"),
        # the colon lookalikes a Windows-safe renamer writes, both spelled out
        ("01 Kapitel\uff1a Titel", "01", "Kapitel - Titel"),
        ("01 Kapitel\u2236 Titel", "01", "Kapitel - Titel"),
        ("Title.", "", "Title"),
        # a separator against a number, one space on the other side, both orders
        ("01 -Title", "01", "Title"),
        ("01- Title", "01", "Title"),
        ("01 .Title", "01", "Title"),
        ("01. Title", "01", "Title"),
        ("01 _Title", "01", "Title"),
        ("01_ Title", "01", "Title"),
        ("01 \u2014Title", "01", "Title"),
        ("01\u2014 Title", "01", "Title"),
        # a separator flanked by spaces on BOTH sides is the " - " rule's, not this one's
        ("01 - Title", "01", "Title"),
        # two or more separators with no space; a SINGLE one is left untouched
        ("01--Title", "01", "Title"),
        ("01.-Title", "01", "Title"),
        ("01..Title", "01", "Title"),
        ("01\u2014\u2014Title", "01", "Title"),
        ("01-Title", "01", "-Title"),
        # comma and semicolon are separators too
        ("01, Title", "01", "Title"),
        ("01 ,Title", "01", "Title"),
        ("01; Title", "01", "Title"),
        ("01 ;Title", "01", "Title"),
        ("01;;Title", "01", "Title"),
        ("01,-Title", "01", "Title"),
        # a range is not a prefix: peeling "12" out of "(12-15)" would strand "15)"
        ("(1 2)", "", "(1-2)"),
        ("Titel (9 10)", "", "Titel (9-10)"),
        ("(9 10) Titel", "", "(9-10) Titel"),
        ("(12 15) Titel", "", "(12-15) Titel"),
        ("[90 100] Titel", "", "[90-100] Titel"),
        ("{10 11}", "", "{10-11}"),
        ("Titel (5 5)", "", "Titel (5-5)"),
        ("(5 5) Titel", "", "(5-5) Titel"),
        ("(12-15) Titel", "", "(12-15) Titel"),
        ("(12/15) Titel", "", "(12/15) Titel"),
        ("(12) Titel", "12", "Titel"),
        # an already dashed range with a stray space on ONE side of the dash
        ("1 -2", "1-2", ""),
        ("1- 2", "1-2", ""),
        ("9 -10 Titel", "9-10", "Titel"),
        ("Titel (1 -2)", "", "Titel (1-2)"),
        # a bare number range is ONE prefix, extended over dash-joined digit runs
        ("1-2", "1-2", ""),
        ("1-2 Titel", "1-2", "Titel"),
        ("90-100 Titel", "90-100", "Titel"),
        ("2024-2025 Something", "2024-2025", "Something"),
        ("1-2-3 Titel", "1-2-3", "Titel"),
        # a dash NOT followed by a digit is an ordinary separator and ends the prefix
        ("10903-deu-x", "10903", "-deu-x"),
    ],
)
def test_clean(raw, prefix, name):
    assert clean(raw) == (prefix, name)


# --- the number/separator/letter normalisation on its own ----------------------

@pytest.mark.parametrize(
    "raw, expected",
    [
        # a bracket a space (and optionally a separator) away from a number hugs it
        ("( 01", "(01"),
        ("01 )", "01)"),
        ("(- 01", "(01"),
        ("01 -)", "01)"),
        ("(, 07", "(07"),
        ("07 ,)", "07)"),
        ("[ 05", "[05"),
        ("05 ]", "05]"),
        ("{ 9", "{9"),
        ("9 }", "9}"),
        ("< 3", "<3"),
        ("3 >", "3>"),
        ("( 2024", "(2024"),
        ("2024 )", "2024)"),
        ("(2024)", "(2024)"),
        # direction matters: an OPENING bracket after the number opens the title,
        # so its space is kept; a CLOSING one before the number closes the title
        ("01 (", "01 ("),
        ("05 [", "05 ["),
        ("01 -(", "01 -("),
        (") 01", ") 01"),
        ("] 05", "] 05"),
        (")- 01", ")- 01"),
        ("01 }", "01}"),
        ("01 >", "01>"),
        ("{ 01", "{01"),
        ("< 01", "<01"),
        # a bracketed pair of non-descending numbers is a RANGE
        ("(1 2)", "(1-2)"),
        ("[3 7]", "[3-7]"),
        ("{2 9}", "{2-9}"),
        ("<0 4>", "<0-4>"),
        # compared as NUMBERS, not digit by digit: 9 < 10 although "9" > "10"
        ("(9 10)", "(9-10)"),
        ("[90 100]", "[90-100]"),
        ("(1990 2020)", "(1990-2020)"),
        ("(08 9)", "(08-9)"),
        ("(007 8)", "(007-8)"),
        ("(09 10)", "(09-10)"),
        ("(010 9)", "(010 9)"),
        # equal numbers are a range: "part 5 out of 5"
        ("(5 5)", "(5-5)"),
        ("(12 12)", "(12-12)"),
        ("[7 7]", "[7-7]"),
        ("(0 0)", "(0-0)"),
        ("(05 5)", "(05-5)"),
        ("(007 7)", "(007-7)"),
        ("(99999999999999999999 999999999999999999990)",
         "(99999999999999999999-999999999999999999990)"),
        ("(999999999999999999990 99999999999999999999)",
         "(999999999999999999990 99999999999999999999)"),
        # what is NOT a range
        ("(2 1)", "(2 1)"),
        ("(100 90)", "(100 90)"),
        ("(05 4)", "(05 4)"),
        ("(1 2]", "(1 2]"),
        ("[1 2)", "[1 2)"),
        ("{1 2>", "{1 2>"),
        ("1 2", "1 2"),
        ("(1 2", "(1 2"),
        ("1 2)", "1 2)"),
        ("(1 2 3)", "(1 2 3)"),
        ("(1 a)", "(1 a)"),
        ("(a 1)", "(a 1)"),
        # idempotent, and the bracket rules run first so "( 1 2 )" closes then tightens
        ("(1-2)", "(1-2)"),
        ("( 1 2 )", "(1-2)"),
        ("(1  2)", "(1-2)"),
        ("(2 1) and (3 8)", "(2 1) and (3-8)"),
        # an already dashed range with a stray space on one side of the dash
        ("1 -2", "1-2"),
        ("1- 2", "1-2"),
        ("9 -10", "9-10"),
        ("90- 100", "90-100"),
        ("1  -2", "1-2"),
        ("1-  2", "1-2"),
        # descending pairs may well be a negative number, so they keep the space
        ("2 -1", "2 -1"),
        ("100- 90", "100- 90"),
        ("5 -5", "5-5"),
        ("5- 5", "5-5"),
        ("(5 -5)", "(5-5)"),
        ("(1 -2)", "(1-2)"),
        ("(1- 2)", "(1-2)"),
        ("[90- 100]", "[90-100]"),
        # a dash flanked by spaces on BOTH sides is the " - " rule's
        ("1 - 2", "1 - 2"),
        ("01 -Title", "01 Title"),
        ("01- Title", "01 Title"),
    ],
)
def test_normalize(raw, expected):
    assert cni.normalize_number_separator(raw) == expected


# --- the numeric comparison the range test relies on ---------------------------

@pytest.mark.parametrize(
    "a, b, expected",
    [
        ("9", "10", True),
        ("10", "9", False),
        ("08", "9", True),
        ("007", "8", True),
        ("010", "9", False),
        ("5", "5", True),
        ("0", "00", True),
        ("007", "7", True),
        ("00", "1", True),
        ("99999999999999999999", "99999999999999999999", True),
        ("999999999999999999990", "99999999999999999999", False),
    ],
)
def test_digits_not_greater(a, b, expected):
    assert cni.digits_not_greater(a, b) is expected


# --- case-insensitive fragment removal ----------------------------------------

def test_fragment_case_insensitive(tmp_path):
    frag = tmp_path / "fragments.txt"
    frag.write_text("sample\n", encoding="utf-8")
    assert clean("The SAMPLE Movie", str(frag)) == ("", "The Movie")
    assert clean("A SaMpLe Clip", str(frag)) == ("", "A Clip")
    assert clean("Plain Movie", str(frag)) == ("", "Plain Movie")


# --- fragment word protection ---------------------------------------------------

def test_fragment_word_protection(tmp_path):
    frag = tmp_path / "cat.txt"
    frag.write_text("cat\n", encoding="utf-8")
    # whole words go; pieces of words do not
    assert clean("the cat sat", str(frag)) == ("", "the sat")
    assert clean("concatenate", str(frag)) == ("", "concatenate")
    assert clean("category five", str(frag)) == ("", "category five")
    assert clean("one wildcat", str(frag)) == ("", "one wildcat")
    # dashes delimit words just like spaces do (the leftover double dash is cosmetic)
    assert clean("top-cat-show", str(frag)) == ("", "top--show")
    # punctuation is stripped before fragments, so "cat's" is the word "cats"
    assert clean("the cat's out", str(frag)) == ("", "the cats out")


def test_fragment_accented_letter_protects(tmp_path):
    frag = tmp_path / "caf.txt"
    frag.write_text("caf\n", encoding="utf-8")
    # accented letters count as letters: "caf" must not be torn out of "café"
    assert clean("un café serré", str(frag)) == ("", "un café serré")


def test_fragment_buried_in_a_word(tmp_path):
    frag = tmp_path / "tt.txt"
    frag.write_text("tt\n", encoding="utf-8")
    assert clean("must be better", str(frag)) == ("", "must be better")
    assert clean("must bett", str(frag)) == ("", "must bett")
    assert clean("butter", str(frag)) == ("", "butter")
    assert clean("a tt b", str(frag)) == ("", "a b")
    assert clean("letter tt", str(frag)) == ("", "letter")
    assert clean("tt", str(frag)) == ("", "")


def test_fragment_touching_one_edge_only(tmp_path):
    frag = tmp_path / "bet.txt"
    frag.write_text("bet\n", encoding="utf-8")
    assert clean("better times", str(frag)) == ("", "better times")
    assert clean("the abet", str(frag)) == ("", "the abet")
    assert clean("place your bet here", str(frag)) == ("", "place your here")


def test_fragment_file_edge_cases(tmp_path):
    # comments and blank lines are ignored, and a CRLF file tolerates the \r
    frag = tmp_path / "mixed.txt"
    frag.write_text("# a comment\r\n\ndog\r\n", encoding="utf-8")
    assert clean("The DOG House", str(frag)) == ("", "The House")
    # an explicitly given path that is missing or empty is exactly like none
    assert clean("Keep Me", str(tmp_path / "absent.txt")) == ("", "Keep Me")
    empty = tmp_path / "empty.txt"
    empty.write_text("", encoding="utf-8")
    assert clean("Keep Me", str(empty)) == ("", "Keep Me")


# --- which fragments file a run uses -------------------------------------------

def test_fragments_file_for_explicit(tmp_path):
    explicit = tmp_path / "mine.txt"
    explicit.write_text("sample\n", encoding="utf-8")
    assert cni.fragments_file_for(str(explicit)) == (str(explicit), True)
    assert cni.fragments_file_for(str(tmp_path / "absent.txt")) == ("", False)
    empty = tmp_path / "empty.txt"
    empty.write_text("", encoding="utf-8")
    assert cni.fragments_file_for(str(empty)) == ("", False)


def test_the_default_sits_under_the_base_everything_else_uses(tmp_path, monkeypatch):
    """`data/fragments.txt` is asked for the same way the podcast tables and the
    beets log are, so one variable moves all three. It used to be resolved from
    this module's own location, which `CLI_SCRIPT_DIR` could not reach."""
    monkeypatch.setenv("CLI_SCRIPT_DIR", str(tmp_path))
    (tmp_path / "data").mkdir()
    fragments = tmp_path / "data" / "fragments.txt"
    fragments.write_text("sample\n", encoding="utf-8")
    assert cni.fragments_file_for() == (str(fragments), True)


def test_an_absent_default_is_not_an_error(tmp_path, monkeypatch):
    """A checkout without a fragments file is normal - the folder is not tracked."""
    monkeypatch.setenv("CLI_SCRIPT_DIR", str(tmp_path))
    assert cni.fragments_file_for() == ("", True)

# --- the cases the bash white box pinned --------------------------------------
# Carried over whole, with its own descriptions as the ids: that white box was
# this module's specification, and this is what it said. Its fragments-file
# cases are not here - they need a file, and the tests above build one.
PINNED_NAMES = [
    pytest.param(
        '20240102 The_Title',
        '20240102',
        'The Title',
        id='date prefix + underscores'),
    pytest.param(
        '2024 Something',
        '2024',
        'Something',
        id='four-digit prefix'),
    pytest.param(
        '05-12 Title',
        '05-12',
        'Title',
        id='NN-NN prefix'),
    pytest.param(
        '01',
        '01',
        '',
        id='bare number -> empty name'),
    pytest.param(
        'Just A Name',
        '',
        'Just A Name',
        id='no prefix'),
    pytest.param(
        '(01) Foo',
        '01',
        'Foo',
        id='bracketed number prefix split'),
    pytest.param(
        '(01)',
        '01',
        '',
        id='bracketed number, empty name'),
    pytest.param(
        'a16z',
        '',
        'a16z',
        id='letter-wrapped number kept whole'),
    pytest.param(
        'a01',
        '',
        'a01',
        id='letter+number token kept whole'),
    pytest.param(
        'x99y',
        '',
        'x99y',
        id='letter-number-letter kept whole'),
    pytest.param(
        'S01 Title',
        '',
        'S01 Title',
        id='letter then number+title kept'),
    pytest.param(
        'My_Show',
        '',
        'My Show',
        id='underscores -> spaces'),
    pytest.param(
        'Title.',
        '',
        'Title',
        id='trailing dot trimmed'),
    pytest.param(
        '01 -Title',
        '01',
        'Title',
        id='num space dash letter'),
    pytest.param(
        '01- Title',
        '01',
        'Title',
        id='num dash space letter'),
    pytest.param(
        '01 .Title',
        '01',
        'Title',
        id='num space point letter'),
    pytest.param(
        '01. Title',
        '01',
        'Title',
        id='num point space letter'),
    pytest.param(
        '01 _Title',
        '01',
        'Title',
        id='num space underscore letter'),
    pytest.param(
        '01_ Title',
        '01',
        'Title',
        id='num underscore space letter'),
    pytest.param(
        '01 —Title',
        '01',
        'Title',
        id='num space emdash letter'),
    pytest.param(
        '01— Title',
        '01',
        'Title',
        id='num emdash space letter'),
    pytest.param(
        '01 - Title',
        '01',
        'Title',
        id='num space dash space letter'),
    pytest.param(
        '01--Title',
        '01',
        'Title',
        id='num double dash letter'),
    pytest.param(
        '01.-Title',
        '01',
        'Title',
        id='num dash point letter'),
    pytest.param(
        '01..Title',
        '01',
        'Title',
        id='num double point letter'),
    pytest.param(
        '01——Title',
        '01',
        'Title',
        id='num double emdash letter'),
    pytest.param(
        '01-Title',
        '01',
        '-Title',
        id='num single dash letter kept'),
    pytest.param(
        '01, Title',
        '01',
        'Title',
        id='num comma space letter'),
    pytest.param(
        '01 ,Title',
        '01',
        'Title',
        id='num space comma letter'),
    pytest.param(
        '01; Title',
        '01',
        'Title',
        id='num semicolon space letter'),
    pytest.param(
        '01 ;Title',
        '01',
        'Title',
        id='num space semicolon letter'),
    pytest.param(
        '01;;Title',
        '01',
        'Title',
        id='num double semicolon letter'),
    pytest.param(
        '01,-Title',
        '01',
        'Title',
        id='num comma-dash letter'),
    pytest.param(
        '(1 2)',
        '',
        '(1-2)',
        id='range alone survives'),
    pytest.param(
        'Titel (9 10)',
        '',
        'Titel (9-10)',
        id='trailing range survives'),
    pytest.param(
        '(9 10) Titel',
        '',
        '(9-10) Titel',
        id='leading range survives'),
    pytest.param(
        '(12 15) Titel',
        '',
        '(12-15) Titel',
        id='two-digit range is no prefix'),
    pytest.param(
        '[90 100] Titel',
        '',
        '[90-100] Titel',
        id='three-digit range is no prefix'),
    pytest.param(
        '{10 11}',
        '',
        '{10-11}',
        id='curly range survives'),
    pytest.param(
        'Titel (5 5)',
        '',
        'Titel (5-5)',
        id='equal-numbers range survives'),
    pytest.param(
        '(5 5) Titel',
        '',
        '(5-5) Titel',
        id='leading equal-numbers range'),
    pytest.param(
        '(12-15) Titel',
        '',
        '(12-15) Titel',
        id='dashed range is no prefix'),
    pytest.param(
        '(12/15) Titel',
        '',
        '(12/15) Titel',
        id='slashed range is no prefix'),
    pytest.param(
        '(12) Titel',
        '12',
        'Titel',
        id='surrounded number still a prefix'),
    pytest.param(
        '1 -2',
        '1-2',
        '',
        id='stray space before dash, whole name'),
    pytest.param(
        '1- 2',
        '1-2',
        '',
        id='stray space after dash, whole name'),
    pytest.param(
        '9 -10 Titel',
        '9-10',
        'Titel',
        id='stray space, then a title'),
    pytest.param(
        'Titel (1 -2)',
        '',
        'Titel (1-2)',
        id='stray space inside brackets'),
    pytest.param(
        '1-2',
        '1-2',
        '',
        id='bare range is one prefix'),
    pytest.param(
        '1-2 Titel',
        '1-2',
        'Titel',
        id='bare range, then a title'),
    pytest.param(
        '90-100 Titel',
        '90-100',
        'Titel',
        id='uneven digit counts'),
    pytest.param(
        '2024-2025 Something',
        '2024-2025',
        'Something',
        id='wider than the NN-NN rule'),
    pytest.param(
        '1-2-3 Titel',
        '1-2-3',
        'Titel',
        id='several dash-joined numbers'),
    pytest.param(
        '10903-deu-x',
        '10903',
        '-deu-x',
        id='dash before a letter still ends the prefix'),
]


@pytest.mark.parametrize("raw,prefix,name", PINNED_NAMES)
def test_the_split_the_bash_white_box_pinned(raw, prefix, name):
    assert clean(raw) == (prefix, name)


# The separator normaliser's own table, likewise.
PINNED_SEPARATORS = [
    pytest.param(
        '( 01',
        '(01',
        id='bracket space number'),
    pytest.param(
        '01 )',
        '01)',
        id='number space bracket'),
    pytest.param(
        '(- 01',
        '(01',
        id='bracket separator space number'),
    pytest.param(
        '01 -)',
        '01)',
        id='number space separator bracket'),
    pytest.param(
        '(, 07',
        '(07',
        id='bracket comma space number'),
    pytest.param(
        '07 ,)',
        '07)',
        id='number space comma bracket'),
    pytest.param(
        '[ 05',
        '[05',
        id='square bracket space number'),
    pytest.param(
        '05 ]',
        '05]',
        id='number space square bracket'),
    pytest.param(
        '{ 9',
        '{9',
        id='brace space number'),
    pytest.param(
        '9 }',
        '9}',
        id='number space brace'),
    pytest.param(
        '< 3',
        '<3',
        id='angle space number'),
    pytest.param(
        '3 >',
        '3>',
        id='number space angle'),
    pytest.param(
        '( 2024',
        '(2024',
        id='bracket space multidigit'),
    pytest.param(
        '2024 )',
        '2024)',
        id='multidigit space bracket'),
    pytest.param(
        '(2024)',
        '(2024)',
        id='bracket already tight kept'),
    pytest.param(
        '01 (',
        '01 (',
        id='number space opening bracket kept'),
    pytest.param(
        '05 [',
        '05 [',
        id='number space open square kept'),
    pytest.param(
        '01 -(',
        '01 -(',
        id='number space sep opening bracket kept'),
    pytest.param(
        ') 01',
        ') 01',
        id='closing bracket space number kept'),
    pytest.param(
        '] 05',
        '] 05',
        id='close square space number kept'),
    pytest.param(
        ')- 01',
        ')- 01',
        id='closing bracket sep space number kept'),
    pytest.param(
        '01 }',
        '01}',
        id='number space closing curly hugs'),
    pytest.param(
        '01 >',
        '01>',
        id='number space closing angle hugs'),
    pytest.param(
        '{ 01',
        '{01',
        id='opening curly space number hugs'),
    pytest.param(
        '< 01',
        '<01',
        id='opening angle space number hugs'),
    pytest.param(
        '(1 2)',
        '(1-2)',
        id='single digits become a range'),
    pytest.param(
        '[3 7]',
        '[3-7]',
        id='square bracket range'),
    pytest.param(
        '{2 9}',
        '{2-9}',
        id='curly bracket range'),
    pytest.param(
        '<0 4>',
        '<0-4>',
        id='angle bracket range'),
    pytest.param(
        '(9 10)',
        '(9-10)',
        id='one and two digits'),
    pytest.param(
        '[90 100]',
        '[90-100]',
        id='two and three digits'),
    pytest.param(
        '(1990 2020)',
        '(1990-2020)',
        id='four-digit year range'),
    pytest.param(
        '<7 1000>',
        '<7-1000>',
        id='one and four digits'),
    pytest.param(
        '(08 9)',
        '(08-9)',
        id='leading zero on the left'),
    pytest.param(
        '(007 8)',
        '(007-8)',
        id='leading zeros do not change the value'),
    pytest.param(
        '(09 10)',
        '(09-10)',
        id='leading zero, still ascending'),
    pytest.param(
        '(010 9)',
        '(010 9)',
        id='leading zero, not ascending'),
    pytest.param(
        '(5 5)',
        '(5-5)',
        id='equal digits are a range'),
    pytest.param(
        '(12 12)',
        '(12-12)',
        id='equal numbers are a range'),
    pytest.param(
        '[7 7]',
        '[7-7]',
        id='equal in square brackets'),
    pytest.param(
        '(0 0)',
        '(0-0)',
        id='zero to zero is a range'),
    pytest.param(
        '(05 5)',
        '(05-5)',
        id='equal despite a leading zero'),
    pytest.param(
        '(007 7)',
        '(007-7)',
        id='equal despite leading zeros'),
    pytest.param(
        '(99999999999999999999 999999999999999999990)',
        '(99999999999999999999-999999999999999999990)',
        id='huge numbers, ascending'),
    pytest.param(
        '(999999999999999999990 99999999999999999999)',
        '(999999999999999999990 99999999999999999999)',
        id='huge numbers, descending'),
    pytest.param(
        '(2 1)',
        '(2 1)',
        id='descending pair is no range'),
    pytest.param(
        '(100 90)',
        '(100 90)',
        id='descending multidigit is no range'),
    pytest.param(
        '(05 4)',
        '(05 4)',
        id='descending by a padded digit'),
    pytest.param(
        '(1 2]',
        '(1 2]',
        id='round then square is no pair'),
    pytest.param(
        '[1 2)',
        '[1 2)',
        id='square then round is no pair'),
    pytest.param(
        '{1 2>',
        '{1 2>',
        id='curly then angle is no pair'),
    pytest.param(
        '1 2',
        '1 2',
        id='unbracketed pair kept'),
    pytest.param(
        '(1 2',
        '(1 2',
        id='opening bracket only kept'),
    pytest.param(
        '1 2)',
        '1 2)',
        id='closing bracket only kept'),
    pytest.param(
        '(1 2 3)',
        '(1 2 3)',
        id='three numbers are no range'),
    pytest.param(
        '(1 a)',
        '(1 a)',
        id='letter on the right is no range'),
    pytest.param(
        '(a 1)',
        '(a 1)',
        id='letter on the left is no range'),
    pytest.param(
        '(1-2)',
        '(1-2)',
        id='range already tight kept'),
    pytest.param(
        '( 1 2 )',
        '(1-2)',
        id='loose brackets then range'),
    pytest.param(
        '(1  2)',
        '(1-2)',
        id='several spaces between numbers'),
    pytest.param(
        '(2 1) and (3 8)',
        '(2 1) and (3-8)',
        id='each pair judged separately'),
    pytest.param(
        '1 -2',
        '1-2',
        id='space before the dash'),
    pytest.param(
        '1- 2',
        '1-2',
        id='space after the dash'),
    pytest.param(
        '9 -10',
        '9-10',
        id='space before, uneven widths'),
    pytest.param(
        '90- 100',
        '90-100',
        id='space after, uneven widths'),
    pytest.param(
        '1  -2',
        '1-2',
        id='a run of spaces before'),
    pytest.param(
        '1-  2',
        '1-2',
        id='a run of spaces after'),
    pytest.param(
        '2 -1',
        '2 -1',
        id='descending keeps its space'),
    pytest.param(
        '100- 90',
        '100- 90',
        id='descending after the dash'),
    pytest.param(
        '5 -5',
        '5-5',
        id='equal loses its space'),
    pytest.param(
        '5- 5',
        '5-5',
        id='equal loses its space after'),
    pytest.param(
        '(5 -5)',
        '(5-5)',
        id='bracketed equal, space before'),
    pytest.param(
        '(1 -2)',
        '(1-2)',
        id='bracketed, space before dash'),
    pytest.param(
        '(1- 2)',
        '(1-2)',
        id='bracketed, space after dash'),
    pytest.param(
        '[90- 100]',
        '[90-100]',
        id='square bracketed, space after'),
    pytest.param(
        '1 - 2',
        '1 - 2',
        id='dash spaced on both sides kept'),
    pytest.param(
        '01 -Title',
        '01 Title',
        id='dash before a letter unchanged'),
    pytest.param(
        '01- Title',
        '01 Title',
        id='dash after a number unchanged'),
]


@pytest.mark.parametrize("raw,expected", PINNED_SEPARATORS)
def test_the_separator_the_bash_white_box_pinned(raw, expected):
    assert cni.normalize_number_separator(raw) == expected


# And the numeric comparison, where the octal and overflow traps live.
PINNED_COMPARISONS = [
    pytest.param(
        '9',
        '10',
        True,
        id='9 <= 10 (numeric, not textual)'),
    pytest.param(
        '10',
        '9',
        False,
        id='10 <= 9 is false'),
    pytest.param(
        '08',
        '9',
        True,
        id='08 <= 9 (no octal reading)'),
    pytest.param(
        '007',
        '8',
        True,
        id='007 <= 8 (leading zeros ignored)'),
    pytest.param(
        '010',
        '9',
        False,
        id='010 <= 9 is false (decimal ten)'),
    pytest.param(
        '5',
        '5',
        True,
        id='5 <= 5 (equal counts)'),
    pytest.param(
        '0',
        '00',
        True,
        id='0 <= 00 (both are zero)'),
    pytest.param(
        '007',
        '7',
        True,
        id='007 <= 7 (padding is not size)'),
    pytest.param(
        '00',
        '1',
        True,
        id='00 <= 1 (a padded zero is zero)'),
    pytest.param(
        '99999999999999999999',
        '99999999999999999999',
        True,
        id='huge equal pair'),
    pytest.param(
        '999999999999999999990',
        '99999999999999999999',
        False,
        id='huge descending pair'),
]


@pytest.mark.parametrize("a,b,expected", PINNED_COMPARISONS)
def test_the_comparison_the_bash_white_box_pinned(a, b, expected):
    assert cni._digits_not_greater(a, b) is expected
