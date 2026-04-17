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

build_macos() {
    if [[ "$(uname -s)" != "Darwin" ]]; then
        echo "Skipping macOS build: host is not macOS."
        return
    fi

    require_command uv

    echo "=== Building macOS (arm64) binary ==="
    uv sync --frozen --extra dev
    uv run pyinstaller lium.spec --clean
    mv "$DIST_DIR/lium" "$DIST_DIR/lium-darwin-arm64"

    (
        cd "$DIST_DIR"
        sha256_file "lium-darwin-arm64"
    )
    smoke_test "$DIST_DIR/lium-darwin-arm64"
    echo "✓ macOS binary: $DIST_DIR/lium-darwin-arm64"
}

build_linux() {
    require_command docker

    echo "=== Building Linux (amd64) binary via Docker ==="
    docker build --platform linux/amd64 -f Dockerfile.build -t lium-build .

    docker rm -f lium-extract >/dev/null 2>&1 || true
    docker create --name lium-extract lium-build true >/dev/null
    docker cp lium-extract:/app/dist/lium "$DIST_DIR/lium-linux-amd64"
    docker rm -f lium-extract >/dev/null

    chmod +x "$DIST_DIR/lium-linux-amd64"
    (
        cd "$DIST_DIR"
        sha256_file "lium-linux-amd64"
    )

    if [[ "$(uname -s)" == "Linux" ]]; then
        smoke_test "$DIST_DIR/lium-linux-amd64"
    fi

    echo "✓ Linux binary: $DIST_DIR/lium-linux-amd64"
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
ls -lh "$DIST_DIR"/lium-* "$DIST_DIR"/*.sha256 2>/dev/null || echo "No binaries found"
