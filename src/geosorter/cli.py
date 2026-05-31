"""geosorter command-line interface (Phase 0a foundations).

Verbs implemented in this task (B1):

* ``init-config`` — write a starter ``geosorter.toml``.
* ``bootstrap``   — load GeoNames reference data into the geonames DB.
* ``version``     — print the package version.

Later tasks add ``organize`` / ``verify-library`` etc.
"""

from __future__ import annotations

from pathlib import Path

import click

from . import __version__, config, db, geonames_loader

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
def bootstrap(config_path: str | None, from_dir: str | None, no_download: bool) -> None:
    """Load GeoNames cities + admin/country data into the geonames database."""
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

        src = geonames_loader.download(cache, progress=_progress)
        click.echo("")

    counts = geonames_loader.load(
        cfg.geonames_db_path, src, spatial_index=effective
    )
    config.update_spatial_index(config_path, effective)

    click.echo(
        "Bootstrap complete: "
        f"{counts['geonames']} places, "
        f"{counts['admin1']} admin1, "
        f"{counts['admin2']} admin2, "
        f"{counts['countries']} countries "
        f"({effective} index) -> {cfg.geonames_db_path}"
    )


@cli.command()
def version() -> None:
    """Print the geosorter version."""
    click.echo(__version__)


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
