#!/usr/bin/env bash
# Exercise GTK against a real Wayland compositor without a desktop session.
set -euo pipefail
runtime_dir=$(mktemp -d)
chmod 700 "$runtime_dir"
export XDG_RUNTIME_DIR="$runtime_dir"
export WAYLAND_DISPLAY=winsnipper-test
export GDK_BACKEND=wayland GSK_RENDERER=cairo GTK_A11Y=none WINSNIPPER_GTK_TESTS=1 GSETTINGS_BACKEND=memory
weston_pid=
cleanup() {
    local status=$?
    if (( status != 0 )); then cat "$runtime_dir/weston.log" >&2 || true; fi
    if [[ -n "$weston_pid" ]]; then
        kill "$weston_pid" 2>/dev/null || true
        wait "$weston_pid" 2>/dev/null || true
    fi
    rm -rf -- "$runtime_dir"
    exit "$status"
}
trap cleanup EXIT
weston_version=$(weston --version)
backend=(--backend=headless-backend.so --use-pixman)
if [[ "$weston_version" =~ ([0-9]+)\. ]] && (( BASH_REMATCH[1] >= 14 )); then
    backend=(--backend=headless --renderer=pixman)
fi
weston "${backend[@]}" --shell=kiosk-shell.so --socket="$WAYLAND_DISPLAY" \
    --width=1280 --height=800 --idle-time=0 --no-config --log="$runtime_dir/weston.log" &
weston_pid=$!
for (( attempt=0; attempt<50; attempt++ )); do
    [[ -S "$runtime_dir/$WAYLAND_DISPLAY" ]] && break
    kill -0 "$weston_pid"
    sleep 0.1
done
[[ -S "$runtime_dir/$WAYLAND_DISPLAY" ]]
dbus-run-session -- /usr/bin/python3 -m unittest discover -s editor/tests
dbus-run-session -- /usr/bin/python3 editor/tests/smoke_service.py
