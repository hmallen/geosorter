"""Fail-safe reader for DJI ``MISC/*.db`` catalog star ratings (B11).

A DJI card writes a ``MISC/`` sibling to ``DCIM/`` holding SQLite catalog DBs
(e.g. ``FC8482.db``, ``dji_finfo.db``). The user's in-app **star ratings** live
ONLY there — the media files carry no ``XMP:Rating`` — in ``gis_info_table`` as a
scalar ``star`` column keyed by ``file_name`` (a full SD-card POSIX path).

This module reads those ratings **read-only and fully fail-safe**: it opens the DB
immutable, runs an integrity check, reads only the two scalar columns, and never
touches the ``image_info_table`` EXIF BLOB. ANY error (corrupt DB, missing table,
locked file, malformed row) yields ``{}`` — the catalog step must never abort the
crash-safe move pipeline. No DB writes, ever.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path


def read_ratings(db_path: str | Path) -> dict[str, int]:
    """Return ``{basename_lower: star}`` from a DJI catalog DB, or ``{}`` on any error.

    Opens ``db_path`` read-only + immutable, verifies ``PRAGMA integrity_check``,
    and reads only ``gis_info_table.file_name``/``star`` (never the EXIF BLOB in
    ``image_info_table``). ``file_name`` is a full SD-card path, so only its
    lowercased basename is kept. Rows with a blank ``file_name`` or ``NULL`` star
    are skipped. The whole body is guarded so a bad catalog can never raise.
    """
    conn = None
    try:
        uri = f"file:{Path(db_path).as_posix()}?mode=ro&immutable=1"
        conn = sqlite3.connect(uri, uri=True)
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            return {}
        rows = conn.execute("SELECT file_name, star FROM gis_info_table").fetchall()
        ratings: dict[str, int] = {}
        for file_name, star in rows:
            if not file_name or star is None:
                continue
            ratings[os.path.basename(file_name).lower()] = int(star)
        return ratings
    except Exception:
        return {}
    finally:
        if conn is not None:
            conn.close()


def select_catalog(
    catalogs: dict[Path, dict[str, int]], organized: list[str]
) -> Path | None:
    """Pick the live catalog by basename overlap, or ``None`` if undecidable.

    ``catalogs`` maps each candidate DB path to its ``read_ratings`` result;
    ``organized`` is the list of lowercased *dest* filenames just filed this run.
    Organized files are renamed ``<date>_<time>_<orig>.<ext>``, so a catalog
    basename matches when it is a suffix of a dest filename. The unique catalog
    with the most matches (and at least one) wins; a tie for the top score or an
    all-zero field returns ``None`` so a stale/ambiguous catalog never
    mis-attributes ratings.
    """
    def score(ratings: dict[str, int]) -> int:
        return sum(1 for cb in ratings if any(dest.endswith(cb) for dest in organized))

    scored = [(path, score(ratings)) for path, ratings in catalogs.items()]
    best = max((s for _p, s in scored), default=0)
    if best == 0:
        return None
    winners = [p for p, s in scored if s == best]
    return winners[0] if len(winners) == 1 else None
