Binary Builds
=============

Official binary releases target:

- ``darwin-amd64``
- ``darwin-arm64``
- ``linux-arm64``
- ``linux-amd64``

Local maintainer builds
-----------------------

Build from the repository root:

.. code-block:: bash

   bash scripts/build.sh macos
   bash scripts/build.sh linux
   bash scripts/build.sh all

The script writes platform binaries plus ``.sha256`` files into ``dist/`` and
runs basic smoke tests on the host-supported artifacts.

Release flow
------------

The manual GitHub Actions release workflow now builds:

- Python sdist/wheel outputs
- ``lium-darwin-amd64``
- ``lium-darwin-arm64``
- ``lium-linux-arm64``
- ``lium-linux-amd64``
- ``install.sh``
- ``checksums.txt``

Binary assets are uploaded to GitHub Releases so the public installer can fetch
``releases/latest/download/<asset>`` without relying on private infrastructure.
Fresh installs keep ``~/.lium/bin/lium`` on ``PATH`` as a symlink to the managed
versioned binary stored in ``~/.lium/versions/<version>/lium``.

Binary runtime notes
--------------------

- The frozen entrypoint uses ``multiprocessing.freeze_support()`` to avoid
  child-process argument parsing issues under PyInstaller.
- The CLI version falls back to in-repo version metadata when distribution
  metadata is unavailable in a frozen build.
- ``lium/cli/themes.json`` is bundled into the PyInstaller build and loaded from
  the extracted bundle when needed.
