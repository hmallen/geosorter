# GeoSorter Windows preview: third-party software

GeoSorter is MIT licensed. Bundled executables are separate programs and retain
their own licenses; GeoSorter does not change or relicense them.

- Python: Python Software Foundation License. https://www.python.org/psf/license/
- ExifTool 13.59 by Phil Harvey: same terms as Perl (Artistic License or GPL).
  The full upstream Windows distribution is retained, including Perl, notices,
  and supporting files. The matching source archive accompanies release builds.
  https://exiftool.org/ and https://exiftool.org/install.html
- FFmpeg 9.0.1 essentials, built by Gyan Doshi: GPLv3; includes libx264 and other
  libraries listed in the upstream build documentation. The original license,
  README, and documentation are retained with the tools.
  https://www.gyan.dev/ffmpeg/builds/ and https://ffmpeg.org/legal.html
  FFmpeg source revision: https://github.com/FFmpeg/FFmpeg/tree/bf1b838f2a
- Python dependency license texts are collected in `third-party/python` by the
  release builder; exact installed versions are in the release manifest.
- Frontend dependency license texts are collected in `third-party/frontend`.

The release builder generates internal preview candidates. Before distributing
the FFmpeg-containing candidate, provide the complete corresponding source and
build materials for the chosen FFmpeg build and its GPL components alongside
the download. A link to FFmpeg alone does not cover its statically linked libraries.
Record this verification together with clean-machine acceptance in the release
checklist. Draft CI releases are not public distribution approval.

Optional Hugin and untrunc are installed separately at the user's request from
their upstream projects and retain their accompanying license files.
