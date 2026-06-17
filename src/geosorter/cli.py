"""geosorter command-line interface (Phase 0a foundations).

Verbs implemented in this task (B1):

* ``init-config`` — write a starter ``geosorter.toml``.
* ``bootstrap``   — load GeoNames reference data into the geonames DB.
* ``version``     — print the package version.

Later tasks add ``organize`` / ``verify-library`` etc.
"""

from __future__ import annotations

import json
import tempfile
import time
from dataclasses import asdict, replace
from pathlib import Path

import click
import uvicorn

from . import __version__, api, auth, config, db, derived, geocoder, geonames_loader, pathing
from .metadata import ExifToolVersionError, MetadataExtractor
from .organize import BatchReport, run_organize
from .organize import verify_library as _verify_library
from .recover import RecoveryReport, run_recovery
from .rescan import RescanReport, run_rescan
from .restitch import RestitchReport, run_restitch
from .undo import UndoReport, latest_batch_id, run_undo
from .warm import WarmResult, warm_library

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


@cli.command(name="set-admin-password")
@_CONFIG_OPTION
def set_admin_password(config_path: str | None) -> None:
    """Set the admin password that gates the map UI's management actions.

    Prompts (hidden, confirmed), hashes the password, and writes
    ``admin_password_hash`` into the existing config file. Once set, ``geosorter
    serve`` is view-only until login and the mutating API routes require an admin
    token. Re-run to change it; clear the key by hand to disable auth.
    """
    cfg_path = config.resolve_config_path(config_path)
    if not cfg_path.exists():
        raise click.ClickException(
            f"No config file at {cfg_path}; run `geosorter init-config` first."
        )
    password = click.prompt(
        "Admin password", hide_input=True, confirmation_prompt=True
    )
    if not password:
        raise click.ClickException("password must not be empty.")
    config.set_admin_password_hash(cfg_path, auth.hash_password(password))
    click.echo(f"Admin password set in {cfg_path}.")


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
    if report.inferred:
        click.echo(f"  inferred location:  {report.inferred} (GPS borrowed from a time-adjacent capture)")
    click.echo(f"  quarantined:        {report.quarantined}")
    click.echo(f"  companions:         {report.companions}")
    if report.retained_frame_bytes:
        mib = report.retained_frame_bytes / (1024 * 1024)
        click.echo(f"  hyperlapse frames:  {mib:.1f} MiB retained (set retain_hyperlapse_frames=false to skip)")
    if report.ratings_applied:
        click.echo(f"  star ratings:       {report.ratings_applied} applied from a MISC catalog")
    click.echo(f"  duplicates skipped: {report.duplicates_skipped}")
    if report.unclaimed:
        click.echo(f"  unclaimed:          {report.unclaimed} file(s) in PANORAMA/MISC (not yet handled) — left in the inbox")
    for warning in report.warnings:
        click.echo(f"  ! {warning}")
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
@_CONFIG_OPTION
@click.option("--batch", "batch_id", default=None, help="Batch id to undo (default: the most recent).")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
def undo(config_path: str | None, batch_id: str | None, yes: bool) -> None:
    """Reverse an organize batch — move its files back to the inbox (most recent by default)."""
    cfg = config.load(config_path)
    conn = db.connect(cfg.index_db_path, integrity_check=False)
    db.init_index_schema(conn)
    try:
        target = batch_id or latest_batch_id(conn)
        if target is None:
            click.echo("Nothing to undo: the move log is empty.")
            return
        n = conn.execute(
            "SELECT COUNT(*) FROM moves WHERE batch_id=?", (target,)
        ).fetchone()[0]
    finally:
        conn.close()

    if not yes and not click.confirm(
        f"Undo batch {target} ({n} file move(s))? Files move back to the inbox.",
        default=False,
    ):
        click.echo("Aborted. Nothing was moved.")
        return

    try:
        report = run_undo(cfg, batch_id=target, progress=lambda msg: click.echo(msg))
    except (ValueError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
    _render_undo_report(report)


def _render_undo_report(report: UndoReport) -> None:
    if report.nothing_to_undo:
        click.echo("Nothing to undo: the move log is empty.")
        return
    click.echo(f"Undo batch {report.batch_id}")
    click.echo(f"  restored:  {report.restored}")
    if report.missing:
        click.echo(f"  missing:   {report.missing} (library copy gone — could not restore)")
    if report.conflicts:
        click.echo(
            f"  conflicts: {len(report.conflicts)} "
            "(inbox path occupied by different content — skipped):"
        )
        for c in report.conflicts:
            click.echo(f"    ! {c}")
    if report.failures:
        click.echo(
            f"  failures:  {len(report.failures)} "
            "(reverse-copy verify failed — left in the library):"
        )
        for failure in report.failures:
            click.echo(f"    ! {failure}")
    if report.conflicts or report.failures:
        raise click.ClickException(
            f"{len(report.conflicts)} conflict(s), {len(report.failures)} failure(s); "
            "some files were not restored."
        )


@cli.command()
@_CONFIG_OPTION
@click.option("--dry-run", is_flag=True, help="Preview what would be pruned; write nothing.")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
def rescan(config_path: str | None, dry_run: bool, yes: bool) -> None:
    """Reconcile the index with disk — prune rows for files no longer in the library.

    Detects captures that left ``library_root`` (e.g. moved back to the inbox by
    hand) and removes their stale index rows so the map stops showing them. Mutates
    the index DB only — it never deletes or moves a file on disk.
    """
    cfg = config.load(config_path)
    if not dry_run and not yes and not click.confirm(
        "Rescan the library and prune index rows for files no longer on disk?",
        default=False,
    ):
        click.echo("Aborted. Nothing was pruned.")
        return
    try:
        report = run_rescan(cfg, dry_run=dry_run, progress=lambda msg: click.echo(msg))
    except (ValueError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
    _render_rescan_report(report)


def _render_rescan_report(report: RescanReport) -> None:
    verb = "would prune" if report.dry_run else "pruned"
    click.echo("Rescan complete." + (" (dry run — no changes written)" if report.dry_run else ""))
    click.echo(f"  checked: {report.checked}")
    click.echo(f"  kept:    {report.kept}")
    click.echo(f"  {verb}: {report.pruned}")
    if report.warnings:
        click.echo(
            f"  warnings: {len(report.warnings)} "
            "(primary present, companion missing on disk — left in place):"
        )
        for w in report.warnings:
            click.echo(f"    ! {w}")
    if report.orphaned:
        click.echo(
            f"  orphaned: {len(report.orphaned)} "
            "(companion still on disk after its capture was pruned — left in place):"
        )
        for o in report.orphaned:
            click.echo(f"    ! {o}")


@cli.command(name="recover-collisions")
@_CONFIG_OPTION
@click.option("--dry-run", is_flag=True, help="Preview the recovery; change nothing.")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
def recover_collisions(config_path: str | None, dry_run: bool, yes: bool) -> None:
    """Recover files damaged by the recycled-DJI-filename destination collision.

    For each destination two unrelated files were filed onto, the surviving (mislabeled)
    file is re-filed to its correct home from its OWN metadata via the fixed pipeline
    (a GPS-less survivor lands in ``_no-gps/<capture-date>/``), the wrong index rows are
    dropped, and the captures whose bytes were destroyed are listed in a recovery report
    next to the index DB. Touches the index DB + the surviving library files only.
    """
    cfg = config.load(config_path)
    if not dry_run and not yes and not click.confirm(
        "Recover collision survivors: re-file the mislabeled files and drop their wrong "
        "index rows?",
        default=False,
    ):
        click.echo("Aborted. Nothing was recovered.")
        return
    try:
        report = run_recovery(cfg, dry_run=dry_run, progress=lambda msg: click.echo(f"  {msg}"))
    except (ValueError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
    _render_recovery_report(report)


def _render_recovery_report(report: RecoveryReport) -> None:
    suffix = " (dry run — no changes written)" if report.dry_run else ""
    click.echo("Recovery complete." + suffix)
    click.echo(f"  collisions found:    {report.collisions_found}")
    verb = "would recover" if report.dry_run else "recovered"
    click.echo(f"  {verb}: {report.recovered}")
    if report.lost:
        click.echo(
            f"  unrecoverable (bytes destroyed by the collision): {len(report.lost)}"
        )
        for lc in report.lost:
            click.echo(f"    - {lc.local_date}  {lc.place_string}")
    if report.failures:
        click.echo(f"  failures: {len(report.failures)}")
        for f in report.failures:
            click.echo(f"    ! {f}")
    if report.report_path:
        click.echo(f"  report written: {report.report_path}")


@cli.command()
@_CONFIG_OPTION
@click.option("--all", "force_all", is_flag=True,
              help="Re-stitch every 'ok' panorama, not just those missing a recorded projection.")
@click.option("--dry-run", is_flag=True,
              help="List the panoramas that would be re-stitched; change nothing.")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
def restitch(config_path: str | None, force_all: bool, dry_run: bool, yes: bool) -> None:
    """Re-stitch panorama heroes baked in the wrong projection (pre-auto-detect).

    Older panoramas were stitched before projection auto-detection and forced into an
    equirectangular canvas; this re-runs the Hugin pipeline so each gets its correct
    projection. By default it targets only panoramas with no recorded projection
    (``--all`` re-stitches every successfully-stitched panorama). Re-runs Hugin per
    panorama — minutes each. Writes only the index DB's stitch columns and the
    regenerable cached hero; it never touches a media file.
    """
    cfg = config.load(config_path)
    if not dry_run and not yes and not click.confirm(
        "Re-stitch the selected panoramas (re-runs Hugin per panorama — minutes each)?",
        default=False,
    ):
        click.echo("Aborted. Nothing was re-stitched.")
        return
    try:
        report = run_restitch(
            cfg, force_all=force_all, dry_run=dry_run, progress=lambda msg: click.echo(msg)
        )
    except (ValueError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
    _render_restitch_report(report)


def _render_restitch_report(report: RestitchReport) -> None:
    if report.dry_run:
        click.echo(f"Re-stitch (dry run): {report.targets} panorama(s) would be re-stitched.")
        return
    if report.unavailable:
        click.echo(
            f"Hugin not found — nothing re-stitched ({report.targets} panorama(s) selected). "
            "Install Hugin or set hugin_bin_dir, then re-run."
        )
        return
    click.echo("Re-stitch complete.")
    click.echo(f"  selected:   {report.targets}")
    breakdown = (
        " (" + ", ".join(f"{k}: {v}" for k, v in sorted(report.projections.items())) + ")"
        if report.projections else ""
    )
    click.echo(f"  restitched: {report.restitched}{breakdown}")
    if report.failed:
        click.echo(f"  failed:     {report.failed} (existing hero kept):")
        for err in report.errors:
            click.echo(f"    ! {err}")


@cli.command(name="warm-proxies")
@_CONFIG_OPTION
@click.option("--batch", "batch_id", default=None, help="Warm just this batch id.")
@click.option(
    "--all",
    "all_batches",
    is_flag=True,
    help="Warm every organized file in the library (a full HEVC sweep — slow).",
)
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
def warm_proxies(
    config_path: str | None, batch_id: str | None, all_batches: bool, yes: bool
) -> None:
    """Pre-generate HEVC->H.264 proxies (and thumbs/posters) for organized media.

    Retroactively warms the derived cache for files filed by past ``organize`` runs,
    so HEVC footage plays back instantly instead of transcoding lazily on first play.
    Pass ``--all`` to sweep the whole library or ``--batch ID`` for one batch (exactly
    one is required). Thumbs/posters are skip-if-fresh, so re-warming is cheap. Unlike
    the automatic post-organize pass, this verb ALWAYS transcodes proxies regardless of
    the ``warm_proxies`` config setting; the ``proxy_cache_max_gb`` cap (if set) still
    applies and the panorama ``stitch`` cache is never evicted.
    """
    if all_batches and batch_id:
        raise click.UsageError("--all and --batch are mutually exclusive.")
    if not all_batches and not batch_id:
        raise click.UsageError("pass --all (whole library) or --batch ID.")

    cfg = config.load(config_path)
    if cfg.library_root is None:
        # Warming reads library files and resolves the proxy cache tier from
        # library_root; without it resolve_proxy_cache_dir hits Path(None). Fail
        # cleanly (the library must have been organized at least once first).
        raise click.ClickException("library_root is not set in the config.")
    cfg = replace(cfg, warm_proxies=True)  # the verb's purpose: force proxy warming on
    # Ensure the index schema exists so a pre-`organize` invocation reports "0 warmed"
    # instead of crashing on the absent `files` table (mirrors the `undo` verb).
    conn = db.connect(cfg.index_db_path, integrity_check=False)
    db.init_index_schema(conn)
    conn.close()
    target = "all organized files" if all_batches else f"batch {batch_id}"
    if not yes and not click.confirm(
        f"Warm proxies (and thumbnails/posters) for {target}? "
        "Transcoding HEVC video can take a long time.",
        default=False,
    ):
        click.echo("Aborted. Nothing was warmed.")
        return
    try:
        report = warm_library(cfg, batch_id=None if all_batches else batch_id)
    except (ValueError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
    _render_warm_report(report)


def _render_warm_report(report: WarmResult) -> None:
    scope = "all organized files" if report.batch_id is None else f"batch {report.batch_id}"
    click.echo(f"Warm complete ({scope}).")
    click.echo(f"  warmed:          {report.warmed} thumbnail/poster asset(s)")
    click.echo(f"  proxies warmed:  {report.proxies_warmed}")
    ev = report.eviction
    click.echo(
        f"  local cache:     {ev.deleted} evicted, now "
        f"{ev.bytes_after / (1024 * 1024):.1f} MiB"
    )
    if report.proxy_eviction is not None:
        pev = report.proxy_eviction
        click.echo(
            f"  proxy cache:     {pev.deleted} evicted, now "
            f"{pev.bytes_after / (1024 * 1024):.1f} MiB"
        )


_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def _resolve_host(host: str | None) -> tuple[str, bool]:
    """Resolve the bind address and whether it warrants an exposure warning.

    Default (``None``) binds loopback silently. An explicit non-loopback host
    opts into exposing the library (home GPS, no auth) and warrants a warning.
    """
    if host is None:
        return "127.0.0.1", False
    return host, host not in _LOOPBACK


@cli.command()
@_CONFIG_OPTION
@click.option(
    "--host",
    default=None,
    help="Bind address (default 127.0.0.1; a non-loopback host exposes home GPS).",
)
@click.option("--port", default=8000, show_default=True, type=int, help="Bind port.")
def serve(config_path: str | None, host: str | None, port: int) -> None:
    """Run the map-viewer HTTP server (binds 127.0.0.1 by default)."""
    cfg = config.load(config_path)
    bind, warn = _resolve_host(host)
    if warn:
        # Admin auth only gates the mutating routes; the library GeoJSON (home GPS)
        # stays a public read either way, so warn about GPS exposure regardless and
        # note whether management actions are at least password-gated.
        gate = (
            "Management actions are admin-password-gated, but the library "
            "(home GPS locations) is a public read."
            if cfg.admin_password_hash
            else "The library (home GPS locations) and all management actions are "
            "open with no authentication (set one with `geosorter set-admin-password`)."
        )
        click.echo(
            f"WARNING: binding to {bind} exposes the server. {gate} "
            "Use only on a trusted network."
        )
    click.echo(f"geosorter serving on http://{bind}:{port}")
    uvicorn.run(api.create_app(cfg), host=bind, port=port)


def _strip(dest_path: str) -> str:
    """Drop the Windows ``\\\\?\\`` long-path prefix if present."""
    return dest_path[4:] if dest_path.startswith("\\\\?\\") else dest_path


@cli.command(name="stitch-bench")
@_CONFIG_OPTION
@click.argument("file_id", type=int)
def stitch_bench(config_path: str | None, file_id: int) -> None:
    """Time a panorama stitch at the old 6000x3000 vs new 4000x2000 canvas.

    Runs the REAL Hugin pipeline twice on one organized panorama (cold, into
    throwaway cache dirs so neither hits a stale cache) and prints both wall-times,
    so the canvas-size speedup can be measured on real tiles. Requires Hugin
    installed and the FILE_ID of an organized panorama (see the map UI / index DB).
    """
    cfg = config.load(config_path)
    if derived.find_hugin(cfg.hugin_bin_dir) is None:
        raise click.ClickException("Hugin CLI tools not found on PATH / hugin_bin_dir.")

    conn = db.connect(cfg.index_db_path, integrity_check=False)
    try:
        row = conn.execute(
            "SELECT dest_path, capture_kind FROM files WHERE id=?", (file_id,)
        ).fetchone()
        if row is None or row[1] != "panorama":
            raise click.ClickException(f"file_id {file_id} is not an organized panorama.")
        primary = _strip(row[0])
        rel_key = pathing.library_rel_key(cfg.library_root, row[0])
        frames = [
            _strip(r[0])
            for r in conn.execute(
                "SELECT dest_path FROM file_companions "
                "WHERE primary_file_id=? AND companion_type='panorama_frame' "
                "ORDER BY dest_path",
                (file_id,),
            )
        ]
    finally:
        conn.close()

    click.echo(f"Benchmarking stitch of {Path(primary).name} ({len(frames) + 1} tiles)…")
    results: dict[str, float] = {}
    for label, canvas in (("old 6000x3000", "6000x3000"), ("new 4000x2000", "4000x2000")):
        with tempfile.TemporaryDirectory(prefix="geosorter-bench-") as tmp:
            started = time.perf_counter()
            result = derived.panorama_stitch(
                tmp, rel_key, primary, frames,
                hugin_bin_dir=cfg.hugin_bin_dir, canvas=canvas,
                celeste=cfg.stitch_celeste, optimise_lens=cfg.stitch_optimise_lens,
            )
            elapsed = time.perf_counter() - started
        results[label] = elapsed
        click.echo(f"  {label}: {elapsed:.1f}s (projection: {result.projection})")

    old, new = results["old 6000x3000"], results["new 4000x2000"]
    if new > 0:
        click.echo(f"speedup: {old / new:.2f}x faster ({old - new:.1f}s saved)")


@cli.command()
def version() -> None:
    """Print the geosorter version."""
    click.echo(__version__)


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
