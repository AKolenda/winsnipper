import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import St from 'gi://St';

import {asyncCall, checkActive, closeStream} from './io.js';
import {warn} from './log.js';

const SHELL_NAME = 'io.github.winsnipper.Shell';
const SHELL_PATH = '/io/github/winsnipper/Shell';
const EDITOR_NAME = 'io.github.winsnipper.Editor';
const EDITOR_PATH = '/io/github/winsnipper/Editor';
const MAX_IMAGE_BYTES = 256 * 1024 * 1024;
const INTERFACE = `<node><interface name="io.github.winsnipper.Shell">
    <method name="SetClipboardImage"><arg type="s" name="path" direction="in"/></method>
    <method name="Snip"/>
</interface></node>`;

async function readClipboardImage(path, cancellable, active) {
    if (!GLib.path_is_absolute(path) || !path.endsWith('.png'))
        throw new Error('Expected an absolute .png path');
    const file = Gio.File.new_for_path(path);
    const info = await asyncCall(file, 'query_info_async', 'query_info_finish',
        ['standard::type,standard::size', Gio.FileQueryInfoFlags.NOFOLLOW_SYMLINKS,
            GLib.PRIORITY_DEFAULT, cancellable]);
    checkActive(active);
    if (info.get_file_type() !== Gio.FileType.REGULAR || info.get_size() >= MAX_IMAGE_BYTES)
        throw new Error('PNG must be a regular file smaller than 256 MiB');
    const stream = await asyncCall(file, 'read_async', 'read_finish',
        [GLib.PRIORITY_DEFAULT, cancellable]);
    try {
        checkActive(active);
        const openedInfo = await asyncCall(stream, 'query_info_async', 'query_info_finish',
            ['standard::type,standard::size', GLib.PRIORITY_DEFAULT, cancellable]);
        checkActive(active);
        if (openedInfo.get_file_type() !== Gio.FileType.REGULAR || openedInfo.get_size() >= MAX_IMAGE_BYTES)
            throw new Error('The opened PNG is not an acceptable regular file');
        const output = Gio.MemoryOutputStream.new_resizable();
        let total = 0;
        try {
            for (;;) {
                const bytes = await asyncCall(stream, 'read_bytes_async', 'read_bytes_finish',
                    [1024 * 1024, GLib.PRIORITY_DEFAULT, cancellable]);
                checkActive(active);
                if (!bytes.get_size())
                    break;
                if (!total) {
                    const data = bytes.get_data();
                    const signature = [137, 80, 78, 71, 13, 10, 26, 10];
                    if (!signature.every((value, index) => data[index] === value))
                        throw new Error('File does not have a PNG signature');
                }
                total += bytes.get_size();
                if (total >= MAX_IMAGE_BYTES)
                    throw new Error('PNG grew beyond the 256 MiB limit');
                // In-memory writes only; each async read yields to the compositor.
                // Avoid retaining 256 MiB of JS chunks and copying them again.
                output.write_bytes(bytes, null);
            }
            if (!total)
                throw new Error('PNG is empty');
            output.close(null);
            return output.steal_as_bytes();
        } finally {
            if (!output.is_closed())
                output.close(null);
        }
    } finally {
        await closeStream(stream);
    }
}

export class ShellService {
    constructor(startSnip, cancellable, active) {
        this._active = active;
        this._cancellable = cancellable;
        this._startSnip = startSnip;
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

    async SetClipboardImageAsync([path], invocation) {
        try {
            checkActive(this._active);
            const bytes = await readClipboardImage(path, this._cancellable, this._active);
            checkActive(this._active);
            St.Clipboard.get_default().set_content(St.ClipboardType.CLIPBOARD, 'image/png', bytes);
            invocation.return_value(null);
        } catch (error) {
            warn('SetClipboardImage failed', error);
            invocation.return_dbus_error(`${SHELL_NAME}.Error`, error.message);
        }
    }

    destroy() {
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

    prewarm() {
        this._call('ActivateAction', new GLib.Variant('(sava{sv})', ['prewarm', [], {}]),
            ['--gapplication-service']);
    }

    open(path) {
        this._call('Open', new GLib.Variant('(assa{sv})',
            [[Gio.File.new_for_path(path).get_uri()], '', {}]), [path]);
    }

    _call(method, parameters, fallbackArgs) {
        if (!this._active())
            return;
        const failed = error => {
            if (!this._active())
                return;
            warn(`Editor ${method} failed; trying launcher`, error);
            try {
                const launcher = GLib.build_filenamev([GLib.get_home_dir(), '.local', 'bin', 'winsnipper-editor']);
                GLib.spawn_async(null, [launcher, ...fallbackArgs], null,
                    GLib.SpawnFlags.DEFAULT, null);
            } catch (spawnError) {
                warn('Could not launch editor', spawnError);
            }
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
