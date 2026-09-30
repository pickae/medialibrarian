"""The white box for medialib/cli/find_gaps.py: how each kind of run is read,
what is called missing in it, and the tree the report draws.

test_find_gaps_cli.py runs the command as a process; what is pinned here is the
reading of the names, one decision at a time.
"""

import datetime

import pytest

from medialib.cli import find_gaps

pytestmark = pytest.mark.fs


def _touch(root, *paths):
    for path in paths:
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch()


def _tree(root):
    return find_gaps.render(find_gaps.analyse(str(root), "lib"))


class TestEpisodes:
    @pytest.mark.parametrize("name,expected", [
        ("Show S01E02 Title.mkv", [(1, 2)]),
        ("show.s1e14.mkv", [(1, 14)]),
        ("Show S02,E13.mkv", [(2, 13)]),
        ("Show S02 E03.mkv", [(2, 3)]),
        ("Show S01E01E02.mkv", [(1, 1), (1, 2)]),
        ("Show S01E01-E03.mkv", [(1, 1), (1, 2), (1, 3)]),
        ("Show 1x05.mkv", [(1, 5)]),
        ("Jobs01e02.mkv", []),
        ("Chapter 01.mp3", []),
    ])
    def test_every_spelling_is_read(self, name, expected):
        assert find_gaps.episodes_of(name) == expected

    def test_every_episode_below_the_highest_is_expected(self):
        entries, count = find_gaps.episode_gaps(
            ["S01E01.mkv", "S01E02.mkv", "S01E05.mkv", "S01E07.mkv"])
        assert entries == ["S01E03-E04", "S01E06"]
        assert count == 3

    def test_with_two_seasons_a_whole_season_between_is_missing(self):
        entries, _count = find_gaps.episode_gaps(
            ["S01E01.mkv", "S01E02.mkv", "S03E01.mkv", "S03E03.mkv"])
        assert entries == ["S02 (the whole season)", "S03E02"]

    def test_a_folder_of_one_season_is_not_missing_the_others(self):
        entries, _count = find_gaps.episode_gaps(["S04E01.mkv", "S04E02.mkv"])
        assert entries == []

    def test_names_that_are_mostly_not_episodes_are_not_a_season(self):
        assert find_gaps.episode_gaps(["S01E01.mkv", "a.mkv", "b.mkv"]) is None


class TestNumbers:
    @pytest.mark.parametrize("names", [
        ["1.mp3", "2.mp3", "4.mp3"],
        ["01.mp3", "02.mp3", "04.mp3"],
        ["001 a.mp3", "002 b.mp3", "004 c.mp3"],
        ["Chapter 1.mp3", "Chapter 2.mp3", "Chapter 4.mp3"],
        ["(01) a.mp3", "(02) b.mp3", "(04) c.mp3"],
        ["1 a.mp3", "02 b.mp3", "004 c.mp3"],
    ])
    def test_every_padding_reads_as_the_same_numbers(self, names):
        values, _width = find_gaps.numbers_of(names, "files")
        assert values == {1, 2, 4}
        assert find_gaps.missing_numbers(values) == [3]

    def test_a_missing_number_is_written_as_wide_as_the_run(self, tmp_path):
        _touch(tmp_path, "Book/001.mp3", "Book/002.mp3", "Book/005.mp3")
        assert _tree(tmp_path) == ["lib", "`-- Book/", "    `-- 003-004"]

    def test_a_range_prefix_is_every_number_in_it(self):
        values, _width = find_gaps.numbers_of(["01-03 a.mp3", "05 b.mp3"],
                                              "files")
        assert values == {1, 2, 3, 5}

    def test_years_are_not_a_run(self):
        assert find_gaps.missing_numbers({1994, 1999, 2011}) == []

    def test_a_run_that_starts_high_reports_only_its_inner_gaps(self):
        assert find_gaps.missing_numbers({101, 102, 104, 105}) == [103]

    def test_a_complete_run_has_nothing_missing(self):
        assert find_gaps.missing_numbers({1, 2, 3}) == []


class TestTheRunIsTheCommonestKind:
    def test_a_cover_and_the_subtitles_are_not_part_of_the_run(self, tmp_path):
        _touch(tmp_path, "Book/01.mp3", "Book/02.m4a", "Book/04.mp3",
               "Book/03 cover.jpg", "Show/S01E01.mkv", "Show/S01E02.srt",
               "Show/S01E03.mkv")
        assert _tree(tmp_path) == [
            "lib", "|-- Book/", "|   `-- 03", "`-- Show/", "    `-- S01E02"]


class TestDates:
    def _dates(self, start, step, count, skip=()):
        return [start + datetime.timedelta(days=step * i)
                for i in range(count) if i not in skip]

    def test_a_week_without_a_daily_run_is_a_gap(self):
        days = self._dates(datetime.date(2024, 1, 1), 1, 40, skip=range(10, 18))
        assert find_gaps.date_gaps(days) == [
            "20240111-20240118 (nothing for 8 days, otherwise daily)"]

    def test_a_weekend_off_a_daily_run_is_not(self):
        days = [d for d in self._dates(datetime.date(2024, 1, 1), 1, 60)
                if d.weekday() < 5]
        assert find_gaps.date_gaps(days) == []

    def test_a_month_without_a_weekly_run_is_a_gap_and_a_week_is_not(self):
        days = self._dates(datetime.date(2024, 1, 1), 7, 30,
                           skip={5} | set(range(12, 17)))
        assert find_gaps.date_gaps(days) == [
            "20240319-20240428 (nothing for 41 days, otherwise weekly)"]

    def test_two_months_without_a_monthly_run_is_a_gap(self):
        days = [datetime.date(2023, month, 1) for month in (1, 2, 3, 4, 8, 9)]
        assert find_gaps.date_gaps(days) == [
            "20230402-20230731 (nothing for 121 days, otherwise monthly)"]

    def test_one_month_off_a_monthly_run_is_not(self):
        days = [datetime.date(2023, month, 1) for month in (1, 2, 3, 5, 6, 7)]
        assert find_gaps.date_gaps(days) == []

    def test_too_few_dates_say_nothing(self):
        assert find_gaps.date_gaps([datetime.date(2024, 1, 1),
                                    datetime.date(2024, 6, 1)]) is None

    @pytest.mark.parametrize("name,expected", [
        ("20240105 Episode.mp3", datetime.date(2024, 1, 5)),
        ("2024-01-05 Episode.mp3", datetime.date(2024, 1, 5)),
        ("20241341 Episode.mp3", None),
        ("Episode 20240105.mp3", None),
    ])
    def test_a_leading_date_is_read(self, name, expected):
        assert find_gaps.date_of(name) == expected

    def test_year_folders_are_one_run_with_their_parent(self, tmp_path):
        day = datetime.date(2023, 10, 2)
        while day < datetime.date(2024, 3, 1):
            if not datetime.date(2023, 12, 1) <= day < datetime.date(2024, 1, 15):
                _touch(tmp_path, "Pod/%d/%s Ep.mp3"
                       % (day.year, day.strftime("%Y%m%d")))
            day += datetime.timedelta(days=7)
        assert _tree(tmp_path) == [
            "lib", "`-- Pod/",
            "    `-- 20231128-20240114 (nothing for 48 days, otherwise weekly)"]


class TestFolders:
    def test_a_missing_season_folder_is_named_like_the_others(self, tmp_path):
        _touch(tmp_path, "Show/Season 1/a.mkv", "Show/Season 3/a.mkv")
        assert _tree(tmp_path) == ["lib", "`-- Show/",
                                   "    `-- Season 2 (folder)"]

    def test_numbered_folders_are_a_run(self, tmp_path):
        _touch(tmp_path, "Books/01 One/a.mp3", "Books/02 Two/a.mp3",
               "Books/04 Four/a.mp3")
        assert _tree(tmp_path) == ["lib", "`-- Books/", "    `-- 03 (folder)"]

    def test_a_folder_with_nothing_missing_is_left_out(self, tmp_path):
        _touch(tmp_path, "Whole/1.mp3", "Whole/2.mp3", "Gappy/1.mp3",
               "Gappy/3.mp3")
        assert _tree(tmp_path) == ["lib", "`-- Gappy/", "    `-- 2"]

    def test_hidden_entries_are_not_looked_at(self, tmp_path):
        _touch(tmp_path, ".cache/1.mp3", ".cache/3.mp3")
        assert _tree(tmp_path) == ["lib"]
