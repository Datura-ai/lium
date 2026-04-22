"""Sphinx configuration for Lium SDK documentation."""

import importlib.metadata
import os
import sys
from datetime import datetime

PROJECT_ROOT = os.path.abspath("..")
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

about_ns: dict[str, str] = {}
with open(os.path.join(PROJECT_ROOT, "lium", "__about__.py"), encoding="utf-8") as about_file:
    exec(about_file.read(), about_ns)

fallback_version = about_ns["__version__"]

project = "Lium SDK"
author = "Lium"
copyright = f"{datetime.now():%Y}, {author}"

try:
    release = importlib.metadata.version("lium.io")
except importlib.metadata.PackageNotFoundError:
    release = fallback_version

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.autosummary",
    "sphinx.ext.napoleon",
    "sphinx.ext.viewcode",
]

autosummary_generate = True
autodoc_typehints = "description"
autodoc_mock_imports = ["paramiko", "requests", "dotenv"]

templates_path = ["_templates"]
exclude_patterns: list[str] = ["_build", "Thumbs.db", ".DS_Store"]

html_theme = "sphinx_rtd_theme"
html_theme_options = {
    "navigation_depth": 4,
    "collapse_navigation": False,
    "sticky_navigation": True,
    "style_external_links": False,
}
html_static_path = ["_static"]
