"""The answers a file's name gives about an archive, and the unpacking of one.

The archive extension list (:mod:`medialib.lib.enums`) becomes a set of
questions: is this an archive, what is it called without its suffix, which tool
opens it, and does a folder of its name sit beside it. The two ``extractArchive``
functions are the ones that unpack: zip, rar and 7z go to the host's real
unpacker with the same arguments the bash did, and the tar family is read by
:mod:`tarfile`.
"""

import os
import shutil
import subprocess
import tarfile
import tempfile

from medialib.lib.enums import ARCHIVE_EXTENSIONS, shell_lower

__all__ = [
    "archive_extension_of",
    "is_archive_file",
    "archive_base_name",
    "archive_tool_specs",
    "seven_zip_command",
    "archive_shadowed_by_folder",
    "extract_archive",
    "extract_archive_as_folder",
    "prune_irregular",
]

# The extractors, the way ``archiveToolSpecs`` names them: one line per suffix,
# and ``7z`` asks for any of the three binary names the host may carry.
_TOOL_SPECS = {
    "zip": "unzip",
    "rar": "unrar",
    "7z": "7z|7zz|7za",
    "tar.zst": "tar zstd",
    "tzst": "tar zstd",
    "tar": "tar",
    "tar.gz": "tar",
    "tgz": "tar",
    "tar.bz2": "tar",
    "tbz2": "tar",
    "tbz": "tar",
    "tar.xz": "tar",
    "txz": "tar",
}


def archive_extension_of(name: str) -> str:
    """The entry of ``ARCHIVE_EXTENSIONS`` that ``name`` ends in, without its
    dot, or ``""`` when it ends in none of them.

    The LONGEST match wins - ``Book.tar.gz`` is a ``tar.gz`` and not a ``tar`` or
    a ``gz`` - and the match is case-insensitive, using the shell's per-character
    fold. The pattern requires at least one character before the dot, so a name
    that is nothing but ``.zip`` is not an archive with an empty name.
    """
    base = name.rsplit("/", 1)[-1]
    lower = shell_lower(base)
    match = ""
    for ext in ARCHIVE_EXTENSIONS:
        dot_ext = "." + ext
        if (
            lower.endswith(dot_ext)
            and len(lower) >= len(ext) + 2
            and len(ext) > len(match)
        ):
            match = ext
    return match


def is_archive_file(name: str) -> bool:
    """True when the name ends in one of the archive suffixes. A question about
    the name, not about whether the file exists or opens."""
    return bool(archive_extension_of(name))


def archive_base_name(name: str) -> str:
    """The file name (no directory part) with the archive suffix removed -
    ``Some Book.tar.gz`` becomes ``Some Book``. A name that is not an archive
    comes back unchanged."""
    base = name.rsplit("/", 1)[-1]
    ext = archive_extension_of(name)
    if ext:
        base = base[: len(base) - len(ext) - 1]
    return base


def archive_tool_specs(ext: str) -> str:
    """The ``requireTools`` specs that can unpack that suffix, space-separated,
    or ``""`` for a suffix none of them opens."""
    return _TOOL_SPECS.get(ext, "")


def seven_zip_command() -> str:
    """The name of this host's 7-Zip binary - the first of ``7z`` ``7zz`` ``7za``
    on the PATH - or ``""`` when none is present."""
    for candidate in ("7z", "7zz", "7za"):
        if shutil.which(candidate):
            return candidate
    return ""


def _dirname(path: str) -> str:
    """``dirname(1)`` on a relative path: the last component dropped, and ``.``
    when there is no component to drop. A trailing slash is stripped first, the
    way the utility does - ``a/b/`` and ``a/b`` both answer ``a``."""
    p = path.rstrip("/") if path != "/" else path
    if "/" not in p:
        return "."
    head, _, _ = p.rpartition("/")
    return head if head else "/"


def archive_shadowed_by_folder(file: str) -> bool:
    """True when a directory of the archive's own name sits next to it -
    ``Some Book.zip`` beside ``Some Book/``. That pair is one book twice, and the
    folder is the one to believe, so the caller keeps it and lets the archive be.

    Compared as spelled, because that is how an unpacked copy beside its archive
    is named: the extractors take the name from the archive.
    """
    directory = _dirname(file)
    base = archive_base_name(file)
    if not base:
        return False
    return os.path.isdir(os.path.join(directory, base))


def _run(command) -> int:
    """Run a tool the way the bash does: stdout dropped, stderr kept (it is the
    one message an archive that will not open is worth), the tool's exit status
    returned."""
    return subprocess.run(command, stdout=subprocess.DEVNULL).returncode


# tarfile learned zstd in Python 3.14; before that the host tar is the only
# reader these two have.
_ZSTD_TAR_EXTENSIONS = ("tar.zst", "tzst")


def _extract_tar(file: str, dest: str, ext: str) -> int:
    """Unpack a member of the tar family into ``dest``.

    ``r:*`` reads the compression off the file itself, the way ``tar -x`` does,
    so the suffix still only chooses the name and never the handling. The ``tar``
    filter is the tool's own path and permission behaviour: stored modes are
    kept, and a member spelled absolutely or reaching through ``..`` is refused
    rather than written outside ``dest``.
    """
    try:
        with tarfile.open(file, "r:*") as archive:
            archive.extractall(dest, filter="tar")
    except tarfile.ReadError:
        # a compression this interpreter cannot open, or a file that is not a
        # tar at all - and only the zstd suffixes can be the first
        if ext in _ZSTD_TAR_EXTENSIONS:
            return _run(["tar", "-xf", file, "-C", dest])
        return 1
    except (OSError, tarfile.TarError):
        return 1
    return 0


def prune_irregular(dest: str) -> int:
    """Every entry the unpacking left that is not a file or a folder, removed;
    how many there were.

    A book, a comic or an audiobook is files in folders. The one that matters is
    the symlink: unzip and 7-Zip write the link an archive asks for, so a member
    spelled "escape -> ../.." leaves the tree with a way out of it for every step
    that walks it afterwards. tarfile's "tar" filter refuses that shape outright;
    this is its equivalent for the three unpackers that do not.

    os.unlink and not shutil, because unlink never follows a link - what goes is
    the link and never what it pointed at, which may well be a real file of the
    user's.
    """
    found = 0
    pending = [dest]
    while pending:
        current = pending.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                pending.append(entry.path)
                continue
            if entry.is_file(follow_symlinks=False):
                continue
            # A link, a device node, a fifo, a socket. One that will not go is
            # still counted: the caller's question is whether the tree is clean.
            found += 1
            try:
                os.unlink(entry.path)
            except OSError:
                pass
    return found


def extract_archive(file: str, dest: str) -> int:
    """Unpack ``file`` into ``dest``, which must already exist. Returns the
    extractor's status - non-zero when the suffix is not one this knows, the
    destination is not a folder, the 7-Zip binary is missing, or the unpacking
    failed.

    What lands is then pruned to files and folders. A dropped link is not made
    into a failure: an archive is otherwise perfectly good content, and the
    thing worth having is a tree with no way out of it, which the removal
    already gives.
    """
    ext = archive_extension_of(file)
    if not ext or not os.path.isdir(dest):
        return 1
    if ext == "zip":
        status = _run(["unzip", "-qq", "-o", "-d", dest, "--", file])
    elif ext == "rar":
        # x, not e: the archive's own folder layout is kept, which a multi-disc
        # book needs. The destination is handed with a trailing slash so unrar
        # reads it as one.
        status = _run(["unrar", "x", "-o+", "-idq", "--", file, dest + "/"])
    elif ext == "7z":
        seven_zip = seven_zip_command()
        if not seven_zip:
            return 1
        status = _run([seven_zip, "x", "-y", "-o" + dest, "--", file])
    else:
        # The tar family, compression and all.
        status = _extract_tar(file, dest, ext)
    prune_irregular(dest)
    return status


def extract_archive_as_folder(file: str, dest: str) -> int:
    """Unpack ``file`` so that ``dest`` IS the folder the archive stood in for.
    ``dest`` must not exist yet; its parent is created if missing, and nothing is
    left behind when the extraction fails.

    Unpacked into a temporary sibling first, then the innermost folder that is an
    archive's only content is renamed into place: packing a folder usually stores
    the folder itself, so the redundant top level is dropped. An archive holding
    its files directly, or holding several entries, is renamed as it is.
    """
    dest = dest[:-1] if dest.endswith("/") and dest != "/" else dest
    if os.path.exists(dest):
        return 1
    parent = _dirname(dest)
    try:
        os.makedirs(parent, exist_ok=True)
    except OSError:
        return 1
    raw = tempfile.mkdtemp(prefix=".extracting.", dir=parent)
    try:
        if extract_archive(file, raw) != 0:
            return 1
        inner = raw
        while True:
            try:
                entries = sorted(os.listdir(inner))
            except OSError:
                break
            if len(entries) != 1:
                break
            entry = os.path.join(inner, entries[0])
            if os.path.isdir(entry) and not os.path.islink(entry):
                inner = entry
            else:
                break
        try:
            os.rename(inner, dest)
        except OSError:
            return 1
    finally:
        shutil.rmtree(raw, ignore_errors=True)
    return 0