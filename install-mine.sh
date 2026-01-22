#!/bin/bash
# Lium Mine Installer & Runner
# curl -fsSL https://raw.githubusercontent.com/Datura-ai/lium-cli/main/install-mine.sh | bash -s -- -k <HOTKEY>

# If running from pipe, save to temp file and re-execute
if [[ ! -t 0 ]] && [[ -z "$LIUM_INSTALLER_REEXEC" ]]; then
    TEMP_SCRIPT=$(mktemp) || { echo "Failed to create temp file"; exit 1; }
    cat > "$TEMP_SCRIPT"
    chmod +x "$TEMP_SCRIPT"
    export LIUM_INSTALLER_REEXEC=1

    # Re-execute with tty if available, otherwise without interactive support
    if [[ -e /dev/tty ]]; then
        bash "$TEMP_SCRIPT" "$@" < /dev/tty
    else
        bash "$TEMP_SCRIPT" "$@"
    fi
    exit_code=$?
    rm -f "$TEMP_SCRIPT"
    exit $exit_code
fi

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
GRAY='\033[0;90m'
NC='\033[0m'

log_ok() { echo -e "${GREEN}✓${NC} $1"; }
log_err() { echo -e "${RED}✗${NC} $1"; }

# Store arguments as array to preserve spaces/special chars
LIUM_ARGS=("$@")

detect_os() {
    if [[ -f /etc/os-release ]]; then
        . /etc/os-release
        OS=$ID
    else
        OS=$(uname -s | tr '[:upper:]' '[:lower:]')
    fi
}

command_exists() { command -v "$1" >/dev/null 2>&1; }

find_lium() {
    command_exists lium && echo "lium" && return 0
    [[ -x "$HOME/.local/bin/lium" ]] && echo "$HOME/.local/bin/lium" && return 0
    [[ -x "/usr/local/bin/lium" ]] && echo "/usr/local/bin/lium" && return 0
    return 1
}

show_elapsed() {
    local start=$1 msg=$2 pid=$3
    while kill -0 "$pid" 2>/dev/null; do
        local elapsed=$(($(date +%s) - start))
        printf "\r${BLUE}▸${NC} %s... ${GRAY}%ds${NC}  " "$msg" "$elapsed"
        sleep 1
    done
    wait "$pid" 2>/dev/null
    return $?
}

run_with_timer() {
    local msg="$1"
    shift
    local start=$(date +%s)
    local log_file
    log_file=$(mktemp) || { log_err "Failed to create log file"; exit 1; }

    "$@" > "$log_file" 2>&1 &
    local pid=$!

    show_elapsed "$start" "$msg" "$pid"
    local exit_code=$?

    local elapsed=$(($(date +%s) - start))
    if [[ $exit_code -eq 0 ]]; then
        printf "\r${GREEN}✓${NC} %s ${GRAY}(%ds)${NC}      \n" "$msg" "$elapsed"
    else
        printf "\r${RED}✗${NC} %s ${GRAY}(%ds)${NC}      \n" "$msg" "$elapsed"
        echo -e "${GRAY}--- Last 15 lines of log ---${NC}"
        tail -15 "$log_file"
        rm -f "$log_file"
        exit $exit_code
    fi
    rm -f "$log_file"
}

ensure_python() {
    command_exists python3 && return 0
    detect_os
    case "$OS" in
        ubuntu|debian)
            run_with_timer "Updating apt" sudo apt-get update -qq
            run_with_timer "Installing Python3" sudo apt-get install -y python3 python3-pip python3-venv
            ;;
        centos|rhel|fedora|rocky|almalinux)
            local pkg_mgr
            pkg_mgr=$(command_exists dnf && echo dnf || echo yum)
            run_with_timer "Installing Python3" sudo "$pkg_mgr" install -y python3 python3-pip
            ;;
        *) log_err "Unsupported OS: $OS"; exit 1 ;;
    esac
}

ensure_pip() {
    python3 -m pip --version >/dev/null 2>&1 && return 0
    detect_os
    case "$OS" in
        ubuntu|debian)
            run_with_timer "Installing pip" sudo apt-get install -y python3-pip
            ;;
        *)
            run_with_timer "Installing pip" bash -c "curl -sS https://bootstrap.pypa.io/get-pip.py | python3"
            ;;
    esac
}

install_lium() {
    run_with_timer "Installing lium-cli" python3 -m pip install --user --upgrade lium-cli
    find_lium >/dev/null || { log_err "Installation failed"; exit 1; }
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
        log_ok "lium-cli found"
    else
        ensure_python
        ensure_pip
        install_lium
        LIUM_BIN=$(find_lium)
    fi

    echo ""

    # Run lium mine with preserved arguments
    if [[ ${#LIUM_ARGS[@]} -gt 0 ]]; then
        exec "$LIUM_BIN" mine "${LIUM_ARGS[@]}"
    else
        exec "$LIUM_BIN" mine
    fi
}

main
