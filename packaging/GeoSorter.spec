from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_data_files, copy_metadata

root = Path(SPECPATH).parent
datas = [(str(root / 'src/geosorter/webui'), 'geosorter/webui'),
         (str(root / 'output/release-build/tools'), 'tools'),
         (str(root / 'packaging/THIRD_PARTY_NOTICES.md'), '.'),
         (str(root / 'LICENSE'), '.')]
binaries, hidden = [], []
for package in ('timezonefinder', 'h3', 'tzdata', 'pystray', 'uvicorn'):
    d, b, h = collect_all(package)
    datas += d; binaries += b; hidden += h
datas += collect_data_files('certifi')
for package in ('geosorter', 'fastapi', 'pydantic', 'starlette', 'uvicorn'):
    datas += copy_metadata(package)
a = Analysis([str(root / 'packaging/entry.py')], pathex=[str(root / 'src')],
             binaries=binaries, datas=datas, hiddenimports=hidden + ['tkinter', 'tkinter.filedialog', 'tkinter.messagebox'],
             excludes=['pytest', 'IPython', 'matplotlib', 'scipy'], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='GeoSorter',
          console=False, debug=False, strip=False, upx=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='GeoSorter')
