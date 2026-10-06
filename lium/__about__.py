"""Project version metadata."""

# lium/_version.py is written by hatch-vcs from the git tag at build time (pyproject.toml)
# and is not committed; an untagged or unbuilt checkout must still import.
try:
    from lium._version import __version__
except ImportError:
    __version__ = "0.0.0+unknown"
