# -*- mode: python ; coding: utf-8 -*-
import os

# 项目根目录（用绝对路径，确保 spec 被复制到临时目录时仍能定位资源）
ROOT = r'D:\workbuddy\诉讼案件网站'
ASSETS = os.path.join(ROOT, 'assets')
ICON = os.path.join(ASSETS, 'app.ico')

a = Analysis(
    [os.path.join(ROOT, 'court_doc_downloader_gui.py')],
    pathex=[],
    binaries=[],
    datas=[(ASSETS, 'assets')],
    hiddenimports=['fitz'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
    name='CourtDocDownloader',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON,
)
