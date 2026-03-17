# lium.spec -- PyInstaller spec for lium-cli
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

hiddenimports = [
    # lium internals
    *collect_submodules('lium'),

    # bittensor ecosystem (lazy imports in fund command)
    *collect_submodules('bittensor'),
    *collect_submodules('bittensor_cli'),
    *collect_submodules('async_substrate_interface'),
    *collect_submodules('scalecodec'),

    # direct deps with C extensions or dynamic loading
    *collect_submodules('Crypto'),
    'paramiko',
    'requests',
    'dotenv',
    'pydantic',
    'loguru',
    'yaml',
    'click',
    'rich',
    'fuzzywuzzy',
    'Levenshtein',
    'netaddr',
    'aiohttp',
    'docker',
    'git',
    'jinja2',
    'plotly',
    'plotille',
    'backoff',

    # stdlib extras
    'importlib.metadata',
]

# data files to include
datas = [
    ('lium/cli/themes.json', 'lium/cli'),
]

# collect data files from packages that ship non-python assets
datas += collect_data_files('bittensor', include_py_files=False)
datas += collect_data_files('bittensor_cli', include_py_files=False)
datas += collect_data_files('plotly', include_py_files=False)
datas += collect_data_files('pywry', include_py_files=False)
datas += collect_data_files('certifi', include_py_files=False)

a = Analysis(
    ['lium_entry.py'],
    pathex=['.'],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter',
        'matplotlib',
        'scipy',
        'numpy.tests',
        'pytest',
        'sphinx',
    ],
    noarchive=False,
    optimize=0,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='lium',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
