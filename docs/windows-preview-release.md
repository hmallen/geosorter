# Windows first-run preview

## User workflow

Install the Windows x64 preview or extract the entire ZIP, then open GeoSorter.
Choose an incoming media folder and a separate empty organized destination. Create
a new destination explicitly if needed. Prepare city data, then select Review
inbox. GeoSorter does not import or move media during setup.

Existing installations can select their original GeoSorter configuration and
catalog. A populated media folder without a catalog cannot be adopted. Missing
drives and missing catalogs require recovery, never silent catalog replacement.

Settings & Help lets users change their inbox while idle, add optional tools,
prepare detailed place data, retry readiness checks, and export allowlisted
diagnostics without secrets or media. Library relocation is not implemented.

The app stays running when the browser closes. Quit waits for active jobs; no
media operation is forcibly terminated. Relaunching opens the existing instance.
Application updates are installed manually from the releases page.

## Developer commands

```powershell
uv sync --group release
npm --prefix frontend ci
npm --prefix frontend run build
uv run geosorter desktop
uv run geosorter desktop --config C:\path\to\geosorter.toml
uv run pytest -q
npm --prefix frontend run test
npm --prefix frontend run lint
uv run --group release python packaging/build_release.py
```

Build on Windows x64 with standard CPython 3.13 (Conda-based environments are
rejected because their DLL layout differs). To leave a developer environment intact:

```powershell
$env:UV_PROJECT_ENVIRONMENT = 'output/release-build/venv'
uv sync --group release --python 3.13
uv run --group release python packaging/build_release.py
```

The builder verifies pinned artifact hashes, includes complete
ExifTool support files, freezes the app, tests bundled tools with a restricted PATH,
and generates the ZIP, Inno Setup installer, release manifest, and SHA256SUMS.txt
under `output/releases`. Inno Setup is installed in the build output directory if
not provided through `--iscc`. Build tools are not included in the application.

Unchanged tools are reused only when every file matches the pinned archive. Use a
fresh `output/release-build/tools` directory when changing dependencies; the builder
refuses to combine old and new tool trees. Retain the downloads cache.
Do not copy development configuration, catalogs, or real media into a bundle.

The Windows workflow prepares the pinned ExifTool and FFmpeg distributions before
backend tests, adds their folders to the job's PATH, and checks that each tool
executes. Python dependencies alone do not install these executables. The later
bundle build reuses the same verified files. To prepare them locally without a full
build, run `uv run --group release python packaging/build_release.py --prepare-tools-only`.

Desktop launch uses an explicit configuration, then the saved desktop selection,
then the platform user configuration directory. It ignores shell/cwd config
discovery. Ordinary CLI commands retain their existing precedence. Both installer
and ZIP use the same per-user instance and data. Desktop state, logs, setup job
recovery, and upgrade backups live under the user data directory's `desktop` folder.

## Architecture and access

The desktop-only application wraps the existing library server. It does not
construct that server until configuration, catalog, tools, and city data are ready.
Configuration changes rebuild library resources only when all workers are idle.
Setup jobs are separate from the library JobManager and persist their phase.
GeoNames is indexed and validated in a staging database before replacement.

The launcher binds loopback, reserves its listening socket, uses a Windows mutex,
and authenticates instance activation. A launch URL fragment is exchanged for an
HttpOnly same-site session cookie and removed before mounting the library UI.
Host and Origin checks protect `/api/desktop` and the wrapped library APIs.
Administrator passwords still gate settings and media management. Ordinary
`geosorter serve` does not expose native dialogs, configuration writes, or shutdown.

Before an upgrade migration, the desktop creates a consistent catalog backup and
copies configuration. Newer schemas are refused. Startup failures retain the old
catalog; restoration is manual using the saved backup after closing GeoSorter.

## Release acceptance record

CI creates **draft candidates**, not approved public releases. Fill in evidence for
each row against the exact SHA-256 of the candidate being distributed.
The [2026-09-09 validation record](windows-preview-validation.md) records local
results and the remaining acceptance work for the current candidate.

| Scenario | Required evidence | Initial status |
| --- | --- | --- |
| Developer tests | Backend, frontend, lint, build results | PASS; one existing lint warning |
| Bundled dependency smoke | Restricted-PATH executable output | PASS on developer host |
| Clean Windows 11 standard user | No Python, Node, ExifTool, FFmpeg; install/setup/import/play/quit/reopen | NOT RUN |
| ZIP on clean Windows 11 | Extract entire folder; same workflow and data retention | NOT RUN |
| Existing-version upgrade | Preserve config, catalog, markers, media; test backup/migration failures | NOT RUN |
| Installer with app running | Refuse binary replacement; let current work finish | PASS on developer host |
| Uninstall/reinstall | Preserve user data and library | PASS on developer host; clean-machine repeat required |
| Native dialogs / tray | Browse cancellation, Unicode, mapped drives, relaunch and quit | PARTIAL; packaged startup/relaunch/quit passed; manual dialogs/tray pending |
| Playback | H.264, HEVC proxy without NVIDIA, seek, fullscreen, maps offline | PARTIAL; synthetic photo and offline maps passed; packaged video matrix pending |
| Corresponding-source distribution | Exact FFmpeg/library sources, patches, build scripts, notices available alongside candidate | PENDING |

The third-party notice and FFmpeg source revision are not substitutes for complete
corresponding source for all statically linked GPL components. Obtain and validate
those upstream build materials before distributing an FFmpeg-containing candidate.
Do not mark this row passed based only on an upstream homepage link.

The first release is unsigned for the agreed small tester group. Public code
signing, automatic updates, and arbitrary existing-library indexing are deferred.
