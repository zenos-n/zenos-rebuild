#!/usr/bin/env bash

set -euo pipefail
umask 077

# --- Configuration & Colors ---
BLUE='\033[0;34m'
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[0;33m'
NC='\033[0m'

SESSION_NAME="zenos-rebuild"
DEFAULT_FLAKE_PATH="/Config/ZenOS"
STATE_HOME="${XDG_STATE_HOME:-$HOME/.local/state}"
STATE_FILE="$STATE_HOME/zenos/rebuild-root"

# --- Notification Helper ---
notify() {
    local title="$1"
    local message="$2"
    local urgency="$3"
    
    # Send desktop notification if available
    if command -v notify-send &> /dev/null; then
        notify-send -u "$urgency" -i "zenos-symbolic" -a "ZenOS Rebuild" "$title" "$message" 2>/dev/null || true
    fi
}

# --- 1. Tmux Safety Check ---
# If we are not inside tmux, re-launch self inside tmux
if [ -z "${TMUX:-}" ]; then
    echo -e "${YELLOW}[Safety] Not in Tmux. Launching safe-mode session...${NC}"
    
    if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
        exec tmux attach-session -t "$SESSION_NAME"
    else
        # Create detached session first to configure it
        tmux new-session -d -s "$SESSION_NAME"
        
        # Enable Mouse (Scrolling) and increase History Limit
        tmux set-option -t "$SESSION_NAME" mouse on
        tmux set-option -t "$SESSION_NAME" history-limit 50000
        
        # Send the command to the session
        # We pass "$@" to ensure flags like -r or -l are preserved inside the session
        printf -v rebuild_command '%q ' bash "$0" "$@"
        tmux send-keys -t "$SESSION_NAME" "${rebuild_command}; echo; echo 'Press Enter to exit...'; read -r; exit" C-m
        
        # Attach to the configured session
        exec tmux attach-session -t "$SESSION_NAME"
    fi
fi

# --- 2. Resource Optimization ---
# Calculate cores to prevent UI freeze during heavy compiles
TOTAL_CORES=$(nproc)
# Reserve 2 cores for system responsiveness, minimum 1
MAX_JOBS=$((TOTAL_CORES - 2))
[ $MAX_JOBS -lt 1 ] && MAX_JOBS=1
# Limit per-job cores to keep context switching low
CORES_PER_JOB=2

# --- 3. Host & Flake Detection ---
TARGET_HOST=$(hostname)
FLAKE_PATH=""
FORCE_HOST=""
AUTO_REBOOT=false
AUTO_LOGOUT=false
SHOW_GENERATED=false

# Argument Parsing
while [[ $# -gt 0 ]]; do
    key="$1"
    case $key in
        -h|--host)
        [ "$#" -ge 2 ] || { echo 'Missing host name' >&2; exit 2; }
        FORCE_HOST="$2"
        shift 2
        ;;
        -d|--dir)
        [ "$#" -ge 2 ] || { echo 'Missing configuration directory' >&2; exit 2; }
        FLAKE_PATH="$2"
        shift 2
        ;;
        -r|--reboot)
        AUTO_REBOOT=true
        shift
        ;;
        -l|--logout)
        AUTO_LOGOUT=true
        shift
        ;;
        --show-generated)
        SHOW_GENERATED=true
        shift
        ;;
        *)
        echo "Unknown argument: $key" >&2
        exit 2
        ;;
    esac
done

if [ -n "$FORCE_HOST" ]; then
    TARGET_HOST="$FORCE_HOST"
fi

# Locate Flake
if [ -n "$FLAKE_PATH" ]; then
    # User specified path
    if [ ! -f "$FLAKE_PATH/flake.nix" ]; then
        echo -e "${RED}[!] Error: No flake.nix found at $FLAKE_PATH${NC}"
        notify "Error" "No flake.nix found at $FLAKE_PATH" "critical"
        exit 1
    fi
elif [ -f "$PWD/flake.nix" ]; then
    FLAKE_PATH="$PWD"
elif [ -f "$STATE_FILE" ] && [ -f "$(cat "$STATE_FILE")/flake.nix" ]; then
    FLAKE_PATH="$(cat "$STATE_FILE")"
elif [ -f "$DEFAULT_FLAKE_PATH/flake.nix" ]; then
    FLAKE_PATH="$DEFAULT_FLAKE_PATH"
elif [ -f "/etc/nixos/flake.nix" ]; then
    FLAKE_PATH="/etc/nixos"
else
    echo -e "${RED}[!] Error: Could not locate flake.nix${NC}"
    notify "Error" "Could not locate a flake in PWD, the last location, $DEFAULT_FLAKE_PATH, or /etc/nixos." "critical"
    exit 1
fi

resolved_path="$(realpath "$FLAKE_PATH")"
FLAKE_PATH="$resolved_path"
mkdir -p "$(dirname "$STATE_FILE")"
printf '%s\n' "$FLAKE_PATH" > "$STATE_FILE.tmp"
mv "$STATE_FILE.tmp" "$STATE_FILE"

HOST_DIR="$FLAKE_PATH/hosts/$TARGET_HOST"
HOST_ZCFG="$HOST_DIR/host.zcfg"
if [ ! -f "$HOST_ZCFG" ]; then
    echo -e "${RED}[!] Error: No host.zcfg found for $TARGET_HOST${NC}"
    notify "Error" "No host.zcfg found for $TARGET_HOST." "critical"
    exit 1
fi

FLAKE_URI="${FLAKE_PATH}#${TARGET_HOST}"
SNAPSHOT_HELPER="$(dirname "$(realpath "$0")")/snapshot.py"
LOG_DIR="$STATE_HOME/zenos/rebuild-logs"
mkdir -p "$LOG_DIR"
LOG_FILE=$(mktemp "$LOG_DIR/rebuild-$(date +%Y%m%d-%H%M%S)-XXXXXX.log")
echo -e "${BLUE}[Log] $LOG_FILE${NC}"
snapshot_options=()
[ "$SHOW_GENERATED" = false ] || snapshot_options+=(--show-generated)

# --- 4. Execution ---
notify "Rebuild Started" "Target: $TARGET_HOST" "normal"

echo -e "${BLUE}========================================${NC}"
echo -e "  ${GREEN}ZenOS Rebuild${NC}"
echo -e "  Target: ${YELLOW}${FLAKE_URI}${NC}"
echo -e "  Optimization: ${YELLOW}-j${MAX_JOBS} -c${CORES_PER_JOB}${NC}"
if [ "$SHOW_GENERATED" = true ]; then
    echo -e "  Mode: ${YELLOW}GENERATED VIEW (NO SWITCH)${NC}"
elif [ "$AUTO_REBOOT" = true ]; then
    echo -e "  Post-Action: ${RED}AUTO-REBOOT ENABLED${NC}"
elif [ "$AUTO_LOGOUT" = true ]; then
    echo -e "  Post-Action: ${YELLOW}AUTO-LOGOUT ENABLED${NC}"
fi
echo -e "${BLUE}========================================${NC}"

# Temporarily disable 'set -e' to capture exit code manually
set +e

# The privileged helper owns a private snapshot for the entire Nix invocation.
# Compilation is the flake's job, using its pinned current compiler.
sudo python3 "$SNAPSHOT_HELPER" "$(realpath "$FLAKE_PATH")" "$TARGET_HOST" "${snapshot_options[@]}" \
    --show-trace \
    --print-build-logs \
    --option max-jobs "$MAX_JOBS" \
    --option cores "$CORES_PER_JOB" \
    --option accept-flake-config true 2>&1 | tee "$LOG_FILE"

EXIT_CODE=${PIPESTATUS[0]}

# Re-enable strict mode
set -e

echo -e "${BLUE}========================================${NC}"

if [ "$EXIT_CODE" -eq 0 ]; then
    if [ "$SHOW_GENERATED" = true ]; then
        notify "Generated Configuration" "Store output is listed in $LOG_FILE" "normal"
        exit 0
    fi
    notify "Rebuild Complete" "System switched successfully." "normal"
    echo -e "${GREEN}SUCCESS: System updated.${NC}"

    # Handle Automation Flags (Only on Success)
    if [ "$AUTO_REBOOT" = true ]; then
        echo -e "${RED}[!] REBOOTING IN 3 SECONDS... (Ctrl+C to cancel)${NC}"
        notify "System" "Rebooting in 3 seconds..." "critical"
        sleep 3
        sudo reboot
    elif [ "$AUTO_LOGOUT" = true ]; then
        echo -e "${YELLOW}[!] LOGGING OUT IN 3 SECONDS... (Ctrl+C to cancel)${NC}"
        notify "System" "Logging out in 3 seconds..." "critical"
        sleep 3
        # Attempt to terminate the current session gracefully via systemd
        loginctl terminate-session "${XDG_SESSION_ID:-self}"
    fi

elif [ "$EXIT_CODE" -eq 130 ]; then
    # 130 is the standard exit code for SIGINT (Ctrl+C)
    notify "Rebuild Interrupted" "Operation cancelled by user." "low"
    echo -e "${YELLOW}INFO: Rebuild cancelled by user (Exit Code: 130).${NC}"
    exit 0
else
    notify "Rebuild Failed" "Check the terminal logs for details." "critical"
    echo -e "${RED}FAILURE: Rebuild encountered errors (Exit Code: $EXIT_CODE).${NC}"
    exit "$EXIT_CODE"
fi
