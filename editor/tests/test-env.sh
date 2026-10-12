# Source this file from bash; extracted native modules must match /usr/bin/python3.
winsnipper_test_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)/.testenv"
export PYTHONPATH="$winsnipper_test_root/usr/lib/python3/dist-packages${PYTHONPATH:+:$PYTHONPATH}"
export GI_TYPELIB_PATH="$winsnipper_test_root/usr/lib/$(dpkg-architecture -qDEB_HOST_MULTIARCH)/girepository-1.0${GI_TYPELIB_PATH:+:$GI_TYPELIB_PATH}"
export LD_LIBRARY_PATH="$winsnipper_test_root/usr/lib/$(dpkg-architecture -qDEB_HOST_MULTIARCH)${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export XDG_DATA_DIRS="$winsnipper_test_root/usr/share:${XDG_DATA_DIRS:-/usr/local/share:/usr/share}"
if [[ -d "$winsnipper_test_root/usr/share/glib-2.0/schemas" ]]; then
    export GSETTINGS_SCHEMA_DIR="$winsnipper_test_root/usr/share/glib-2.0/schemas"
fi
export GSETTINGS_BACKEND=memory
unset winsnipper_test_root
