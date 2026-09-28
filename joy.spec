# -*- mode: python ; coding: utf-8 -*-
"""Joy 看屏副驾 — 单文件 exe（瘦身：不打进 onnxruntime.tools/transformers）。"""

from __future__ import annotations

from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
)

ROOT = Path(SPECPATH).resolve()
SRC = ROOT / "src"

hiddenimports: list[str] = [
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "PySide6.QtNetwork",
    "win32gui",
    "win32con",
    "win32api",
    "win32process",
    "win32ui",
    "pythoncom",
    "pywintypes",
    "mss",
    "PIL",
    "PIL.Image",
    "openai",
    "pydantic",
    "pydantic_settings",
    "httpx",
    "numpy",
    "cv2",
    "shapely",
    "onnxruntime",
    "onnxruntime.capi.onnxruntime_pybind11_state",
    "rapidocr_onnxruntime",
]
# 只收 joy 包，不要 collect_all(onnxruntime)（会拖进 tools/transformers）
hiddenimports += collect_submodules("joy")
hiddenimports += collect_submodules("rapidocr_onnxruntime")

datas = collect_data_files("rapidocr_onnxruntime")
binaries = collect_dynamic_libs("onnxruntime")
# OpenCV / shapely 原生库由各自 hook 随 import 带上即可

a = Analysis(
    [str(ROOT / "packaging" / "run_joy.py")],
    pathex=[str(SRC)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "PySide6.Qt3DAnimation",
        "PySide6.Qt3DCore",
        "PySide6.Qt3DExtras",
        "PySide6.Qt3DInput",
        "PySide6.Qt3DLogic",
        "PySide6.Qt3DRender",
        "PySide6.QtBluetooth",
        "PySide6.QtCharts",
        "PySide6.QtDataVisualization",
        "PySide6.QtDesigner",
        "PySide6.QtHelp",
        "PySide6.QtMultimedia",
        "PySide6.QtMultimediaWidgets",
        "PySide6.QtNfc",
        "PySide6.QtPdf",
        "PySide6.QtPdfWidgets",
        "PySide6.QtPositioning",
        "PySide6.QtQuick",
        "PySide6.QtQuick3D",
        "PySide6.QtQuickControls2",
        "PySide6.QtQuickWidgets",
        "PySide6.QtQml",
        "PySide6.QtRemoteObjects",
        "PySide6.QtSensors",
        "PySide6.QtSerialPort",
        "PySide6.QtSpatialAudio",
        "PySide6.QtSql",
        "PySide6.QtSvg",
        "PySide6.QtSvgWidgets",
        "PySide6.QtTest",
        "PySide6.QtTextToSpeech",
        "PySide6.QtWebChannel",
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineWidgets",
        "PySide6.QtWebEngineQuick",
        "PySide6.QtWebSockets",
        "PySide6.QtXml",
        "tkinter",
        "matplotlib",
        "scipy",
        "pandas",
        "pytest",
        "IPython",
        "notebook",
        "unittest",
        "test",
        "tests",
        # OCR 推理用不到；排除避免进包
        "onnxruntime.tools",
        "onnxruntime.transformers",
        "onnxruntime.quantization",
        "onnxruntime.datasets",
        "torch",
        "tensorflow",
        "onnx",
        "transformers",
        "sympy",
    ],
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
    name="Joy",
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
)
