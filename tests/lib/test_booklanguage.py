"""The white box for medialib/lib/booklanguage.py.

What is pinned here is the code
table, the two mechanisms of the text fallback (a script of its own settles its
language outright, the Latin script is scored on stop words), and the metadata
parsing (the first OPF language element, the first ``Languages:`` field).
"""

import pytest

from medialib.lib import booklanguage

pytestmark = pytest.mark.stubbed


class _Result:
    def __init__(self, stdout=b""):
        self.stdout = stdout
        self.returncode = 0


class TestBookLanguageCode:
    @pytest.mark.parametrize("raw,expected", [
        ("en", "eng"),
        ("en-US", "eng"),
        ("de_DE", "deu"),
        ("ger", "deu"),
        ("fre", "fra"),
        ("dut", "nld"),
        ("cze", "ces"),
        ("German", "deu"),
        ("ITA", "ita"),
        ("pt-BR", "por"),
        ("zh-CN", "zho"),
        ("jp", "jpn"),
        ("chi", "zho"),
        ("cmn", "zho"),
        ("xx", ""),
        ("", ""),
    ])
    def test_spelling(self, raw, expected):
        assert booklanguage.book_language_code(raw) == expected


class TestDetectTextLanguage:
    PROSE = {
        "eng": ("The quick brown fox is not the animal that was seen from the "
                "window, and it was not what they have said about this. The "
                "house is old but the roof is new, and from the top of that "
                "hill you have a view of the sea. They are not what you think "
                "they are, and this is what the story is about."),
        "deu": ("Der Mann ist nicht das, was die Leute schon immer über ihn "
                "gesagt haben, und auch die Frau wurde nicht gefragt. Das Haus "
                "ist alt, aber das Dach ist neu, und wenn man oben steht, sieht "
                "man das Meer. Es werden noch mehr Leute kommen, aber nur wenn "
                "das Wetter schon besser ist."),
        "fra": ("Les gens qui sont dans la maison ne savent pas ce que cette "
                "histoire pour eux est vraiment, mais elle est plus longue que "
                "tout ce qui a dans le village. Cette femme est aussi comme "
                "leur mère, sans elle rien dans ce pays qui est pour nous."),
        "spa": ("Los hombres que estan en la casa no saben lo que esta "
                "historia para ellos es, pero es mas larga que todo cuando el "
                "pueblo esta muy sobre esta. Esta mujer es como sus madres, "
                "para nosotros porque entre las cosas todo esta muy sobre "
                "esto."),
        "ita": ("Che cosa non per questo sono come alla fine dei giorni delle "
                "case, questo anche quando essere tutto dopo senza molto nella "
                "casa. Non sono per delle cose che alla fine sono come questo, "
                "anche quando dopo tutto essere nella stanza."),
        "nld": ("Het huis van een man die niet weet dat zijn dochter maar ook "
                "deze dingen naar de stad door hij zij waren werd over omdat "
                "nog niet worden gezegd. Het is een van deze dingen die niet "
                "naar door hij worden."),
    }

    @pytest.mark.parametrize("code", sorted(PROSE))
    def test_prose(self, code):
        assert booklanguage.detect_text_language(self.PROSE[code]) == code

    @pytest.mark.parametrize("code,text", [
        ("rus", "Это был очень длинный день, и никто не знал, что будет "
                "дальше. Дом стоял на холме, а море было далеко внизу."),
        ("jpn", "これは日本語の文章です。彼は本を読んでいました。空はとても"
                "青くて、風が静かに吹いていた。"),
        ("zho", "这是一个中文的测试句子，没有假名，只有汉字，足够长。"),
        ("kor", "이것은 한국어 문장입니다. 한글로만 작성된 긴 문장입니다."),
        ("ara", "هذا نص طويل باللغة العربية للاختبار ولا شيء آخر فيه."),
        ("hin", "यह हिन्दी में लिखा गया एक बहुत लंबा टेक्स्ट है जो भाषा "
                "परीक्षण के लिए है।"),
    ])
    def test_script_of_its_own(self, code, text):
        assert booklanguage.detect_text_language(text) == code

    def test_kana_beats_han(self):
        # The Japanese passage carries both; the kana test runs first.
        assert booklanguage.detect_text_language(
            "これは漢字ですこれは漢字ですこれは漢字です") == "jpn"

    def test_a_quotation_does_not_flip_the_verdict(self):
        text = (self.PROSE["deu"]
                + ' "The quick brown fox", sagte er, und das Haus ist alt.')
        assert booklanguage.detect_text_language(text) == "deu"

    def test_gibberish_gets_no_answer(self):
        assert booklanguage.detect_text_language(
            "xyzzy plugh frotz blorple zork grue") == ""

    def test_empty_input_gets_no_answer(self):
        assert booklanguage.detect_text_language("") == ""

    def test_a_script_below_the_share_is_not_a_decision(self):
        # A few Cyrillic words in an English page: not enough of the letters
        # to settle the script, and the English stop words outscore the rest.
        text = self.PROSE["eng"] + " но это был дом на холме у моря"
        assert booklanguage.detect_text_language(text) == "eng"

    def test_one_word_scores_every_language_it_belongs_to(self):
        # 'che' is Italian; a page of only it is Italian, whatever the table
        # order would do with a tie.
        assert booklanguage.detect_text_language("che che che che che") == "ita"


class TestMetadataParsing:
    def test_first_opf_language_element(self):
        opf = ("<package>\n  <metadata>\n    <dc:language>de-DE</dc:language>\n"
               "    <dc:language>en</dc:language>\n  </metadata>\n</package>")
        assert booklanguage._first_opf_language(opf) == "de-DE"

    def test_a_bare_language_element_counts_too(self):
        assert booklanguage._first_opf_language(
            "<metadata><language>fra </language></metadata>") == "fra "

    def test_no_language_element(self):
        assert booklanguage._first_opf_language("<metadata></metadata>") == ""

    def test_an_empty_language_element(self):
        assert booklanguage._first_opf_language(
            "<dc:language></dc:language>") == ""

    def test_first_languages_field_of_metadata(self):
        meta = "Title: A Book\nLanguages:  deu, eng\nAuthor: None\n"
        assert booklanguage._first_metadata_language(meta) == "deu, eng"

    def test_the_singular_field_matches_too(self):
        assert booklanguage._first_metadata_language("Language: por\n") == "por"

    def test_no_field(self):
        assert booklanguage._first_metadata_language("Title: A Book\n") == ""

    def test_the_first_matching_line_wins(self):
        meta = "Languages: spa\nTags: x\nLanguages: fra\n"
        assert booklanguage._first_metadata_language(meta) == "spa"


class _Tools:
    """A which/run pair that answers the tool questions as told to."""

    def __init__(self, available=(), outputs=()):
        self.available = set(available)
        self.outputs = list(outputs)

    def which(self, name):
        return name if name in self.available else None

    def run(self, argv, **_kwargs):
        assert self.outputs, "more converter calls than canned outputs"
        return self.outputs.pop(0)


@pytest.fixture
def tools(monkeypatch):
    return _Tools


def test_epub_reads_its_own_opf(tools, tmp_path):
    epub = tmp_path / "book.epub"
    epub.write_bytes(b"not a zip")
    opf = _Result(b"<metadata><dc:language>de-DE</dc:language></metadata>")
    assert booklanguage.book_metadata_language(
        str(epub),
        which=tools(available={"unzip"}, outputs=[opf]).which,
        run=tools(available={"unzip"}, outputs=[opf]).run) == "deu"


def test_an_epub_that_states_nothing_goes_to_ebook_meta(tools, tmp_path):
    epub = tmp_path / "book.epub"
    epub.write_bytes(b"not a zip")
    silent = _Result(b"<metadata></metadata>")
    meta = _Result(b"Title: A Book\nLanguages:  ita\n")
    assert booklanguage.book_metadata_language(
        str(epub),
        which=tools(available={"unzip", "ebook-meta"},
                    outputs=[silent, meta]).which,
        run=tools(available={"unzip", "ebook-meta"},
                  outputs=[silent, meta]).run) == "ita"


def test_an_unreadable_epub_is_not_guessed_at(tools, tmp_path):
    epub = tmp_path / "book.epub"
    epub.write_bytes(b"not a zip")
    broken = _Result(b"")
    assert booklanguage.book_metadata_language(
        str(epub),
        which=tools(available={"unzip"}).which,
        run=tools(available={"unzip"}, outputs=[broken]).run) == ""


def test_every_other_format_goes_to_ebook_meta(tools, tmp_path):
    book = tmp_path / "book.mobi"
    book.write_bytes(b"whatever")
    meta = _Result(b"Languages:\nTitle: A Book\n")
    assert booklanguage.book_metadata_language(
        str(book),
        which=tools(available={"ebook-meta"}).which,
        run=tools(available={"ebook-meta"}, outputs=[meta]).run) == ""


def test_without_any_tool_there_is_no_answer(tools, tmp_path):
    book = tmp_path / "book.epub"
    book.write_bytes(b"whatever")
    assert booklanguage.book_metadata_language(
        str(book), which=tools().which, run=tools().run) == ""


def test_the_comma_cut_and_the_normalisation_happen(tools, tmp_path):
    book = tmp_path / "book.epub"
    book.write_bytes(b"whatever")
    meta = _Result(b"Languages:  pt-BR, de\n")
    assert booklanguage.book_metadata_language(
        str(book),
        which=tools(available={"ebook-meta"}).which,
        run=tools(available={"ebook-meta"}, outputs=[meta]).run) == "por"


class _Writer:
    """A bookToText stand-in: writes the canned text, reports the canned status."""

    def __init__(self, data=b"", status=0):
        self.data = data
        self.status = status
        self.calls = []

    def __call__(self, src, dest):
        self.calls.append((src, dest))
        if self.data:
            with open(dest, "wb") as handle:
                handle.write(self.data)
        return self.status


def test_a_book_shorter_than_the_skip_reads_from_the_top(tools, tmp_path,
                                                          monkeypatch):
    monkeypatch.setattr(booklanguage.shutil, "which", tools(
        available={"ebook-convert"}).which)
    writer = _Writer(TestDetectTextLanguage.PROSE["deu"].encode("utf-8"))
    assert booklanguage.book_text_language(
        str(tmp_path / "book.epub"), ram_base=str(tmp_path),
        book_to_text_fn=writer) == "deu"
    assert writer.calls and writer.calls[0][0].endswith("book.epub")


def test_the_skip_window_is_read_from_past_the_front_matter(tools, tmp_path,
                                                            monkeypatch):
    monkeypatch.setattr(booklanguage.shutil, "which", tools(
        available={"ebook-convert"}).which)
    front = (TestDetectTextLanguage.PROSE["eng"] + " ") * 60
    front = front.encode("utf-8")[:3999]
    back = TestDetectTextLanguage.PROSE["deu"].encode("utf-8") * 3
    writer = _Writer(front + back)
    assert booklanguage.book_text_language(
        str(tmp_path / "book.epub"), ram_base=str(tmp_path),
        book_to_text_fn=writer) == "deu"


def test_a_failed_conversion_gets_no_answer(tools, tmp_path, monkeypatch):
    monkeypatch.setattr(booklanguage.shutil, "which", tools(
        available={"ebook-convert"}).which)
    writer = _Writer(status=1)
    assert booklanguage.book_text_language(
        str(tmp_path / "book.epub"), ram_base=str(tmp_path),
        book_to_text_fn=writer) == ""
    assert writer.calls, "the conversion was attempted"


def test_an_empty_conversion_gets_no_answer(tools, tmp_path, monkeypatch):
    monkeypatch.setattr(booklanguage.shutil, "which", tools(
        available={"ebook-convert"}).which)
    writer = _Writer(data=b"")
    assert booklanguage.book_text_language(
        str(tmp_path / "book.epub"), ram_base=str(tmp_path),
        book_to_text_fn=writer) == ""


def test_without_a_converter_the_book_is_not_even_opened(tools, tmp_path,
                                                         monkeypatch):
    monkeypatch.setattr(booklanguage.shutil, "which", tools().which)
    writer = _Writer(TestDetectTextLanguage.PROSE["deu"].encode("utf-8"))
    assert booklanguage.book_text_language(
        str(tmp_path / "book.epub"), ram_base=str(tmp_path),
        book_to_text_fn=writer) == ""
    assert not writer.calls, "no converter, no conversion"


def test_a_pdf_without_pdftotext_still_asks_for_ebook_convert(tools, tmp_path,
                                                              monkeypatch):
    monkeypatch.setattr(booklanguage.shutil, "which", tools(
        available={"ebook-convert"}).which)
    writer = _Writer(status=1)
    assert booklanguage.book_text_language(
        str(tmp_path / "book.pdf"), ram_base=str(tmp_path),
        book_to_text_fn=writer) == ""
    assert writer.calls, "ebook-convert can read a pdf"


def test_book_language_asks_the_metadata_first(tools, tmp_path, monkeypatch):
    monkeypatch.setattr(booklanguage.shutil, "which", tools(
        available={"unzip"}).which)
    opf = _Result(b"<metadata><dc:language>de-DE</dc:language></metadata>")
    runs = tools(available={"unzip"}, outputs=[opf])
    original = booklanguage.book_metadata_language
    monkeypatch.setattr(booklanguage, "book_metadata_language",
                        lambda src: original(src, which=runs.which,
                                             run=runs.run))

    def _no_text(src, ram_base=None):
        raise AssertionError("the text must not be read when the metadata answers")
    monkeypatch.setattr(booklanguage, "book_text_language", _no_text)
    assert booklanguage.book_language(
        str(tmp_path / "book.epub"), ram_base=str(tmp_path)) == "deu"


def test_book_language_falls_back_to_the_text(tools, tmp_path, monkeypatch):
    monkeypatch.setattr(booklanguage.shutil, "which", tools(
        available={"ebook-meta", "ebook-convert"}).which)
    meta = _Result(b"Title: A Book\n")
    runs = tools(available={"ebook-meta"}, outputs=[meta])
    original_meta = booklanguage.book_metadata_language
    monkeypatch.setattr(booklanguage, "book_metadata_language",
                        lambda src: original_meta(src, which=runs.which,
                                                  run=runs.run))
    writer = _Writer(TestDetectTextLanguage.PROSE["fra"].encode("utf-8"))
    original_text = booklanguage.book_text_language
    monkeypatch.setattr(booklanguage, "book_text_language",
                        lambda src, ram_base=None: original_text(
                            src, ram_base=ram_base, book_to_text_fn=writer))
    assert booklanguage.book_language(
        str(tmp_path / "book.epub"), ram_base=str(tmp_path)) == "fra"
    assert writer.calls, "the metadata said nothing, so the text was read"