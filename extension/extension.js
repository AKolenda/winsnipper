import Gio from 'gi://Gio';
import Meta from 'gi://Meta';
import Shell from 'gi://Shell';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

import {encodeCapture, requestCapture, saveCapture, saveDirectory, snapshotWindows} from './lib/capture.js';
import {EditorClient, ShellService} from './lib/dbus.js';
import {SelectionOverlay} from './lib/overlay.js';
import {SnipToast} from './lib/toast.js';
import {report} from './lib/log.js';

export default class WinSnipper extends Extension {
    enable() {
        this._generation = (this._generation ?? 0) + 1;
        this._enabled = true;
        this._busy = false;
        this._snipToken = null;
        this._selecting = false;
        this._overlay = null;
        this._toast = null;
        this._signals = [];
        this._settings = this.getSettings();
        this._cancellable = new Gio.Cancellable();
        this._shooter = new Shell.Screenshot();
        const generation = this._generation;
        const active = () => this._enabled && this._generation === generation;
        try {
            this._editor = new EditorClient(this._cancellable, active);
            this._service = new ShellService(() => this._startSnip(), this._cancellable, active);
            Main.wm.addKeybinding('snip-shortcut', this._settings, Meta.KeyBindingFlags.NONE,
                Shell.ActionMode.NORMAL | Shell.ActionMode.OVERVIEW, () => this._startSnip());
            this._keybindingAdded = true;
            const onLayoutChange = () => {
                if (this._selecting) {
                    this._snipToken.cancelled = true;
                    this._overlay?.close();
                    this._overlay = null;
                    this._selecting = false;
                    // Native stage capture is not cancellable. Keep the shortcut
                    // locked until it settles, so the shooter is never reused
                    // while an X11 after-paint callback is still pending.
                    this._busy = this._snipToken.pending;
                }
                this._toast?.close();
                this._toast = null;
            };
            this._signals.push([Main.layoutManager, Main.layoutManager.connect('monitors-changed', onLayoutChange)]);
            this._signals.push([global.display, global.display.connect('workareas-changed', onLayoutChange)]);
        } catch (error) {
            this.disable();
            throw error;
        }
    }

    _startSnip() {
        if (!this._enabled || this._busy)
            return;
        this._busy = true;
        // FIRST compositor action: request capture. No actor/grab/menu changes.
        // The shooter was constructed at enable(), so even its creation is absent.
        let capture;
        let token;
        const generation = this._generation;
        try {
            capture = requestCapture(this._shooter);
            token = {cancelled: false, pending: true};
            this._snipToken = token;
            this._selecting = true;
            const windows = snapshotWindows();
            const bounds = {x: 0, y: 0, width: global.stage.width, height: global.stage.height};
            const active = () => this._enabled && this._generation === generation && !token.cancelled;
            this._select(capture, windows, bounds, active, token).catch(error => {
                if (active()) {
                    this._overlay?.close();
                    this._overlay = null;
                    this._selecting = false;
                    this._busy = false;
                    report('Snip failed', error);
                }
            });
        } catch (error) {
            // Consume the capture promise if snapshotting failed after request.
            if (token)
                token.cancelled = true;
            if (capture) {
                capture.catch(e => {
                    if (this._enabled && this._generation === generation)
                        report('Capture failed', e);
                }).finally(() => {
                    if (this._snipToken === token && this._generation === generation)
                        this._busy = false;
                });
            } else {
                this._busy = false;
            }
            this._selecting = false;
            report('Could not begin snip', error);
        }
    }

    async _select(capture, windows, bounds, active, token) {
        let captured;
        try {
            captured = await capture;
        } finally {
            token.pending = false;
            if (this._snipToken === token && token.cancelled)
                this._busy = false;
        }
        if (!active())
            return;
        const [content, scale] = captured;
        this._overlay = new SelectionOverlay(content, bounds, windows,
            this._settings.get_string('default-mode'), rect => {
                this._overlay = null;
                this._selecting = false;
                this._finish(content, scale, rect, active).catch(error => {
                    if (active())
                        report('Could not encode or save snip', error);
                }).finally(() => {
                    if (active())
                        this._busy = false;
                });
            }, () => {
                this._overlay = null;
                this._selecting = false;
                this._busy = false;
            });
        this._overlay.open();
    }

    async _finish(content, scale, rect, active) {
        if (!active())
            return;
        const {bytes, pixbuf} = await encodeCapture(content, scale, rect, active);
        if (!active())
            return;
        const path = await saveCapture(bytes, saveDirectory(this._settings), this._cancellable, active);
        if (!active())
            return;
        this._toast?.close();
        this._toast = new SnipToast(pixbuf, this._settings.get_int('toast-timeout'), () => {
            if (active())
                this._editor.open(path);
        });
        const toast = this._toast;
        toast.connect('destroy', () => {
            if (this._toast === toast)
                this._toast = null;
        });
        try {
            toast.showToast();
            this._editor.prewarm();
        } catch (error) {
            toast.close();
            throw error;
        }
    }

    disable() {
        this._enabled = false;
        this._generation = (this._generation ?? 0) + 1;
        const cleanup = (label, callback) => {
            try {
                callback();
            } catch (error) {
                report(`Cleanup failed (${label})`, error);
            }
        };
        cleanup('IO cancellation', () => this._cancellable?.cancel());
        if (this._keybindingAdded) {
            cleanup('keybinding', () => Main.wm.removeKeybinding('snip-shortcut'));
            this._keybindingAdded = false;
        }
        for (const [object, id] of this._signals ?? [])
            cleanup('signal', () => object.disconnect(id));
        this._signals = [];
        cleanup('overlay', () => this._overlay?.close());
        this._overlay = null;
        cleanup('toast', () => this._toast?.close());
        this._toast = null;
        cleanup('D-Bus service', () => this._service?.destroy());
        this._service = null;
        this._editor = null;
        this._shooter = null;
        this._settings = null;
        this._cancellable = null;
        this._busy = false;
        this._snipToken = null;
        this._selecting = false;
    }
}
