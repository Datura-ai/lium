#!/bin/bash
# Lium Mine Installer & Runner
# curl -fsSL https://raw.githubusercontent.com/Datura-ai/lium-cli/main/mine.sh | bash -s -- -k <HOTKEY>

# If running from pipe, save to temp file and re-execute
# (Preserves interactive capabilities if needed)
if [[ ! -t 0 ]] && [[ -z "$LIUM_INSTALLER_REEXEC" ]]; then
    TEMP_SCRIPT=$(mktemp) || { echo "Failed to create temp file"; exit 1; }
    trap 'rm -f "$TEMP_SCRIPT"' EXIT INT TERM
    cat > "$TEMP_SCRIPT" || { echo "Failed to write installer script"; exit 1; }
    chmod 700 "$TEMP_SCRIPT" || { echo "Failed to make script executable"; exit 1; }
    export LIUM_INSTALLER_REEXEC=1

    if { true < /dev/tty; } 2>/dev/null; then
        bash "$TEMP_SCRIPT" "$@" < /dev/tty
    else
        bash "$TEMP_SCRIPT" "$@"
    fi
    exit $?
fi

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
GRAY='\033[0;90m'
NC='\033[0m'

log_ok() { echo -e "${GREEN}✓${NC} $1"; }
log_err() { echo -e "${RED}✗${NC} $1"; }

LIUM_ARGS=("$@")

command_exists() { command -v "$1" >/dev/null 2>&1; }

# Try to find lium in path or common locations
find_lium() {
    command_exists lium && echo "lium" && return 0
    [[ -x "$HOME/.local/bin/lium" ]] && echo "$HOME/.local/bin/lium" && return 0
    [[ -x "/usr/local/bin/lium" ]] && echo "/usr/local/bin/lium" && return 0
    return 1
}

# Show elapsed time while waiting for background process
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

install_lium() {
    # Simple pip install
    run_with_timer "Installing lium-cli" python3 -m pip install --user --upgrade lium.io

    # CRITICAL: Reset bash hash table to find the new command
    hash -r
}

ensure_path() {
    if [[ ":$PATH:" != *":$HOME/.local/bin:"* ]]; then
        export PATH="$HOME/.local/bin:$PATH"
    fi
}

main() {
    echo -e "${BLUE}══════════════════════════════════════${NC}"
    echo -e "${BLUE}     Lium Mine Installer & Runner${NC}"
    echo -e "${BLUE}══════════════════════════════════════${NC}"
    echo ""

    # Basic Pre-flight check
    if ! command_exists python3; then
        log_err "Python3 is not installed. Please install Python3 and try again."
        exit 1
    fi

    ensure_path

    if LIUM_BIN=$(find_lium); then
        log_ok "lium-cli found"
    else
        echo -e "${BLUE}▸${NC} lium-cli not found, installing via pip..."

        install_lium

        # CRITICAL: Safe assignment with error handling
        # This prevents 'set -e' from killing the script if finding the binary fails immediately
        LIUM_BIN=$(find_lium) || { log_err "Critical: Could not locate lium binary after install. Please check your PATH."; exit 1; }
    fi

    echo ""

    # Final sanity check
    if [[ -z "$LIUM_BIN" ]]; then
        log_err "Binary path is empty."
        exit 1
    fi

    # Execute lium mine
    if [[ ${#LIUM_ARGS[@]} -gt 0 ]]; then
        exec "$LIUM_BIN" mine "${LIUM_ARGS[@]}"
    else
        exec "$LIUM_BIN" mine
    fi
}

main
