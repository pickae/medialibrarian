#!/usr/bin/env python3
"""Bring the tools medialibrarian drives up to date, wherever each one came from.

The commands lean on software from half a dozen sources - apt, a snap, pipx
venvs, ``pipx run`` script environments, cargo, GitHub release binaries and git
checkouts built by hand - and each source has its own way of being updated.
This runs all of them from one place:

    tools/update_deps.py -n          # what is out of date, changing nothing
    tools/update_deps.py             # the same report, then update after a yes
    tools/update_deps.py --only ffmpeg,av1gpu
    tools/update_deps.py --no-sudo   # leave out everything that needs root

It is a script in the checkout rather than an installed command because one of
the things it updates is the pipx venv the installed commands run from.

How it behaves, and why:

  * **Look first, then change.** Every source is checked (in parallel, read-only)
    and the whole plan is printed before anything is touched; nothing is changed
    without a yes (or ``-y``). ``-n`` stops after the report. The one thing the
    check does write is ``git fetch``'s remote-tracking refs.
  * **Only what this repository uses, only what is already installed.** It
    updates the tools the commands drive, not the whole machine, and never
    installs a tool that is missing - the commands' own preflight says how.
  * **Each install is updated by whoever owns it.** A pipx venv by pipx, a
    cargo binary by cargo with the features it was built with, a nightly
    yt-dlp with ``--pre``, a checkout by fast-forward only.
  * **Downloads are verified.** A release asset is checked against the SHA-256
    digest GitHub records for it, and against the release's own checksum file
    where it publishes one; an asset with neither is refused.
  * **A new build has to do what the old one did.** A new ffmpeg must still
    have every encoder the old one had, and a new av1gpu every option, before
    it replaces the installed one (``--force`` overrides).
  * **Nothing is lost.** A replaced binary is kept beside the new one as
    ``<name>.<old version>.bak``, and the swap is a rename, so a run that has
    the old one open keeps it. A venv a running process is using is left alone.
  * **Major versions are held** for the sources that use semantic versions
    (cargo crates, DuckDB) until ``--allow-major``; a major release is the one
    allowed to break what the commands ask of it.
  * **One failure does not stop the rest.** Each step's outcome is in the
    summary at the end, and the exit status is 1 when any step failed.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import functools
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOME = Path.home()
USER_AGENT = "medialibrarian-update-deps"

# The pipx venvs the commands run: the package itself, the subtitle aligner and
# both yt-dlp lines. ruff and mypy are the repository's own lint gate.
PIPX_VENVS = ("medialibrarian", "ffsubsync", "yt-dlp", "yt-dlp-nightly", "uv",
              "ruff", "mypy")

# The two helpers started through ``pipx run``: the module that marks their
# cached environment, and the PyPI name of the package it belongs to.
PIPX_RUN_PACKAGES = {"whisper_ctranslate2": "whisper-ctranslate2",
                     "subliminal": "subliminal"}

# The cargo-installed tools the commands use.
CARGO_CRATES = ("dovi_tool", "hdr10plus_tool", "ssimulacra2_rs")

# Which builds of ffmpeg sit where on the ladder, and the release repository
# each one comes from. A directory with no ffmpeg in it is passed over.
FFMPEG_BUILDS = (
    (HOME / ".local/bin", "BtbN/FFmpeg-Builds"),
    (Path("/opt/ffmpeg/bin"), "QuickFatHedgehog/FFmpeg-Builds-SVT-AV1-HDR"),
)

# What is installed here but deliberately not updated, said at the end so it
# is not forgotten: (the path that shows it is installed, why it is left).
NOT_MANAGED = (
    ("/usr/local/bin/exhale", "exhale: built by hand from its GitLab source"),
    ("~/vapoursynth", "the upscale stack: VapourSynth is pinned at R79 on purpose"),
)

UPDATE, CURRENT, HELD, SKIPPED, FAILED, DONE = (
    "update", "current", "held", "skipped", "FAILED", "updated")


class StepError(Exception):
    """A step that could not be done, with the reason to print."""


@dataclass
class Row:
    """One thing that was checked, and what became of it."""
    step: str
    item: str
    installed: str = ""
    available: str = ""
    status: str = CURRENT
    note: str = ""
    # What brings it up to date. Rows that one call updates together (the apt
    # packages) share the same function, which then runs once.
    apply: Callable[[], str | None] | None = field(default=None, repr=False)
    root: bool = False


@dataclass
class Options:
    dry_run: bool = False
    yes: bool = False
    sudo: bool = True
    allow_major: bool = False
    force: bool = False


OPTS = Options()


# --- running things ----------------------------------------------------------

def run(cmd: list[str], *, cwd: Path | str | None = None, live: bool = False,
        check: bool = True, timeout: float | None = None) -> str:
    """Run ``cmd``; return its stdout. ``live`` shows the command and lets its
    output through, for the steps that change something."""
    if live:
        print("  $ " + shlex.join(str(c) for c in cmd), flush=True)
    result = subprocess.run(
        [str(c) for c in cmd], cwd=cwd, text=True, timeout=timeout,
        stdout=None if live else subprocess.PIPE,
        stderr=None if live else subprocess.PIPE,
        stdin=None if live else subprocess.DEVNULL)
    if check and result.returncode != 0:
        detail = (result.stderr or "").strip().splitlines()
        raise StepError("%s exited %d%s" % (
            shlex.join(str(c) for c in cmd[:3]), result.returncode,
            ": " + detail[-1] if detail else ""))
    return result.stdout or ""


def live_step(cmd: list[str]) -> Callable[[], str | None]:
    """An update that is just ``cmd``, run where it can be seen."""
    def step() -> None:
        run(cmd, live=True)
    return step


def sudo(cmd: list[str]) -> list[str]:
    return cmd if os.geteuid() == 0 else ["sudo", *cmd]


def keep_sudo_alive(stop: threading.Event, every: float = 60) -> None:
    """Refresh sudo's timestamp until ``stop``: a slow download can outlast
    it, and the step that needs root then sits at a password prompt."""
    while not stop.wait(every):
        subprocess.run(["sudo", "-n", "-v"], stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def urlopen(url: str, timeout: float, accept: str | None = None):
    """``urlopen`` that tries three times, a few seconds apart: a release CDN
    answering 5xx for a moment is common and not worth a failed step."""
    headers = {"User-Agent": USER_AGENT, **({"Accept": accept} if accept else {})}
    request = urllib.request.Request(url, headers=headers)
    for attempt in range(3):
        try:
            return urllib.request.urlopen(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            if error.code < 500 or attempt == 2:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise
        time.sleep(5 * (attempt + 1))
    raise AssertionError("unreachable")


def http_get(url: str, timeout: float = 30) -> bytes:
    with urlopen(url, timeout) as response:
        return bytes(response.read())


def github_json(path: str):
    """A GitHub API answer: through ``gh`` when it is there (authenticated, so
    no rate limit to speak of), else anonymously - also when ``gh`` is there
    but not logged in."""
    if shutil.which("gh"):
        try:
            return json.loads(run(["gh", "api", path], timeout=60))
        except StepError:
            pass
    return json.loads(http_get("https://api.github.com/" + path))


def pypi_latest(package: str) -> str:
    return json.loads(http_get("https://pypi.org/pypi/%s/json" % package))["info"]["version"]


def download(asset: dict, dest: Path, digest: str | None) -> None:
    """Fetch a release asset to ``dest`` and check it against ``sha256:<hex>``.

    Through the API's asset address first: github.com's download address is
    the one that goes down on its own now and then, and the API's carries on.
    """
    for url, accept in ((asset["url"], "application/octet-stream"),
                        (asset["browser_download_url"], None)):
        sha = hashlib.sha256()
        try:
            with urlopen(url, 60, accept) as response, open(dest, "wb") as out:
                while chunk := response.read(1 << 20):
                    sha.update(chunk)
                    out.write(chunk)
            break
        except urllib.error.URLError:
            if url == asset["browser_download_url"]:
                raise
    verify_digest(dest.name, sha.hexdigest(), digest)


def verify_digest(name: str, actual: str, expected: str | None) -> None:
    if not expected:
        if OPTS.force:
            return
        raise StepError("%s has no published checksum to verify it against "
                        "(--force takes it anyway)" % name)
    algorithm, _, value = expected.partition(":")
    if algorithm != "sha256" or value.lower() != actual.lower():
        raise StepError("%s does not match its published checksum" % name)


def checksum_from_file(text: str, name: str) -> str | None:
    """The ``sha256:<hex>`` a sha256sum-style file gives for ``name``."""
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == name:
            return "sha256:" + parts[0]
    return None


def processes_using(path: Path) -> list[int]:
    """The processes (other than this one) running from under ``path``."""
    needle = str(path)
    pids = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(
                errors="replace")
            exe = os.readlink(entry / "exe")
        except OSError:
            continue
        if needle in cmdline or exe.startswith(needle):
            pids.append(int(entry.name))
    return pids


def refuse_if_in_use(path: Path) -> None:
    pids = processes_using(path)
    if pids:
        raise StepError("in use by PID %s; run again once it has finished"
                        % ", ".join(map(str, pids[:5])))


def replace_file(new: Path, target: Path, backup_name: str | None) -> str:
    """Put ``new`` in ``target``'s place by a rename, keeping the old file as
    ``backup_name`` beside it (a hard link where possible, so it costs no
    space). Returns what to say about the backup."""
    said = ""
    if backup_name and target.exists():
        backup = target.with_name(backup_name)
        if not backup.exists():
            try:
                os.link(target, backup)
            except OSError:
                shutil.copy2(target, backup)
        said = "old one kept as %s" % backup.name
    staging = target.with_name("." + target.name + ".new")
    shutil.copy2(new, staging)
    staging.chmod(0o755)
    os.replace(staging, target)
    return said


def replace_file_as_root(new: Path, target: Path, backup_name: str) -> str:
    backup = target.with_name(backup_name)
    if not backup.exists():
        run(sudo(["cp", "-p", str(target), str(backup)]), live=True)
    run(sudo(["install", "-m", "755", str(new), str(target)]), live=True)
    return "old one kept as %s" % backup.name


# --- versions ----------------------------------------------------------------

def version_key(version: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", version))


def is_newer(available: str, installed: str) -> bool:
    return version_key(available) > version_key(installed)


def is_major_bump(available: str, installed: str) -> bool:
    new, old = version_key(available), version_key(installed)
    return bool(new and old) and new[0] > old[0]


def update_or_hold(row: Row, apply: Callable[[], str | None]) -> Row:
    """Mark ``row`` as an update, or hold it when it is a major release."""
    if is_major_bump(row.available, row.installed) and not OPTS.allow_major:
        row.status, row.note = HELD, "a major release (--allow-major takes it)"
    else:
        row.status, row.apply = UPDATE, apply
    return row


# --- apt ---------------------------------------------------------------------

def apt_packages() -> list[str]:
    """The Debian packages the commands' install hints name."""
    sys.path.insert(0, str(REPO))
    from medialib.lib import tooldeps
    names: set[str] = set()
    for note in tooldeps._TOOL_NOTES.values():
        names.update(re.findall(r"apt install ([\w.+-]+)", note))
    return sorted(names)


def parse_apt_simulation(text: str) -> list[tuple[str, str, str]]:
    """(package, installed, candidate) for each ``Inst`` line of ``apt-get -s``."""
    found = []
    for line in text.splitlines():
        m = re.match(r"Inst (\S+) \[([^\]]+)\] \((\S+)", line)
        if m:
            found.append((m.group(1), m.group(2), m.group(3)))
    return found


def check_apt() -> list[Row]:
    if not shutil.which("apt-get"):
        return []
    listing = run(["dpkg-query", "-W", "-f", "${Package}\t${db:Status-Status}\n",
                   *apt_packages()], check=False)
    installed = [line.split("\t")[0] for line in listing.splitlines()
                 if line.endswith("\tinstalled")]
    if not installed:
        return []
    note = ""
    if OPTS.dry_run or not OPTS.sudo:
        lists = Path("/var/lib/apt/lists")
        age = (time.time() - lists.stat().st_mtime) / 86400 if lists.exists() else 0
        note = "package lists are %.0f days old" % age if age >= 1 else ""
    else:
        run(sudo(["apt-get", "update", "-qq"]), timeout=600)
    pending = parse_apt_simulation(
        run(["apt-get", "-s", "install", "--only-upgrade", *installed]))
    if not pending:
        return [Row("apt", "%d packages" % len(installed), note=note)]

    def apply() -> None:
        run(sudo(["apt-get", "install", "--only-upgrade", "-y", *installed]), live=True)

    return [Row("apt", name, old, new, UPDATE, note, apply, root=True)
            for name, old, new in pending]


# --- uv ----------------------------------------------------------------------

def snap_name(command: str) -> str:
    """The snap a /snap/bin command belongs to: its link names ``<snap>.<app>``,
    or points straight at snap when the app is called as the snap is."""
    target = os.readlink(command) if os.path.islink(command) else ""
    if target and "/" not in target:
        return target.split(".", 1)[0]
    return os.path.basename(command)


def check_uv() -> list[Row]:
    uv = shutil.which("uv")
    if not uv:
        return []
    real = os.path.realpath(uv)
    if uv.startswith("/snap/"):
        snap = snap_name(uv)
        version = run(["snap", "list", snap]).splitlines()[1].split()[1]
        pending = run(["snap", "refresh", "--list"], check=False)
        row = Row("uv", "uv (snap %s)" % snap, version)
        for line in pending.splitlines()[1:]:
            parts = line.split()
            if parts and parts[0] == snap:
                row.available, row.status, row.root = parts[1], UPDATE, True
                row.apply = live_step(sudo(["snap", "refresh", snap]))
                return [row]
        row.note = "snapd also refreshes it on its own"
        return [row]
    if "/pipx/venvs/" in real:
        return []  # the pipx step has it
    answer = run(["uv", "self", "update", "--dry-run"], check=False)
    m = re.search(r"from (v?[\d.]+) to (v?[\d.]+)", answer)
    if m:
        row = Row("uv", "uv", m.group(1), m.group(2))
        return [update_or_hold(row, live_step(["uv", "self", "update"]))]
    version = run(["uv", "--version"]).split()[1]
    return [Row("uv", "uv", version)]


# --- pipx --------------------------------------------------------------------

def pipx_venvs() -> dict:
    if not shutil.which("pipx"):
        return {}
    return dict(json.loads(run(["pipx", "list", "--json"]))["venvs"])


def pipx_home() -> Path:
    return Path(run(["pipx", "environment", "--value", "PIPX_HOME"]).strip())


def normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def check_pipx_venv(name: str, meta: dict, venv_dir: Path) -> list[Row]:
    main = meta["metadata"]["main_package"]
    pip_args = main.get("pip_args") or []
    pre = ["--pre"] if "--pre" in pip_args else []
    outdated = json.loads(run(["pipx", "runpip", name, "list", "--outdated",
                               "--format=json", *pre], timeout=300) or "[]")
    spec = main["package_or_url"]
    if os.path.isdir(spec):
        # A checkout: its code is the checkout's business, so what is updated
        # here is what it depends on, within what its pyproject allows.
        stale = [p for p in outdated
                 if normalise(p["name"]) not in ("pip", "setuptools", "wheel")]
        if not stale:
            return [Row("pipx", name + " (dependencies)", note="from " + spec)]
        editable = ["-e"] if "--editable" in pip_args else []

        def apply_deps() -> None:
            refuse_if_in_use(venv_dir)
            run(["pipx", "runpip", name, "install", "--upgrade",
                 "--upgrade-strategy", "eager", *editable, spec], live=True)

        return [Row("pipx", "%s: %s" % (name, p["name"]), p["version"],
                    p["latest_version"], UPDATE, apply=apply_deps) for p in stale]

    package = normalise(main["package"])
    installed = main.get("package_version", "")
    for p in outdated:
        if normalise(p["name"]) == package:
            upgrade = ["pipx", "upgrade", *(["--pip-args=--pre"] if pre else []), name]

            def apply(upgrade: list[str] = upgrade) -> None:
                refuse_if_in_use(venv_dir)
                run(upgrade, live=True)

            return [Row("pipx", name, installed, p["latest_version"], UPDATE, apply=apply)]
    return [Row("pipx", name, installed)]


def check_pipx() -> list[Row]:
    venvs = pipx_venvs()
    if not venvs:
        return []
    home = pipx_home()
    wanted = [n for n in PIPX_VENVS if n in venvs]
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(wanted) or 1) as pool:
        futures = [(n, pool.submit(check_pipx_venv, n, venvs[n], home / "venvs" / n))
                   for n in wanted]
    rows: list[Row] = []
    for n, future in futures:
        try:
            rows += future.result()
        except Exception as error:
            rows.append(Row("pipx", n, status=FAILED, note=str(error)))
    return rows


# --- pipx run script environments -------------------------------------------

def helper_upper_bound(package: str) -> int | None:
    """The major version a helper's inline metadata keeps ``package`` below."""
    for helper in (REPO / "medialib/helpers").glob("*.py"):
        m = re.search(r'"%s[^"]*<\s*(\d+)' % re.escape(package), helper.read_text())
        if m:
            return int(m.group(1))
    return None


def check_pipx_run() -> list[Row]:
    if not shutil.which("pipx"):
        return []
    cache = pipx_home() / ".cache"
    rows = []
    for module, package in PIPX_RUN_PACKAGES.items():
        found = sorted(cache.glob("*/lib/python*/site-packages/%s-*.dist-info" % module))
        if not found:
            rows.append(Row("pipx-run", package, status=SKIPPED,
                            note="not built yet; pipx builds it on first use"))
            continue
        env = found[0].parents[3]
        installed = found[0].name[len(module) + 1:-len(".dist-info")]
        row = Row("pipx-run", package, installed, pypi_latest(package))
        bound = helper_upper_bound(package)
        if not is_newer(row.available, installed):
            row.available = ""
        elif bound is not None and version_key(row.available)[0] >= bound:
            row.status, row.note = HELD, "the helper keeps it below %d" % bound
        else:
            def apply(env: Path = env) -> str:
                refuse_if_in_use(env)
                shutil.rmtree(env)
                return "cached environment removed; the next run rebuilds it"
            row.status, row.apply = UPDATE, apply
        rows.append(row)
    return rows


# --- cargo -------------------------------------------------------------------

def cargo_home() -> Path:
    return Path(os.environ.get("CARGO_HOME", HOME / ".cargo"))


def parse_crates2_key(key: str) -> tuple[str, str, str]:
    """(name, version, source) from a ``.crates2.json`` install key."""
    m = re.match(r"^(\S+) (\S+) \((.+)\)$", key)
    if not m:
        raise StepError("cannot read cargo's record %r" % key)
    return m.group(1), m.group(2), m.group(3)


def github_repo_of(url: str) -> str | None:
    m = re.search(r"github\.com[/:]([^/]+)/([^/?#]+?)(?:\.git)?(?:[?#]|$)", url)
    return "%s/%s" % (m.group(1), m.group(2)) if m else None


def cargo_install_command(name: str, record: dict, source: str, available: str) -> list[str]:
    cmd = ["cargo", "install", "--locked"]
    if record.get("no_default_features"):
        cmd.append("--no-default-features")
    if record.get("all_features"):
        cmd.append("--all-features")
    if record.get("features"):
        cmd += ["--features", ",".join(record["features"])]
    if source.startswith("git+"):
        cmd += ["--git", re.split(r"[?#]", source[4:])[0], "--tag", available, name]
    else:
        cmd += [name, "--version", available]
    return cmd


def check_cargo() -> list[Row]:
    record_file = cargo_home() / ".crates2.json"
    if not shutil.which("cargo") or not record_file.exists():
        return []
    rows = []
    for key, record in json.loads(record_file.read_text())["installs"].items():
        name, installed, source = parse_crates2_key(key)
        if name not in CARGO_CRATES:
            continue
        if source.startswith("registry+"):
            info = json.loads(http_get("https://crates.io/api/v1/crates/" + name))
            available = info["crate"]["max_stable_version"]
        else:
            repo = github_repo_of(source)
            if not repo or "tag=" not in source:
                rows.append(Row("cargo", name, installed, status=SKIPPED,
                                note="built from a branch, not a release"))
                continue
            available = github_json("repos/%s/releases/latest" % repo)["tag_name"]
        row = Row("cargo", name, installed, available)
        if not is_newer(available, installed):
            row.available = ""
            rows.append(row)
            continue
        cmd = cargo_install_command(name, record, source, available)
        rows.append(update_or_hold(row, live_step(cmd)))
    return rows


# --- ffmpeg builds -----------------------------------------------------------

FFMPEG_ARCH = {"x86_64": "linux64", "aarch64": "linuxarm64"}


def ffmpeg_build_number(version_output: str) -> int | None:
    m = re.search(r"version N-(\d+)-g", version_output)
    return int(m.group(1)) if m else None


def pick_ffmpeg_asset(releases: list[dict], arch: str) -> tuple[dict, dict, int] | None:
    """The newest dated release's versioned GPL build: (release, asset, N)."""
    pattern = re.compile(r"^ffmpeg-N-(\d+)-g[0-9a-f]+-%s-gpl\.tar\.xz$" % re.escape(arch))
    for release in releases:
        if not release["tag_name"].startswith("autobuild-"):
            continue
        for asset in release["assets"]:
            m = pattern.match(asset["name"])
            if m:
                return release, asset, int(m.group(1))
    return None


def encoders(ffmpeg: Path) -> set[str]:
    listing = run([str(ffmpeg), "-hide_banner", "-encoders"]).split("------", 1)[-1]
    return {line.split()[1] for line in listing.splitlines() if len(line.split()) > 1}


def release_asset_digest(release: dict, asset: dict, workdir: Path) -> str | None:
    """The asset's digest from GitHub, cross-checked with the release's own
    checksum file when it has one."""
    digest = asset.get("digest")
    sums = next((a for a in release["assets"]
                 if a["name"] in ("checksums.sha256", "SHA256SUMS", "sha256sums.txt")), None)
    if sums:
        sums_path = workdir / sums["name"]
        download(sums, sums_path, sums.get("digest"))
        listed = checksum_from_file(sums_path.read_text(), asset["name"])
        if digest and listed and digest.lower() != listed.lower():
            raise StepError("%s: GitHub's digest and the checksum file disagree"
                            % asset["name"])
        digest = digest or listed
    return digest


def install_ffmpeg(bindir: Path, release: dict, asset: dict, old_number: int) -> str:
    with tempfile.TemporaryDirectory(prefix="update-deps-") as tmp:
        work = Path(tmp)
        archive = work / asset["name"]
        download(asset, archive,
                 release_asset_digest(release, asset, work))
        with tarfile.open(archive) as tar:
            for member in tar.getmembers():
                tool = member.name.rsplit("/", 1)[-1]
                if member.isfile() and "/bin/" in member.name and tool in ("ffmpeg", "ffprobe"):
                    source = tar.extractfile(member)
                    assert source is not None
                    (work / tool).write_bytes(source.read())
                    (work / tool).chmod(0o755)
        for tool in ("ffmpeg", "ffprobe"):
            if not (work / tool).exists():
                raise StepError("the archive holds no %s" % tool)
            run([str(work / tool), "-hide_banner", "-version"])
        lost = encoders(bindir / "ffmpeg") - encoders(work / "ffmpeg")
        if lost and not OPTS.force:
            raise StepError("the new build lacks encoders the old one has: %s "
                            "(--force installs it anyway)" % ", ".join(sorted(lost)))
        for tool in ("ffmpeg", "ffprobe"):
            backup = "%s.N-%d.bak" % (tool, old_number)
            if os.access(bindir, os.W_OK):
                replace_file(work / tool, bindir / tool, backup)
            else:
                replace_file_as_root(work / tool, bindir / tool, backup)
        return "old pair kept as ffmpeg.N-%d.bak, ffprobe.N-%d.bak" % (old_number, old_number)


def check_ffmpeg() -> list[Row]:
    arch = FFMPEG_ARCH.get(platform.machine())
    if not arch or sys.platform != "linux":
        return []
    rows = []
    for bindir, repo in FFMPEG_BUILDS:
        exe = bindir / "ffmpeg"
        if not exe.exists():
            continue
        item = "ffmpeg in %s" % str(bindir).replace(str(HOME), "~")
        number = ffmpeg_build_number(run([str(exe), "-hide_banner", "-version"]))
        if number is None:
            rows.append(Row("ffmpeg", item, status=SKIPPED,
                            note="not a master build; left as it is"))
            continue
        found = pick_ffmpeg_asset(github_json("repos/%s/releases?per_page=5" % repo), arch)
        if not found:
            rows.append(Row("ffmpeg", item, "N-%d" % number, status=SKIPPED,
                            note="no build for this machine in %s" % repo))
            continue
        release, asset, latest = found
        row = Row("ffmpeg", item, "N-%d" % number, "N-%d" % latest,
                  root=not os.access(bindir, os.W_OK))
        if latest > number:
            row.status = UPDATE
            row.apply = functools.partial(install_ffmpeg, bindir, release, asset, number)
        else:
            row.available = ""
        rows.append(row)
    return rows


# --- DuckDB ------------------------------------------------------------------

DUCKDB_ARCH = {"x86_64": ("amd64",), "aarch64": ("aarch64", "arm64")}


def pick_duckdb_asset(assets: list[dict], machine: str) -> dict | None:
    pattern = re.compile(r"^duckdb[-_]cli-linux-(\w+)\.(?:tar\.gz|zip)$")
    for asset in assets:
        m = pattern.match(asset["name"])
        if m and m.group(1) in DUCKDB_ARCH.get(machine, ()):
            return asset
    return None


def unpack_one(archive: Path, name: str, dest: Path) -> None:
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            dest.write_bytes(z.read(name))
    else:
        with tarfile.open(archive) as tar:
            member = next(m for m in tar.getmembers()
                          if m.isfile() and m.name.rsplit("/", 1)[-1] == name)
            source = tar.extractfile(member)
            assert source is not None
            dest.write_bytes(source.read())
    dest.chmod(0o755)


def install_duckdb(exe: Path, release: dict, asset: dict, installed: str) -> str:
    version = release["tag_name"].lstrip("v")
    with tempfile.TemporaryDirectory(prefix="update-deps-") as tmp:
        work = Path(tmp)
        archive = work / asset["name"]
        download(asset, archive,
                 release_asset_digest(release, asset, work))
        unpack_one(archive, "duckdb", work / "duckdb")
        run([str(work / "duckdb"), "--version"])
        # The official installer's layout: ~/.duckdb/cli/<version>/duckdb, with
        # "latest" a link to the one in use. The old version stays where it is.
        if exe.parent.parent.name == "cli" and (exe.parent.parent / "latest").is_symlink():
            versioned = exe.parent.parent / version
            versioned.mkdir(exist_ok=True)
            shutil.copy2(work / "duckdb", versioned / "duckdb")
            staging = exe.parent.parent / ".latest.new"
            with contextlib.suppress(FileNotFoundError):
                staging.unlink()
            staging.symlink_to(versioned)
            os.replace(staging, exe.parent.parent / "latest")
            return "%s kept beside it" % exe.parent
        return replace_file(work / "duckdb", exe, "duckdb.%s.bak" % installed)


def check_duckdb() -> list[Row]:
    found = shutil.which("duckdb")
    if not found or sys.platform != "linux":
        return []
    exe = Path(os.path.realpath(found))
    # A search rather than the first word: DuckDB 2 can put a line of its own
    # ahead of the version when it thinks an agent is running it.
    m = re.search(r"\bv\d+\.\d+[\w.]*", run([str(exe), "--version"]))
    if not m:
        return [Row("duckdb", "duckdb", status=SKIPPED, note="cannot read its version")]
    installed = m.group(0)
    release = github_json("repos/duckdb/duckdb/releases/latest")
    row = Row("duckdb", "duckdb", installed, release["tag_name"])
    if not is_newer(row.available, installed):
        row.available = ""
        return [row]
    asset = pick_duckdb_asset(release["assets"], platform.machine())
    if not asset:
        row.status, row.note = SKIPPED, "no build for this machine"
        return [row]
    return [update_or_hold(row, lambda: install_duckdb(exe, release, asset, installed))]


# --- git checkouts -----------------------------------------------------------

def git(path: Path, *args: str, check: bool = True) -> str:
    return run(["git", "-C", str(path), *args], check=check, timeout=120).strip()


def check_checkout(step: str, path: Path,
                   after: Callable[[str], str | None] | None = None) -> list[Row]:
    """Fast-forward a checkout to its upstream, then ``after(old_head)``."""
    if not (path / ".git").exists():
        return []
    item = str(path).replace(str(HOME), "~")
    git(path, "fetch", "--quiet")
    branch = git(path, "symbolic-ref", "--short", "-q", "HEAD", check=False)
    upstream = git(path, "rev-parse", "--abbrev-ref", "@{u}", check=False)
    if not branch or not upstream or "fatal" in upstream:
        return [Row(step, item, status=SKIPPED, note="not on a branch that tracks one")]
    behind, ahead = map(int, git(path, "rev-list", "--left-right", "--count",
                                 "@{u}...HEAD").split())
    head = git(path, "rev-parse", "--short", "HEAD")
    row = Row(step, item, head, git(path, "rev-parse", "--short", "@{u}"))
    if behind == 0:
        row.available = ""
    elif ahead:
        row.status = SKIPPED
        row.note = "%d local commits on %s; only a fast-forward is done" % (ahead, branch)
    else:
        row.status = UPDATE
        row.note = "%d new commits on %s" % (behind, upstream)

        def apply() -> str | None:
            # --ff-only refuses rather than merging, and git refuses to
            # overwrite a local change, so a hand-edited file stays as it is.
            git(path, "merge", "--ff-only", "--quiet", "@{u}")
            return after(head) if after else None

        row.apply = apply
    return [row]


def check_medialibrarian() -> list[Row]:
    meta = pipx_venvs().get("medialibrarian")
    spec = meta["metadata"]["main_package"]["package_or_url"] if meta else ""
    return check_checkout("medialibrarian", Path(spec)) if os.path.isdir(spec) else []


def check_subcleaner() -> list[Row]:
    found = shutil.which("subcleaner")
    if not found:
        return []
    script = Path(os.path.realpath(found))

    def after(_old: str) -> None:
        # The checkout is kept with core.fileMode off, so a merge that rewrites
        # the script writes it without the execute bit the link onto PATH needs.
        script.chmod(script.stat().st_mode | 0o111)

    return check_checkout("subcleaner", script.parent, after)


def check_ebook2audiobook() -> list[Row]:
    path = os.environ.get("narrationHome") or str(HOME / "ebook2audiobook")
    return check_checkout("ebook2audiobook", Path(path))


def help_options(exe: Path | str) -> set[str]:
    text = run([str(exe), "--help"], check=False)
    return set(re.findall(r"(?<![\w-])--[a-z][\w-]*", text))


def check_av1gpu() -> list[Row]:
    checkout = Path(os.environ.get("AV1GPU_SOURCE", HOME / "av1gpu"))
    name = os.environ.get("imageGpuEncoder") or "av1gpu"
    installed = shutil.which(name)
    if not installed:
        return []
    target = Path(installed)

    def build_and_install(old_head: str) -> str:
        run(["cmake", "--build", "build"], cwd=checkout, live=True)
        built = checkout / "build/av1gpu"
        lost = help_options(target) - help_options(built)
        if lost and not OPTS.force:
            raise StepError("the new build no longer takes %s; not installed "
                            "(--force installs it anyway)" % ", ".join(sorted(lost)))
        return replace_file(built, target, "av1gpu.%s.bak" % old_head)

    return check_checkout("av1gpu", checkout, build_and_install)


# --- the run -----------------------------------------------------------------

# In the order they are applied: the checkout first, because the dependency
# refresh after it reads its pyproject; the long av1gpu build last.
STEPS: dict[str, Callable[[], list[Row]]] = {
    "medialibrarian": check_medialibrarian,
    "apt": check_apt,
    "uv": check_uv,
    "pipx": check_pipx,
    "pipx-run": check_pipx_run,
    "cargo": check_cargo,
    "ffmpeg": check_ffmpeg,
    "duckdb": check_duckdb,
    "subcleaner": check_subcleaner,
    "ebook2audiobook": check_ebook2audiobook,
    "av1gpu": check_av1gpu,
}


def check_all(steps: Iterable[str]) -> list[Row]:
    steps = list(steps)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = {name: pool.submit(STEPS[name]) for name in steps}
    rows: list[Row] = []
    for name in steps:
        try:
            rows += futures[name].result()
        except Exception as error:
            rows.append(Row(name, name, status=FAILED, note="check failed: %s" % error))
    return rows


def print_table(rows: list[Row]) -> None:
    widths = [max([len(getattr(r, f)) for r in rows] + [len(h)])
              for f, h in (("step", "STEP"), ("item", "ITEM"), ("installed", "INSTALLED"),
                           ("available", "AVAILABLE"))]
    template = "  %%-%ds  %%-%ds  %%-%ds  %%-%ds  %%s" % tuple(widths)
    print(template % ("STEP", "ITEM", "INSTALLED", "AVAILABLE", "STATUS"))
    for r in rows:
        status = r.status + (" (sudo)" if r.root and r.status == UPDATE else "")
        print((template % (r.step, r.item, r.installed, r.available, status)
               + ("  - " + r.note if r.note else "")).rstrip())


def apply_all(rows: list[Row]) -> None:
    done: dict[int, tuple[str, str]] = {}
    for row in rows:
        if row.status != UPDATE or row.apply is None:
            continue
        key = id(row.apply)
        if key not in done:
            print("\n== %s: %s" % (row.step, row.item), flush=True)
            try:
                done[key] = (DONE, row.apply() or "")
            except (StepError, OSError, subprocess.SubprocessError, ValueError) as error:
                done[key] = (FAILED, str(error))
                print("  ! %s" % error, flush=True)
        row.status, outcome = done[key]
        row.note = outcome or row.note


@contextlib.contextmanager
def single_run():
    """One update at a time: two would race each other's renames."""
    import fcntl  # POSIX only; imported here so the module loads anywhere
    runtime = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    with open(Path(runtime) / "medialib-update-deps.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.exit("Another update is already running.")
        yield


def load_settings() -> None:
    """The settings file, so a checkout moved with narrationHome is found."""
    sys.path.insert(0, str(REPO))
    from medialib.lib import settings
    settings.load()


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="update_deps.py", allow_abbrev=False,
        description="Update the external tools medialibrarian uses, each from "
                    "where it came from: %s." % ", ".join(STEPS))
    parser.add_argument("-n", "--dry-run", action="store_true",
                        help="report what is out of date and change nothing")
    parser.add_argument("-y", "--yes", action="store_true",
                        help="update without asking first")
    parser.add_argument("--only", metavar="STEPS", help="comma-separated steps to run")
    parser.add_argument("--skip", metavar="STEPS", help="comma-separated steps to leave out")
    parser.add_argument("--no-sudo", action="store_true",
                        help="leave out the updates that need root")
    parser.add_argument("--allow-major", action="store_true",
                        help="take major releases too")
    parser.add_argument("--force", action="store_true",
                        help="install a build that fails its regression check, "
                             "or a download with no checksum")
    args = parser.parse_args(argv)
    for flag in ("only", "skip"):
        names = [n for n in (getattr(args, flag) or "").split(",") if n]
        unknown = [n for n in names if n not in STEPS]
        if unknown:
            parser.error("unknown step %s (the steps: %s)"
                         % (", ".join(unknown), ", ".join(STEPS)))
        setattr(args, flag, names)
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if os.geteuid() == 0 and os.environ.get("SUDO_USER"):
        # As root, ~ is root's home: the user's pipx venvs, ~/.local/bin and
        # gh login are not the ones checked.
        sys.exit("Run this as yourself, not through sudo: it asks for sudo "
                 "itself for the updates that need root.")
    OPTS.dry_run, OPTS.yes, OPTS.sudo = args.dry_run, args.yes, not args.no_sudo
    OPTS.allow_major, OPTS.force = args.allow_major, args.force
    steps = [s for s in (args.only or STEPS) if s not in args.skip]
    load_settings()

    with single_run(), contextlib.ExitStack() as stack:
        if "apt" in steps and OPTS.sudo and not OPTS.dry_run and os.geteuid() != 0:
            # Asked for once, up front, so the parallel checks never prompt.
            if subprocess.run(["sudo", "-v"]).returncode != 0:
                print("No sudo: leaving out the updates that need root.")
                OPTS.sudo = False
            else:
                stop_keepalive = threading.Event()
                threading.Thread(target=keep_sudo_alive, args=(stop_keepalive,),
                                 daemon=True).start()
                stack.callback(stop_keepalive.set)
        print("Checking %s ..." % ", ".join(steps), flush=True)
        rows = check_all(steps)
        if not OPTS.sudo:
            for r in rows:
                if r.root and r.status == UPDATE:
                    r.status, r.apply, r.note = SKIPPED, None, "needs sudo"
        print_table(rows)
        pending = [r for r in rows if r.status == UPDATE]
        if pending and not OPTS.dry_run:
            if not OPTS.yes:
                if not sys.stdin.isatty():
                    print("\nNot a terminal to ask in: pass -y to update.")
                    return 1
                answer = input("\nUpdate the %d marked above? [y/N] " % len(pending))
                if answer.strip().lower() not in ("y", "yes"):
                    return 0
            apply_all(rows)
            print("\nSummary:")
            print_table([r for r in rows if r.status in (DONE, FAILED, SKIPPED, HELD)])
        elif not pending:
            held = sum(r.status == HELD for r in rows)
            print("\nNothing to update%s." % (", %d held back" % held if held else ""))

    left = [why for where, why in NOT_MANAGED if Path(where).expanduser().exists()]
    if left:
        print("\nNot updated here: " + "; ".join(left) + ".")
    return 1 if any(r.status == FAILED for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
