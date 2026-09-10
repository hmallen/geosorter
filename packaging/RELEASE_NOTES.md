Windows preview candidate for GeoSorter first-run setup.

- Version 0.2.2 shows when detailed places are installed and prevents repeat
  downloads. Existing detailed-place databases are recognized without downloading
  again, including after restarting GeoSorter or installing another optional tool.
- Version 0.2.1 fixes video-repair downloads on fresh Windows installations by
  loading the bundled trusted certificates alongside the Windows certificate store.
- Open GeoSorter from the Start Menu or extract the ZIP and run GeoSorter.exe.
- Choose separate incoming and organized-library folders, then prepare city data.
- Setup never imports media automatically. Use Review inbox when ready.
- Settings & Help includes optional detailed place data, Hugin setup, video repair,
  connection checks, diagnostics, and a link to releases.
- Closing the browser leaves GeoSorter running. Use Quit GeoSorter or the tray icon.
- Upgrading and uninstalling keep your catalog, annotations, settings, and media.
- The ZIP uses per-user application data; it is not a self-contained portable library.

This candidate is unsigned. Clean-machine acceptance and the complete corresponding
source review for bundled GPL components must be recorded before sharing the build.
See docs/windows-preview-release.md for the acceptance record and distribution gate.
