# Windows preview validation — 2026-09-09

Status: **implemented preview candidate; not approved for tester distribution**.
Clean-machine acceptance and complete corresponding-source materials for the
selected FFmpeg build remain required. No GitHub release was published.

## Candidate identity

Version: **0.2.0**. Built on the developer's Windows host with standard CPython
3.13.11, PyInstaller 6.22.0, and Inno Setup 6.7.3. The repository has uncommitted
implementation changes; `release-manifest.json` records this and the build-input
digest, rather than presenting the base Git commit as the complete source.

| Artifact | Bytes | SHA-256 |
| --- | ---: | --- |
| GeoSorter-0.2.0-windows-x64-Setup.exe | 150638954 | `0388fd94dee44883ddff02ced26feb8d806118c5e48677e6184b30f834f07e1b` |
| GeoSorter-0.2.0-windows-x64.zip | 226978901 | `a2aecf24b69e886c9eab9e8b80191f0e9b6007da2032df2c9d4c495ec6023d80` |

Artifacts, manifest, ExifTool source archive, and checksums are in
`output/releases`. Build inputs digest:
`ba99e54a365aaa3173dc73a136ce6bb7ac1ea56c37cbfba9ac40a76753558bda`.

## Automated validation

- Backend: **726 passed, 4 skipped**. Desktop tests were rerun after adding their
  Windows-only mutex test guard: **19 passed**.
- Frontend: **361 passed across 34 files**.
- Frontend lint: **0 errors**, one existing React Compiler/TanStack Virtual
  warning in `FileListPanel.tsx`.
- TypeScript and production frontend build: **passed**. Existing bundle-size
  warnings remain.
- Ruff for changed desktop/bootstrap/packaging code and tests: **passed**.
- Bundled executable smoke with PATH restricted to Windows System32: **passed**
  for Tcl, ExifTool, ffprobe, and FFmpeg's H.264 encoder.

Tests cover configuration preservation and atomic writes; distinct/nonnested
folders; catalog recovery and newer-schema refusal; consistent upgrade backups;
download resumption, invalid ranges, interrupted transfers, corrupt archives,
cache receipts, staging failure and disk-space failure; setup job deduplication
and interruption recovery; Host/Origin/session/admin controls; shutdown tracking;
and execution checks for every discovered Hugin tool. Mocked failures establish
software behavior; they do not substitute for the physical-drive/manual matrix.

## Developer-host integration results

All media used here were generated synthetic fixtures. Tests used isolated
configuration, catalog, state, cache, inbox, and library folders under
`output/desktop-qa`. The developer's unrelated server was left running.

| Check | Result |
| --- | --- |
| Browser setup: welcome, folder validation, prepare data, ready, Review inbox | PASS; fixture place data for the initial browser journey |
| Setup does not move media | PASS; import occurred only after explicit submission |
| Desktop/narrow layouts | PASS; inspected 1280-pixel and 480-pixel screenshots |
| Diagnostics | PASS; exported allowlisted fields without paths, tokens, passwords, or media |
| Synthetic photo import and viewing | PASS through the existing browser interface and installed application API |
| Network map failure | PASS; blocked external map requests and retained local file-list browsing |
| Live city bootstrap | PASS; real download, extraction, staging, validation and replacement: 235694 places, 3865 admin1 regions, 47593 admin2 regions, 252 countries |
| Fresh packaged startup and existing configured-library startup | PASS |
| Final ZIP with restricted PATH and real place database | PASS |
| Simultaneous installed and ZIP launch | PASS; exactly one desktop process remained active |
| Occupied port 8000 | PASS; authenticated desktop used a different loopback port |
| Stale instance record | PASS; recovered without activating the unrelated socket |
| Unauthenticated desktop request | PASS; rejected |
| Running-app installer protection | PASS; installer exit 7 and actionable message |
| Per-user install and same-version reinstall | PASS; installer exit 0 |
| Graceful idle quit | PASS; both launch processes exited and instance record was removed |
| Uninstall | PASS; exit 0 and installed executable removed |
| Data retention after reinstall and uninstall | PASS; configuration, populated catalog, and imported photo retained identical SHA-256 hashes |

The final simultaneous-launch, port, stale-record, access, install-refusal, and
shutdown checks used the artifacts identified above. The earlier fresh-setup and
browser journey preceded the final additions for technical details, staging-space
checks, and Hugin validation; those additions have focused automated coverage.

Local evidence is in `output/desktop-qa` and `output/release-build/final-build.log`.
These ignored directories include private launch-session records and must not be
attached wholesale to a release. Sanitized diagnostic exports are appropriate for
support. The temporary test installation and browser/server processes were closed;
the test data and candidate artifacts remain for review.

## Outstanding release acceptance

1. Run the complete installer and ZIP workflow on clean Windows 11 x64 as a
   standard user without development or media tools. No clean VM/Sandbox was
   available in this session.
2. Exercise native dialog cancellation, Unicode/space-containing paths, actual
   mapped/UNC drives and disconnection, keyboard navigation, browser-open failure,
   and the interactive tray menu.
3. Run packaged H.264 and HEVC playback, CPU fallback, seeking and fullscreen;
   test graceful shutdown while each actual media worker pool is busy. Existing
   backend/frontend unit tests cover these subsystems but do not establish the
   complete packaged-device behavior.
4. Upgrade from an earlier packaged version with a representative populated
   catalog and video markers. Exercise actual backup/migration failure and restore,
   and repeat uninstall/reinstall on the clean machine. Same-version reinstall
   and schema unit tests are not an earlier-version upgrade acceptance result.
5. Obtain and validate the complete corresponding sources, patches and build
   scripts for the exact bundled FFmpeg build and its linked GPL components.
   The included FFmpeg license and source revision alone do not complete this.
6. Run the new Windows GitHub workflow after pushing the implementation. Tag
   builds create draft prereleases; publishing and signing are not performed here.

Record pass/fail evidence against a candidate's exact hashes before sharing it
with testers. Any rebuild changes the artifact identity and needs fresh signoff.
