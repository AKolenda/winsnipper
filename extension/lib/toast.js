import Clutter from 'gi://Clutter';
import Cogl from 'gi://Cogl';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import St from 'gi://St';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import {toastPosition} from './geometry.js';
import {report} from './log.js';

export const SnipToast = GObject.registerClass(
class SnipToast extends St.BoxLayout {
    _init(pixbuf, timeout, onOpen) {
        super._init({style_class: 'winsnipper-toast', reactive: true,
            track_hover: true, opacity: 0});
        this._timeoutSeconds = timeout;
        this._timer = 0;
        this._closed = false;
        this._onOpen = onOpen;
        // Same preview content API as Shell 45–49's ui/screenshot.js.
        const content = St.ImageContent.new_with_preferred_size(pixbuf.width, pixbuf.height);
        content.set_bytes(pixbuf.read_pixel_bytes(),
            pixbuf.get_has_alpha() ? Cogl.PixelFormat.RGBA_8888 : Cogl.PixelFormat.RGB_888,
            pixbuf.width, pixbuf.height, pixbuf.rowstride);
        this._openButton = new St.Button({can_focus: true, x_expand: true,
            accessible_name: 'Open snip in WinSnipper Editor'});
        const row = new St.BoxLayout({style_class: 'winsnipper-toast-content'});
        row.add_child(new St.Icon({gicon: content, icon_size: 80,
            style_class: 'winsnipper-thumbnail'}));
        // vertical is available through 47; orientation is the replacement in 48+.
        const text = new St.BoxLayout('orientation' in St.BoxLayout.prototype ?
            {orientation: Clutter.Orientation.VERTICAL, y_align: Clutter.ActorAlign.CENTER} :
            {vertical: true, y_align: Clutter.ActorAlign.CENTER});
        text.add_child(new St.Label({text: 'Snip copied to clipboard', style_class: 'winsnipper-toast-title'}));
        text.add_child(new St.Label({text: 'Select here to mark up and share.', style_class: 'winsnipper-toast-body'}));
        row.add_child(text);
        this._openButton.set_child(row);
        this.add_child(this._openButton);
        const close = new St.Button({label: '✕', can_focus: true,
            accessible_name: 'Dismiss snip', style_class: 'winsnipper-toast-close',
            y_align: Clutter.ActorAlign.START});
        this.add_child(close);
        close.connect('clicked', () => this.close());
        this._openButton.connect('clicked', () => {
            const callback = this._onOpen;
            this.close();
            try {
                callback?.();
            } catch (error) {
                report('Could not open toast', error);
            }
        });
        this.connect('notify::hover', () => {
            if (this.hover)
                this._clearTimer();
            else
                this._armTimer();
        });
        this.connect('destroy', () => {
            this._closed = true;
            this._clearTimer();
            this._onOpen = null;
        });
    }

    showToast() {
        Main.layoutManager.addChrome(this, {affectsInputRegion: true, trackFullscreen: false});
        const index = Main.layoutManager.primaryIndex;
        const area = Main.layoutManager.getWorkAreaForMonitor(index);
        const [, width] = this.get_preferred_width(-1);
        const [, height] = this.get_preferred_height(width);
        this.set_size(width, height);
        const position = toastPosition(area, width, height);
        this.set_position(position.x, position.y + 12);
        this.ease({y: position.y, opacity: 255, duration: 180,
            mode: Clutter.AnimationMode.EASE_OUT_QUAD});
        this._armTimer();
    }

    _clearTimer() {
        if (this._timer) {
            GLib.source_remove(this._timer);
            this._timer = 0;
        }
    }

    _armTimer() {
        this._clearTimer();
        if (this._closed || this.hover || !this._timeoutSeconds)
            return;
        this._timer = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, this._timeoutSeconds, () => {
            this._timer = 0;
            this.close();
            return GLib.SOURCE_REMOVE;
        });
    }

    close() {
        if (this._closed)
            return;
        this._closed = true;
        this._clearTimer();
        this.remove_all_transitions();
        this._onOpen = null;
        try {
            if (this.get_parent())
                Main.layoutManager.removeChrome(this);
        } finally {
            this.destroy();
        }
    }
});
