"""The answers a file's name gives about an archive, and the unpacking of one.

The archive extension list (:mod:`medialib.lib.enums`) becomes a set of
questions: is this an archive, what is it called without its suffix, which tool
opens it, and does a folder of its name sit beside it. The two ``extractArchive``
functions are the ones that unpack: zip, rar and 7z go to the host's real
unpacker with the same arguments the bash did, and the tar family is read by
:mod:`tarfile`.

ONE POLICY FOR WHAT MAY BE WRITTEN, and it is applied BEFORE anything is
unpacked. tarfile states it as ``filter="tar"``: a member spelled absolutely or
through ``..`` is refused, and so is a link that points out of the destination.
The three external unpackers state nothing at all - they write what the archive
asks for, and by the time a tree could be tidied up afterwards the write has
already happened, which for a link followed by a member below it means a file of
the user's overwritten from inside an archive. So the members are LISTED first
and the same policy is applied to the list; an archive carrying one that fails it
is refused whole, and nothing is unpacked from it.

The tidy-up afterwards stays, for the links that are allowed: a book is files in
folders, and a link inside the tree is one more thing every later walk has to
know about.
"""

import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile

from medialib.lib.enums import ARCHIVE_EXTENSIONS, shell_lower

__all__ = [
    "archive_extension_of",
    "is_archive_file",
    "archive_base_name",
    "archive_tool_specs",
    "seven_zip_command",
    "archive_shadowed_by_folder",
    "unsafe_member",
    "archive_members",
    "unsafe_members",
    "archive_root_folder",
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


def archive_shadowed_by_folder(file: str, base: str = "") -> bool:
    """True when a directory of the archive's own name sits next to it -
    ``Some Book.zip`` beside ``Some Book/``. That pair is one book twice, and the
    folder is the one to believe, so the caller keeps it and lets the archive be.

    Compared as spelled, because that is how an unpacked copy beside its archive
    is named: the extractors take the name from the archive. ``base`` is the name
    to look for and defaults to the archive's own - a caller that unpacks under
    the name from INSIDE the archive asks about that one instead, since that is
    the name the pair would be one book twice under.
    """
    directory = _dirname(file)
    base = base or archive_base_name(file)
    if not base:
        return False
    return os.path.isdir(os.path.join(directory, base))


def _run(command) -> int:
    """Run a tool the way the bash does: stdout dropped, stderr kept (it is the
    one message an archive that will not open is worth), the tool's exit status
    returned."""
    return subprocess.run(command, stdout=subprocess.DEVNULL).returncode


def _capture(command) -> str:
    """A tool run for what it PRINTS rather than for what it does; ``""`` when it
    is missing or answers non-zero. Its own stderr goes to the terminal, because
    a listing that failed is a refusal the user has to be able to explain."""
    try:
        done = subprocess.run(command, stdout=subprocess.PIPE)
    except OSError:
        return ""
    if done.returncode != 0:
        return ""
    return done.stdout.decode("utf-8", "replace")


# tarfile learned zstd in Python 3.14; before that the zstd binary decompresses
# and tarfile still reads - which keeps the filter below in charge of what is
# written, on every interpreter.
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
            return _extract_zstd_tar(file, dest)
        return 1
    except (OSError, tarfile.TarError):
        return 1
    return 0


def _extract_zstd_tar(file: str, dest: str) -> int:
    """A .tar.zst on an interpreter whose tarfile cannot open one.

    The host's ``zstd`` decompresses and tarfile still reads, over the pipe: the
    host is asked for the COMPRESSION, which has no policy in it, and which
    members may be written stays with the same filter as the rest of the family.
    """
    try:
        zstd = subprocess.Popen(["zstd", "-dc", "--", file],
                                stdout=subprocess.PIPE)
    except OSError:
        return 1
    try:
        # "r|", the stream reader: the pipe cannot be seeked, and asking for a
        # seekable reader over one is an error rather than a slow path.
        with tarfile.open(fileobj=zstd.stdout, mode="r|") as archive:
            archive.extractall(dest, filter="tar")
    except (OSError, tarfile.TarError):
        return 1
    finally:
        if zstd.stdout is not None:
            zstd.stdout.close()
        zstd.wait()
    return zstd.returncode


# --- what an archive may ask for ------------------------------------------------
# The policy tarfile's "tar" filter states, applied to the members of the three
# archives no filter reads: nothing absolute, nothing reaching through "..", no
# link that points out of the destination, and no device, fifo or socket.

def unsafe_member(name: str, kind: str = "file", target: str = "") -> str:
    """Why this member may not be unpacked, or ``""``.

    ``kind`` is one of ``file``, ``dir``, ``link`` and ``special``; ``target`` is
    where a link points. A link is judged where it would LAND: "cover.jpg" beside
    the link is inside the tree and allowed (and removed afterwards, like every
    other link), "../../etc/passwd" is the escape this exists for.
    """
    spelling = name.replace("\\", "/")
    if not spelling.strip("/") or spelling.strip("/") == ".":
        return ""
    if spelling.startswith("/"):
        return "an absolute path"
    if _drive_relative(spelling):
        return "a drive-relative path"
    if ".." in spelling.split("/"):
        return "a path reaching through .."
    if kind == "special":
        return "a device, fifo or socket"
    if kind != "link":
        return ""
    if not target:
        return "a link whose target this cannot read"
    landing_target = target.replace("\\", "/")
    if landing_target.startswith("/") or _drive_relative(landing_target):
        return "a link to an absolute path"
    # Where the link lands, resolved against the folder it sits in. Lexical on
    # purpose: nothing is on disk yet, so there is nothing to ask the filesystem.
    landing = os.path.normpath(os.path.join(_dirname(spelling),
                                            target.replace("\\", "/")))
    if landing == ".." or landing.startswith("../"):
        return "a link out of the archive's own tree"
    return ""


def _drive_relative(spelling: str) -> bool:
    """``C:`` and what follows it. A letter, a colon and then a separator or the
    end of the name - so a member simply called "a:b.txt", which is a legal name
    where these archives are unpacked, is not read as a drive."""
    return (len(spelling) > 1 and spelling[0].isascii()
            and spelling[0].isalpha() and spelling[1] == ":"
            and spelling[2:3] in ("", "/"))


def _zip_members(file: str) -> list:
    """Every member of a zip as (name, kind, target), read by zipfile.

    zipfile rather than ``unzip -l``: the information wanted is the member's unix
    MODE, which the listing does not print and the central directory carries -
    and a name with a newline in it cannot be misread out of a structure.
    """
    import stat

    members = []
    with zipfile.ZipFile(file) as archive:
        for info in archive.infolist():
            # The type bits of the stored mode, which are zero on an archive
            # packed by a DOS-minded writer - that is "not stated" and not "not
            # a file", so it reads as an ordinary member.
            shape = stat.S_IFMT(info.external_attr >> 16)
            kind = "file"
            target = ""
            if info.is_dir():
                kind = "dir"
            elif shape == stat.S_IFLNK:
                kind = "link"
                # The link's target IS its content, and a path is short: a
                # member claiming otherwise is not a link this will unpack.
                if info.file_size <= 4096:
                    target = archive.read(info).decode("utf-8", "replace")
            elif shape not in (0, stat.S_IFREG, stat.S_IFDIR):
                kind = "special"
            members.append((info.filename, kind, target))
    return members


def _rar_members(file: str) -> list:
    """Every member of a rar as (name, kind, target), from ``unrar vt``.

    The technical listing is the only one that names a member's TYPE, which is
    the whole question here. A target is never printed, so a link is a link this
    cannot check - and is refused on that ground.
    """
    text = _capture(["unrar", "vt", "-idq", "--", file])
    members = []
    name = ""
    for line in text.split("\n"):
        stripped = line.strip()
        field, _sep, value = stripped.partition(":")
        field = field.strip().lower()
        value = value.strip()
        if field == "name":
            name = value
            continue
        if field != "type" or not name:
            continue
        lowered = value.lower()
        if "link" in lowered:
            kind = "link"
        elif lowered.startswith("director"):
            kind = "dir"
        elif lowered == "file":
            kind = "file"
        else:
            kind = "special"
        members.append((name, kind, ""))
        name = ""
    return members


def _seven_zip_members(file: str, binary: str) -> list:
    """Every member of a 7z as (name, kind, target), from ``l -slt``.

    The one field that answers the question is ``Attributes``, whose unix half is
    the mode as ``ls`` spells it - ``lrwxrwxrwx`` for a link, ``drwxr-xr-x`` for
    a folder. An archive packed on Windows has no unix half, and there ``D`` is
    the only type there is to read.
    """
    text = _capture([binary, "l", "-slt", "--", file])
    members: list = []
    name = ""
    kind = "file"
    # The row of dashes is where the archive's own header block ends and the
    # members begin. Both blocks carry a "Path", so reading before it would take
    # the archive itself for its first member.
    started = False
    for line in text.split("\n") + [""]:
        stripped = line.strip()
        # Ten of them, and never the two-dash rule 7-Zip prints above the
        # archive's own block - which is inside the header this is skipping.
        if len(stripped) >= 5 and stripped == "-" * len(stripped):
            started = True
            members = []
            name, kind = "", "file"
            continue
        if not started:
            continue
        if not stripped:
            if name:
                members.append((name, kind, ""))
            name, kind = "", "file"
            continue
        field, sep, value = stripped.partition(" = ")
        if not sep:
            continue
        field = field.strip().lower()
        value = value.strip()
        if field == "path":
            name = value
        elif field == "folder" and value == "+":
            kind = "dir"
        elif field == "symbolic link" or field == "link":
            kind = "link"
        elif field == "attributes":
            kind = _seven_zip_kind(value, kind)
    return members


def _seven_zip_kind(attributes: str, default: str) -> str:
    """The member type in one ``Attributes`` line: the unix mode when the
    archive carries one, and the DOS ``D`` flag when it does not."""
    tokens = attributes.split()
    for token in tokens:
        if len(token) >= 10 and token[0] in "-dlpcbs":
            return {"d": "dir", "l": "link", "-": "file"}.get(token[0],
                                                              "special")
    if tokens and "D" in tokens[0]:
        return "dir"
    return default


def archive_members(file: str, ext: str = "") -> list | None:
    """Every member of an archive as (name, kind, target), or ``None`` when this
    host's tools cannot list it.

    ``None`` is not "empty": an archive whose members cannot be read is one whose
    contents cannot be judged, and the caller refuses it rather than unpacking it
    unseen.
    """
    ext = ext or archive_extension_of(file)
    try:
        if ext == "zip":
            return _zip_members(file)
    except (OSError, zipfile.BadZipFile, RuntimeError):
        return None
    if ext == "rar":
        return _rar_members(file) or None
    if ext == "7z":
        binary = seven_zip_command()
        return (_seven_zip_members(file, binary) or None) if binary else None
    return []


def unsafe_members(file: str, ext: str = "") -> list:
    """The members of ``file`` that may not be unpacked, as (name, reason).

    A listing that could not be made is itself one entry, because it is the same
    answer: this archive is not going to be unpacked.
    """
    members = archive_members(file, ext)
    if members is None:
        return [(os.path.basename(file),
                 "its contents could not be listed, so they cannot be checked")]
    found = []
    for name, kind, target in members:
        reason = unsafe_member(name, kind, target)
        if reason:
            found.append((name, reason))
    return found


# --- the name a folder-shaped archive carries ------------------------------------
# Packing a folder stores the folder itself, and the name it was packed under is
# the one the person who packed it chose - which the file it arrived in may well
# have lost on the way here.


def _member_names(file: str, ext: str) -> list | None:
    """Every member of an archive as (name, kind), or ``None`` when this host's
    tools cannot list it.

    The tar family is read HERE rather than through ``archive_members``, which
    answers ``[]`` for it on purpose: what a tar may write is the extraction
    filter's business, and this is a question about names. A compressed tar has
    no index, so answering it reads the archive through once.
    """
    if ext in ("zip", "rar", "7z"):
        members = archive_members(file, ext)
        return None if members is None else [(name, kind)
                                             for name, kind, _target in members]
    if not ext:
        return None
    return _tar_names(file, ext)


def _tar_names(file: str, ext: str) -> list | None:
    try:
        with tarfile.open(file, "r:*") as archive:
            return [(info.name, "dir" if info.isdir() else "file")
                    for info in archive]
    except tarfile.ReadError:
        if ext in _ZSTD_TAR_EXTENSIONS:
            return _zstd_tar_names(file)
        return None
    except (OSError, tarfile.TarError):
        return None


def _zstd_tar_names(file: str) -> list | None:
    """A .tar.zst on an interpreter whose tarfile cannot open one - the host's
    zstd decompresses and tarfile still reads."""
    try:
        zstd = subprocess.Popen(["zstd", "-dc", "--", file],
                                stdout=subprocess.PIPE)
    except OSError:
        return None
    try:
        with tarfile.open(fileobj=zstd.stdout, mode="r|") as archive:
            return [(info.name, "dir" if info.isdir() else "file")
                    for info in archive]
    except (OSError, tarfile.TarError):
        return None
    finally:
        if zstd.stdout is not None:
            zstd.stdout.close()
        zstd.wait()


def archive_root_folder(file: str, ext: str = "") -> str:
    """The name of the ONE folder an archive holds at its root, or ``""``.

    ``Some Book.zip`` whose only root entry is ``Read by Someone/`` answers
    ``Read by Someone``: that is the name the book was packed under, and a file
    renamed - or numbered, or truncated - on the way here has lost it. An
    archive holding several entries at its root, or holding files there, or one
    whose members cannot be listed, has no such name to give and answers ``""``,
    so the caller falls back to the archive's own name.
    """
    entries = _member_names(file, ext or archive_extension_of(file))
    if entries is None:
        return ""
    root = ""
    holds_a_folder = False
    for name, kind in entries:
        spelling = name.replace("\\", "/")
        # The path policy the members themselves go through. A name spelled
        # absolutely or through ".." does not say where the archive lands - it
        # is refused outright, or rewritten by the tar filter - so an archive
        # carrying one has no root to name a folder after.
        if unsafe_member(spelling):
            return ""
        # "./Book/01.mp3" is how tar spells what zip spells "Book/01.mp3", and
        # the depth is what says the root is a folder rather than a file: a
        # member lying under it, or a member that IS one.
        parts = [part for part in spelling.split("/") if part not in ("", ".")]
        if not parts:
            continue
        if root and parts[0] != root:
            return ""
        root = parts[0]
        if len(parts) > 1 or kind == "dir":
            holds_a_folder = True
    return root if holds_a_folder else ""


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
    destination is not a folder, the 7-Zip binary is missing, the archive asks
    for something it may not have, or the unpacking failed.

    The members are checked BEFORE the unpacker is started, because afterwards
    is too late: what an escaping member does, it has already done. An archive
    with one is refused whole and nothing of it is written - the alternative,
    unpacking the rest, means deciding which half of a book is the book.

    What lands is then pruned to files and folders. A dropped link is not made
    into a failure: an archive is otherwise perfectly good content, and the
    thing worth having is a tree with no way out of it, which the removal
    already gives.
    """
    ext = archive_extension_of(file)
    if not ext or not os.path.isdir(dest):
        return 1
    refused = unsafe_members(file, ext)
    if refused:
        sys.stderr.write('\nRefusing to unpack "%s": an archive may not write '
                         "outside the folder it is unpacked into.\n" % file)
        for name, reason in refused[:10]:
            sys.stderr.write("  %s: %s\n" % (name, reason))
        if len(refused) > 10:
            sys.stderr.write("  ... and %d more\n" % (len(refused) - 10))
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