# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['C:\\Users\\toxic\\Desktop\\hackrf-utility\\cema_app.py'],
    pathex=['C:\\Users\\toxic\\Desktop\\hackrf-utility'],
    binaries=[('fpv_decoder.dll', '.')],
    datas=[],
    hiddenimports=['PyQt6.QtWebEngineWidgets', 'PyQt6.QtWebEngineCore', 'PyQt6.QtGui', 'PyQt6.QtCore', 'PyQt6.QtWidgets', 'PyQt6.uic', 'PyQt6.QtSvg', 'PyQt6.QtSvgWidgets', 'PyQt6.QtPrintSupport', 'serial', 'serial.tools.list_ports', 'heltec_bridge'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['PyQt5', 'PySide2', 'PySide6', 'torch', 'torchvision', 'xformers', 'torchaudio', 'numba', 'pandas', 'matplotlib', 'skimage', 'sklearn', 'tkinter', 'IPython'],
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
    name='CEMA_Tracker',
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
)
