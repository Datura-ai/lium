#!/bin/bash
# Lium Mine Installer & Runner
# One-line: curl -fsSL https://raw.githubusercontent.com/Datura-ai/lium-cli/main/install-mine.sh | bash
# With hotkey: curl -fsSL ... | bash -s -- -k <YOUR_HOTKEY>

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
GRAY='\033[0;90m'
NC='\033[0m'

log_info() { echo -e "${BLUE}▸${NC} $1"; }
log_success() { echo -e "${GREEN}✓${NC} $1"; }
log_error() { echo -e "${RED}✗${NC} $1"; }

# Pass all args through to lium mine
LIUM_ARGS="$@"

# Detect OS for package manager
detect_os() {
    if [[ -f /etc/os-release ]]; then
        . /etc/os-release
        OS=$ID
    elif [[ -f /etc/debian_version ]]; then
        OS="debian"
    elif [[ -f /etc/redhat-release ]]; then
        OS="rhel"
    else
        OS=$(uname -s | tr '[:upper:]' '[:lower:]')
    fi
}

command_exists() {
    command -v "$1" >/dev/null 2>&1
}

find_lium() {
    if command_exists lium; then
        echo "lium"
        return 0
    elif [[ -x "$HOME/.local/bin/lium" ]]; then
        echo "$HOME/.local/bin/lium"
        return 0
    elif [[ -x "/usr/local/bin/lium" ]]; then
        echo "/usr/local/bin/lium"
        return 0
    fi
    return 1
}

ensure_python() {
    command_exists python3 && return 0

    log_info "Installing Python3..."
    detect_os

    case "$OS" in
        ubuntu|debian)
            sudo apt-get update -qq >/dev/null 2>&1
            sudo apt-get install -y python3 python3-pip python3-venv >/dev/null 2>&1
            ;;
        centos|rhel|fedora|rocky|almalinux)
            if command_exists dnf; then
                sudo dnf install -y python3 python3-pip >/dev/null 2>&1
            else
                sudo yum install -y python3 python3-pip >/dev/null 2>&1
            fi
            ;;
        arch|manjaro)
            sudo pacman -Sy --noconfirm python python-pip >/dev/null 2>&1
            ;;
        *)
            log_error "Unsupported OS: $OS"
            exit 1
            ;;
    esac
    log_success "Python3 installed"
}

ensure_pip() {
    python3 -m pip --version >/dev/null 2>&1 && return 0

    log_info "Installing pip..."
    detect_os

    case "$OS" in
        ubuntu|debian)
            sudo apt-get install -y python3-pip >/dev/null 2>&1
            ;;
        centos|rhel|fedora|rocky|almalinux)
            if command_exists dnf; then
                sudo dnf install -y python3-pip >/dev/null 2>&1
            else
                sudo yum install -y python3-pip >/dev/null 2>&1
            fi
            ;;
        *)
            curl -sS https://bootstrap.pypa.io/get-pip.py | python3 >/dev/null 2>&1
            ;;
    esac
    log_success "pip installed"
}

install_lium() {
    log_info "Installing lium-cli..."
    python3 -m pip install --user --upgrade lium-cli >/dev/null 2>&1

    if find_lium >/dev/null; then
        log_success "lium-cli installed"
    else
        log_error "Installation failed"
        exit 1
    fi
}

ensure_path() {
    if [[ ":$PATH:" != *":$HOME/.local/bin:"* ]]; then
        export PATH="$HOME/.local/bin:$PATH"
    fi
}

main() {
    echo ""
    echo -e "${BLUE}══════════════════════════════════════${NC}"
    echo -e "${BLUE}     Lium Mine Installer & Runner${NC}"
    echo -e "${BLUE}══════════════════════════════════════${NC}"
    echo ""

    ensure_path

    if LIUM_BIN=$(find_lium); then
        log_success "lium-cli found"
    else
        ensure_python
        ensure_pip
        install_lium
        LIUM_BIN=$(find_lium)
    fi

    echo ""

    # Restore stdin from terminal (for interactive prompts in lium mine)
    exec < /dev/tty

    if [[ -n "$LIUM_ARGS" ]]; then
        exec "$LIUM_BIN" mine $LIUM_ARGS
    else
        exec "$LIUM_BIN" mine
    fi
}

main
