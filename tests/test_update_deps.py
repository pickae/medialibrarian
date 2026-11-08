"""Tests for the dependency updater kept beside the package, in tools/.

What is pinned here is what decides whether something gets replaced: reading
versions and release listings, holding major releases, refusing a download that
does not match its checksum, keeping the old binary, and only ever
fast-forwarding a checkout. Nothing here reaches the network.
"""

import importlib.util
import subprocess
import sys

import pytest

from tests import blackbox

_spec = importlib.util.spec_from_file_location(
    "update_deps", blackbox.REPO / "tools" / "update_deps.py")
assert _spec and _spec.loader
u = importlib.util.module_from_spec(_spec)
sys.modules["update_deps"] = u  # the dataclasses look themselves up there
_spec.loader.exec_module(u)


@pytest.mark.pure
class TestVersions:
    def test_newer_compares_numerically(self):
        assert u.is_newer("2.3.10", "2.3.9")
        assert not u.is_newer("v1.5.5", "1.5.5")

    def test_a_major_release_is_held(self, monkeypatch):
        monkeypatch.setattr(u.OPTS, "allow_major", False)
        row = u.update_or_hold(u.Row("duckdb", "duckdb", "v1.5.5", "v2.0.0"), lambda: None)
        assert row.status == u.HELD and row.apply is None

    def test_allow_major_takes_it(self, monkeypatch):
        monkeypatch.setattr(u.OPTS, "allow_major", True)
        row = u.update_or_hold(u.Row("duckdb", "duckdb", "v1.5.5", "v2.0.0"), lambda: None)
        assert row.status == u.UPDATE

    def test_a_minor_release_is_taken(self, monkeypatch):
        monkeypatch.setattr(u.OPTS, "allow_major", False)
        row = u.update_or_hold(u.Row("cargo", "dovi_tool", "2.3.3", "2.3.4"), lambda: None)
        assert row.status == u.UPDATE


@pytest.mark.pure
class TestParsing:
    def test_apt_simulation(self):
        text = ("NOTE: This is only a simulation!\n"
                "Inst ffmpeg [7:8.0-1] (7:8.0-2 Ubuntu:26.04/resolute-updates [amd64])\n"
                "Conf ffmpeg (7:8.0-2 Ubuntu:26.04/resolute-updates [amd64])\n")
        assert u.parse_apt_simulation(text) == [("ffmpeg", "7:8.0-1", "7:8.0-2")]

    def test_apt_packages_come_from_the_install_hints(self):
        packages = u.apt_packages()
        assert {"ffmpeg", "mkvtoolnix", "poppler-utils"} <= set(packages)
        assert all(" " not in p for p in packages)

    def test_crates2_keys(self):
        assert u.parse_crates2_key(
            "ssimulacra2_rs 0.5.2 (registry+https://github.com/rust-lang/crates.io-index)"
        ) == ("ssimulacra2_rs", "0.5.2", "registry+https://github.com/rust-lang/crates.io-index")
        name, version, source = u.parse_crates2_key(
            "some_tool 2.3.3 (git+https://github.com/example-owner/some_tool?tag=2.3.3#d1abe0e2)")
        assert (name, version) == ("some_tool", "2.3.3")
        assert u.github_repo_of(source) == "example-owner/some_tool"

    def test_cargo_reinstalls_with_the_features_it_was_built_with(self):
        cmd = u.cargo_install_command(
            "ssimulacra2_rs", {"no_default_features": True, "features": []},
            "registry+https://github.com/rust-lang/crates.io-index", "0.6.0")
        assert cmd == ["cargo", "install", "--locked", "--no-default-features",
                       "ssimulacra2_rs", "--version", "0.6.0"]

    def test_cargo_follows_a_git_install_by_its_tag(self):
        cmd = u.cargo_install_command(
            "some_tool", {}, "git+https://github.com/example-owner/some_tool?tag=2.3.3#d1a",
            "2.3.4")
        assert cmd[-5:] == ["--git", "https://github.com/example-owner/some_tool",
                            "--tag", "2.3.4", "some_tool"]

    def test_ffmpeg_build_number(self):
        assert u.ffmpeg_build_number(
            "ffmpeg version N-128433-gdb8537d8cc-20261105 Copyright") == 128433
        assert u.ffmpeg_build_number("ffmpeg version 8.1 Copyright") is None

    def test_the_ffmpeg_asset_is_the_newest_dated_build_for_this_machine(self):
        releases = [
            {"tag_name": "latest",
             "assets": [{"name": "ffmpeg-master-latest-linux64-gpl.tar.xz"}]},
            {"tag_name": "autobuild-2026-11-07-12-47",
             "assets": [{"name": "ffmpeg-N-128560-g967f0ffb10-linuxarm64-gpl.tar.xz"},
                        {"name": "ffmpeg-N-128560-g967f0ffb10-linux64-gpl-shared.tar.xz"},
                        {"name": "ffmpeg-N-128560-g967f0ffb10-linux64-gpl.tar.xz"}]},
            {"tag_name": "autobuild-2026-11-06-12-50",
             "assets": [{"name": "ffmpeg-N-128500-g0000000000-linux64-gpl.tar.xz"}]},
        ]
        release, asset, number = u.pick_ffmpeg_asset(releases, "linux64")
        assert release["tag_name"] == "autobuild-2026-11-07-12-47"
        assert asset["name"] == "ffmpeg-N-128560-g967f0ffb10-linux64-gpl.tar.xz"
        assert number == 128560

    def test_the_duckdb_asset_is_not_the_musl_one(self):
        assets = [{"name": "duckdb-cli-linux-amd64-musl.tar.gz"},
                  {"name": "duckdb-cli-linux-aarch64.tar.gz"},
                  {"name": "duckdb-cli-linux-amd64.tar.gz"}]
        assert u.pick_duckdb_asset(assets, "x86_64")["name"] == "duckdb-cli-linux-amd64.tar.gz"
        assert u.pick_duckdb_asset(assets, "aarch64")["name"] == "duckdb-cli-linux-aarch64.tar.gz"

    def test_the_subliminal_helper_bounds_its_major_version(self):
        assert u.helper_upper_bound("subliminal") == 3
        assert u.helper_upper_bound("whisper-ctranslate2") is None


@pytest.mark.pure
class TestChecksums:
    def test_from_a_checksum_file(self):
        text = "abc123  other.zip\ndef456  ffmpeg.tar.xz\n"
        assert u.checksum_from_file(text, "ffmpeg.tar.xz") == "sha256:def456"
        assert u.checksum_from_file(text, "missing") is None

    def test_a_mismatch_is_refused(self, monkeypatch):
        monkeypatch.setattr(u.OPTS, "force", True)
        with pytest.raises(u.StepError, match="does not match"):
            u.verify_digest("a", "00", "sha256:ff")

    def test_no_checksum_is_refused_unless_forced(self, monkeypatch):
        monkeypatch.setattr(u.OPTS, "force", False)
        with pytest.raises(u.StepError, match="no published checksum"):
            u.verify_digest("a", "00", None)
        monkeypatch.setattr(u.OPTS, "force", True)
        u.verify_digest("a", "00", None)

    def test_a_match_passes(self):
        u.verify_digest("a", "ABcd", "sha256:abCD")


@pytest.mark.fs
class TestFiles:
    def test_replace_keeps_the_old_one(self, tmp_path):
        target, new = tmp_path / "tool", tmp_path / "built"
        target.write_text("old")
        new.write_text("new")
        said = u.replace_file(new, target, "tool.1.bak")
        assert target.read_text() == "new"
        assert (tmp_path / "tool.1.bak").read_text() == "old"
        assert "tool.1.bak" in said
        assert not (tmp_path / ".tool.new").exists()

    def test_snap_name_from_the_link(self, tmp_path):
        (tmp_path / "uv").symlink_to("example-snap.uv")
        (tmp_path / "jq").symlink_to("/usr/bin/snap")
        assert u.snap_name(str(tmp_path / "uv")) == "example-snap"
        assert u.snap_name(str(tmp_path / "jq")) == "jq"


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, text=True,
                          capture_output=True).stdout.strip()


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    """A clone of a local upstream, both with one commit."""
    for who in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv("GIT_%s_NAME" % who, "test")
        monkeypatch.setenv("GIT_%s_EMAIL" % who, "test@example.invalid")
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-q", "-b", "main")
    (upstream / "file").write_text("1")
    _git(upstream, "add", "file")
    _git(upstream, "commit", "-q", "-m", "one")
    _git(tmp_path, "clone", "-q", str(upstream), "clone")
    return upstream, tmp_path / "clone"


def _commit(repo, text):
    (repo / "file").write_text(text)
    _git(repo, "commit", "-q", "-am", text)


@pytest.mark.fs
class TestCheckouts:
    def test_up_to_date(self, checkout):
        _, clone = checkout
        [row] = u.check_checkout("x", clone)
        assert row.status == u.CURRENT

    def test_behind_is_fast_forwarded_then_built(self, checkout):
        upstream, clone = checkout
        old = _git(clone, "rev-parse", "--short", "HEAD")
        _commit(upstream, "2")
        built = []
        [row] = u.check_checkout("x", clone, lambda head: built.append(head) or "built")
        assert row.status == u.UPDATE and row.note.startswith("1 new commit")
        assert (clone / "file").read_text() == "1"  # the check changed nothing
        assert row.apply() == "built"
        assert (clone / "file").read_text() == "2"
        assert built == [old]

    def test_local_commits_are_never_merged_over(self, checkout):
        upstream, clone = checkout
        _commit(upstream, "2")
        _commit(clone, "mine")
        [row] = u.check_checkout("x", clone)
        assert row.status == u.SKIPPED and row.apply is None
        assert "local commits" in row.note

    def test_a_detached_head_is_left_alone(self, checkout):
        _, clone = checkout
        _git(clone, "checkout", "-q", "--detach")
        [row] = u.check_checkout("x", clone)
        assert row.status == u.SKIPPED

    def test_not_a_checkout(self, tmp_path):
        assert u.check_checkout("x", tmp_path) == []


@pytest.mark.pure
def test_unknown_steps_are_refused(capsys):
    with pytest.raises(SystemExit):
        u.parse_args(["--only", "nope"])
    assert "unknown step nope" in capsys.readouterr().err
