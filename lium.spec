# PyInstaller spec for the Lium CLI binary.
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

hiddenimports = [
    *collect_submodules("lium"),
    *collect_submodules("bittensor"),
    *collect_submodules("bittensor_cli"),
    *collect_submodules("async_substrate_interface"),
    # cyscale and py-scale-codec both expose the "scalecodec" import namespace.
    *collect_submodules("scalecodec"),
    *collect_submodules("Crypto"),
    "paramiko",
    "requests",
    "dotenv",
    "pydantic",
    "loguru",
    "yaml",
    "click",
    "rich",
    "fuzzywuzzy",
    "Levenshtein",
    "netaddr",
    "aiohttp",
    "docker",
    "git",
    "jinja2",
    "plotly",
    "plotille",
    "backoff",
    "importlib.metadata",
]

datas = [
    ("lium/cli/themes.json", "lium/cli"),
]
datas += collect_data_files("bittensor", include_py_files=False)
datas += collect_data_files("bittensor_cli", include_py_files=False)
datas += collect_data_files("plotly", include_py_files=False)
datas += collect_data_files("certifi", include_py_files=False)

a = Analysis(
    ["lium_entry.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "matplotlib",
        "scipy",
        "numpy.tests",
        "pytest",
        "sphinx",
    ],
    noarchive=False,
    optimize=0,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# onedir build: the bundle is laid out as dist/lium/ (executable + _internal/)
# so startup does NOT self-extract on every launch. upx is off — it only adds
# decompression cost at startup for marginal size savings.
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="lium",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
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
    upx=False,
    upx_exclude=[],
    name="lium",
)
