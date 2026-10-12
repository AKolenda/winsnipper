import Clutter from 'gi://Clutter';
import GObject from 'gi://GObject';
import Meta from 'gi://Meta';
import Shell from 'gi://Shell';
import St from 'gi://St';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import {clampRect, hitTestWindows, normalizeRect} from './geometry.js';
import {report, warn} from './log.js';

export const SelectionOverlay = GObject.registerClass(
class SelectionOverlay extends St.Widget {
    _init(content, bounds, windows, mode, onFinish, onCancel) {
        super._init({reactive: true, can_focus: true,
            x: bounds.x, y: bounds.y, width: bounds.width, height: bounds.height,
            style_class: 'winsnipper-overlay'});
        this._bounds = bounds;
        this._windows = windows;
        this._mode = mode;
        this._onFinish = onFinish;
        this._onCancel = onCancel;
        this._grab = null;
        this._closed = false;
        this._start = null;
        this._selection = null;
        this.add_child(new St.Widget({content, width: bounds.width, height: bounds.height}));
        // Four dim rectangles leave the frozen selection untouched underneath.
        this._shades = Array.from({length: 4}, () => {
            const actor = new St.Widget({style_class: 'winsnipper-shade'});
            this.add_child(actor);
            return actor;
        });
        this._border = new St.Widget({style_class: 'winsnipper-selection', visible: false});
        this.add_child(this._border);
        this._label = new St.Label({style_class: 'winsnipper-size', visible: false});
        this.add_child(this._label);
        this._toolbar = new St.BoxLayout({style_class: 'winsnipper-toolbar'});
        this.add_child(this._toolbar);
        this._buttons = new Map();
        for (const [value, label] of [['rectangle', 'Rectangle'], ['window', 'Window'],
            ['fullscreen', 'Fullscreen'], ['cancel', '✕']]) {
            const button = new St.Button({label, can_focus: true,
                style_class: 'winsnipper-tool', accessible_name: value === 'cancel' ? 'Cancel snip' : label});
            button.connect('clicked', () => this._safe(() => {
                if (value === 'cancel')
                    this._cancel();
                else
                    this._setMode(value);
            }));
            this._toolbar.add_child(button);
            this._buttons.set(value, button);
        }
        const primary = Main.layoutManager.primaryMonitor ?? bounds;
        const [, toolbarWidth] = this._toolbar.get_preferred_width(-1);
        const [, toolbarHeight] = this._toolbar.get_preferred_height(toolbarWidth);
        this._toolbar.set_size(toolbarWidth, toolbarHeight);
        this._toolbar.set_position(primary.x - bounds.x + (primary.width - toolbarWidth) / 2,
            primary.y - bounds.y + 24);
        this.connect('captured-event', (_actor, event) => this._safe(() => {
            if (event.type() === Clutter.EventType.KEY_PRESS &&
                event.get_key_symbol() === Clutter.KEY_Escape) {
                this._cancel();
                return Clutter.EVENT_STOP;
            }
            if (event.type() === Clutter.EventType.BUTTON_PRESS && event.get_button() === 3) {
                this._cancel();
                return Clutter.EVENT_STOP;
            }
            return Clutter.EVENT_PROPAGATE;
        }));
        this.connect('button-press-event', (_actor, event) => this._safe(() => this._press(event)));
        this.connect('motion-event', (_actor, event) => this._safe(() => this._motion(event)));
        this.connect('button-release-event', (_actor, event) => this._safe(() => this._release(event)));
        this._setMode(mode);
    }

    _safe(callback) {
        if (this._closed)
            return Clutter.EVENT_STOP;
        try {
            return callback() ?? Clutter.EVENT_STOP;
        } catch (error) {
            report('Selection UI failed', error);
            this._cancel();
            return Clutter.EVENT_STOP;
        }
    }

    open() {
        Main.uiGroup.add_child(this);
        this._grab = Main.pushModal(this, {actionMode: Shell.ActionMode.NONE});
        if (this._grab.get_seat_state() !== Clutter.GrabState.ALL)
            throw new Error('Could not acquire the snip input grab');
        global.display.set_cursor(Meta.Cursor.CROSSHAIR);
        this.grab_key_focus();
    }

    _setMode(mode) {
        this._mode = mode;
        this._start = null;
        for (const [value, button] of this._buttons) {
            if (value === mode)
                button.add_style_pseudo_class('checked');
            else
                button.remove_style_pseudo_class('checked');
        }
        const [x, y] = global.get_pointer();
        this._setSelection(mode === 'fullscreen' ? this._bounds :
            mode === 'window' ? hitTestWindows(this._windows, x, y) : null);
    }

    _overToolbar(event) {
        const [x, y] = event.get_coords();
        const [tx, ty] = this._toolbar.get_transformed_position();
        return x >= tx && x < tx + this._toolbar.width &&
            y >= ty && y < ty + this._toolbar.height;
    }

    _press(event) {
        if (event.get_button() !== 1 || this._overToolbar(event))
            return Clutter.EVENT_PROPAGATE;
        const [x, y] = event.get_coords();
        if (this._mode === 'fullscreen') {
            this._finish(this._bounds);
        } else if (this._mode === 'window') {
            const rect = hitTestWindows(this._windows, x, y);
            if (rect)
                this._finish(clampRect(rect, this._bounds));
        } else {
            this._start = [x, y];
            this._setSelection(null);
        }
        return Clutter.EVENT_STOP;
    }

    _motion(event) {
        const [x, y] = event.get_coords();
        if (this._mode === 'rectangle' && this._start)
            this._setSelection(normalizeRect(...this._start, x, y));
        else if (this._mode === 'window')
            this._setSelection(this._overToolbar(event) ? null : hitTestWindows(this._windows, x, y));
        return Clutter.EVENT_PROPAGATE;
    }

    _release(event) {
        if (event.get_button() !== 1 || !this._start)
            return Clutter.EVENT_PROPAGATE;
        const [x, y] = event.get_coords();
        const [sx, sy] = this._start;
        this._start = null;
        const rect = clampRect(normalizeRect(sx, sy, x, y), this._bounds);
        if (Math.hypot(x - sx, y - sy) >= 4 && rect.width > 0 && rect.height > 0)
            this._finish(rect);
        else
            this._setSelection(null);
        return Clutter.EVENT_STOP;
    }

    _setSelection(rect) {
        this._selection = rect ? clampRect(rect, this._bounds) : null;
        const selected = this._selection;
        const w = this._bounds.width;
        const h = this._bounds.height;
        const x = selected ? selected.x - this._bounds.x : 0;
        const y = selected ? selected.y - this._bounds.y : 0;
        const sw = selected?.width ?? 0;
        const sh = selected?.height ?? 0;
        const shades = selected ? [
            [0, 0, w, y], [0, y + sh, w, h - y - sh],
            [0, y, x, sh], [x + sw, y, w - x - sw, sh],
        ] : [[0, 0, w, h], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]];
        shades.forEach(([ax, ay, aw, ah], index) => {
            this._shades[index].set_position(ax, ay);
            this._shades[index].set_size(aw, ah);
        });
        this._border.visible = Boolean(selected && sw && sh);
        this._label.visible = this._border.visible;
        if (!this._border.visible)
            return;
        this._border.set_position(x, y);
        this._border.set_size(sw, sh);
        this._label.set_text(`${Math.round(sw)} × ${Math.round(sh)}`);
        const [, lw] = this._label.get_preferred_width(-1);
        const [, lh] = this._label.get_preferred_height(lw);
        this._label.set_position(Math.min(x, w - lw), y >= lh + 8 ? y - lh - 8 : Math.min(h - lh, y + sh + 8));
    }

    _finish(rect) {
        const callback = this._onFinish;
        this.close(); // Pop/destroy synchronously, BEFORE the encoder starts.
        callback(rect);
    }

    _cancel() {
        const callback = this._onCancel;
        this.close();
        callback?.();
    }

    close() {
        if (this._closed)
            return;
        this._closed = true;
        if (this._grab) {
            const grab = this._grab;
            this._grab = null;
            try {
                Main.popModal(grab);
            } catch (error) {
                warn('Could not release modal grab', error);
            }
        }
        global.display.set_cursor(Meta.Cursor.DEFAULT);
        this._onFinish = null;
        this._onCancel = null;
        this.destroy();
    }
});
