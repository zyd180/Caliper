# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onedir 打包配置：Caliper VCU 测量数据分析工具。

用法：pyinstaller Caliper.spec --noconfirm
数据目录（rules/ DBC/ description/）外置到 exe 旁边，便于用户维护；
realdata/ 是验收样本，不打包。out/ 由程序运行时自建。
"""
import os

block_cipher = None

# 数据目录：源路径 -> 分发目录名（都放在 exe 同级）
DATAS = [
    ("rules", "rules"),
    ("DBC", "DBC"),
    ("description", "description"),
]

a = Analysis(
    ["start.py"],
    pathex=[],
    binaries=[],
    datas=DATAS,
    hiddenimports=[
        "PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets",
        "pyqtgraph", "pyqtgraph.graphicsItems", "pyqtgraph.graphicsItems.LinearRegionItem",
        "pyqtgraph.graphicsItems.PlotItem", "pyqtgraph.GraphicsScene",
        "asammdf", "asammdf.blocks", "asammdf.blocks.v4_blocks",
        "canmatrix", "canmatrix.formats", "can", "can.io", "can.io.blf",
        "pandas", "numpy", "openpyxl",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["matplotlib", "scipy", "tkinter"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Caliper",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,               # GUI 程序无控制台；异常写 out/startup_error.log
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Caliper",
)
