"""The standard sizes a still image is scaled down to.

Cover art, thumbnails and book illustrations all land on one of these, and the
point of the table is that the sizes a caller did NOT pick are visible next to
the one it did. """

__all__ = ["DEFAULT_TIER", "geometry", "height"]

# The floor of the table, and the tier a caller that names none gets: these are
# downscale ceilings for a still image travelling inside every output file, and a
# step up multiplies the bytes for a picture nobody looks at twice.
DEFAULT_TIER = "fullHD"

# tier -> (longer edge, shorter edge), in pixels, smallest first
_TIERS = {
    "fullHD": ("1920", "1080"),
    "quadHD": ("2560", "1440"),
    "ultraHD4K": ("3840", "2160"),
}


def _row(tier: str) -> tuple[str, str] | None:
    """The named tier, or the default one for an empty name; None for a typo.

    A name that is not in the table is an error rather than a fallback, so a
    misspelt selection is caught where it is made instead of silently scaling to
    something nobody chose.
    """
    return _TIERS.get(tier or DEFAULT_TIER)


def geometry(tier: str = "") -> str | None:
    """The tier as an ImageMagick ``WxH`` geometry."""
    row = _row(tier)
    return None if row is None else f"{row[0]}x{row[1]}"


def height(tier: str = "") -> str | None:
    """The tier's shorter edge alone, for callers that cap against one number."""
    row = _row(tier)
    return None if row is None else row[1]
