#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="${LIUM_INSTALL_DIR:-$HOME/.lium/bin}"
REPO_SLUG="${LIUM_REPO_SLUG:-Datura-ai/lium-cli}"
RELEASE_BASE="https://github.com/${REPO_SLUG}/releases"

detect_os() {
    printf '%s' "${LIUM_INSTALLER_UNAME_S:-$(uname -s)}" | tr '[:upper:]' '[:lower:]'
}

detect_arch() {
    local arch

    arch="${LIUM_INSTALLER_UNAME_M:-$(uname -m)}"

    case "$arch" in
        x86_64|amd64) printf 'amd64' ;;
        arm64|aarch64) printf 'arm64' ;;
        *)
            echo "Error: unsupported architecture: $arch" >&2
            exit 1
            ;;
    esac
}

detect_asset_name() {
    local os arch

    os="$(detect_os)"
    arch="$(detect_arch)"

    case "$os/$arch" in
        linux/amd64|linux/arm64|darwin/amd64|darwin/arm64)
            printf 'lium-%s-%s' "$os" "$arch"
            ;;
        *)
            echo "Error: unsupported platform: ${os}-${arch}. Supported binaries: darwin-amd64, darwin-arm64, linux-amd64, linux-arm64." >&2
            exit 1
            ;;
    esac
}

normalize_version() {
    local version="$1"

    version="${version#v}"
    if [[ -z "$version" ]]; then
        echo "Error: release version could not be determined." >&2
        exit 1
    fi

    printf '%s' "$version"
}

resolve_latest_release_url() {
    local latest_url resolved_url

    if [[ -n "${LIUM_INSTALLER_RELEASE_URL:-}" ]]; then
        printf '%s' "${LIUM_INSTALLER_RELEASE_URL}"
        return
    fi

    latest_url="${RELEASE_BASE}/latest"

    if command -v curl >/dev/null 2>&1; then
        curl -fsSLI -o /dev/null -w '%{url_effective}' "$latest_url"
        return
    fi

    if command -v wget >/dev/null 2>&1; then
        resolved_url="$(
            wget -qO /dev/null --server-response "$latest_url" 2>&1 \
                | awk '/^[[:space:]]*Location: / {print $2}' \
                | tr -d '\r' \
                | tail -n 1
        )"
        if [[ -n "$resolved_url" ]]; then
            printf '%s' "$resolved_url"
            return
        fi
    fi

    echo "Error: could not determine the latest Lium release version." >&2
    exit 1
}

resolve_version() {
    local release_url version

    if [[ -n "${LIUM_VERSION:-}" ]]; then
        normalize_version "${LIUM_VERSION}"
        return
    fi

    release_url="$(resolve_latest_release_url)"
    version="${release_url%/}"
    version="${version##*/}"
    version="${version%%\?*}"
    version="${version%%\#*}"
    normalize_version "$version"
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

ensure_managed_cli_path() {
    local cli_path="$1"

    if [[ -e "$cli_path" && ! -L "$cli_path" ]]; then
        echo "Error: ${cli_path} already exists as a regular file." >&2
        echo "Managed symlink installs are only used for fresh installs." >&2
        echo "Leave the current install as-is or remove it before reinstalling." >&2
        exit 1
    fi
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
    local asset_name version release_path temp_dir binary_url checksum_url
    local install_root version_dir cli_path versioned_binary

    asset_name="$(detect_asset_name)"
    version="$(resolve_version)"
    release_path="${RELEASE_BASE}/download/v${version}"
    install_root="$(dirname "$INSTALL_DIR")"
    version_dir="${install_root}/versions/${version}"
    cli_path="${INSTALL_DIR}/lium"
    versioned_binary="${version_dir}/lium"

    temp_dir="$(mktemp -d)"
    trap "rm -rf \"$temp_dir\"" EXIT

    mkdir -p "$INSTALL_DIR" "$version_dir"
    ensure_managed_cli_path "$cli_path"

    binary_url="${release_path}/${asset_name}"
    checksum_url="${release_path}/checksums.txt"

    echo "Downloading ${asset_name} from ${binary_url}"
    download "$binary_url" "$temp_dir/${asset_name}"
    download "$checksum_url" "$temp_dir/checksums.txt"
    verify_checksum "$asset_name" "$temp_dir"

    install -m 0755 "$temp_dir/${asset_name}" "$versioned_binary"
    ln -sfn "../versions/${version}/lium" "$cli_path"
    add_to_path

    echo
    echo "Lium CLI installed to ${INSTALL_DIR}/lium"
    echo "Managed binary location: ${versioned_binary}"
    echo
    echo "Next steps:"
    echo "  1. Restart your shell or run: export PATH=\"${INSTALL_DIR}:\$PATH\""
    echo "  2. Initialize: lium init"
    echo "  3. List GPUs:  lium ls"
    echo

    "$cli_path" --version || true
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
