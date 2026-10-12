import Adw from 'gi://Adw';
import Gio from 'gi://Gio';
import Gtk from 'gi://Gtk';

import {ExtensionPreferences} from 'resource:///org/gnome/Shell/Extensions/js/extensions/prefs.js';

export default class WinSnipperPreferences extends ExtensionPreferences {
    fillPreferencesWindow(window) {
        const settings = this.getSettings();
        const page = new Adw.PreferencesPage({title: 'WinSnipper', icon_name: 'camera-photo-symbolic'});
        const group = new Adw.PreferencesGroup({title: 'Capture and save'});
        page.add(group);
        window.add(page);
        group.add(new Adw.ActionRow({title: 'Shortcut', subtitle: settings.get_strv('snip-shortcut').join(', ')}));
        const save = new Gtk.Switch({valign: Gtk.Align.CENTER});
        settings.bind('save-to-disk', save, 'active', Gio.SettingsBindFlags.DEFAULT);
        const saveRow = new Adw.ActionRow({title: 'Save to disk', subtitle: 'Otherwise keep the editor file in the cache directory', activatable_widget: save});
        saveRow.add_suffix(save);
        group.add(saveRow);
        const directory = new Adw.EntryRow({title: 'Save directory (empty = Pictures/Screenshots)'});
        settings.bind('save-directory', directory, 'text', Gio.SettingsBindFlags.DEFAULT);
        group.add(directory);
        const timeout = new Gtk.SpinButton({adjustment: new Gtk.Adjustment({lower: 0,
            upper: 2147483647, step_increment: 1, page_increment: 5}), valign: Gtk.Align.CENTER});
        settings.bind('toast-timeout', timeout, 'value', Gio.SettingsBindFlags.DEFAULT);
        const timeoutRow = new Adw.ActionRow({title: 'Toast timeout (seconds)', subtitle: '0 keeps the toast until dismissed'});
        timeoutRow.add_suffix(timeout);
        group.add(timeoutRow);
        const modes = ['rectangle', 'window', 'fullscreen'];
        const mode = new Adw.ComboRow({title: 'Default mode', model: Gtk.StringList.new(['Rectangle', 'Window', 'Fullscreen'])});
        mode.selected = Math.max(0, modes.indexOf(settings.get_string('default-mode')));
        mode.connect('notify::selected', () => settings.set_string('default-mode', modes[mode.selected]));
        group.add(mode);
        // Keep Settings alive for bindings for the preferences window lifetime.
        window._winsnipperSettings = settings;
    }
}
