#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

DIST_DIR="dist"
mkdir -p "$DIST_DIR"

sha256_file() {
    local target="$1"
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$(basename "$target")" > "$(basename "$target").sha256"
    else
        shasum -a 256 "$(basename "$target")" > "$(basename "$target").sha256"
    fi
}

smoke_test() {
    local target="$1"
    echo "Smoke testing $(basename "$target")"
    "$target" --version
    "$target" --help >/dev/null
}

require_command() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "Error: required command '$1' is not installed." >&2
        exit 1
    fi
}

# Package the onedir bundle (dist/lium/, executable + _internal/) into a
# tarball whose top-level directory is always "lium/". install.sh extracts it
# and points the managed symlink at lium/lium.
package_bundle() {
    local asset="$1"
    rm -f "$DIST_DIR/${asset}" "$DIST_DIR/${asset}.sha256"
    tar -czf "$DIST_DIR/${asset}" -C "$DIST_DIR" lium
    (
        cd "$DIST_DIR"
        sha256_file "$asset"
    )
}

# Publish the legacy single-file binary under the bare lium-<os>-<arch> name.
# The self-update shipped in pre-onedir releases fetches this single-file asset;
# keeping it lets those installs keep auto-updating. Remove once old installs
# have migrated.
package_legacy() {
    local source="$1" asset="$2"
    rm -f "$DIST_DIR/${asset}" "$DIST_DIR/${asset}.sha256"
    cp "$source" "$DIST_DIR/${asset}"
    chmod +x "$DIST_DIR/${asset}"
    (
        cd "$DIST_DIR"
        sha256_file "$asset"
    )
}

build_macos() {
    if [[ "$(uname -s)" != "Darwin" ]]; then
        echo "Skipping macOS build: host is not macOS."
        return
    fi

    require_command uv

    echo "=== Building macOS (arm64) bundle ==="
    uv sync --frozen --extra dev
    rm -rf "$DIST_DIR/lium"
    uv run pyinstaller lium.spec --clean
    smoke_test "$DIST_DIR/lium/lium"
    package_bundle "lium-darwin-arm64.tar.gz"
    package_legacy "$DIST_DIR/lium-onefile" "lium-darwin-arm64"
    echo "✓ macOS bundle: $DIST_DIR/lium-darwin-arm64.tar.gz (+ legacy lium-darwin-arm64)"
}

build_linux() {
    require_command docker

    echo "=== Building Linux (amd64) bundle via Docker ==="
    docker build --platform linux/amd64 -f Dockerfile.build -t lium-build .

    docker rm -f lium-extract >/dev/null 2>&1 || true
    docker create --name lium-extract lium-build true >/dev/null
    rm -rf "$DIST_DIR/lium"
    docker cp lium-extract:/app/dist/lium "$DIST_DIR/lium"
    docker cp lium-extract:/app/dist/lium-onefile "$DIST_DIR/lium-onefile"
    docker rm -f lium-extract >/dev/null

    if [[ "$(uname -s)" == "Linux" ]]; then
        smoke_test "$DIST_DIR/lium/lium"
    fi
    package_bundle "lium-linux-amd64.tar.gz"
    package_legacy "$DIST_DIR/lium-onefile" "lium-linux-amd64"
    echo "✓ Linux bundle: $DIST_DIR/lium-linux-amd64.tar.gz (+ legacy lium-linux-amd64)"
}

case "${1:-all}" in
    macos) build_macos ;;
    linux) build_linux ;;
    all)
        build_macos
        echo
        build_linux
        ;;
    *)
        echo "Usage: $0 [macos|linux|all]" >&2
        exit 1
        ;;
esac

echo
echo "=== Build results ==="
ls -lh "$DIST_DIR"/lium-*.tar.gz "$DIST_DIR"/lium-linux-* "$DIST_DIR"/lium-darwin-* "$DIST_DIR"/*.sha256 2>/dev/null || echo "No bundles found"
