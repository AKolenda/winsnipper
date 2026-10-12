import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools import keybindings as kb


class FakeSettings:
    def __init__(self):
        self.values = {
            ('org.gnome.shell.keybindings', 'screenshot'): "['<Super><Shift>s', 'Print', '<Ctrl>s']",
            ('org.gnome.shell.keybindings', 'other'): "['<Shift><Super>a']",
            ('org.gnome.desktop.wm.keybindings', 'sample'): "['<Mod4><Shift>S', '<Shift><Mod4>s', '<Alt>s']",
            (kb.MEDIA_SCHEMA, 'custom-keybindings'): "['/custom/one/', '/custom/two/']",
            (kb.MEDIA_SCHEMA, 'string-binding'): "'<Shift><Super>s'",
            (kb.MEDIA_SCHEMA, 'numeric'): 'uint32 100',
            (kb.MEDIA_SCHEMA, 'empty'): '@as []',
            (kb.CUSTOM_SCHEMA + ':/custom/one/', 'binding'): "'<Mod4><Shift>S'",
            (kb.CUSTOM_SCHEMA + ':/custom/two/', 'binding'): "'<Super>s'",
            ('org.gnome.shell', 'enabled-extensions'): "['another@example']",
        }
        self.calls = []

    def run(self, *args):
        self.calls.append(args)
        if args[0] == 'list-schemas':
            return '\n'.join([kb.SCHEMAS[0], kb.SCHEMAS[1], kb.MEDIA_SCHEMA, 'org.gnome.shell'])
        if args[0] == 'list-relocatable-schemas':
            return kb.CUSTOM_SCHEMA
        if args[0] == 'list-keys':
            return '\n'.join(key for schema, key in self.values if schema == args[1])
        if args[0] == 'get':
            return self.values[(args[1], args[2])]
        if args[0] == 'set':
            self.values[(args[1], args[2])] = args[3]
            return ''
        raise AssertionError(args)


class NormalizationTests(unittest.TestCase):
    def test_all_modifier_orders_aliases_and_case(self):
        for value in ['<Shift><Super>s', '<Super><Shift>S', '<Mod4><Shift>s',
                      '<shift><mod4>S', 'Super+Shift+S', 'Shift+Mod4+s',
                      ' <Super><Shift>s ', '<Super><Shift><Super>s']:
            with self.subTest(value=value):
                self.assertTrue(kb.is_snip_accelerator(value))

    def test_unrelated_or_malformed_accelerators_never_match(self):
        for value in ['', 's', '<Super>s', '<Shift>s', '<Super><Shift>a',
                      '<Ctrl><Super><Shift>s', '<Super><Shift>ss', '<Super><Shift>',
                      '<<Super>><Shift>s', '<Alt><Shift>s', 'Super++Shift+S', None, 12]:
            with self.subTest(value=value):
                self.assertFalse(kb.is_snip_accelerator(value))

    def test_variant_parsing_and_string_roundtrip(self):
        self.assertEqual(kb.parse_variant('@as []'), [])
        self.assertEqual(kb.parse_variant("@s '<Shift><Super>s'"), '<Shift><Super>s')
        self.assertIsNone(kb.parse_variant('uint32 9'))
        value = "quote' slash\\ newline\n"
        self.assertEqual(kb.parse_variant(kb.string_variant(value)), value)


class ScanTests(unittest.TestCase):
    def test_scan_every_key_skip_missing_schemas_and_include_custom_bindings(self):
        settings = FakeSettings()
        result = kb.scan(settings)
        self.assertEqual(len(result), 4)
        self.assertEqual(result[0]['replacement'], "['Print', '<Ctrl>s']")
        self.assertEqual(result[1]['replacement'], "['<Alt>s']")
        self.assertEqual(result[2]['replacement'], "''")
        self.assertEqual(result[3]['schema'], kb.CUSTOM_SCHEMA + ':/custom/one/')
        self.assertNotIn(('list-keys', 'org.gnome.mutter.keybindings'), settings.calls)
        self.assertFalse(any(call[0] == 'set' for call in settings.calls))

    def test_gsettings_subprocess_scan(self):
        fake = FakeSettings()
        def command(args, **kwargs):
            self.assertEqual(args[0], 'gsettings')
            self.assertTrue(kwargs['check'])
            return subprocess.CompletedProcess(args, 0, fake.run(*args[1:]), '')
        with patch('tools.keybindings.subprocess.run', side_effect=command):
            self.assertEqual(len(kb.scan(kb.GSettings())), 4)

    def test_disable_restore_exact_original_values_and_backup_reinstall(self):
        settings = FakeSettings()
        before = settings.values.copy()
        with tempfile.TemporaryDirectory() as directory:
            backup = Path(directory) / 'backup.json'
            kb.disable(settings, backup)
            saved = backup.read_bytes()
            self.assertFalse(kb.scan(settings))
            kb.disable(settings, backup)
            self.assertEqual(backup.read_bytes(), saved)
            kb.restore(settings, backup)
            self.assertEqual(settings.values, before)
            self.assertFalse(backup.exists())

    def test_dry_run_never_sets_or_creates_backup(self):
        settings = FakeSettings()
        with tempfile.TemporaryDirectory() as directory:
            backup = Path(directory) / 'backup.json'
            kb.disable(settings, backup, dry_run=True)
            self.assertFalse(backup.exists())
            self.assertFalse(any(call[0] == 'set' for call in settings.calls))
            backup.write_text(json.dumps({'version': 1, 'bindings': kb.scan(settings)}))
            saved = backup.read_bytes()
            kb.restore(settings, backup, dry_run=True)
            self.assertEqual(saved, backup.read_bytes())
            self.assertFalse(any(call[0] == 'set' for call in settings.calls))

    def test_new_reinstall_conflict_keeps_original_backup_and_settings(self):
        settings = FakeSettings()
        with tempfile.TemporaryDirectory() as directory:
            backup = Path(directory) / 'backup.json'
            kb.disable(settings, backup)
            saved = backup.read_bytes()
            settings.values[(kb.MEDIA_SCHEMA, 'new')] = "'<Shift><Super>s'"
            before = settings.values.copy()
            with self.assertRaisesRegex(RuntimeError, 'New shortcut conflicts'):
                kb.disable(settings, backup)
            self.assertEqual(backup.read_bytes(), saved)
            self.assertEqual(settings.values, before)

    def test_restore_failure_preserves_backup_for_retry(self):
        settings = FakeSettings()
        with tempfile.TemporaryDirectory() as directory:
            backup = Path(directory) / 'backup.json'
            kb.disable(settings, backup)
            with patch.object(settings, 'run', side_effect=lambda *args:
                              (_ for _ in ()).throw(RuntimeError('write failed')) if args[0] == 'set'
                              else FakeSettings.run(settings, *args)):
                with self.assertRaisesRegex(RuntimeError, 'write failed'):
                    kb.restore(settings, backup)
            self.assertTrue(backup.exists())

    def test_enable_preserves_other_extensions_and_prevents_duplicates(self):
        settings = FakeSettings()
        kb.set_extension_enabled(settings, True)
        kb.set_extension_enabled(settings, True)
        self.assertEqual(kb.parse_variant(settings.values[('org.gnome.shell', 'enabled-extensions')]),
                         ['another@example', kb.EXTENSION_UUID])
        kb.set_extension_enabled(settings, False)
        self.assertEqual(kb.parse_variant(settings.values[('org.gnome.shell', 'enabled-extensions')]),
                         ['another@example'])


if __name__ == '__main__':
    unittest.main()
