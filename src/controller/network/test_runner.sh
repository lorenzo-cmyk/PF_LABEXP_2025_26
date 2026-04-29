#!/usr/bin/env bash
#
# test_runner.sh — Run all network tests in sequence
#
# For each test_*.py script: starts the SDN controller, runs the test,
# then stops the controller. Controller and test output are saved to
# separate files under results/.
#
# Usage: sudo ./test_runner.sh [test_name]
#   test_name: optional — run only tests matching this substring (e.g. "vlan", "test_vlan.py")
#              if omitted, all test_*.py files are run.
#

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$(cd "$SCRIPT_DIR/../backend" && pwd)"
NETWORK_DIR="$SCRIPT_DIR"
RESULTS_DIR="$NETWORK_DIR/results"
RUN_DIR="$RESULTS_DIR/run_$(date +%Y%m%d_%H%M%S)"

BACKEND_PYTHON="$BACKEND_DIR/.venv/bin/python"
NETWORK_PYTHON="$NETWORK_DIR/.venv/bin/python"

# ---- Pre-flight checks ---------------------------------------------------
for path in "$BACKEND_PYTHON" "$NETWORK_PYTHON"; do
    if [ ! -f "$path" ]; then
        echo "Error: $path not found" >&2
        exit 1
    fi
done

mkdir -p "$RUN_DIR"

# Gather test files (sorted)
filters=("$@")
tests=()
while IFS= read -r f; do
    test_name="$(basename "$f" .py)"
    if [ "${#filters[@]}" -eq 0 ]; then
        tests+=("$f")
    else
        for filter in "${filters[@]}"; do
            if [[ "$test_name" == *"$filter"* ]] || [[ "$(basename "$f")" == *"$filter"* ]]; then
                tests+=("$f")
                break
            fi
        done
    fi
done < <(find "$NETWORK_DIR" -maxdepth 1 -name 'test_*.py' -type f | sort)

total=${#tests[@]}
if [ "$total" -eq 0 ]; then
    echo "Error: No matching test files found${filters:+ for \"${filters[*]}\"} in $NETWORK_DIR" >&2
    exit 1
fi

if [ "${#filters[@]}" -gt 0 ]; then
    echo "Filter: running ${total} test(s) matching: ${filters[*]}"
fi

# ---- Helpers -------------------------------------------------------------

_kill_controller() {
    local pid=$1 i
    # os-ken handles SIGINT (Ctrl+C), not SIGTERM
    kill -INT "$pid" 2>/dev/null || true
    # Wait up to 5s for graceful shutdown
    for ((i = 0; i < 5; i++)); do
        kill -0 "$pid" 2>/dev/null || return 0
        sleep 1
    done
    # Force kill if still alive
    kill -9 "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
}

_kill_leftover_controllers() {
    # Kill any lingering run.py processes from previous runs
    local pids
    pids="$(pgrep -f "python.*run\.py" 2>/dev/null || true)"
    if [ -n "$pids" ]; then
        echo "$pids" | while read -r pid; do
            _kill_controller "$pid"
        done
    fi
}

_wait_for_port_free() {
    local max_wait=10 i
    for ((i = 0; i < max_wait; i++)); do
        if command -v ss &>/dev/null; then
            ss -tln 2>/dev/null | grep -q ":6653" || return 0
        else
            nc -z 127.0.0.1 6653 2>/dev/null || return 0
        fi
        sleep 1
    done
    return 1
}

_wait_for_controller_ready() {
    local pid=$1 max_wait=20 i
    for ((i = 0; i < max_wait; i++)); do
        kill -0 "$pid" 2>/dev/null || return 1
        if command -v ss &>/dev/null; then
            ss -tln 2>/dev/null | grep -q ":6653" && return 0
        else
            nc -z 127.0.0.1 6653 2>/dev/null && return 0
        fi
        sleep 1
    done
    return 1
}

_start_controller() {
    local log_file=$1
    _wait_for_port_free || {
        echo "Warning: Port 6653 still in use, killing leftovers" >&2
        _kill_leftover_controllers
        _wait_for_port_free || return 1
    }
    
    cd "$BACKEND_DIR"
    "$BACKEND_PYTHON" run.py >"$log_file" 2>&1 &
    CTRL_PID=$!
    
    if ! _wait_for_controller_ready "$CTRL_PID"; then
        echo "Warning: Controller did not start within 20s" >&2
        _kill_controller "$CTRL_PID"
        return 1
    fi
    sleep 1
    return 0
}

_stop_controller() {
    _kill_controller "$CTRL_PID"
    _wait_for_port_free || true
}

_cleanup() {
    if [ -n "${CTRL_PID:-}" ]; then
        _stop_controller 2>/dev/null || true
    fi
    _kill_leftover_controllers 2>/dev/null || true
    sudo mn -c 2>/dev/null || true
}

# ---- Main loop -----------------------------------------------------------

CTRL_PID=""
trap _cleanup EXIT

passed=0
failed=0

for ((i = 0; i < total; i++)); do
    test_file="${tests[$i]}"
    test_name="$(basename "$test_file" .py)"
    test_label="$([ "$i" -lt 9 ] && echo "0$((i+1))" || echo "$((i+1))")__$test_name"
    
    echo ""
    echo "========================================="
    echo "  [$((i+1))/$total]  $test_name"
    echo "========================================="
    echo ""
    
    test_dir="$RUN_DIR/$test_label"
    mkdir -p "$test_dir"
    ctrl_log="$test_dir/controller.log"
    test_log="$test_dir/test.log"
    
    # Clean up any leftover Mininet state
    sudo mn -c 2>/dev/null || true
    
    # Start the controller
    if ! _start_controller "$ctrl_log"; then
        echo "FAIL  — controller failed to start, skipping test" >&2
        echo "FAIL  — controller failed to start" >> "$test_log"
        failed=$((failed + 1))
        continue
    fi
    
    # Run the test
    cd "$NETWORK_DIR"
    "$NETWORK_PYTHON" "$test_file" 2>&1 | tee "$test_log"
    rc=${PIPESTATUS[0]}
    
    # Stop the controller
    _stop_controller
    
    # Track pass/fail from test process exit code
    if [ "$rc" -eq 0 ]; then
        passed=$((passed + 1))
    else
        failed=$((failed + 1))
    fi
done

trap - EXIT
_cleanup

# ---- Summary -------------------------------------------------------------

echo ""
echo "========================================="
echo "  Done — $passed/$total passed, $failed failed"
echo "  Results: $RUN_DIR"
echo "========================================="

[ "$failed" -eq 0 ]
