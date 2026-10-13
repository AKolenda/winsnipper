#!/usr/bin/env python3
"""Filesystem operations shared by the per-user installer and uninstaller."""
import argparse
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile

UUID = 'winsnipper@winsnipper.local'
APP_ID = 'io.github.winsnipper.Editor'


def data_home():
    data = Path(os.environ.get('XDG_DATA_HOME') or Path.home().absolute() / '.local/share')
    if not data.is_absolute():
        raise ValueError('XDG_DATA_HOME must be an absolute path')
    return data


def validate_paths(paths):
    for path in paths:
        if any(ord(char) < 32 or ord(char) == 127 for char in str(path)):
            raise ValueError('Install paths cannot contain control characters')
        # Refuse redirected managed files and parents before changing anything.
        # This also prevents uninstall from removing files through a redirected parent.
        for component in (path, *path.parents):
            if component.is_symlink():
                raise ValueError(f'Refusing symbolic link in install path: {component}')


def check_setup_paths():
    # Package setup changes only state here, not source-install destinations.
    data = data_home() / 'winsnipper'
    validate_paths((data, data / 'keybinding-backup.json', data / 'setup-done'))


def user_paths():
    home = Path.home().absolute()
    data = data_home()
    paths = {
        'data': data / 'winsnipper',
        'extension': data / 'gnome-shell/extensions' / UUID,
        'editor': data / 'winsnipper/editor',
        'tools': data / 'winsnipper/tools',
        'launcher': home / '.local/bin/winsnipper-editor',
        'desktop': data / 'applications' / (APP_ID + '.desktop'),
        'service': data / 'dbus-1/services' / (APP_ID + '.service'),
    }
    validate_paths(paths.values())
    return paths


def exec_quote(path):
    return '"' + str(path).replace('\\', '\\\\').replace('"', '\\"').replace('`', '\\`').replace('$', '\\$') + '"'


def desktop_quote(path):
    return exec_quote(path).replace('\\', '\\\\').replace('%', '%%')


def desktop_entry(launcher):
    return ('[Desktop Entry]\nType=Application\nName=WinSnipper Editor\n'
            'Exec=' + desktop_quote(launcher) + ' %U\nIcon=accessories-screenshot\n'
            'DBusActivatable=true\nNoDisplay=true\nTerminal=false\nMimeType=image/png;\n')


def write_file(path, text, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.winsnipper-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(text)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


def remove_path(path):
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def replace_tree(source, destination):
    """Build beside the destination; never copy through an old tree's symlinks."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.winsnipper-', dir=destination.parent))
    previous = stage / 'previous'
    try:
        replacement = stage / 'replacement'
        shutil.copytree(source, replacement, ignore=shutil.ignore_patterns('tests', '__pycache__', '*.pyc'))
        if destination.exists():
            destination.rename(previous)
        try:
            replacement.rename(destination)
        except OSError:
            if previous.exists():
                previous.rename(destination)
            raise
    finally:
        shutil.rmtree(stage)


def install(root, portable):
    paths = user_paths()
    # Compile and validate the whole payload before replacing installed files.
    with tempfile.TemporaryDirectory(prefix='winsnipper-install-') as directory:
        stage = Path(directory)
        for name in ('editor', 'tools') if portable else ('editor', 'tools', 'extension'):
            source = root / name
            if any(path.is_symlink() for path in source.rglob('*')):
                raise ValueError(f'Symbolic links are not supported in the {name} payload')
            shutil.copytree(source, stage / name, ignore=shutil.ignore_patterns('tests', '__pycache__', '*.pyc'))
        if not portable:
            subprocess.run(['glib-compile-schemas', '--strict', str(stage / 'extension/schemas')], check=True)
        for name in ('editor', 'tools') if portable else ('editor', 'tools', 'extension'):
            replace_tree(stage / name, paths[name])
    command = '#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' ' + shlex.quote(str(paths['editor'] / 'winsnipper_editor.py')) + ' "$@"\n'
    write_file(paths['launcher'], command, 0o755)
    write_file(paths['desktop'], desktop_entry(paths['launcher']))
    write_file(paths['service'], '[D-BUS Service]\nName=' + APP_ID + '\nExec=' + exec_quote(paths['launcher']) + ' --gapplication-service\n')


def uninstall():
    paths = user_paths()
    for key in ('extension', 'editor', 'tools', 'launcher', 'desktop', 'service'):
        remove_path(paths[key])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('check', 'check-setup', 'install', 'uninstall'))
    parser.add_argument('--portable', action='store_true')
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    try:
        if args.command == 'check':
            user_paths()
        elif args.command == 'check-setup':
            check_setup_paths()
        elif args.command == 'install':
            install(args.root, args.portable)
        else:
            uninstall()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f'Install operation failed: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
