# geosorter

geosorter is a local-first DJI media organizer and browser. It extracts capture
metadata, resolves each capture to a place and local date, moves related files as a
unit, and presents the resulting library on an interactive map.

It supports photos, videos, hyperlapses, DJI panorama sets, SRT flight tracks,
no-GPS recovery, HEVC playback proxies, and optional Hugin panorama stitching.

![geosorter map and library interface](docs/images/geosorter-map-library.png)

_Screenshots use a small synthetic library; the interface is the real application._

The media viewer keeps the capture's place and local time visible while providing
previous/next navigation through the currently displayed files.

![geosorter media viewer](docs/images/geosorter-media-viewer.png)

The Locations panel provides a quick way to search the library and move the map to a
place.

<img src="docs/images/geosorter-locations.png" alt="geosorter Locations panel" width="380">

## Install and run on Windows

The Windows x64 desktop preview includes the Python runtime, built web interface,
ExifTool, FFmpeg, and ffprobe. You do not need to install Python, uv, Node.js, or
these media tools separately to use a packaged build.

See [Releases](https://github.com/hmallen/geosorter/releases) for available builds.
Preview packages come in two forms:

- **Installer:** run `GeoSorter-<version>-windows-x64-Setup.exe`, then open
  **GeoSorter** from the Start menu or the optional desktop shortcut.
- **ZIP:** extract the entire `GeoSorter-<version>-windows-x64.zip` archive, then
  run `GeoSorter.exe` inside the extracted `GeoSorter` folder. Keep the accompanying
  files and folders together.

GeoSorter starts its local server and opens your default browser automatically.
The desktop launcher currently supports Windows only; other systems can use the
[source installation](#install-from-source) and manual server below.

### First launch

1. Select **Set up a new library**.
2. Choose your **Incoming media** folder and an empty **Organized library** folder.
   The folders must be separate; neither can contain the other. Use **Create folder
   at this path** if you need a new destination, then **Save folders & continue**.
3. Select **Download & prepare place data** to download and index city and region
   names. This step requires an internet connection.
4. Select **Review inbox** to inspect incoming captures and start an import, or
   **Open library** to browse. Setup saves your choices and prepares data; it does
   not import or move your media automatically.

If you already use GeoSorter, select **Use existing GeoSorter configuration** and
choose your original `geosorter.toml` to retain its catalog, annotations, and media.
A populated media folder without its GeoSorter catalog cannot be adopted as a new
library. Connect any missing drives before continuing.

### Everyday use and updates

Open GeoSorter from its shortcut or extracted executable whenever you want to use
it. Launching it again reopens the running instance. Closing the browser tab leaves
GeoSorter running; use **Settings & Help → Quit GeoSorter** or **Quit** on its
system tray icon to stop it. If work is active, choose **Quit when finished**.

**Settings & Help** lets you change the incoming folder while idle, check connections
and tools, and export diagnostics. Under **Extras**, you can download detailed
parks, peaks, and lakes, locate an optional Hugin installation for panorama
stitching, or install video repair support. Library relocation is not available in
this preview.

Updates are manual: quit GeoSorter and wait for active work to finish, then install
the newer preview or extract its ZIP into a new folder. Settings and the catalog
are stored in per-user application data and are shared by the installer and ZIP.
The ZIP is not a self-contained portable library. Updating or uninstalling the
application preserves your settings, catalog, and media.

Preview status, known limitations, and build/release instructions are tracked in
[`docs/windows-preview-release.md`](docs/windows-preview-release.md).

The map uses hosted OpenFreeMap tiles. Organizing and browsing cached media are local,
but displaying the basemap requires an internet connection.

## Install from source

### Requirements

- Python 3.13 or newer
- [uv](https://docs.astral.sh/uv/) for the recommended Python workflow
- Node.js and npm to build or develop the web interface
- ExifTool 12.24 or newer for DJI photo and video metadata
- FFmpeg and ffprobe for video inspection, posters, previews, and HEVC proxies
- Optional: Hugin CLI tools for stitched panorama heroes

ExifTool, `ffmpeg`, and `ffprobe` must be available on `PATH`. Hugin can be on
`PATH` or configured with `hugin_bin_dir`.

### Build and launch

From a clone of this repository, install dependencies and build the interface:

```bash
uv sync
npm --prefix frontend ci
npm --prefix frontend run build
```

The frontend build is written to `src/geosorter/webui`, where the Python server can
serve it on the same origin as the API.

On Windows, start the desktop launcher and follow the first-launch steps above:

```bash
uv run geosorter desktop
```

To open an existing configuration explicitly:

```powershell
uv run geosorter desktop --config "C:\path\to\geosorter.toml"
```

Desktop launch uses `--config`, then its saved desktop selection, then the platform
user configuration path. It does **not** discover `./geosorter.toml` or use
`GEOSORTER_CONFIG`. Use the explicit command above when switching from a
repository-local configuration. **Settings & Help → Configuration location** shows
the active desktop configuration path.

The following manual configuration and bootstrap commands are for command-line
use or running the server directly. Desktop setup handles these steps for you.

## Configure for command-line use

Create a starter configuration:

```bash
uv run geosorter init-config
```

The command prints the path it created. Edit at least these two values:

```toml
inbox_path = 'D:\Drone\Inbox'
library_root = 'Z:\DroneLibrary'
```

- `inbox_path` is the folder to scan for new card dumps or uploads.
- `library_root` is the organized destination and may be a local disk, mapped drive,
  or NAS path.
- The index, GeoNames database, and thumbnail cache default to local user-data/cache
  directories rather than the media library.

See [`geosorter.example.toml`](geosorter.example.toml) for cache tiers, duplicate
handling, GPS inference, panorama settings, HEVC proxy warming, and other optional
settings.

Ordinary CLI commands (including `serve`, but excluding `desktop`) resolve
configuration in this order:

1. `--config PATH`
2. `GEOSORTER_CONFIG`
3. `./geosorter.toml` in the current directory, when it exists
4. the platform-specific user configuration directory

For a repository-local config, create `geosorter.toml` at the checkout root and
run commands from there — it is picked up automatically (or pass `--config` to
target it from anywhere):

```bash
uv run geosorter organize --dry-run
```

The local `geosorter.toml` is ignored by Git because it normally contains personal
paths.

## Bootstrap place data from the command line

Before the first import, download and index the GeoNames city/admin data:

```bash
uv run geosorter bootstrap
```

To also prefer nearby named parks, peaks, and hydro features over a distant town:

```bash
uv run geosorter bootstrap --features
```

`--features` downloads the much larger GeoNames `allCountries` dataset. Bootstrap is
normally a one-time operation.

## Organize media from the command line

Desktop users can import through **Review inbox** or **Process Inbox**. To import
from a source installation's command line, use the commands below.

Start with the read-only diagnostics and dry run:

```bash
uv run geosorter diagnose-inbox
uv run geosorter organize --dry-run
```

When the proposed destinations look correct, run the import:

```bash
uv run geosorter organize
```

The first destructive run asks for confirmation. Captures are filed under:

```text
<library_root>/<Place>/<YYYY-MM-DD>/<YYYY-MM-DD>_<HH-MM-SS>_<DJI filename>
```

geosorter groups primary media with companions such as DNG, LRF, SRT, panorama
frames, and retained hyperlapse frames. Cross-volume moves copy and verify the bytes
before deleting the source, and each move is recorded so interrupted runs can be
resumed safely.

Captures without a usable GPS location are quarantined instead of guessed. A nearby
capture can supply an inferred location when it falls within
`inference_max_gap_minutes`; otherwise the capture remains available in the
interface's No-GPS workflow.

## Run the server manually

For command-line or non-Windows use, configure and bootstrap as described above,
then start the local server:

```bash
uv run geosorter serve
```

Then open [http://127.0.0.1:8000](http://127.0.0.1:8000). Keep the terminal running;
press `Ctrl+C` there to stop the server. `serve` does not provide the desktop setup,
native folder dialogs, tray icon, or **Settings & Help** controls.

## Browse and manage the library

The main interface provides:

- Clustered map markers for GPS, inferred, manually assigned, and panorama captures
- Satellite and heatmap display modes
- A viewport-aware file rail grouped by day, month, or year
- Newest/oldest sorting and photo, video, panorama, and hyperlapse filters
- Photo viewing, video playback, panorama source-frame galleries, and flight tracks
- A searchable Locations panel that flies the map to a selected place
- Selective inbox imports, undo, rescan, no-GPS assignment, re-tagging, and panorama
  stitching for administrators

Click a map cluster to zoom in, a marker or thumbnail to open the viewer, or
**Locations** to jump directly to a named place. **Re-tag location** moves an
organized capture and its companions after you choose a new point on the map.

The **Process Inbox** panel scans the configured inbox and lets you import every
capture or only selected capture groups. Progress is shown in the toolbar, and the
library refreshes when the job finishes.

In an organized video, **Add marker** pauses at the current position and opens a
timestamp-and-note editor. Enter seconds, `MM:SS`, or `HH:MM:SS` (fractional seconds
are supported), optionally add a note, and select **Save**. The video resumes only
if it was playing before the editor opened. Markers appear on the seek bar and in
the player's **Markers** list, where administrators can edit or delete them.
The same controls work in the flight-map video window and fullscreen.

The toolbar's **Markers** browser searches saved timestamps across the entire
library, including videos outside the current map or filters. Selecting a marker
opens its video paused at that timestamp, retaining the full flight's navigation.
Markers are shared library annotations: everyone can view them; changes require
an admin login when a password is configured. Identical video content shares its
markers, which survive moves and re-imports. Notes are stored in the local index
database, so include that database in backups.

## Admin password and network access

The app is fully open when no admin password is configured. To make management
actions require a login:

```bash
uv run geosorter set-admin-password
```

With a password set, unauthenticated users can browse the library but cannot organize,
undo, rescan, assign locations, re-tag files, or start stitches.

The server binds to loopback by default. Binding another address exposes the library's
media and GPS coordinates to that network:

```bash
uv run geosorter serve --host 0.0.0.0 --port 8000
```

The admin password protects management actions, not read access to the map and media.
Only use a non-loopback bind on a trusted network or behind an appropriate VPN/reverse
proxy.

## Useful maintenance commands

These commands use the source installation. Prefix each with `uv run` when running
from the repository. If you normally use the desktop app, pass the configuration
path shown in **Settings & Help** so the CLI operates on the same library.

| Command | Purpose |
| --- | --- |
| `geosorter diagnose-inbox` | Explain why each inbox file would organize, quarantine, or remain in place without changing anything |
| `geosorter undo` | Move the most recent organized batch back to the inbox |
| `geosorter undo --batch ID` | Undo a specific batch |
| `geosorter rescan --dry-run` | Preview stale index rows for files no longer present in the library |
| `geosorter rescan` | Remove those stale rows from the index; never deletes or moves media |
| `geosorter verify-library` | Recompute stored hashes to detect missing or changed library files |
| `geosorter warm-proxies --all` | Pre-generate thumbnails, posters, and H.264 proxies for existing HEVC media |
| `geosorter clear-derived-cache` | Clear local thumbnails/posters/previews so they regenerate; keeps expensive proxies and stitches |
| `geosorter recover-collisions --dry-run` | Preview recovery for libraries affected by the historical recycled-filename collision |
| `geosorter restitch --dry-run` | Preview panorama heroes that need projection-aware re-stitching |

When using a non-default configuration, add `--config PATH` to the chosen command:

```bash
uv run geosorter verify-library --config geosorter.toml
```

## Development

Complete the source installation and CLI configuration/bootstrap above, then run
the backend and Vite development server in separate terminals:

```bash
uv run geosorter serve
```

```bash
npm --prefix frontend run dev
```

Vite proxies `/api` requests to the backend at `127.0.0.1:8000`.

Run the checks with:

```bash
uv run pytest
npm --prefix frontend run test
npm --prefix frontend run lint
npm --prefix frontend run build
```

The Python package lives in `src/geosorter`, the React/TypeScript application lives in
`frontend`, and the higher-level architecture notes are indexed in [`wiki/index.md`](wiki/index.md).

## License

MIT. See [`LICENSE`](LICENSE).
