"""geosorter command-line interface (Phase 0a foundations).

Verbs implemented in this task (B1):

* ``init-config`` — write a starter ``geosorter.toml``.
* ``bootstrap``   — load GeoNames reference data into the geonames DB.
* ``version``     — print the package version.

Later tasks add ``organize`` / ``verify-library`` etc.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import click

from . import __version__, config, db, geocoder, geonames_loader
from .metadata import ExifToolVersionError, MetadataExtractor
from .organize import BatchReport, run_organize
from .organize import verify_library as _verify_library

_CONFIG_OPTION = click.option(
    "--config",
    "config_path",
    type=click.Path(dir_okay=False),
    default=None,
    help="Path to geosorter.toml (overrides $GEOSORTER_CONFIG and the default).",
)


@click.group()
@click.version_option(__version__, prog_name="geosorter")
def cli() -> None:
    """Organize DJI drone media by location and date."""


@cli.command()
@_CONFIG_OPTION
@click.option("--force", is_flag=True, help="Overwrite an existing config file.")
def init_config(config_path: str | None, force: bool) -> None:
    """Write a starter geosorter.toml."""
    try:
        written = config.write_starter(config_path, overwrite=force)
    except FileExistsError as exc:
        raise click.ClickException(
            f"{exc} already exists; pass --force to overwrite."
        ) from exc
    click.echo(f"Wrote starter config to {written}")


@cli.command()
@_CONFIG_OPTION
@click.option(
    "--from",
    "from_dir",
    type=click.Path(exists=True, file_okay=False),
    default=None,
    help="Load from already-downloaded GeoNames files in this directory.",
)
@click.option(
    "--no-download",
    is_flag=True,
    help="Do not fetch from geonames.org; requires --from.",
)
@click.option(
    "--features",
    is_flag=True,
    help="Also load parks/peaks/hydro (L/T/H) features from allCountries "
    "(a much larger ~400 MB download).",
)
def bootstrap(
    config_path: str | None, from_dir: str | None, no_download: bool, features: bool
) -> None:
    """Load GeoNames cities + admin/country data into the geonames database.

    With ``--features`` it additionally loads curated parks/peaks/hydro features
    so wilderness captures fold under a named feature instead of a distant town.
    """
    cfg = config.load(config_path)

    # Decide the spatial-index mode: honour config, but fall back to columnar
    # if the SQLite R-tree module is unavailable on this platform.
    effective = cfg.spatial_index
    if effective == "rtree":
        probe = db.connect(cfg.geonames_db_path, integrity_check=False)
        try:
            if not db.probe_rtree(probe):
                effective = "columnar"
                click.echo("R-tree module unavailable; using columnar index.")
        finally:
            probe.close()

    # Resolve the source directory.
    if from_dir:
        src = Path(from_dir)
    elif no_download:
        raise click.UsageError("--no-download requires --from <dir>.")
    else:
        cache = config.default_data_dir() / "geonames-src"
        click.echo("Downloading GeoNames data from geonames.org ...")

        def _progress(name: str, done: int, total: int) -> None:
            pct = f"{done * 100 // total}%" if total else f"{done} bytes"
            click.echo(f"  {name}: {pct}", nl=False)
            click.echo("\r", nl=False)

        src = geonames_loader.download(cache, progress=_progress, features=features)
        click.echo("")

    counts = geonames_loader.load(
        cfg.geonames_db_path, src, spatial_index=effective, features=features
    )
    config.update_spatial_index(config_path, effective)

    feature_note = f", {counts['features']} features" if "features" in counts else ""
    click.echo(
        "Bootstrap complete: "
        f"{counts['geonames']} places, "
        f"{counts['admin1']} admin1, "
        f"{counts['admin2']} admin2, "
        f"{counts['countries']} countries"
        f"{feature_note} "
        f"({effective} index) -> {cfg.geonames_db_path}"
    )


@cli.command()
@click.argument(
    "path", type=click.Path(exists=True, dir_okay=False, path_type=Path)
)
def extract_test(path: Path) -> None:
    """Print extracted metadata for a single media file as JSON (debug)."""
    try:
        with MetadataExtractor() as extractor:
            metadata = extractor.extract(path)
    except ExifToolVersionError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(asdict(metadata), indent=2))


@cli.command(name="geocode-test")
@_CONFIG_OPTION
@click.argument("lat", type=float)
@click.argument("lon", type=float)
def geocode_test(config_path: str | None, lat: float, lon: float) -> None:
    """Print the geocode candidates and the chosen place for one coordinate.

    A negative longitude is parsed as an option unless you separate it with ``--``:
    ``geosorter geocode-test -- 40.4 -105.6``.
    """
    cfg = config.load(config_path)
    if not Path(cfg.geonames_db_path).exists():
        raise click.ClickException(
            f"no geonames database at {cfg.geonames_db_path}; run `geosorter bootstrap` first."
        )
    conn = db.connect(cfg.geonames_db_path, integrity_check=False)
    try:
        cands = geocoder.candidates(conn, lat, lon)
        chosen = geocoder.reverse_geocode(
            conn, lat, lon, feature_proximity_km=cfg.feature_proximity_km
        )
    finally:
        conn.close()

    click.echo(
        f"Candidates near ({lat}, {lon}) "
        f"[feature-preference radius {cfg.feature_proximity_km} km]:"
    )
    if not cands:
        click.echo("  (none in search window)")
    for c in cands:
        click.echo(
            f"  [{c.feature_class}] {c.ascii_name} (id {c.geonameid}) — {c.dist_km:.2f} km"
        )
    click.echo(
        f"Chosen: {chosen.place_string or '(none)'} "
        f"[{chosen.geocode_confidence}]"
    )


def _render_report(report: BatchReport, dry_run: bool) -> None:
    if not report.confirmed:
        click.echo("Aborted: first-run confirmation declined. Nothing was moved.")
        return
    click.echo("DRY RUN — nothing moved" if dry_run else f"Batch {report.batch_id}")
    click.echo(f"  organized:          {report.organized}")
    for place, n in sorted(report.per_place.items()):
        click.echo(f"      - {place}: {n}")
    click.echo(f"  quarantined:        {report.quarantined}")
    click.echo(f"  companions:         {report.companions}")
    click.echo(f"  duplicates skipped: {report.duplicates_skipped}")
    click.echo(
        f"  codec: h264={report.codec['h264']} "
        f"h265={report.codec['h265']} unknown={report.codec['unknown']}"
    )
    if report.tz_ambiguous:
        click.echo(
            f"  tz-ambiguous (DST fold): {report.tz_ambiguous} "
            "— local wall-clock occurs twice; double-check if exact time matters"
        )
    if not dry_run:
        click.echo(f"  undo log: {report.organized + report.quarantined} move(s) recorded in the index DB")
    if report.aborted:
        click.echo("  ABORTED after a verify/IO failure — remaining sources left untouched:")
        for failure in report.failures:
            click.echo(f"    ! {failure}")
        raise click.ClickException("organize aborted; see the failures above.")


@cli.command()
@_CONFIG_OPTION
@click.option("--dry-run", is_flag=True, help="Preview the plan without moving or deleting anything.")
@click.option("--yes", is_flag=True, help="Skip the first-run confirmation prompt.")
def organize(config_path: str | None, dry_run: bool, yes: bool) -> None:
    """Scan the inbox and file each capture into the library (crash-safe move)."""
    cfg = config.load(config_path)
    try:
        report = run_organize(
            cfg,
            dry_run=dry_run,
            assume_yes=yes,
            confirm=lambda preview: click.confirm(preview + "\nProceed?", default=False),
            progress=lambda msg: click.echo(msg),
        )
    except (ValueError, OSError, ExifToolVersionError) as exc:
        raise click.ClickException(str(exc)) from exc
    _render_report(report, dry_run)


@cli.command(name="verify-library")
@_CONFIG_OPTION
def verify_library(config_path: str | None) -> None:
    """Recompute library hashes to detect post-move bit-rot."""
    cfg = config.load(config_path)
    report = _verify_library(cfg)
    click.echo(f"verify-library: checked {report.checked}, ok {report.ok}")
    for missing in report.missing:
        click.echo(f"  MISSING: {missing}")
    for mismatch in report.mismatched:
        click.echo(f"  MISMATCH: {mismatch}")
    if report.missing or report.mismatched:
        raise click.ClickException(
            f"{len(report.missing)} missing, {len(report.mismatched)} mismatched"
        )


@cli.command()
def version() -> None:
    """Print the geosorter version."""
    click.echo(__version__)


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
