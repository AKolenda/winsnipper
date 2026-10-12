#!/usr/bin/env python3
"""Back up only Super+Shift+S conflicts and restore their exact GVariant values."""
import argparse
import ast
import json
import os
from pathlib import Path
import re
import subprocess
import sys

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
DEFAULT_BACKUP = Path.home() / '.local/share/winsnipper/keybinding-backup.json'


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


def disable(settings, backup, dry_run=False):
    conflicts = scan(settings)
    if backup.exists():
        saved = json.loads(backup.read_text())
        known = {(item['schema'], item['key']) for item in saved['bindings']}
        new = [item for item in conflicts if (item['schema'], item['key']) not in known]
        if new:
            # Never discard someone's only restore record on reinstall.
            raise RuntimeError('New shortcut conflicts appeared after the original backup. '
                               'Run uninstall.sh, then install.sh to preserve their originals.')
        print(f'Preserving existing backup: {backup}')
    else:
        print(f'{"Would save" if dry_run else "Saving"} keybinding backup: {backup}')
        if not dry_run:
            backup.parent.mkdir(parents=True, exist_ok=True)
            with backup.open('x') as stream:
                json.dump({'version': 1, 'bindings': conflicts}, stream, indent=2)
                stream.write('\n')
    for item in conflicts:
        print(f"Remove Super+Shift+S: {item['schema']} {item['key']}")
        if not dry_run:
            settings.run('set', item['schema'], item['key'], item['replacement'])
    if not conflicts:
        print('No Super+Shift+S conflicts found.')


def restore(settings, backup, dry_run=False):
    if not backup.exists():
        print('No keybinding backup to restore.')
        return
    saved = json.loads(backup.read_text())
    available = set(settings.run('list-schemas').splitlines())
    relocatable = set(settings.run('list-relocatable-schemas').splitlines())
    for item in saved['bindings']:
        schema = item['schema']
        if schema.split(':', 1)[0] not in available | relocatable:
            print(f'Skipping unavailable schema: {schema}')
            continue
        print(f"Restore: {schema} {item['key']}")
        if not dry_run:
            settings.run('set', schema, item['key'], item['original'])
    print(f'{"Would delete" if dry_run else "Deleting"} backup: {backup}')
    if not dry_run:
        backup.unlink()


def set_extension_enabled(settings, enabled, dry_run=False):
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
        elif args.command == 'disable':
            disable(settings, args.backup, args.dry_run)
        elif args.command == 'restore':
            restore(settings, args.backup, args.dry_run)
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
