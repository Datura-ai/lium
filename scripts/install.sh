#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="${LIUM_INSTALL_DIR:-$HOME/.lium/bin}"
REPO_SLUG="${LIUM_REPO_SLUG:-Datura-ai/lium-cli}"
RELEASE_BASE="https://github.com/${REPO_SLUG}/releases"

detect_asset_name() {
    local os arch

    os="$(uname -s | tr '[:upper:]' '[:lower:]')"
    arch="$(uname -m)"

    case "$arch" in
        x86_64|amd64) arch="amd64" ;;
        arm64|aarch64) arch="arm64" ;;
        *)
            echo "Error: unsupported architecture: $arch" >&2
            exit 1
            ;;
    esac

    case "$os" in
        linux|darwin)
            printf 'lium-%s-%s' "$os" "$arch"
            ;;
        *)
            echo "Error: unsupported OS: $os" >&2
            exit 1
            ;;
    esac
}

download() {
    local url="$1"
    local target="$2"

    if command -v curl >/dev/null 2>&1; then
        curl -fsSL "$url" -o "$target"
    elif command -v wget >/dev/null 2>&1; then
        wget -q "$url" -O "$target"
    else
        echo "Error: curl or wget is required." >&2
        exit 1
    fi
}

verify_checksum() {
    local asset_name="$1"
    local install_root="$2"

    if ! command -v sha256sum >/dev/null 2>&1 && ! command -v shasum >/dev/null 2>&1; then
        echo "Warning: sha256sum/shasum not available; skipping checksum verification." >&2
        return
    fi

    (
        cd "$install_root"
        if command -v sha256sum >/dev/null 2>&1; then
            grep " ${asset_name}\$" checksums.txt | sha256sum --check --status
        else
            grep " ${asset_name}\$" checksums.txt | shasum -a 256 --check --status
        fi
    )
}

add_to_path() {
    local shell_name shell_rc path_line

    if [[ ":$PATH:" == *":${INSTALL_DIR}:"* ]]; then
        return
    fi

    shell_name="${SHELL##*/}"
    case "$shell_name" in
        bash) shell_rc="$HOME/.bashrc"; path_line="export PATH=\"${INSTALL_DIR}:\$PATH\"" ;;
        zsh) shell_rc="$HOME/.zshrc"; path_line="export PATH=\"${INSTALL_DIR}:\$PATH\"" ;;
        fish) shell_rc="$HOME/.config/fish/config.fish"; path_line="set -gx PATH ${INSTALL_DIR} \$PATH" ;;
        *) shell_rc="" ;;
    esac

    if [[ -n "$shell_rc" ]]; then
        mkdir -p "$(dirname "$shell_rc")"
        if ! grep -qF "$INSTALL_DIR" "$shell_rc" 2>/dev/null; then
            {
                echo
                echo "# Lium CLI"
                echo "$path_line"
            } >> "$shell_rc"
            echo "Added ${INSTALL_DIR} to PATH in ${shell_rc}"
        fi
    fi

    export PATH="${INSTALL_DIR}:$PATH"
}

main() {
    local asset_name release_path temp_dir binary_url checksum_url

    asset_name="$(detect_asset_name)"
    release_path="${RELEASE_BASE}/latest/download"
    if [[ -n "${LIUM_VERSION:-}" ]]; then
        release_path="${RELEASE_BASE}/download/v${LIUM_VERSION}"
    fi

    temp_dir="$(mktemp -d)"
    trap 'rm -rf "$temp_dir"' EXIT

    mkdir -p "$INSTALL_DIR"

    binary_url="${release_path}/${asset_name}"
    checksum_url="${release_path}/checksums.txt"

    echo "Downloading ${asset_name} from ${binary_url}"
    download "$binary_url" "$temp_dir/${asset_name}"
    download "$checksum_url" "$temp_dir/checksums.txt"
    verify_checksum "$asset_name" "$temp_dir"

    install -m 0755 "$temp_dir/${asset_name}" "${INSTALL_DIR}/lium"
    add_to_path

    echo
    echo "Lium CLI installed to ${INSTALL_DIR}/lium"
    echo
    echo "Next steps:"
    echo "  1. Restart your shell or run: export PATH=\"${INSTALL_DIR}:\$PATH\""
    echo "  2. Initialize: lium init"
    echo "  3. List GPUs:  lium ls"
    echo

    "${INSTALL_DIR}/lium" --version || true
}

main "$@"
