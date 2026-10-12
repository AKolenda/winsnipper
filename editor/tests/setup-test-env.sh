#!/usr/bin/env bash
# Extract distro-matched bindings/libraries locally. No package installation.
set -euo pipefail
repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
test_dir="$repo_dir/.testenv"
mkdir -p "$test_dir/debs"
packages=(python3-cairo)
if [[ "${1:-}" == --gtk ]]; then
    packages+=(python3-gi python3-gi-cairo gir1.2-gtk-4.0 libgtk-4-1 libgtk-4-common libgtk-4-bin libglib2.0-bin)
fi
# Walk runtime dependencies, retaining only packages missing on this host (plus
# the requested bindings). apt-cache uses local metadata and apt-get downloads
# archives; dpkg -x never changes the system package database.
mapfile -t downloads < <(python3 - "${packages[@]}" <<'PY'
import subprocess
import sys
roots = sys.argv[1:]
output = subprocess.check_output([
    "apt-cache", "depends", "--recurse", "--no-recommends", "--no-suggests",
    "--no-conflicts", "--no-breaks", "--no-replaces", "--no-enhances", *roots], text=True)
packages = {line for line in output.splitlines() if line and not line[0].isspace()
            and not line.startswith("<")}
for package in sorted(packages | set(roots)):
    result = subprocess.run(["dpkg-query", "-W", "-f=${db:Status-Status}", package],
                            capture_output=True, text=True)
    if package in roots or result.stdout != "installed":
        print(package)
PY
)
(cd "$test_dir/debs" && apt-get download "${downloads[@]}")
for archive in "$test_dir"/debs/*.deb; do
    dpkg -x "$archive" "$test_dir"
done
if [[ -d "$test_dir/usr/share/glib-2.0/schemas" ]]; then
    schema_compiler="$(command -v glib-compile-schemas || true)"
    multiarch="$(dpkg-architecture -qDEB_HOST_MULTIARCH)"
    if [[ -z "$schema_compiler" ]]; then
        for candidate in "/usr/lib/$multiarch/glib-2.0/glib-compile-schemas" \
                         "$test_dir/usr/bin/glib-compile-schemas" \
                         "$test_dir/usr/lib/$multiarch/glib-2.0/glib-compile-schemas"; do
            if [[ -x "$candidate" ]]; then
                schema_compiler="$candidate"
                break
            fi
        done
    fi
    "$schema_compiler" "$test_dir/usr/share/glib-2.0/schemas"
fi
printf 'Local environment ready. Run:\n  source editor/tests/test-env.sh\n  /usr/bin/python3 -m unittest discover -s editor/tests\n'
