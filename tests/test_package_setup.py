"""Package shortcut setup must only validate the user files it manages."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PackageSetupTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.home = self.root / 'home'
        self.home.mkdir()
        self.data = self.home / 'data'
        self.data.mkdir()
        self.state = self.data / 'winsnipper'
        self.calls = self.root / 'settings-calls'
        fake_bin = self.root / 'fake-bin'
        fake_bin.mkdir()
        settings = fake_bin / 'gsettings'
        settings.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
with open(os.environ['SETUP_CALLS'], 'a') as stream:
    stream.write(json.dumps(args) + '\\n')
state = pathlib.Path(os.environ['SETUP_EXTENSION_STATE'])
if args == ['list-schemas']:
    print('org.gnome.shell')
elif args == ['list-relocatable-schemas']:
    pass
elif args == ['get', 'org.gnome.shell', 'enabled-extensions']:
    print(state.read_text() if state.exists() else '[]')
elif args[:3] == ['set', 'org.gnome.shell', 'enabled-extensions']:
    state.write_text(args[3])
else:
    sys.exit('Unexpected settings call: ' + repr(args))
''')
        settings.chmod(0o755)
        extensions = fake_bin / 'gnome-extensions'
        extensions.write_text('#!/bin/sh\nexit 0\n')
        extensions.chmod(0o755)
        self.environment = {
            **os.environ, 'HOME': str(self.home), 'XDG_DATA_HOME': str(self.data),
            'XDG_CURRENT_DESKTOP': 'GNOME', 'PATH': str(fake_bin) + os.pathsep + os.environ['PATH'],
            'SETUP_CALLS': str(self.calls), 'SETUP_EXTENSION_STATE': str(self.root / 'enabled-extensions'),
        }
        # Exercise the shipped script with its installed tool directory relocated.
        self.setup = self.root / 'winsnipper-setup'
        script = (ROOT / 'packaging/winsnipper-setup').read_text()
        self.assertEqual(script.count('TOOLS=/usr/share/winsnipper/tools\n'), 1)
        self.setup.write_text(script.replace('TOOLS=/usr/share/winsnipper/tools\n',
                                             'TOOLS=' + shlex.quote(str(ROOT / 'tools')) + '\n'))

    def run_setup(self, *args):
        return subprocess.run(['bash', str(self.setup), *args], env=self.environment,
                              capture_output=True, text=True)

    def check_setup(self):
        return subprocess.run([sys.executable, str(ROOT / 'tools/install.py'), 'check-setup'],
                              env=self.environment, capture_output=True, text=True)

    def test_unrelated_symlinked_user_directories_allow_check_setup_and_remove(self):
        outside = self.root / 'outside'
        outside.mkdir()
        untouched = outside / 'important'
        untouched.write_text('keep')
        for path in (self.home / '.local/bin', self.data / 'applications',
                     self.data / 'gnome-shell/extensions', self.data / 'dbus-1/services'):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.symlink_to(outside, target_is_directory=True)

        checked = self.check_setup()
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertFalse(self.state.exists())
        self.assertFalse(self.calls.exists())
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads((self.state / 'keybinding-backup.json').read_text()),
                         {'version': 1, 'bindings': []})
        self.assertTrue((self.state / 'setup-done').is_file())
        calls = self.calls.read_text()
        self.assertIn('winsnipper@winsnipper.local', calls)
        first_run = self.run_setup('--first-run')
        self.assertEqual(first_run.returncode, 0, first_run.stderr)
        self.assertEqual(self.calls.read_text(), calls)
        removed = self.run_setup('--remove')
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertFalse((self.state / 'keybinding-backup.json').exists())
        self.assertEqual(Path(self.environment['SETUP_EXTENSION_STATE']).read_text(), '[]')
        self.assertEqual(untouched.read_text(), 'keep')
        self.assertEqual(list(outside.iterdir()), [untouched])

    def test_symlinked_setup_state_is_rejected_before_settings_calls(self):
        outside = self.root / 'outside'
        outside.mkdir()
        untouched = outside / 'important'
        untouched.write_text('keep')
        for name in ('', 'keybinding-backup.json', 'setup-done'):
            with self.subTest(path=name or 'winsnipper'):
                if name:
                    self.state.mkdir(exist_ok=True)
                path = self.state / name
                path.symlink_to(outside if not name else untouched)
                try:
                    for args in ((), ('--first-run',), ('--remove',)):
                        result = self.run_setup(*args)
                        self.assertNotEqual(result.returncode, 0, result.stdout)
                        self.assertIn('symbolic link', result.stderr)
                        self.assertFalse(self.calls.exists())
                    checked = self.check_setup()
                    self.assertNotEqual(checked.returncode, 0, checked.stdout)
                finally:
                    path.unlink()
                self.assertEqual(untouched.read_text(), 'keep')
        self.assertEqual(list(outside.iterdir()), [untouched])

    def test_setup_check_rejects_relative_or_control_character_data_paths(self):
        for location in ('relative/path', str(self.home / 'bad\npath')):
            with self.subTest(location=location):
                self.environment['XDG_DATA_HOME'] = location
                result = self.check_setup()
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertFalse(self.calls.exists())
        self.assertEqual(list(self.home.iterdir()), [self.data])


if __name__ == '__main__':
    unittest.main()
