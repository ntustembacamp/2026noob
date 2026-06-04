# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path


# PyInstaller 執行 spec 時不保證有 __file__，改用當前工作目錄。
BASE = Path.cwd()
SCRIPT = str(BASE / "windows_activity_import_laptop_tool.py")

a = Analysis(
    [SCRIPT],
    pathex=[str(BASE), str(BASE / "service")],
    binaries=[],
    datas=[
        (str(BASE / "service" / "tools"), "tools"),
        (str(BASE / "service" / "embedding" / "faces_embedding_antelopev2.pkl"), "embeddings"),
        (str(BASE / "shared" / "win" / "config.py"), "."),
    ],
    hiddenimports=[
        "cv2",
        "numpy",
        "PIL",
        "insightface",
        "onnxruntime",
        "tools.new_face_laptop",
        "service.tools.new_face_laptop",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "matplotlib",
        "IPython",
        "jupyter",
        "pytest",
        "tensorboard",
        "bitsandbytes",
        "transformers",
        "datasets",
        "spacy",
        "pyarrow",
        "numba",
        "llvmlite",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="laptop_activity_import_tool",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="laptop_activity_import_tool",
)
