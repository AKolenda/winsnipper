"""Per-user installation isolation and distro payload regression checks."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools import install as installer

ROOT = Path(__file__).resolve().parents[1]


class UserInstallTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.home = self.root / "home '$`% spaced"
        self.home.mkdir()
        environment = patch.dict(os.environ, {'HOME': str(self.home), 'XDG_DATA_HOME': str(self.home / 'data')})
        environment.start()
        self.addCleanup(environment.stop)

    def test_portable_install_and_uninstall_preserve_user_data(self):
        installer.install(ROOT, portable=True)
        paths = installer.user_paths()
        saved = paths['data'] / 'screenshot.png'
        saved.write_bytes(b'user capture')
        backup = paths['data'] / 'keybinding-backup.json'
        backup.write_text('{"version": 1, "bindings": []}')
        self.assertFalse(paths['extension'].exists())
        self.assertTrue((paths['editor'] / 'winsnipper_editor.py').is_file())
        self.assertFalse((paths['editor'] / 'tests').exists())
        self.assertEqual(paths['launcher'].stat().st_mode & 0o777, 0o755)
        # A Python executable named in PATH cannot intercept the fixed launcher.
        self.assertIn(installer.shlex.quote(installer.sys.executable), paths['launcher'].read_text())
        installer.uninstall()
        self.assertEqual(saved.read_bytes(), b'user capture')
        self.assertTrue(backup.exists())
        for key in ('editor', 'tools', 'launcher', 'desktop', 'service'):
            self.assertFalse(paths[key].exists(), key)

    def test_launcher_quotes_home_metacharacters(self):
        installer.install(ROOT, portable=True)
        paths = installer.user_paths()
        entry = paths['editor'] / 'winsnipper_editor.py'
        entry.write_text('import json, sys; print(json.dumps(sys.argv[1:]))')
        arguments = ['file with spaces.png', '$(touch should-not-exist)', '`false`']
        result = subprocess.run([str(paths['launcher']), *arguments], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), arguments)
        if shutil.which('desktop-file-validate'):
            subprocess.run(['desktop-file-validate', str(paths['desktop'])], check=True)

    def test_install_and_uninstall_refuse_symlinked_managed_directory(self):
        paths = installer.user_paths()
        outside = self.root / 'outside'
        outside.mkdir()
        marker = outside / 'important'
        marker.write_text('keep')
        paths['editor'].parent.mkdir(parents=True)
        paths['editor'].symlink_to(outside)
        for operation in (lambda: installer.install(ROOT, portable=True), installer.uninstall):
            with self.assertRaisesRegex(ValueError, 'symbolic link'):
                operation()
        self.assertEqual(marker.read_text(), 'keep')
        self.assertFalse(paths['launcher'].exists())

    def test_install_refuses_symlinked_launcher_without_touching_target(self):
        paths = installer.user_paths()
        target = self.root / 'important'
        target.write_text('keep')
        paths['launcher'].parent.mkdir(parents=True)
        paths['launcher'].symlink_to(target)
        with self.assertRaisesRegex(ValueError, 'symbolic link'):
            installer.install(ROOT, portable=True)
        self.assertEqual(target.read_text(), 'keep')
        self.assertFalse(paths['editor'].exists())

    def test_reinstall_replaces_stale_tree_without_following_nested_symlinks(self):
        installer.install(ROOT, portable=True)
        paths = installer.user_paths()
        outside = self.root / 'outside.py'
        outside.write_text('keep')
        (paths['editor'] / 'winsnipper_editor.py').unlink()
        (paths['editor'] / 'winsnipper_editor.py').symlink_to(outside)
        (paths['editor'] / 'obsolete.py').write_text('old')
        installer.install(ROOT, portable=True)
        self.assertEqual(outside.read_text(), 'keep')
        self.assertFalse((paths['editor'] / 'winsnipper_editor.py').is_symlink())
        self.assertFalse((paths['editor'] / 'obsolete.py').exists())

    def test_relative_xdg_and_control_characters_fail_before_writing(self):
        for location in ('relative/path', str(self.home / 'bad\nExec=bad')):
            with self.subTest(location=location), patch.dict(os.environ, {'XDG_DATA_HOME': location}):
                with self.assertRaises(ValueError):
                    installer.install(ROOT, portable=True)
        self.assertFalse((self.home / '.local').exists())

    def test_install_dry_run_does_not_require_desktop_or_write_files(self):
        result = subprocess.run(['bash', str(ROOT / 'install.sh'), '--dry-run'],
                                env={**os.environ, 'XDG_CURRENT_DESKTOP': ''}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('GNOME', result.stdout)
        self.assertEqual(list(self.home.iterdir()), [])


@unittest.skipUnless(shutil.which('dpkg-deb'), 'Debian packaging tools are required')
class PackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.directory.cleanup)
        cls.path = Path(cls.directory.name)
        result = subprocess.run(['bash', str(ROOT / 'packaging/build-deb.sh'), '1.2.3', str(cls.path)],
                                capture_output=True, text=True, check=True)
        cls.deb = Path(result.stdout.strip())
        cls.payload = cls.path / 'payload'
        subprocess.run(['dpkg-deb', '--extract', str(cls.deb), str(cls.payload)], check=True)

    def test_package_contains_launchers_desktop_service_and_no_tests(self):
        for name in ('winsnipper-editor', 'winsnipper-setup'):
            path = self.payload / 'usr/bin' / name
            self.assertEqual(path.stat().st_mode & 0o777, 0o755)
        self.assertTrue((self.payload / 'usr/share/dbus-1/services/io.github.winsnipper.Editor.service').is_file())
        self.assertFalse(list(self.payload.rglob('__pycache__')))
        self.assertFalse((self.payload / 'usr/share/winsnipper/editor/tests').exists())
        desktop = self.payload / 'usr/share/applications/io.github.winsnipper.Editor.desktop'
        self.assertIn('NoDisplay=true', desktop.read_text())
        if shutil.which('desktop-file-validate'):
            subprocess.run(['desktop-file-validate', str(desktop)], check=True)

    def test_editor_dependencies_include_cairo_bindings(self):
        depends = subprocess.check_output(['dpkg-deb', '--field', str(self.deb), 'Depends'], text=True)
        self.assertIn('python3-gi-cairo', depends)

    def test_payload_has_root_ownership_and_no_world_writable_files(self):
        contents = subprocess.check_output(['dpkg-deb', '--contents', str(self.deb)], text=True)
        for line in contents.splitlines():
            fields = line.split()
            self.assertEqual(fields[1], 'root/root')
            self.assertNotEqual(fields[0][8], 'w', line)

    def test_versions_cannot_inject_metadata_or_paths(self):
        output = self.path / 'must-not-exist'
        for version in ('1.0\nDepends: evil', '../escape', '1/../../escape', '-1', '1.0:bad'):
            with self.subTest(version=version):
                result = subprocess.run(['bash', str(ROOT / 'packaging/build-deb.sh'), version, str(output)],
                                        capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(output.exists())

    def test_setup_rejects_extra_arguments_without_settings_changes(self):
        result = subprocess.run(['bash', str(ROOT / 'packaging/winsnipper-setup'), '--remove', 'unexpected'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)

    def test_autostart_outside_gnome_leaves_settings_alone(self):
        result = subprocess.run(['bash', str(ROOT / 'packaging/winsnipper-setup'), '--first-run'],
                                env={**os.environ, 'XDG_CURRENT_DESKTOP': 'KDE'}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
