# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the GUI build (场景 A / B).

--windowed 是场景 B 能做到「完全无提示」的关键：进程从出生就没有控制台，
不像 --console 那样先弹一个黑框再靠 ShowWindow 藏起来。

    pyinstaller --clean --noconfirm build/CampusNet.spec

CLI 版本请继续用 build/auto_login.spec（console=True）。
"""


a = Analysis(
    ['..\\gui_app.py'],
    pathex=['..'],
    binaries=[],
    # The tray icon is a real .ico loaded through LoadImageW, which needs a file
    # on disk — bundle it so the onefile exe is still self-contained.
    # (The window icon is embedded as base64 in gui_app.py instead.)
    datas=[('..\\assets\\campusnet.ico', 'assets')],
    hiddenimports=['auto_login'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['numpy', 'pandas', 'matplotlib', 'PIL', 'scipy', 'setuptools', 'pip'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='CampusNet',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='..\\assets\\campusnet.ico',
)
