#!/bin/bash
# Lium Mine Installer & Runner
# One-line: curl -fsSL https://raw.githubusercontent.com/Datura-ai/lium-cli/main/install-mine.sh | bash
# With hotkey: curl -fsSL ... | bash -s -- -k <YOUR_HOTKEY>

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
GRAY='\033[0;90m'
NC='\033[0m'
CLEAR_LINE='\033[2K'

# Spinner characters
SPINNER='⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'

log_info() { echo -e "${BLUE}[INFO]${NC} $1"; }
log_success() { echo -e "${GREEN}[OK]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# Run command with spinner, hiding output
run_with_spinner() {
    local msg="$1"
    shift
    local cmd="$@"
    local log_file=$(mktemp)
    local pid
    local i=0

    # Start command in background
    eval "$cmd" > "$log_file" 2>&1 &
    pid=$!

    # Show spinner while command runs
    while kill -0 $pid 2>/dev/null; do
        i=$(( (i + 1) % ${#SPINNER} ))
        printf "\r${CLEAR_LINE}${BLUE}${SPINNER:$i:1}${NC} %s..." "$msg"
        sleep 0.1
    done

    # Get exit code
    wait $pid
    local exit_code=$?

    if [[ $exit_code -eq 0 ]]; then
        printf "\r${CLEAR_LINE}${GREEN}✓${NC} %s\n" "$msg"
    else
        printf "\r${CLEAR_LINE}${RED}✗${NC} %s\n" "$msg"
        echo -e "${GRAY}--- Error log ---${NC}"
        tail -20 "$log_file"
        rm -f "$log_file"
        exit $exit_code
    fi

    rm -f "$log_file"
}

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

# Find lium binary in common locations
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

# Install Python3 if missing
ensure_python() {
    if command_exists python3; then
        return 0
    fi

    detect_os

    case "$OS" in
        ubuntu|debian)
            run_with_spinner "Installing Python3" "sudo apt-get update -qq && sudo apt-get install -y python3 python3-pip python3-venv"
            ;;
        centos|rhel|fedora|rocky|almalinux)
            if command_exists dnf; then
                run_with_spinner "Installing Python3" "sudo dnf install -y python3 python3-pip"
            else
                run_with_spinner "Installing Python3" "sudo yum install -y python3 python3-pip"
            fi
            ;;
        arch|manjaro)
            run_with_spinner "Installing Python3" "sudo pacman -Sy --noconfirm python python-pip"
            ;;
        *)
            log_error "Unsupported OS: $OS. Please install Python3 manually."
            exit 1
            ;;
    esac
}

# Install pip if missing
ensure_pip() {
    if python3 -m pip --version >/dev/null 2>&1; then
        return 0
    fi

    detect_os

    case "$OS" in
        ubuntu|debian)
            run_with_spinner "Installing pip" "sudo apt-get install -y python3-pip"
            ;;
        centos|rhel|fedora|rocky|almalinux)
            if command_exists dnf; then
                run_with_spinner "Installing pip" "sudo dnf install -y python3-pip"
            else
                run_with_spinner "Installing pip" "sudo yum install -y python3-pip"
            fi
            ;;
        *)
            run_with_spinner "Installing pip" "curl -sS https://bootstrap.pypa.io/get-pip.py | python3"
            ;;
    esac
}

# Install lium-cli via pip
install_lium() {
    run_with_spinner "Installing lium-cli" "python3 -m pip install --user --upgrade lium-cli"

    if ! LIUM_BIN=$(find_lium); then
        log_error "Installation failed - lium not found"
        exit 1
    fi
}

# Add ~/.local/bin to PATH for current session
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

    # Check if lium already installed
    if LIUM_BIN=$(find_lium); then
        log_success "lium-cli found: $LIUM_BIN"
    else
        log_info "Installing lium-cli..."
        ensure_python
        ensure_pip
        install_lium
        LIUM_BIN=$(find_lium)
        log_success "lium-cli ready: $LIUM_BIN"
    fi

    echo ""

    if [[ -n "$LIUM_ARGS" ]]; then
        exec "$LIUM_BIN" mine $LIUM_ARGS
    else
        exec "$LIUM_BIN" mine
    fi
}

main
