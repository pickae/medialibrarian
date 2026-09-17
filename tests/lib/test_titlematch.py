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
        ("Sélène", "selene"),
        ("IRON-WOLF: Homecoming", "iron wolf homecoming"),
        ("Café au lait", "cafe au lait"),
        ("Übung 2: Die Æra", "ubung 2 die aera"),
        ("Nìghthawk", "nighthawk"),
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
        assert titlematch.normalize_title("Sélène 2") == "s l ne 2"

    def test_and_a_transliteration_that_succeeds_is_the_one_used(self,
                                                                 monkeypatch):
        _stub_iconv(monkeypatch, 0, b"Selene 2")
        assert titlematch.normalize_title("Sélène 2") == "selene 2"

    def test_an_iconv_that_spells_the_accent_out_is_not_used(self,
                                                             monkeypatch):
        """The macOS rung. GNU libiconv answers "'e" where glibc answers "e",
        and a fold that took that would key "Sélène" as "am elie" - which
        stops matching the same film's ASCII spelling, so the whole reason
        the fold exists is gone."""
        monkeypatch.setattr(
            titlematch.subprocess, "run",
            lambda *_a, **_k: subprocess.CompletedProcess([], 0, b"'e", b""))
        titlematch.reset_iconv_flavour()
        assert titlematch.normalize_title("Sélène") == "selene"

    def test_and_neither_is_a_host_with_no_iconv_at_all(self, monkeypatch):
        """Which used to be a FileNotFoundError out of the middle of a
        lookup."""
        def absent(*_a, **_k):
            raise FileNotFoundError(2, "no such file", "iconv")

        monkeypatch.setattr(titlematch.subprocess, "run", absent)
        titlematch.reset_iconv_flavour()
        assert titlematch.normalize_title("Café au lait") == "cafe au lait"

    @pytest.mark.parametrize("title,want", [
        ("Sélène", "selene"),
        ("Übung 2: Die Æra", "ubung 2 die aera"),
        ("Nìghthawk", "nighthawk"),
        ("Mì Đội", "mi doi"),
        ("Straße", "strasse"),
        ("Night Runner", "night runner"),
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
            titlematch.normalize_title("Sélène")
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
        assert titlematch.normalize_title("IRON-WOLF: Homecoming") \
            == "iron wolf homecoming"

    def test_and_the_short_circuit_answers_what_iconv_would_have(self):
        """Character for character, over every printable ASCII run."""
        for title in ("a---b..c   d", "!!!", "  spaced   out  ", "half life 3",
                      "(Weird)  Title!!", "A Cat's Tale", "N.E.S.T."):
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
        ("Selene", "S\u00e9l\u00e8ne", "an accent the keyboard could not reach"),
        ("Meridian 88", "MERIDIAN 88", "capitals"),
        ("Iron-Wolf", "Iron Wolf", "a dash written as a space"),
        ("Iron-Wolf", "IronWolf", "a dash written as nothing"),
        ("Harbour\u2019s Eleven", "Harbours Eleven", "an apostrophe stripped out"),
        ("Dover, Kansas", "Dover - Kansas", "a comma written as a dash"),
        ("Dover, Kansas", "Dover \u2014 Kansas", "a comma written as an em dash"),
        ("N.E.S.T.", "NEST", "an abbreviation with its points"),
        ("N.E.S.T.", "N E S T", "an abbreviation spaced out"),
        ("Granite II", "Granite 2", "a roman numeral"),
        ("Nordwind Episode IV", "Nordwind Episode 4", "the same, mid-name"),
        ("The Shape", "Shape", "an article the folder left off"),
        ("Beacon, The", "The Beacon", "an article a catalogue moved"),
        ("Jonas & Greta", "Jonas and Greta", "an ampersand written out"),
        ("Jonas & Greta", "Jonas und Greta", "and written in German"),
        ("Seewolf vs. Baer", "Seewolf versus Baer", "versus written out"),
        ("Movie - Rivertown", "Rivertown", "a filler word in front"),
        ("What Now?", "What Now", "a question mark"),
        ("Fi5e!!", "Fi5e", "exclamation marks"),
    ])
    def test_the_two_spellings_meet(self, one, other, about):
        assert titlematch.equivalent(one, other), about

    @pytest.mark.parametrize("one,other", [
        ("A Cat's Tale", "A Dog's Tale"),
        ("Granite II", "Granite 3"),
        ("The Shape", "The Shape II"),
        ("Tin Soldier", "Tin Soldier 2"),
        ("Seewolf", "Seewoelfe"),
        ("Heat", "The Frost Wave"),
    ])
    def test_and_two_different_titles_do_not(self, one, other):
        assert not titlematch.equivalent(one, other)

    def test_a_roman_numeral_is_read_one_way_only(self):
        """A number has one roman spelling, so folding the roman ONTO the
        arabic meets everything folding the other way would."""
        assert "granite 2" in titlematch.title_keys("Granite II")

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
        """The short ones are articles and letters elsewhere: "E.D.G.E." opens
        and closes on an "e" and "I Sentinel" begins with an "i", and none of
        the three is an "and"."""
        assert not titlematch.equivalent("I Sentinel", "And Sentinel")
        assert not titlematch.equivalent("E.D.G.E.", "And D G And")
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
        assert titlematch.normalize_title("Rivertown") \
            in titlematch.title_keys("Rivertown")


class TestWhatToGoAskingUnder:
    """The spellings a catalogue is asked about, which is a different question
    from whether an answer is the title: the catalogue matches text of its own
    and has never heard of a key."""

    def test_the_written_title_always_leads(self):
        assert titlematch.search_titles("Rivertown")[0] == "Rivertown"

    def test_a_title_that_is_already_right_costs_one_query(self):
        assert titlematch.search_titles("Rivertown") == ["Rivertown"]

    def test_and_so_does_one_that_only_differs_by_a_fold(self):
        """A catalogue's own search is no more troubled by an accent or a
        capital than the fold is, so asking it twice buys the same answer and a
        second request."""
        assert titlematch.search_titles("S\u00c9L\u00c8NE") == ["S\u00c9L\u00c8NE"]

    def test_the_filler_a_library_wrote_in_front_comes_off(self):
        assert "Agent Ward - Blackfeather" in titlematch.search_titles(
            "Movie - Agent Ward - Blackfeather")

    def test_and_so_does_the_franchise_repeated_on_every_film(self):
        assert "Blackfeather" in titlematch.search_titles(
            "Movie - Agent Ward - Blackfeather")

    def test_a_roman_numeral_is_offered_as_a_number(self):
        assert "Nordwind: Episode 4 - Der Sturm" in titlematch.search_titles(
            "Nordwind: Episode IV - Der Sturm")

    def test_a_title_that_is_all_separator_asks_nothing_extra(self):
        """A tail the split cuts to nothing has nothing to ask about."""
        assert "" not in titlematch.search_titles("Frost - ")

    def test_and_neither_does_one_whose_tail_is_only_a_number(self):
        """A catalogue asked for "Part 2" answers with every film that was ever
        released in halves, which is twenty results and no film."""
        assert titlematch.search_titles("Gorse and Gallows - Part 2") \
            == ["Gorse and Gallows - Part 2"]

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
        "Night Runner 2049",
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
        ("The Keeper of the Keys", "Keeper of Keys"),
        ("The Keeper of the Keys", "Keeper of the Keys"),
        ("The Keeper of the Keys", "The Keeper of Keys"),
        ("Le Seigneur de Val-Mont", "Seigneur de Val-Mont"),
        ("De Zaak Alzheimer", "Zaak Alzheimer"),
        ("Il Buono, il Brutto, il Cattivo", "Buono Brutto Cattivo"),
    ])
    def test_however_many_went_missing_from_either_side(self, one, other):
        assert titlematch.equivalent(one, other)

    @pytest.mark.parametrize("one,other", [
        ("Bel Bel Ville", "Land"),
        ("The Grand Riverside Hotel", "Hotel"),
    ])
    def test_but_not_down_to_a_single_word(self, one, other):
        """Not for correctness - a widening cannot name the wrong film, since a
        second candidate is answered by naming neither - but for recall: a key
        a different real film already holds takes a certain match away."""
        assert not titlematch.equivalent(one, other)

    def test_the_leading_article_still_comes_off_a_two_word_title(self):
        """That one IS the convention a catalogue varies by, and the floor
        above is the other reading's own."""
        assert titlematch.equivalent("The Shape", "Shape")

    def test_two_titles_that_only_share_their_articles_do_not_meet(self):
        assert not titlematch.equivalent("The Keeper of the Keys",
                                         "The Circle of Keys")

    def test_the_keys_are_still_bounded(self):
        stacked = "The Movie Special Film Part II & The Sequel III et IV"
        assert len(titlematch.title_keys(stacked)) <= titlematch.MAX_KEYS


class TestAnAccentALanguageWritesOut:
    """Dropping the accent is what a transliteration does. Writing it out is
    what a language does when it cannot reach it, and both spellings turn up in
    a library."""

    @pytest.mark.parametrize("accented,written,dropped", [
        ("Übung", "Uebung", "Ubung"),
        ("Vögel im Regen", "Voegel im Regen", "Vogel im Regen"),
        ("Der Förster", "Der Foerster", "Der Forster"),
        ("Århus", "Aarhus", "Arhus"),
        ("Køge", "Koege", "Koge"),
    ])
    def test_the_accented_spelling_meets_both(self, accented, written, dropped):
        assert titlematch.equivalent(accented, written)
        assert titlematch.equivalent(accented, dropped)

    @pytest.mark.parametrize("one,other", [
        ("Rafael", "Rafal"),
        ("Aeon Vale", "Aon Vale"),
    ])
    def test_and_the_reading_never_runs_backwards(self, one, other):
        """Reading "ae" back as "a" is the same rule reversed, and it cannot be:
        the two written spellings meet at the ACCENTED one, which is what a
        catalogue carries."""
        assert not titlematch.equivalent(one, other)

    @pytest.mark.parametrize("one,other", [
        ("François", "Francois"),
        ("Sélène", "Selene"),
        ("Niño", "Nino"),
        ("Straße", "Strasse"),
        ("Æon Vale", "Aeon Vale"),
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
        ("THE BEACON", "Beacon, The", "case and where the article sits"),
        ("DER FOERSTER VOM NEBELTAL", "Förster vom Nebeltal",
         "case, a written-out accent and a dropped article"),
        ("HARBOURS ELEVEN II", "Harbour's Eleven 2",
         "case, an apostrophe and a roman numeral"),
        ("MOVIE - UEBUNG II", "Übung 2",
         "a filler, a separator, a written-out accent and a numeral"),
        ("JONAS AND GRETA: STORM-CHASERS",
         "Jönas & Greta: StormChasers",
         "an ampersand, an accent, a colon and a joined-up compound"),
        ("KEEPER OF KEYS PART II — THE TOWERS",
         "The Keeper of the Keys Part 2 - The Towers",
         "two missing articles, a numeral, an em dash and case"),
        ("N.E.S.T. II", "The NEST 2",
         "an abbreviation's points, an article and a numeral"),
    ])
    def test_a_name_that_went_wrong_twice_is_still_the_same_title(self, one,
                                                                   other,
                                                                   about):
        assert titlematch.equivalent(one, other), about

    def test_stacking_them_does_not_stack_the_keys(self):
        """Each reading only widens where it CHANGES something, so a name five
        readings all have something to say about still costs a handful of keys
        rather than every combination of five."""
        keys = titlematch.title_keys(
            "KEEPER OF KEYS PART II — THE TOWERS")
        assert len(keys) <= 32

    def test_and_two_different_films_do_not_meet_through_a_stack(self):
        """Widening is not the same as matching everything."""
        assert not titlematch.equivalent(
            "THE KEEPER OF THE KEYS PART II",
            "The Smith Part 2")


class TestALetterFromTheWrongKeyboard:
    """A name whose letters are the SHAPE of Latin ones is that name. The fold
    has no opinion about Cyrillic beyond dropping it, which turned "Tempest" whose
    T is a Cyrillic TE into "hor" and called the file a different film."""

    @pytest.mark.parametrize("latin,confusable,about", [
        ("Tempest - Rising", "Тempest - Rising", "Cyrillic TE for T"),
        ("Aurora 13", "Аurora 13", "Cyrillic A"),
        ("Cars", "Сars", "Cyrillic ES for C"),
        ("Home", "Hоme", "Cyrillic O, mid-word"),
        ("Alpha Beta", "Αlpha Βeta", "Greek alpha and beta"),
        ("Origin", "Οrigin", "Greek omicron"),
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
        assert titlematch.normalize_title("Тempest - Rising (2017)") \
            == "tempest rising 2017"

    def test_a_title_really_written_in_cyrillic_is_unharmed(self):
        """Both sides of every comparison are folded by this same function, so
        a real Cyrillic title matches itself whether these apply or not."""
        assert titlematch.equivalent("Корабль",
                                     "Корабль")

    def test_and_two_different_films_still_do_not_meet(self):
        assert not titlematch.equivalent("Тempest", "Loki")


class TestTheFirstOfASeries:
    """Numbered three ways and meant identically."""

    @pytest.mark.parametrize("one,other", [
        ("Falkenauge I", "Falkenauge 1"),
        ("Falkenauge I", "Falkenauge"),
        ("Falkenauge 1", "Falkenauge"),
        ("Falkenauge I (1963)", "Falkenauge (1963)"),
    ])
    def test_all_three_numberings_are_one_film(self, one, other):
        assert titlematch.equivalent(one, other)

    @pytest.mark.parametrize("one,other", [
        ("Falkenauge II", "Falkenauge"),
        ("Falkenauge 2", "Falkenauge"),
        ("Falkenauge 2", "Falkenauge 1"),
        ("Granite III", "Granite"),
    ])
    def test_but_only_the_first(self, one, other):
        """A trailing "2" is the sequel, and dropping it would file the sequel
        under the film before it."""
        assert not titlematch.equivalent(one, other)

    def test_the_bare_name_is_offered_as_a_query(self):
        assert "Falkenauge" in titlematch.search_titles("Falkenauge I")

    def test_a_roman_numeral_in_a_conjunctions_place_is_still_a_numeral(self):
        """"Falkenauge I (1963)" has its "I" between two words, which is where a
        conjunction sits - read as one it folds to "falkenauge and 1963" and the
        numeral is never read at all."""
        keys = titlematch.title_keys("Falkenauge I (1963)")
        assert "falkenauge 1 1963" in keys
        assert "falkenauge 1963" in keys

    def test_and_a_conjunction_in_that_place_is_still_a_conjunction(self):
        assert titlematch.equivalent("Jonas & Greta", "Jönas und Greta")


class TestANumberTheTitleSurrounds:
    """A number with title on both sides of it may be left out, because what
    surrounds it still says which film it is."""

    @pytest.mark.parametrize("written,without", [
        ("Steel Halo 2: Silence", "Steel Halo Silence"),
        ("Steel Halo II: Silence", "Steel Halo Silence"),
        ("Ranger 3 The Return", "Ranger The Return"),
    ])
    def test_the_number_may_be_left_out(self, written, without):
        assert titlematch.equivalent(written, without)

    @pytest.mark.parametrize("one,other", [
        ("Falkenauge 2", "Falkenauge"),
        ("Ember 3", "Ember"),
        ("Cut Short Vol 2", "Cut Short Vol"),
        ("Granite II", "Granite"),
    ])
    def test_but_never_the_one_at_the_end(self, one, other):
        """There is nothing after it to say which film it is: the sequel would
        be wearing the original's name."""
        assert not titlematch.equivalent(one, other)

    def test_nor_a_year_in_the_middle(self, ):
        """The year is the one thing besides the title that says which film
        this is."""
        assert not titlematch.equivalent("Night Runner 2049 Nexus",
                                         "Night Runner Nexus")

    def test_and_two_sequels_that_differ_only_by_it_are_still_two(self):
        assert not titlematch.equivalent("Hallows 4 The Return",
                                         "Hallows 5 The Revenge")

    def test_the_keys_stay_bounded(self):
        stacked = "The Movie Special Film Part II & The Sequel III et IV I"
        assert len(titlematch.title_keys(stacked)) <= titlematch.MAX_KEYS


class TestAPossessiveThatWentMissing:
    """A series' name is written with the possessive and without it, and the
    fold turns the apostrophe into a space - so the "s" survives as a word of
    its own and the two titles differ by it."""

    @pytest.mark.parametrize("one,other", [
        ("Wilder & Bright's Watch This", "Wilder and Bright Watch This"),
        ("Wilder and Bright's Watch This", "Wilder and Bright Watch This"),
        ("A Cat's Tale", "A Cat Tale"),
    ])
    def test_the_two_spellings_meet(self, one, other):
        assert titlematch.equivalent(one, other)

    def test_and_the_apostrophe_readings_still_work(self):
        assert titlematch.equivalent("A Cat's Tale", "A Cats Tale")

    def test_two_different_films_still_do_not(self):
        assert not titlematch.equivalent("A Cat's Tale", "A Dog's Tale")


class TestALabelWrittenIntoATitle:
    """"Sunstriker the Movie - Ember" is the film a catalogue holds as
    "Sunstriker: Ember", and the only thing between them is a label somebody
    wrote into the name to say what KIND of thing the file is."""

    def test_the_label_and_the_number_it_stands_in_for_meet(self):
        assert titlematch.equivalent(
            "Sunstriker 19 - Ember and the Clockwork Wonder",
            "Sunstriker the Movie - Ember and the Clockwork Wonder")

    @pytest.mark.parametrize("written", [
        "Sunstriker the Movie - Ember",
        "Sunstriker The Film: Ember",
        "Sunstriker Movie - Ember",
    ])
    def test_however_it_was_written(self, written):
        assert titlematch.equivalent("Sunstriker: Ember", written)

    @pytest.mark.parametrize("written", [
        "Looking for Group Documentary",
        "Looking for Group - The Documentary",
        "Looking for Group the Movie",
        "Looking for Group Dokumentation",
    ])
    def test_and_the_end_of_a_name_is_the_same_word_in_another_place(
            self, written):
        """Where the label landed is where their hand happened to be, not a
        difference in what it says."""
        assert titlematch.equivalent("Looking for Group", written)

    def test_but_a_label_is_only_one_where_title_stands_in_FRONT_of_it(self):
        """A leading run can be cut on sight - nothing standing before the
        title is the title - while a word further in is only a label if a title
        came first. And a word of the name is still a word of it."""
        assert not titlematch.equivalent("Falcons Forever The Movie",
                                         "Falcons Forever The Sequel")
        assert not titlematch.equivalent("Falcons Forever The Sequel",
                                         "Falcons Forever")

    def test_and_the_spelling_is_only_asked_about_where_it_was_written(self):
        assert "Sunstriker - Ember" in titlematch.search_titles(
            "Sunstriker the Movie - Ember")
        assert "Looking for Group" in titlematch.search_titles(
            "Looking for Group Documentary")
        assert titlematch.search_titles("Sunstriker - Ember") \
            == ["Sunstriker - Ember", "Ember"]


class TestTheWordThatOnlyLabelsANumber:
    """A catalogue writes "Part 2" where a folder writes "2", and a German one
    writes "2. Teil". The figure says which film of the series this is; the
    word beside it says only that a figure is there."""

    @pytest.mark.parametrize("one,other", [
        ("The Hills Beyond 2", "The Hills Beyond Part 2"),
        ("The Hills Beyond 2", "The Hills Beyond Part II"),
        ("Falkenauge 2", "Falkenauge Teil 2"),
        ("Falkenauge 2", "Falkenauge 2. Teil"),
        ("Steel Halo 3", "Steel Halo Chapter Three"),
        ("Granite 2", "Granite Vol. 2"),
    ])
    def test_the_label_may_be_on_either_side_or_neither(self, one, other):
        assert titlematch.equivalent(one, other)

    def test_and_it_stacks_with_the_number_a_title_surrounds(self):
        """The folder above said the franchise, the file left the number out,
        and the catalogue wrote both plus the word labelling the number - which
        is three readings between one film and itself."""
        assert titlematch.equivalent(
            "Falkenauge The New Blood",
            "Falkenauge Part VII - The New Blood")

    @pytest.mark.parametrize("one,other", [
        ("Cut Short Vol 2", "Cut Short Vol"),
        ("Falkenauge Part", "Falkenauge"),
        ("Falkenauge Part 2", "Falkenauge Part 3"),
    ])
    def test_but_a_word_with_no_number_to_label_is_a_word(self, one, other):
        assert not titlematch.equivalent(one, other)


class TestANumberSpelledOut:
    """A title counts in figures on one side and in words on the other."""

    @pytest.mark.parametrize("one,other", [
        ("2 Wanderers", "Two Wanderers"),
        ("The Glorious 7", "The Glorious Seven"),
        ("Falkenauge 2", "Falkenauge Two"),
    ])
    def test_the_word_and_the_figure_are_one_number(self, one, other):
        assert titlematch.equivalent(one, other)

    def test_the_reading_runs_one_way_only(self):
        """A number has one spelling in words, so folding the word ONTO the
        figure meets everything folding the other way would."""
        assert "2 wanderers" in titlematch.title_keys("Two Wanderers")

    def test_and_two_different_numbers_are_still_two(self):
        assert not titlematch.equivalent("Falkenauge Two", "Falkenauge Three")

    def test_the_figure_is_offered_as_a_query(self):
        assert "Falkenauge Chapter 2" in titlematch.search_titles(
            "Falkenauge Chapter Two")


class TestTheNameASequelHasOfItsOwn:
    """What follows a sequel's number is the sequel's own name, and it is the
    half of the title one side routinely leaves out - because nobody says it
    out loud."""

    @pytest.mark.parametrize("one,other", [
        ("Missing Since 2", "Missing Since 2 - The Beginning"),
        ("Missing Since 2", "Missing Since 2. Teil - Die Rückkehr"),
        ("Steel Halo II", "Steel Halo Part 2: Silence"),
        ("Falkenauge", "Falkenauge 1: The First Flight"),
    ])
    def test_the_number_already_says_which_film_it_is(self, one, other):
        assert titlematch.equivalent(one, other)

    def test_and_so_is_a_second_title_written_after_the_first(self):
        """A catalogue that concatenates a market's name on to the film's, and
        a folder that wrote the other name into brackets, are the same shape
        from two directions."""
        assert titlematch.equivalent("W-D-R-2 (S:W-D-R)", "W/D/R/2")

    @pytest.mark.parametrize("one,other", [
        ("Nordwind", "Nordwind: Der Sturm"),
        ("Rivertown", "Rivertown - The Reckoning"),
        ("Falkenauge 2", "Falkenauge 3: The Return"),
    ])
    def test_but_a_title_with_no_number_in_it_is_only_its_name(self, one,
                                                               other):
        """Cutting one at its separator would file every subtitle under a
        franchise that is a film of its own."""
        assert not titlematch.equivalent(one, other)


class TestOneSlipOfTheHand:
    """The last reading of all, and the only one that says somebody wrote the
    title wrongly rather than differently."""

    @pytest.mark.parametrize("one,other", [
        ("Thinner Then Air", "Thinner Than Air"),       # the plain slip
        ("The Colorized Print", "The Colourized Print"),  # an americanism
        ("Marekk Halvor", "Marek Halvor"),              # a doubled letter
        ("Falcons Forevr", "Falcons Forever"),          # a dropped one
    ])
    def test_a_single_letter_is_read_as_a_slip(self, one, other):
        assert titlematch.one_typo_apart(one, other)
        assert titlematch.one_typo_apart(other, one)

    @pytest.mark.parametrize("one,other", [
        ("Thinner Tahn Air", "Thinner Than Air"),   # a hand at speed
        ("Marek Halovr", "Marek Halvor"),           # the last two crossed
        ("Flacons Forever", "Falcons Forever"),     # the first two
        ("Nordiwnd Rising", "Nordwind Rising"),     # inside a word
    ])
    def test_and_so_are_two_letters_written_in_the_wrong_order(self, one,
                                                               other):
        assert titlematch.one_typo_apart(one, other)
        assert titlematch.one_typo_apart(other, one)

    @pytest.mark.parametrize("one,other", [
        ("Falcon sForever", "Falcons Forever"),     # the space struck late
        ("FalconsF orever", "Falcons Forever"),     # and struck early
        ("MarekH alvor Sunfall", "Marek Halvor Sunfall"),
    ])
    def test_and_a_letter_that_crossed_the_space_beside_it(self, one, other):
        """The likeliest crossing of all, because the space is the one key a
        thumb strikes while the fingers go on spelling."""
        assert titlematch.one_typo_apart(one, other)
        assert titlematch.one_typo_apart(other, one)

    @pytest.mark.parametrize("one,other", [
        ("Falcons Forevr", "Falcons Forever"),      # a letter never typed
        ("FalconsForever", "Falcons Forever"),      # the space never typed
        ("Marek HalvorSunfall", "Marek Halvor Sunfall"),
    ])
    def test_and_a_character_that_was_never_typed_at_all(self, one, other):
        assert titlematch.one_typo_apart(one, other)
        assert titlematch.one_typo_apart(other, one)

    @pytest.mark.parametrize("one,other", [
        ("Marekk Halvor Sunfall", "Marek Halvor Sunfall"),  # a key struck twice
        ("Falconsx Forever", "Falcons Forever"),    # a stray letter in a word
        ("xFalcons Forever", "Falcons Forever"),    # one hanging off the front
        ("Falcons Foreverx", "Falcons Forever"),    # and off the back
        ("Falcon s Forever", "Falcons Forever"),    # a stray space
    ])
    def test_and_a_character_struck_one_time_too_many(self, one, other):
        """The same reading as the one above and not a second one: a character
        too many in this title is a character too few in that one, and which
        way round somebody typed it is not known and does not matter."""
        assert titlematch.one_typo_apart(one, other)
        assert titlematch.one_typo_apart(other, one)

    @pytest.mark.parametrize("one,other", [
        ("Falcons  Forever", "Falcons Forever"),    # a doubled space
        (" Falcons Forever", "Falcons Forever"),    # a leading one
        ("Falcons Forever ", "Falcons Forever"),    # a trailing one
    ])
    def test_but_a_surplus_space_the_fold_drops_is_not_a_slip_at_all(
            self, one, other):
        """The fold collapses a run of spaces to one and strips the ends, so
        these are not two titles one slip apart - they are one title, and
        matched a rung earlier."""
        assert not titlematch.one_typo_apart(one, other)
        assert titlematch.equivalent(one, other)

    def test_and_not_a_digit_struck_twice(self):
        """"Falkenauge 1233" is not "Falkenauge 123" typed clumsily, it is
        another number - and nothing here guesses about numbers."""
        assert not titlematch.one_typo_apart("Falkenauge 1233",
                                             "Falkenauge 123")
        assert not titlematch.equivalent("Falkenauge 1233", "Falkenauge 123")

    def test_but_not_the_space_that_holds_two_numbers_apart(self):
        """Putting that one in, or leaving it out, is not a boundary between
        words - it is the boundary between two counts."""
        assert not titlematch.one_typo_apart("Falkenauge 12 3",
                                             "Falkenauge 123")

    def test_and_not_a_space_that_became_a_letter(self):
        """That changes the letters of the title, which is a name spelled
        otherwise rather than a key struck wrongly."""
        assert not titlematch.one_typo_apart("Falcon Sand", "FalconXSand")

    @pytest.mark.parametrize("one,other", [
        ("Falkenauge 2", "Falkenauge 3"),      # the sequel
        ("Falkenauge 12", "Falkenauge 21"),    # the sequel, crossed
        ("Falkenauge (1963)", "Falkenauge (1964)"),   # the year
        ("Falkenauge (1963)", "Falkenauge (1936)"),   # the year, crossed
        ("Ash", "Ashe"),                        # too short to risk
        ("Orbit", "Orbits"),                    # still too short
        ("Falcons Forever", "Falcons Forever"),  # already the same title
        ("Falcons Forever", "Falcon Forevr"),   # two letters, not one
        ("Rivertown Abcd", "Rivertown Cbad"),   # two crossings, not one
        ("Marek Halvor", "Halvor Marek"),       # two WORDS crossed is a
                                                # different title, not a slip
    ])
    def test_and_nothing_else_is(self, one, other):
        assert not titlematch.one_typo_apart(one, other)
        assert not titlematch.one_typo_apart(other, one)

    def test_it_is_not_a_key_and_never_widens_a_fold(self):
        """A guess must not be able to make two titles equivalent - every
        caller asks for it by name, and asks last."""
        assert not titlematch.equivalent("Thinner Then Air", "Thinner Than Air")
        assert not titlematch.equivalent("Thinner Tahn Air", "Thinner Than Air")

    @pytest.mark.parametrize("one,other", [
        ("Falcon sForever", "Falcons Forever"),
        ("FalconsForever", "Falcons Forever"),
    ])
    def test_but_a_boundary_that_moved_was_already_a_fold(self, one, other):
        """The two readings that touch a space decide nothing a caller had not
        already been told: a slip that leaves the LETTERS in the order they
        were is the key this module builds with no separator at all, and every
        caller asks that first. They are here so the question answers correctly
        when it is asked on its own."""
        assert titlematch.equivalent(one, other)


class TestTheHeadingAPrefixMightBe:
    def test_a_title_breaks_at_its_first_separator(self):
        assert titlematch.leading_segment("Marek Halvor - Sunfall") \
            == ("Marek Halvor", "Sunfall")
        assert titlematch.leading_segment("Nordwind: Der Sturm") \
            == ("Nordwind", "Der Sturm")

    def test_and_a_title_with_no_break_has_no_heading(self):
        assert titlematch.leading_segment("Falcons Forever") == ("", "")
