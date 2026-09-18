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

What the Linux bundle ships
---------------------------

PyInstaller copies the build image's ``libssl.so.1.1``, ``libcrypto.so.1.1`` and
``libpython3.12.so.1.0`` into ``dist/lium/_internal``. After every Linux build,
``ci.yml`` and ``release.yml`` run ``scripts/linux_bundle_report.py``, which writes
to the run's step summary the Debian ``libssl1.1`` package version,
``ssl.OPENSSL_VERSION``, the Python version and the bundled ``requests``,
``paramiko`` and ``cryptography`` versions. They are read inside the build image;
the OpenSSL and Python values are tied to the bundle by the sha256 of those three
libraries, the wheel versions are the build venv's. The step fails when
``libssl1.1`` is below ``1.1.1w-0+deb11u8`` (the version ``Dockerfile.build`` pins)
or a bundled library is not the image's file; ``release-assets`` needs the build
job, so such a bundle cannot be published. ``ssl.OPENSSL_VERSION`` alone cannot
tell ``deb11u3`` from ``deb11u8`` (both print ``OpenSSL 1.1.1w  11 Sep 2023``),
which is why the check reads the Debian package version.

Locally, after ``docker build -f Dockerfile.build -t lium-build:local .`` and
copying ``/app/dist/lium`` out of the image to ``dist/lium``:

.. code-block:: bash

   python3 scripts/linux_bundle_report.py --bundle dist/lium --image lium-build:local

How the Linux bundle is built in CI
-----------------------------------

``Dockerfile.build`` copies ``pyproject.toml`` and ``uv.lock`` and runs
``uv sync --frozen --no-install-project`` before it copies the source, so the
dependency layer is reused by every build whose lockfile did not change; the
source ``COPY``, the project install and PyInstaller are the only steps a code
change reruns. ``ci.yml`` and ``release.yml`` build with
``docker/build-push-action`` on a ``docker-container`` builder and keep the
layers in the GitHub Actions cache (``type=gha``, one scope per asset:
``linux-bundle-lium-linux-amd64``, ``linux-bundle-lium-linux-arm64``), so a
PR push after the first one starts from cached layers; a cache export the
service refuses does not fail the build (``ignore-error=true``). The context
excludes ``.git``, ``dist``, ``build``, ``.venv`` and ``**/__pycache__``
(``.dockerignore``); the version is handed in as ``LIUM_VERSION`` because the
image has no git.

What the macOS bundles ship
---------------------------

Both macOS jobs run ``uv sync --frozen`` and PyInstaller on GitHub's runners
(``macos-14`` for arm64, ``macos-15-intel`` for x86_64). ``cryptography`` 49 and
later publish no macOS Intel wheel, so on the Intel runner uv builds it from
source; the job sets ``OPENSSL_STATIC=1`` and ``OPENSSL_DIR=$(brew --prefix
openssl@3)`` first, so OpenSSL is linked into ``_rust.abi3.so`` instead of being
copied into ``dist/lium/_internal`` as ``libssl.3.dylib`` — python.org's ``_ssl``
ships a dylib of the same name, and with two candidates PyInstaller kept the
older one (``Symbol not found: _SSL_get0_group_name`` at import, the first
0.0.37 Intel build). The arm64 wheel is static already. After the build, the
smoke test step of ``ci.yml`` and ``release.yml`` runs ``otool -L`` on that
``_rust.abi3.so`` and fails when it references ``libssl`` or ``libcrypto``.

Binary runtime notes
--------------------

- The frozen entrypoint uses ``multiprocessing.freeze_support()`` to avoid
  child-process argument parsing issues under PyInstaller.
- The CLI version falls back to in-repo version metadata when distribution
  metadata is unavailable in a frozen build.
- ``lium/cli/themes.json`` is bundled into the PyInstaller build and loaded from
  the extracted bundle when needed.
