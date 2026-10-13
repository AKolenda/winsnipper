import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import St from 'gi://St';

import {asyncCall, checkActive} from './io.js';
import {readClipboardImage} from './clipboard-reader.js';
import {warn} from './log.js';

const SHELL_NAME = 'io.github.winsnipper.Shell';
const SHELL_PATH = '/io/github/winsnipper/Shell';
const EDITOR_NAME = 'io.github.winsnipper.Editor';
const EDITOR_PATH = '/io/github/winsnipper/Editor';
const INTERFACE = `<node><interface name="io.github.winsnipper.Shell">
    <method name="SetClipboardImage"><arg type="s" name="path" direction="in"/></method>
    <method name="Snip"/>
</interface></node>`;

export class ShellService {
    constructor(startSnip, cancellable, active) {
        this._active = active;
        this._cancellable = cancellable;
        this._startSnip = startSnip;
        this._clipboardRead = null;
        this._clipboardPending = null;
        this._destroyed = false;
        this._object = Gio.DBusExportedObject.wrapJSObject(INTERFACE, this);
        try {
            this._object.export(Gio.DBus.session, SHELL_PATH);
            this._owner = Gio.bus_own_name_on_connection(Gio.DBus.session, SHELL_NAME,
                Gio.BusNameOwnerFlags.NONE, null,
                () => {
                    if (active())
                        warn('Shell D-Bus name is unavailable');
                });
        } catch (error) {
            this._object.unexport();
            throw error;
        }
    }

    SnipAsync(_params, invocation) {
        try {
            checkActive(this._active);
            this._startSnip();
            invocation.return_value(null);
        } catch (error) {
            invocation.return_dbus_error(`${SHELL_NAME}.Error`, error.message);
        }
    }

    SetClipboardImageAsync([path], invocation) {
        try {
            checkActive(() => !this._destroyed && this._active());
            const request = {path, invocation, cancellable: new Gio.Cancellable()};
            // A request can retain almost 256 MiB. Keep only one read and one
            // newest pending path, and stop an obsolete read before starting it.
            if (this._clipboardRead) {
                this._clipboardRead.cancellable.cancel();
                this._cancelPendingClipboard();
                this._clipboardPending = request;
            } else {
                this._readClipboard(request);
            }
        } catch (error) {
            invocation.return_dbus_error(`${SHELL_NAME}.Error`, error.message);
        }
    }

    _cancelPendingClipboard() {
        this._clipboardPending?.invocation.return_dbus_error(
            `${SHELL_NAME}.Cancelled`, 'Clipboard request was superseded or cancelled');
        this._clipboardPending = null;
    }

    setClipboardBytes(bytes) {
        checkActive(() => !this._destroyed && this._active());
        this._clipboardRead?.cancellable.cancel();
        this._cancelPendingClipboard();
        St.Clipboard.get_default().set_content(St.ClipboardType.CLIPBOARD, 'image/png', bytes);
    }

    async _readClipboard(request) {
        this._clipboardRead = request;
        const active = () => !this._destroyed && this._active() && !request.cancellable.is_cancelled();
        try {
            const bytes = await readClipboardImage(request.path, request.cancellable, active);
            checkActive(active);
            St.Clipboard.get_default().set_content(St.ClipboardType.CLIPBOARD, 'image/png', bytes);
            request.invocation.return_value(null);
        } catch (error) {
            const cancelled = !active();
            if (!cancelled)
                warn('SetClipboardImage failed', error);
            request.invocation.return_dbus_error(
                `${SHELL_NAME}.${cancelled ? 'Cancelled' : 'Error'}`, error.message);
        } finally {
            this._clipboardRead = null;
            const pending = this._clipboardPending;
            this._clipboardPending = null;
            if (pending)
                this._readClipboard(pending);
        }
    }

    destroy() {
        this._destroyed = true;
        this._clipboardRead?.cancellable.cancel();
        this._cancelPendingClipboard();
        if (this._owner) {
            Gio.bus_unown_name(this._owner);
            this._owner = 0;
        }
        this._object?.unexport();
        this._object = null;
        this._startSnip = null;
    }
}

export class EditorClient {
    constructor(cancellable, active) {
        this._cancellable = cancellable;
        this._active = active;
    }

    prewarm(path = '') {
        const action = path ? 'prewarm-path' : 'prewarm';
        const parameters = path ? [new GLib.Variant('s', path)] : [];
        this._call('ActivateAction', new GLib.Variant('(sava{sv})', [action, parameters, {}]),
            ['--gapplication-service']);
    }

    open(path) {
        this._call('Open', new GLib.Variant('(asa{sv})',
            [[Gio.File.new_for_path(path).get_uri()], {}]), [path]);
    }

    async _launch(fallbackArgs) {
        const userLauncher = GLib.build_filenamev([GLib.get_home_dir(), '.local', 'bin', 'winsnipper-editor']);
        let launcher = 'winsnipper-editor';
        let flags = GLib.SpawnFlags.SEARCH_PATH;
        try {
            const info = await asyncCall(Gio.File.new_for_path(userLauncher),
                'query_info_async', 'query_info_finish',
                ['standard::type,access::can-execute', Gio.FileQueryInfoFlags.NONE,
                    GLib.PRIORITY_DEFAULT, this._cancellable]);
            if (info.get_file_type() === Gio.FileType.REGULAR && info.get_attribute_boolean('access::can-execute')) {
                launcher = userLauncher;
                flags = GLib.SpawnFlags.DEFAULT;
            }
        } catch (error) {
            if (error?.matches?.(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED))
                return;
            // A missing/unavailable per-user install may still have a system
            // launcher. Let the child search PATH instead of probing it here.
        }
        if (this._active())
            GLib.spawn_async(null, [launcher, ...fallbackArgs], null, flags, null);
    }

    _call(method, parameters, fallbackArgs) {
        if (!this._active())
            return;
        const failed = error => {
            if (!this._active())
                return;
            // A slow D-Bus activation may still succeed; spawning would race it.
            if (error?.matches?.(Gio.IOErrorEnum, Gio.IOErrorEnum.TIMED_OUT) ||
                error?.matches?.(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED) ||
                error?.matches?.(Gio.DBusError, Gio.DBusError.TIMEOUT) ||
                error?.matches?.(Gio.DBusError, Gio.DBusError.TIMED_OUT) ||
                error?.matches?.(Gio.DBusError, Gio.DBusError.NO_REPLY)) {
                warn(`Editor ${method} did not reply in time`, error);
                return;
            }
            warn(`Editor ${method} failed; trying launcher`, error);
            this._launch(fallbackArgs).catch(spawnError => {
                warn('Could not launch editor', spawnError);
            });
        };
        try {
            Gio.DBus.session.call(EDITOR_NAME, EDITOR_PATH, 'org.freedesktop.Application',
                method, parameters, null, Gio.DBusCallFlags.NONE, 5000, this._cancellable,
                (connection, result) => {
                    try {
                        connection.call_finish(result);
                    } catch (error) {
                        failed(error);
                    }
                });
        } catch (error) {
            failed(error);
        }
    }
}
