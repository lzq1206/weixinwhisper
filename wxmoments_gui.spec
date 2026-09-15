# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules


project = Path(SPECPATH)
sys.path.insert(0, str(project / "src"))
pkg_datas = collect_data_files("wechat_decrypt_tool")
pkg_binaries = collect_dynamic_libs("wechat_decrypt_tool")
pkg_hiddenimports = collect_submodules("wechat_decrypt_tool")
datas = list(pkg_datas)
datas.append((str(project / "config" / "config.json"), "config"))

a = Analysis(
    [str(project / "wxmoments_gui.py")],
    pathex=[str(project / "src"), str(project)],
    binaries=pkg_binaries,
    datas=datas,
    hiddenimports=pkg_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["matplotlib", "numpy", "pandas", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="wxMoments",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
)
