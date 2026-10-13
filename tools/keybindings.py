#!/usr/bin/env python3
"""Back up only Super+Shift+S conflicts and restore their exact GVariant values."""
import argparse
import ast
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile

SCHEMAS = (
    'org.gnome.shell.keybindings',
    'org.gnome.desktop.wm.keybindings',
    'org.gnome.mutter.keybindings',
    'org.gnome.mutter.wayland.keybindings',
    'org.gnome.settings-daemon.plugins.media-keys',
)
MEDIA_SCHEMA = SCHEMAS[-1]
CUSTOM_SCHEMA = MEDIA_SCHEMA + '.custom-keybinding'
EXTENSION_UUID = 'winsnipper@winsnipper.local'
DEFAULT_BACKUP = Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local/share') / 'winsnipper/keybinding-backup.json'
MAX_BACKUP_BYTES = 1024 * 1024


def normalize_accelerator(value):
    """Return (modifier set, casefolded key), or None for malformed input."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if value.startswith('<'):
        match = re.fullmatch(r'((?:<[^<>]+>)+)([^<>+]+)', value)
        if not match:
            return None
        modifiers = re.findall(r'<([^<>]+)>', match[1])
        key = match[2]
    else:
        parts = value.split('+')
        modifiers, key = parts[:-1], parts[-1]
    aliases = {'mod4': 'super', 'primary': 'control', 'ctrl': 'control'}
    modifiers = [aliases.get(item.casefold(), item.casefold()) for item in modifiers]
    if not key or any(not item for item in modifiers):
        return None
    return frozenset(modifiers), key.casefold()


def is_snip_accelerator(value):
    return normalize_accelerator(value) == (frozenset({'shift', 'super'}), 's')


def parse_variant(value):
    # gsettings prints type annotations for empty arrays and sometimes strings.
    value = re.sub(r'^@[a-z{}()]+\s+', '', value.strip())
    try:
        return ast.literal_eval(value)
    except (ValueError, SyntaxError):
        return None


def string_variant(value):
    # GVariant string literals accept these standard backslash escapes.
    return "'" + value.replace('\\', '\\\\').replace("'", "\\'").replace('\n', '\\n').replace('\r', '\\r').replace('\t', '\\t') + "'"


def array_variant(values):
    return '[' + ', '.join(string_variant(value) for value in values) + ']'


class GSettings:
    def run(self, *args):
        return subprocess.run(['gsettings', *args], check=True, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()


def scan(settings):
    available = set(settings.run('list-schemas').splitlines())
    conflicts = []
    custom_paths = []
    for schema in SCHEMAS:
        if schema not in available:
            continue
        for key in settings.run('list-keys', schema).splitlines():
            original = settings.run('get', schema, key)
            value = parse_variant(original)
            if schema == MEDIA_SCHEMA and key == 'custom-keybindings' and isinstance(value, list):
                custom_paths = value
            if isinstance(value, list) and all(isinstance(item, str) for item in value):
                remaining = [item for item in value if not is_snip_accelerator(item)]
                if remaining != value:
                    conflicts.append({'schema': schema, 'key': key, 'original': original,
                                      'replacement': array_variant(remaining)})
            elif isinstance(value, str) and is_snip_accelerator(value):
                conflicts.append({'schema': schema, 'key': key, 'original': original,
                                  'replacement': "''"})
    relocatable = set(settings.run('list-relocatable-schemas').splitlines())
    if CUSTOM_SCHEMA in relocatable:
        for path in custom_paths:
            if not isinstance(path, str) or not path.startswith('/') or not path.endswith('/'):
                continue
            schema = CUSTOM_SCHEMA + ':' + path
            original = settings.run('get', schema, 'binding')
            if is_snip_accelerator(parse_variant(original)):
                conflicts.append({'schema': schema, 'key': 'binding', 'original': original,
                                  'replacement': "''"})
    return conflicts


def _safe_backup_path(backup):
    if not backup.is_absolute():
        raise ValueError('The keybinding backup must have an absolute path')
    for path in (backup, *backup.parents):
        if path.is_symlink():
            raise ValueError(f'Refusing symbolic link in backup path: {path}')


def _validate_backup(saved):
    if not isinstance(saved, dict) or saved.get('version') != 1 or not isinstance(saved.get('bindings'), list):
        raise ValueError('Unsupported keybinding backup format')
    seen = set()
    for item in saved['bindings']:
        if not isinstance(item, dict) or any(not isinstance(item.get(key), str)
                                             for key in ('schema', 'key', 'original', 'replacement')):
            raise ValueError('Invalid keybinding backup record')
        schema, key = item['schema'], item['key']
        custom = schema.startswith(CUSTOM_SCHEMA + ':') and re.fullmatch(r'/[A-Za-z0-9_/-]*/', schema.split(':', 1)[1])
        if not (schema in SCHEMAS or (custom and key == 'binding')) or not re.fullmatch(r'[a-z][a-z0-9-]*', key):
            raise ValueError('Backup contains a setting outside the supported keybindings')
        original, replacement = parse_variant(item['original']), parse_variant(item['replacement'])
        if isinstance(original, list) and all(isinstance(value, str) for value in original):
            expected = [value for value in original if not is_snip_accelerator(value)]
        elif isinstance(original, str) and is_snip_accelerator(original):
            expected = ''
        else:
            raise ValueError('Backup contains an invalid original shortcut')
        if original == expected or replacement != expected or (schema, key) in seen:
            raise ValueError('Backup contains an invalid or duplicate shortcut replacement')
        seen.add((schema, key))
    return saved


def read_backup(backup):
    _safe_backup_path(backup)
    try:
        fd = os.open(backup, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > MAX_BACKUP_BYTES:
            raise ValueError('Keybinding backup must be a regular, user-owned file smaller than 1 MiB')
        if info.st_mode & 0o022:
            raise ValueError('Keybinding backup must not be writable by other users')
        return _validate_backup(json.loads(stream.read(MAX_BACKUP_BYTES + 1)))


def write_backup(backup, saved):
    _safe_backup_path(backup)
    backup.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.keybinding-backup-', dir=backup.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(saved, stream, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, backup)
        directory_fd = os.open(backup.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def backup_lock(backup, dry_run=False):
    # Login setup and a manually started installer must not lose each other's record.
    _safe_backup_path(backup)
    if dry_run:
        yield
        return
    backup.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(backup.parent / '.keybinding-backup.lock',
                 os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ValueError('Unsafe keybinding lock file')
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def disable(settings, backup, dry_run=False):
    conflicts = scan(settings)
    saved = read_backup(backup)
    if saved is not None:
        known = {(item['schema'], item['key']) for item in saved['bindings']}
        new = [item for item in conflicts if (item['schema'], item['key']) not in known]
        if new:
            print(f'{"Would merge" if dry_run else "Merging"} {len(new)} new conflicts into: {backup}')
            if not dry_run:
                saved['bindings'].extend(new)
                write_backup(backup, saved)
        print(f'Preserving existing backup: {backup}')
    else:
        print(f'{"Would save" if dry_run else "Saving"} keybinding backup: {backup}')
        if not dry_run:
            write_backup(backup, {'version': 1, 'bindings': conflicts})
    for item in conflicts:
        print(f"Remove Super+Shift+S: {item['schema']} {item['key']}")
        if not dry_run:
            settings.run('set', item['schema'], item['key'], item['replacement'])
    if not conflicts:
        print('No Super+Shift+S conflicts found.')


def restore(settings, backup, dry_run=False):
    saved = read_backup(backup)
    if saved is None:
        print('No keybinding backup to restore.')
        return
    available = set(settings.run('list-schemas').splitlines())
    relocatable = set(settings.run('list-relocatable-schemas').splitlines())
    pending = []
    for item in saved['bindings']:
        schema, key = item['schema'], item['key']
        if schema.split(':', 1)[0] not in available | relocatable:
            print(f'Retaining unavailable schema for later restore: {schema}')
            pending.append(item)
            continue
        if key not in settings.run('list-keys', schema).splitlines():
            print(f'Retaining unavailable key for later restore: {schema} {key}')
            pending.append(item)
            continue
        current = parse_variant(settings.run('get', schema, key))
        if current == parse_variant(item['original']):
            continue  # A preceding partial restore already succeeded for this key.
        if current != parse_variant(item['replacement']):
            print(f'Preserving shortcut changed after setup: {schema} {key}')
            continue
        print(f'Restore: {schema} {key}')
        if not dry_run:
            settings.run('set', schema, key, item['original'])
    if pending:
        print(f'Retaining {len(pending)} unavailable bindings in: {backup}')
        if not dry_run:
            write_backup(backup, {'version': 1, 'bindings': pending})
    else:
        print(f'{"Would delete" if dry_run else "Deleting"} backup: {backup}')
        if not dry_run:
            backup.unlink()


def set_extension_enabled(settings, enabled, dry_run=False):
    if 'org.gnome.shell' not in settings.run('list-schemas').splitlines():
        if enabled:
            raise RuntimeError('GNOME Shell settings are unavailable; use portable mode')
        return
    values = parse_variant(settings.run('get', 'org.gnome.shell', 'enabled-extensions'))
    if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
        raise RuntimeError('Could not read org.gnome.shell enabled-extensions')
    if enabled:
        values = list(dict.fromkeys([*values, EXTENSION_UUID]))
    else:
        values = [item for item in values if item != EXTENSION_UUID]
    print(f'{"Enable" if enabled else "Disable"} {EXTENSION_UUID} in enabled-extensions')
    if not dry_run:
        settings.run('set', 'org.gnome.shell', 'enabled-extensions', array_variant(values))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--backup', type=Path, default=DEFAULT_BACKUP)
    parser.add_argument('command', choices=['scan', 'disable', 'restore', 'enable-extension', 'disable-extension'])
    args = parser.parse_args()
    settings = GSettings()
    try:
        if args.command == 'scan':
            print(json.dumps(scan(settings), indent=2))
        elif args.command in ('disable', 'restore'):
            with backup_lock(args.backup, args.dry_run):
                operation = disable if args.command == 'disable' else restore
                operation(settings, args.backup, args.dry_run)
        else:
            set_extension_enabled(settings, args.command == 'enable-extension', args.dry_run)
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f'Keybinding operation failed: {error}', file=sys.stderr)
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            print(error.stderr.strip(), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
