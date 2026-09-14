"""Tests for medialib.lib.titlematch - when two spellings name the same title.

The fold is the awkward part, because what it delegates to is not the same
everywhere. A glibc iconv is its source of truth and the recorded folds are that
iconv's answers - so the two cases about the codepoints that make it GIVE UP ask
this host before asserting on what happens then, because which codepoints those
are belongs to the installed glibc. A host whose iconv is GNU libiconv (macOS,
MSYS) spells accents out instead and is not used at all; the cases for that rung,
and for the Python fold it falls to, are further down, and one of them holds the
two to the same answers.

The rest is the readings a title is matched under, and each is pinned from both
sides: the spellings that MUST meet, because a library and a catalogue really do
disagree that way, and the pairs that must not, because a reading that matched
everything would name films at random.
"""

import re
import subprocess

import pytest

from medialib.lib import titlematch

pytestmark = pytest.mark.stubbed


def _iconv_gives_up_on(text: str) -> bool:
    """Whether this host's iconv FAILS on ``text`` rather than transliterating
    it: the condition the two reset cases below are about.

    A handful of codepoints transliterate AND fail, which resets the string to
    the original, and which ones do is glibc's business and its version's - so
    the two cases ask this host rather than recording one host's answer.
    """
    try:
        done = subprocess.run(
            ["iconv", "-f", "UTF-8", "-t", "ASCII//TRANSLIT"],
            input=text.encode("utf-8"), capture_output=True)
    except OSError:
        return False
    return done.returncode != 0


# Both reset cases are about what the module does with ICONV's answer, so they
# are only asked on a host the module actually reaches iconv on - a GNU
# libiconv one (macOS, MSYS) folds in Python and never sees a give-up at all.
_RESETS_MICRO = titlematch._iconv_drops_accents() and _iconv_gives_up_on("a\u00b5b")
_RESETS_FRACTION = (titlematch._iconv_drops_accents()
                    and _iconv_gives_up_on("\u00bd life"))
titlematch.reset_iconv_flavour()


def _stub_iconv(monkeypatch, returncode: int, stdout: bytes = b""):
    """Stand a fake iconv in front of the fold, answering the module's flavour
    probe the way a glibc one does and every other call as the case asks.

    The probe needs answering separately: a fake that failed it too would send
    the fold down the Python path, and these cases are about what the module
    does with what ICONV said.
    """
    probe = titlematch._TRANSLIT_PROBE.encode("utf-8")
    answer = titlematch._TRANSLIT_PROBE_GLIBC.encode("utf-8")

    def fake_run(*_args, input=None, **_kwargs):
        if input == probe:
            return subprocess.CompletedProcess([], 0, answer, b"")
        return subprocess.CompletedProcess([], returncode, stdout, b"")

    titlematch.reset_iconv_flavour()
    monkeypatch.setattr(titlematch.subprocess, "run", fake_run)


class TestNormalizeTitle:
    @pytest.mark.parametrize("title,want", [
        ("Amélie", "amelie"),
        ("SPIDER-MAN: Homecoming", "spider man homecoming"),
        ("Café au lait", "cafe au lait"),
        ("Übung 2: The Æra", "ubung 2 the aera"),
        ("Bàtman", "batman"),
        ("(Weird)  Title!!", "weird title"),
        ("Mì Đội", "mi doi"),
        ("  spaced   out  ", "spaced out"),
        ("half life 3", "half life 3"),
    ])
    def test_folds_accents_case_and_punctuation(self, title, want):
        assert titlematch.normalize_title(title) == want

    def test_non_alphanumeric_runs_collapse_to_one_space(self):
        assert titlematch.normalize_title("a---b..c   d") == "a b c d"

    @pytest.mark.skipif(not _RESETS_MICRO,
                        reason="this host's iconv transliterates U+00B5")
    def test_an_untransliteratable_codepoint_resets_to_the_original(self):
        # micro U+00B5 makes this host's iconv fail rather than emit "?", so the
        # shell's `|| s="$1"` stands the original up and only the fold applies:
        # the micro becomes a space, the letters stay
        assert titlematch.normalize_title("aµb") == "a b"

    @pytest.mark.skipif(not _RESETS_FRACTION,
                        reason="this host's iconv transliterates U+00BD")
    def test_a_fraction_resets_to_the_original(self):
        # U+00BD (one half) also falls in the reset set, so "½ life" keeps its
        # letters and loses the fraction to a dropped run
        assert titlematch.normalize_title("½ life") == "life"

    def test_the_reset_itself_needs_no_iconv_to_pin(self, monkeypatch):
        """WHICH codepoints make iconv give up is the host's; that giving up
        resets the string to the original is this module's, and is pinned
        without asking any host."""
        _stub_iconv(monkeypatch, 1)
        assert titlematch.normalize_title("Amélie 2") == "am lie 2"

    def test_and_a_transliteration_that_succeeds_is_the_one_used(self,
                                                                 monkeypatch):
        _stub_iconv(monkeypatch, 0, b"Amelie 2")
        assert titlematch.normalize_title("Amélie 2") == "amelie 2"

    def test_an_iconv_that_spells_the_accent_out_is_not_used(self,
                                                             monkeypatch):
        """The macOS rung. GNU libiconv answers "'e" where glibc answers "e",
        and a fold that took that would key "Amélie" as "am elie" - which
        stops matching the same film's ASCII spelling, so the whole reason
        the fold exists is gone."""
        monkeypatch.setattr(
            titlematch.subprocess, "run",
            lambda *_a, **_k: subprocess.CompletedProcess([], 0, b"'e", b""))
        titlematch.reset_iconv_flavour()
        assert titlematch.normalize_title("Amélie") == "amelie"

    def test_and_neither_is_a_host_with_no_iconv_at_all(self, monkeypatch):
        """Which used to be a FileNotFoundError out of the middle of a
        lookup."""
        def absent(*_a, **_k):
            raise FileNotFoundError(2, "no such file", "iconv")

        monkeypatch.setattr(titlematch.subprocess, "run", absent)
        titlematch.reset_iconv_flavour()
        assert titlematch.normalize_title("Café au lait") == "cafe au lait"

    @pytest.mark.parametrize("title,want", [
        ("Amélie", "amelie"),
        ("Übung 2: The Æra", "ubung 2 the aera"),
        ("Bàtman", "batman"),
        ("Mì Đội", "mi doi"),
        ("Straße", "strasse"),
        ("Blade Runner", "blade runner"),
    ])
    def test_the_python_fold_answers_what_glibc_answers(self, title, want):
        """The fallback is only a fallback while it agrees with the tool it
        stands in for: one library read on Linux and on a Mac has to fold to
        the same keys, or the same film reads as two."""
        folded = titlematch._fold_without_iconv(title)
        assert re.sub(r"[^a-z0-9]+", " ", folded.lower()).strip() == want

    def test_the_flavour_is_settled_once_and_not_per_title(self, monkeypatch):
        """A lookup folds once per folder, and the answer cannot change under
        a running command."""
        calls = []

        def counted(*_a, input=None, **_k):
            calls.append(input)
            return subprocess.CompletedProcess(
                [], 0, titlematch._TRANSLIT_PROBE_GLIBC.encode("utf-8"), b"")

        monkeypatch.setattr(titlematch.subprocess, "run", counted)
        titlematch.reset_iconv_flavour()
        for _ in range(3):
            titlematch.normalize_title("Amélie")
        probe = titlematch._TRANSLIT_PROBE.encode("utf-8")
        assert calls.count(probe) == 1

    def test_an_ascii_title_never_reaches_iconv(self, monkeypatch):
        """ASCII goes through ASCII//TRANSLIT unchanged, so asking is a process
        per title for no answer - and a run over a library asks once per name
        per word boundary."""
        def refuse(*_a, **_k):
            raise AssertionError("asked iconv about an ASCII title")

        titlematch.reset_iconv_flavour()
        monkeypatch.setattr(titlematch.subprocess, "run", refuse)
        assert titlematch.normalize_title("SPIDER-MAN: Homecoming") \
            == "spider man homecoming"

    def test_and_the_short_circuit_answers_what_iconv_would_have(self):
        """Character for character, over every printable ASCII run."""
        for title in ("a---b..c   d", "!!!", "  spaced   out  ", "half life 3",
                      "(Weird)  Title!!", "A Cat's Tale", "S.W.A.T."):
            titlematch.reset_iconv_flavour()
            short = titlematch.normalize_title(title)
            done = subprocess.run(
                ["iconv", "-f", "UTF-8", "-t", "ASCII//TRANSLIT"],
                input=title.encode("utf-8"), capture_output=True)
            through = done.stdout.decode("utf-8") if done.returncode == 0 \
                else title
            assert short == re.sub(r"[^a-z0-9]+", " ",
                                   through.lower()).strip(" ")

    def test_empty_and_blank_fold_to_empty(self):
        assert titlematch.normalize_title("") == ""
        assert titlematch.normalize_title("   ") == ""
        assert titlematch.normalize_title("!!!") == ""


class TestTitleKeys:
    """A title carrying an apostrophe is matched by both readings of it.

    normalize_title collapses each run of punctuation to a SPACE, which is
    right for a dash and wrong for an apostrophe: a library whose names have
    had their apostrophes stripped would otherwise never meet the same title
    on TMDb.
    """

    def test_the_spaced_and_the_squeezed_reading_are_both_offered(self):
        assert {"the movie", "themovie"} <= titlematch.title_keys("The Movie")

    @pytest.mark.parametrize("written,stripped", [
        ("A Cat's Tale", "A Cats Tale"),
        ("A Cat\u2019s Tale", "A Cats Tale"),
        ("L'Animal", "LAnimal"),
    ])
    def test_the_two_spellings_of_one_title_meet(self, written, stripped):
        assert titlematch.title_keys(written) & titlematch.title_keys(stripped)

    def test_a_title_that_matched_before_still_matches(self):
        """Widening only ever ADDS keys."""
        assert titlematch.normalize_title("The Movie") \
            in titlematch.title_keys("The Movie")

    def test_two_different_titles_still_do_not_meet(self):
        assert not (titlematch.title_keys("A Cat's Tale")
                    & titlematch.title_keys("A Dog's Tale"))

class TestTheReadingsOfOneTitle:
    """The disagreements a library and a catalogue actually have."""

    @pytest.mark.parametrize("one,other,about", [
        ("Amelie", "Am\u00e9lie", "an accent the keyboard could not reach"),
        ("Fahrenheit 451", "FAHRENHEIT 451", "capitals"),
        ("Spider-Man", "Spider Man", "a dash written as a space"),
        ("Spider-Man", "SpiderMan", "a dash written as nothing"),
        ("Ocean\u2019s Eleven", "Oceans Eleven", "an apostrophe stripped out"),
        ("Paris, Texas", "Paris - Texas", "a comma written as a dash"),
        ("Paris, Texas", "Paris \u2014 Texas", "a comma written as an em dash"),
        ("S.W.A.T.", "SWAT", "an abbreviation with its points"),
        ("S.W.A.T.", "S W A T", "an abbreviation spaced out"),
        ("Rocky II", "Rocky 2", "a roman numeral"),
        ("Star Wars Episode IV", "Star Wars Episode 4", "the same, mid-name"),
        ("The Thing", "Thing", "an article the folder left off"),
        ("Shining, The", "The Shining", "an article a catalogue moved"),
        ("Hansel & Gretel", "Hansel and Gretel", "an ampersand written out"),
        ("Hansel & Gretel", "Hansel und Gretel", "and written in German"),
        ("Alien vs. Predator", "Alien versus Predator", "versus written out"),
        ("Movie - Casablanca", "Casablanca", "a filler word in front"),
        ("Who Am I?", "Who Am I", "a question mark"),
        ("Se7en!!", "Se7en", "exclamation marks"),
    ])
    def test_the_two_spellings_meet(self, one, other, about):
        assert titlematch.equivalent(one, other), about

    @pytest.mark.parametrize("one,other", [
        ("A Cat's Tale", "A Dog's Tale"),
        ("Rocky II", "Rocky 3"),
        ("The Thing", "The Thing II"),
        ("Toy Story", "Toy Story 2"),
        ("Alien", "Aliens"),
        ("Heat", "The Heat Wave"),
    ])
    def test_and_two_different_titles_do_not(self, one, other):
        assert not titlematch.equivalent(one, other)

    def test_a_roman_numeral_is_read_one_way_only(self):
        """A number has one roman spelling, so folding the roman ONTO the
        arabic meets everything folding the other way would."""
        assert "rocky 2" in titlematch.title_keys("Rocky II")

    @pytest.mark.parametrize("word", ["Did", "Dim", "Civil", "Mill", "Lid"])
    def test_a_word_that_looks_roman_is_still_a_word(self, word):
        """A run of roman letters is not a numeral unless it is a numeral
        SPELLED the way one is."""
        assert not titlematch.equivalent(word, "1000")
        assert titlematch.normalize_title(word) in titlematch.title_keys(word)

    def test_except_the_one_that_really_is_a_numeral(self):
        """MIX is 1009 spelled correctly, and no rule short of a dictionary
        tells it from the word. The reading is additive, so the cost is one key
        on a title nothing else carries."""
        assert titlematch.equivalent("Mix", "1009")

    def test_a_conjunction_is_only_one_in_a_conjunctions_place(self):
        """The short ones are articles and letters elsewhere: "E.T." ends in a
        "t" and "I, Robot" begins with an "i", and neither is an "and"."""
        assert not titlematch.equivalent("I Robot", "And Robot")
        assert titlematch.equivalent("Dogs et Cats", "Dogs and Cats")

    def test_a_filler_is_never_the_whole_title(self):
        """Stripping "Movie" off "Movie" would leave nothing to look up."""
        assert titlematch.title_keys("Movie") == frozenset({"movie"})

    def test_an_article_is_never_the_whole_title_either(self):
        assert titlematch.title_keys("The") == frozenset({"the"})

    def test_the_keys_of_one_title_are_bounded(self):
        """Every reading multiplies, and a run over a library asks this once
        per file per folder."""
        stacked = "The Movie Special Film Part II & The Sequel III et IV"
        assert len(titlematch.title_keys(stacked)) <= titlematch.MAX_KEYS

    def test_a_title_that_matched_before_still_matches(self):
        """Widening only ever ADDS keys."""
        assert titlematch.normalize_title("Casablanca") \
            in titlematch.title_keys("Casablanca")


class TestWhatToGoAskingUnder:
    """The spellings a catalogue is asked about, which is a different question
    from whether an answer is the title: the catalogue matches text of its own
    and has never heard of a key."""

    def test_the_written_title_always_leads(self):
        assert titlematch.search_titles("Casablanca")[0] == "Casablanca"

    def test_a_title_that_is_already_right_costs_one_query(self):
        assert titlematch.search_titles("Casablanca") == ["Casablanca"]

    def test_and_so_does_one_that_only_differs_by_a_fold(self):
        """A catalogue's own search is no more troubled by an accent or a
        capital than the fold is, so asking it twice buys the same answer and a
        second request."""
        assert titlematch.search_titles("AM\u00c9LIE") == ["AM\u00c9LIE"]

    def test_the_filler_a_library_wrote_in_front_comes_off(self):
        assert "James Bond - Goldfinger" in titlematch.search_titles(
            "Movie - James Bond - Goldfinger")

    def test_and_so_does_the_franchise_repeated_on_every_film(self):
        assert "Goldfinger" in titlematch.search_titles(
            "Movie - James Bond - Goldfinger")

    def test_a_roman_numeral_is_offered_as_a_number(self):
        assert "Star Wars: Episode 4 - A New Hope" in titlematch.search_titles(
            "Star Wars: Episode IV - A New Hope")

    def test_a_title_that_is_all_separator_asks_nothing_extra(self):
        """A tail the split cuts to nothing has nothing to ask about."""
        assert "" not in titlematch.search_titles("Heat - ")

    def test_the_queries_are_bounded(self):
        many = "Movie - Special - Film - The Franchise - Part II - The Sequel"
        assert len(titlematch.search_titles(many)) <= titlematch.MAX_QUERIES


class TestTheMarkerACopyCarries:
    """What a file manager appends when two files of one name land in a folder.
    None of it says anything about the film."""

    @pytest.mark.parametrize("written,bare", [
        ("Film (1961) (1)", "Film (1961)"),
        ("Film (1961) (2)", "Film (1961)"),
        ("Film (1961) (copy)", "Film (1961)"),
        ("Film (1961) (another copy)", "Film (1961)"),
        ("Film (1961) (3rd copy)", "Film (1961)"),
        ("Film (1961) - Copy", "Film (1961)"),
        ("Film (1961) - Copy (2)", "Film (1961)"),
        ("Film (1961) copy", "Film (1961)"),
        ("Film (1961) copy 2", "Film (1961)"),
    ])
    def test_the_marker_comes_off(self, written, bare):
        assert titlematch.strip_duplicate_marker(written) == bare

    @pytest.mark.parametrize("name", [
        "Film (1961)",
        "Film (1961) Part1",
        "Film (1961) {edition-Colorized}",
        "Blade Runner 2049",
    ])
    def test_and_a_name_that_carries_none_is_untouched(self, name):
        assert titlematch.strip_duplicate_marker(name) == name

    def test_one_marker_at_a_time(self):
        """A copy of a copy is a copy of something this can be asked about
        again."""
        assert titlematch.strip_duplicate_marker("Film (1) (2)") == "Film (1)"



class TestAnArticleThatWentMissingAltogether:
    """An article does not only go missing from the front, and does not only go
    missing one at a time."""

    @pytest.mark.parametrize("one,other", [
        ("The Lord of the Rings", "Lord of Rings"),
        ("The Lord of the Rings", "Lord of the Rings"),
        ("The Lord of the Rings", "The Lord of Rings"),
        ("Le Comte de Monte-Cristo", "Comte de Monte-Cristo"),
        ("De Zaak Alzheimer", "Zaak Alzheimer"),
        ("Il Buono, il Brutto, il Cattivo", "Buono Brutto Cattivo"),
    ])
    def test_however_many_went_missing_from_either_side(self, one, other):
        assert titlematch.equivalent(one, other)

    @pytest.mark.parametrize("one,other", [
        ("La La Land", "Land"),
        ("The Grand Budapest Hotel", "Hotel"),
    ])
    def test_but_not_down_to_a_single_word(self, one, other):
        """Not for correctness - a widening cannot name the wrong film, since a
        second candidate is answered by naming neither - but for recall: a key
        a different real film already holds takes a certain match away."""
        assert not titlematch.equivalent(one, other)

    def test_the_leading_article_still_comes_off_a_two_word_title(self):
        """That one IS the convention a catalogue varies by, and the floor
        above is the other reading's own."""
        assert titlematch.equivalent("The Thing", "Thing")

    def test_two_titles_that_only_share_their_articles_do_not_meet(self):
        assert not titlematch.equivalent("The Lord of the Rings",
                                         "The Fellowship of the Ring")

    def test_the_keys_are_still_bounded(self):
        stacked = "The Movie Special Film Part II & The Sequel III et IV"
        assert len(titlematch.title_keys(stacked)) <= titlematch.MAX_KEYS


class TestAnAccentALanguageWritesOut:
    """Dropping the accent is what a transliteration does. Writing it out is
    what a language does when it cannot reach it, and both spellings turn up in
    a library."""

    @pytest.mark.parametrize("accented,written,dropped", [
        ("Übung", "Uebung", "Ubung"),
        ("Mädchen in Uniform", "Maedchen in Uniform", "Madchen in Uniform"),
        ("Der Förster", "Der Foerster", "Der Forster"),
        ("Århus", "Aarhus", "Arhus"),
        ("Køge", "Koege", "Koge"),
    ])
    def test_the_accented_spelling_meets_both(self, accented, written, dropped):
        assert titlematch.equivalent(accented, written)
        assert titlematch.equivalent(accented, dropped)

    @pytest.mark.parametrize("one,other", [
        ("Michael", "Michal"),
        ("Aeon Flux", "Aon Flux"),
    ])
    def test_and_the_reading_never_runs_backwards(self, one, other):
        """Reading "ae" back as "a" is the same rule reversed, and it cannot be:
        the two written spellings meet at the ACCENTED one, which is what a
        catalogue carries."""
        assert not titlematch.equivalent(one, other)

    @pytest.mark.parametrize("one,other", [
        ("François", "Francois"),
        ("Amélie", "Amelie"),
        ("Niño", "Nino"),
        ("Straße", "Strasse"),
        ("Æon Flux", "Aeon Flux"),
    ])
    def test_the_accents_with_only_one_reading_still_have_it(self, one, other):
        """A cedilla, an acute and a tilde are dropped and nothing else; the
        ligatures are spelled out by the fold itself, on both sides."""
        assert titlematch.equivalent(one, other)

    @pytest.mark.parametrize("title,want", [
        ("Işık", "isik"),          # Turkish dotless i
        ("ẞTRASSE", "sstrasse"),        # capital sharp s
        ("ſun", "sun"),                 # long s
    ])
    def test_a_letter_a_decomposition_cannot_reach_still_folds(self, title,
                                                                want):
        """Not through NFKD - these are single codepoints with no combining
        form, so the ASCII pass would drop them altogether and a Turkish title
        would key as its consonants."""
        folded = titlematch._fold_without_iconv(title)
        assert re.sub(r"[^a-z0-9]+", " ", folded.lower()).strip() == want


class TestSeveralOfThemInOneName:
    """The readings compose. Each one widens the whole set of forms the next
    one then reads, so the forms are every combination of the readings and not
    a list of single corrections - which is what a real library needs, because a
    name that went wrong once usually went wrong twice."""

    @pytest.mark.parametrize("one,other,about", [
        ("THE SHINING", "Shining, The", "case and where the article sits"),
        ("DER FOERSTER VOM SILBERWALD", "Förster vom Silberwald",
         "case, a written-out accent and a dropped article"),
        ("OCEANS ELEVEN II", "Ocean's Eleven 2",
         "case, an apostrophe and a roman numeral"),
        ("MOVIE - UEBUNG II", "Übung 2",
         "a filler, a separator, a written-out accent and a numeral"),
        ("HANSEL AND GRETEL: WITCH-HUNTERS",
         "Hänsel & Gretel: WitchHunters",
         "an ampersand, an accent, a colon and a joined-up compound"),
        ("LORD OF RINGS PART II — THE TOWERS",
         "The Lord of the Rings Part 2 - The Towers",
         "two missing articles, a numeral, an em dash and case"),
        ("S.W.A.T. II", "The SWAT 2",
         "an abbreviation's points, an article and a numeral"),
    ])
    def test_a_name_that_went_wrong_twice_is_still_the_same_title(self, one,
                                                                   other,
                                                                   about):
        assert titlematch.equivalent(one, other), about

    def test_stacking_them_does_not_stack_the_keys(self):
        """Each reading only widens where it CHANGES something, so a name that
        is wrong in six ways still costs a handful of keys rather than every
        combination of six."""
        keys = titlematch.title_keys(
            "LORD OF RINGS PART II — THE TOWERS")
        assert len(keys) <= 16

    def test_and_two_different_films_do_not_meet_through_a_stack(self):
        """Widening is not the same as matching everything."""
        assert not titlematch.equivalent(
            "THE LORD OF THE RINGS PART II",
            "The Hobbit Part 2")


class TestALetterFromTheWrongKeyboard:
    """A name whose letters are the SHAPE of Latin ones is that name. The fold
    has no opinion about Cyrillic beyond dropping it, which turned "Thor" whose
    T is a Cyrillic TE into "hor" and called the file a different film."""

    @pytest.mark.parametrize("latin,confusable,about", [
        ("Thor - Ragnarok", "Тhor - Ragnarok", "Cyrillic TE for T"),
        ("Apollo 13", "Аpollo 13", "Cyrillic A"),
        ("Cars", "Сars", "Cyrillic ES for C"),
        ("Home", "Hоme", "Cyrillic O, mid-word"),
        ("Alpha Beta", "Αlpha Βeta", "Greek alpha and beta"),
        ("Oxygen", "Οxygen", "Greek omicron"),
    ])
    def test_it_reads_as_the_letter_it_looks_like(self, latin, confusable,
                                                   about):
        assert titlematch.equivalent(latin, confusable), about

    def test_and_the_name_costs_no_process_once_it_is_ascii(self, monkeypatch):
        """The substitution runs before the short-circuit, so a name whose only
        non-ASCII character was a wrong-keyboard letter never reaches iconv."""
        def refuse(*_a, **_k):
            raise AssertionError("asked iconv about what is now ASCII")

        titlematch.reset_iconv_flavour()
        monkeypatch.setattr(titlematch.subprocess, "run", refuse)
        assert titlematch.normalize_title("Тhor - Ragnarok (2017)") \
            == "thor ragnarok 2017"

    def test_a_title_really_written_in_cyrillic_is_unharmed(self):
        """Both sides of every comparison are folded by this same function, so
        a real Cyrillic title matches itself whether these apply or not."""
        assert titlematch.equivalent("Сталкер",
                                     "Сталкер")

    def test_and_two_different_films_still_do_not_meet(self):
        assert not titlematch.equivalent("Тhor", "Loki")
