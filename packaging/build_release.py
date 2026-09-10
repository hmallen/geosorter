"""Reproducible Windows candidate build. Run with uv run --group release."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "output" / "release-build"
CACHE = BUILD / "downloads"
MANIFEST = json.loads((ROOT / "packaging/dependencies.json").read_text())


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def source_inventory():
    files = [ROOT / "pyproject.toml", ROOT / "uv.lock"]
    files += [p for p in (ROOT / "src/geosorter").rglob("*")
              if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"]
    files += [p for p in (ROOT / "packaging").glob("*") if p.is_file()]
    return {str(p.relative_to(ROOT)).replace("\\", "/"): digest(p) for p in sorted(files)}


def download(item):
    CACHE.mkdir(parents=True, exist_ok=True)
    target = CACHE / item["file"]
    if target.exists() and digest(target) == item["sha256"]:
        return target
    request = urllib.request.Request(item["url"], headers={"User-Agent": "GeoSorter-release-build"})
    stage = target.with_suffix(target.suffix + ".partial")
    with urllib.request.urlopen(request, timeout=120) as response, stage.open("wb") as out:
        shutil.copyfileobj(response, out)
    if digest(stage) != item["sha256"]:
        raise RuntimeError(f"Checksum mismatch: {item['name']}. Refusing to build.")
    stage.replace(target)
    print(f"Verified {item['file']}", flush=True)
    return target


def licenses(bundle):
    dest = bundle / "third-party"
    packages = {}
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata["Name"]
        packages[name] = distribution.version
        for file in distribution.files or []:
            if any(part.lower().startswith(("license", "copying", "notice")) for part in file.parts):
                source = Path(distribution.locate_file(file))
                if source.is_file():
                    target = dest / "python" / name / str(file).replace("/", "_").replace("\\", "_")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
    modules = ROOT / "frontend/node_modules"
    for source in modules.rglob("*"):
        if source.is_file() and source.name.lower().startswith(("license", "copying", "notice")):
            target = dest / "frontend" / source.relative_to(modules)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    return packages


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--download-only", action="store_true")
    parser.add_argument("--iscc", type=Path)
    args = parser.parse_args()
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(download, MANIFEST["artifacts"]))
    if args.download_only:
        return
    if os.name != "nt":
        raise RuntimeError("Build Windows releases on Windows.")
    if (Path(sys.base_prefix) / "conda-meta").exists():
        raise RuntimeError("Build with standard CPython, not a Conda-backed virtual environment (native DLL collection differs).")
    for item in MANIFEST["artifacts"]:
        if item["name"] not in ("ffmpeg", "exiftool"):
            continue
        dest = BUILD / "tools" / item["name"]
        # Reuse only a byte-for-byte match for the pinned archive; otherwise stop.
        reuse = dest.exists()
        dest.mkdir(parents=True, exist_ok=True)
        expected_files = set()
        with zipfile.ZipFile(CACHE / item["file"]) as archive:
            for info in archive.infolist():
                parts = Path(info.filename).parts
                if item["name"] == "ffmpeg" or (item["name"] == "exiftool" and parts[0].startswith("exiftool-")):
                    parts = parts[1:]
                if not parts or info.is_dir():
                    continue
                relative = Path(*parts)
                if relative.name == "exiftool(-k).exe":
                    relative = relative.with_name("exiftool.exe")
                if relative.is_absolute() or ".." in relative.parts:
                    raise RuntimeError("Invalid dependency archive path")
                target = dest / relative
                expected_files.add(relative)
                if reuse:
                    with archive.open(info) as source:
                        expected = hashlib.file_digest(source, "sha256").hexdigest()
                    if not target.is_file() or digest(target) != expected:
                        raise RuntimeError(f"Stale or modified tool file: {target}. Build into a fresh tools directory.")
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target.open("wb") as out:
                    shutil.copyfileobj(source, out)
        actual_files = {p.relative_to(dest) for p in dest.rglob("*") if p.is_file()}
        if actual_files != expected_files:
            raise RuntimeError(f"Unexpected files in {dest}. Build into a fresh tools directory.")
    subprocess.run([shutil.which("npm.cmd"), "--prefix", "frontend", "ci"], cwd=ROOT, check=True)
    subprocess.run([shutil.which("npm.cmd"), "--prefix", "frontend", "run", "build"], cwd=ROOT, check=True)
    sources = source_inventory()
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", str(BUILD / "dist"),
                    "--workpath", str(BUILD / "pyinstaller"), str(ROOT / "packaging/GeoSorter.spec")], cwd=ROOT, check=True)
    bundle = BUILD / "dist/GeoSorter"
    from geosorter import __version__
    tag = os.environ.get("GITHUB_REF_NAME", "")
    if os.environ.get("GITHUB_REF_TYPE") == "tag" and tag != "v" + __version__:
        raise RuntimeError("Release tag must match the application version.")
    packages = licenses(bundle)
    smoke = BUILD / "smoke.json"
    env = os.environ.copy()
    env["PATH"] = str(Path(os.environ["SystemRoot"]) / "System32")
    subprocess.run([str(bundle / "GeoSorter.exe"), "--smoke-test", str(smoke)], env=env, check=True, timeout=90)
    if sources != source_inventory():
        raise RuntimeError("Build inputs changed during packaging. Rebuild the candidate.")
    shutil.copy2(ROOT / "packaging/THIRD_PARTY_NOTICES.md", bundle)
    shutil.copy2(ROOT / "LICENSE", bundle)
    release = ROOT / "output/releases"
    release.mkdir(parents=True, exist_ok=True)
    manifest = {**MANIFEST, "version": __version__, "python_packages": packages,
                "build_input_sha256": hashlib.sha256(json.dumps(sources, sort_keys=True).encode()).hexdigest(),
                "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                "working_tree_dirty": bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip()),
                "smoke": json.loads(smoke.read_text()), "clean_machine_acceptance": "NOT RUN",
                "redistribution_source_review": "PENDING"}
    (bundle / "release-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    shutil.copy2(bundle / "release-manifest.json", release)
    shutil.copy2(CACHE / "Image-ExifTool-13.59.tar.gz", release)
    shutil.make_archive(str(release / f"GeoSorter-{__version__}-windows-x64"), "zip", bundle.parent, bundle.name)
    iscc = args.iscc or Path(shutil.which("ISCC") or BUILD / "inno/ISCC.exe")
    if not iscc.is_file():
        installer = CACHE / "innosetup-6.7.3.exe"
        subprocess.run([str(installer), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CURRENTUSER",
                        "/NOICONS", f"/DIR={BUILD / 'inno'}"], check=True)
    subprocess.run([str(iscc), f"/DAppVersion={__version__}", f"/DBundleDir={bundle}", str(ROOT / "packaging/GeoSorter.iss")], check=True)
    files = sorted(p for p in release.iterdir() if p.is_file() and p.name != "SHA256SUMS.txt")
    (release / "SHA256SUMS.txt").write_text("".join(f"{digest(p)}  {p.name}\n" for p in files))
    print(f"Candidate artifacts: {release}")


if __name__ == "__main__":
    main()
