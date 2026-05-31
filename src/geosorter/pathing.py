r"""Windows-safe destination-path computation.

Builds the library target
``library/<City, Region, Country>/<YYYY-MM-DD>/<YYYY-MM-DD>_<HH-MM-SS>_<DJI_orig>.<ext>``
from a :class:`~geosorter.geocoder.GeocodeResult` and a
:class:`~geosorter.tz_resolver.LocalTime` (the **GPS-derived local** date/time,
never UTC).

``sanitize_component`` makes a single path segment safe for NTFS/Explorer:
strips characters illegal on Windows, NFC-normalizes, removes trailing dots and
spaces, replaces reserved device names (``CON``/``PRN``/``AUX``/``NUL``/
``COM1``-``COM9``/``LPT1``-``LPT9``), and truncates to a length cap. The place
folder is sourced from GeoNames ``ascii_name`` with the ``geonameid`` as the
fallback when the name sanitizes to nothing. Absolute paths get the ``\\?\``
long-path prefix so a deep ``library_root`` survives the legacy 260-char limit.
"""

from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path

from geosorter.geocoder import GeocodeResult
from geosorter.tz_resolver import LocalTime

# Characters illegal in Windows path components, plus C0 control chars.
_ILLEGAL_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

_PLACE_MAX = 40  # ceiling for the city component of the place folder


def sanitize_component(name: str | None, *, max_len: int = 40, fallback: str = "") -> str:
    """Return a single Windows-safe path segment for ``name``.

    NFC-normalizes, strips illegal/control chars and trailing dots/spaces,
    truncates to ``max_len`` (re-stripping any trailing space the cut exposes),
    and escapes reserved device names with a leading underscore. Returns
    ``fallback`` if nothing usable remains.
    """
    if not name:
        return fallback
    s = unicodedata.normalize("NFC", name)
    s = _ILLEGAL_RE.sub("", s)
    s = s.strip().rstrip(". ")
    if len(s) > max_len:
        s = s[:max_len].rstrip(". ")
    if not s:
        return fallback
    if s.split(".", 1)[0].upper() in _RESERVED:
        s = "_" + s
    return s


def _place_folder(geocode: GeocodeResult) -> str:
    """Compose the ``<City, Region, Country>`` folder name (city truncated ≤40)."""
    gid = str(geocode.geonameid) if geocode.geonameid is not None else "_unknown"
    city = sanitize_component(geocode.ascii_name, max_len=_PLACE_MAX, fallback=gid)

    remainder = ""
    place = geocode.place_string
    if place and geocode.ascii_name and place.startswith(geocode.ascii_name + ", "):
        remainder = place[len(geocode.ascii_name) + 2:]
    elif place and not geocode.ascii_name:
        remainder = place
    remainder = sanitize_component(remainder, max_len=120) if remainder else ""

    return f"{city}, {remainder}" if remainder else city


def compute_dest_path(
    library_root: Path,
    geocode: GeocodeResult,
    local: LocalTime,
    dji_orig: str,
    ext: str,
) -> str:
    r"""Compute the absolute, ``\\?\``-prefixed destination path for one capture.

    ``ext`` includes its leading dot (e.g. ``".JPG"``). Raises :class:`ValueError`
    if ``local`` lacks a resolved date/time (such files quarantine upstream).
    """
    if not local.local_date or not local.local_time_hms:
        raise ValueError("compute_dest_path requires a resolved local date/time")

    place_folder = _place_folder(geocode)
    filename = f"{local.local_date}_{local.local_time_hms}_{dji_orig}{ext}"
    joined = os.path.join(str(library_root), place_folder, local.local_date, filename)
    return "\\\\?\\" + os.path.abspath(joined)
