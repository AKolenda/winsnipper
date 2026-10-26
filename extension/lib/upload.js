import Gio from 'gi://Gio';
import GLib from 'gi://GLib';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';

import {writeUnique} from './capture.js';
import {asyncCall, checkActive, closeStream} from './io.js';
import {report} from './log.js';

// Older snips can no longer be edited in practice; forget them eventually.
const MAX_TRACKED = 100;

function mountOperation() {
    // gvfs tries the keyring first; this only answers when nothing is saved.
    const operation = new Gio.MountOperation();
    operation.connect('ask-password', (op, _message, _user, _domain, flags) => {
        if (flags & Gio.AskPasswordFlags.ANONYMOUS_SUPPORTED) {
            op.anonymous = true;
            op.reply(Gio.MountOperationResult.HANDLED);
        } else {
            op.reply(Gio.MountOperationResult.ABORTED);
        }
    });
    return operation;
}

// Copies staged snips to a network folder, and copies them again whenever the
// editor saves an edit, so the share always has the latest version.
export class Uploader {
    constructor(cancellable, active) {
        this._cancellable = cancellable;
        this._active = active;
        // Local path -> {directory, file, running, dirty}.
        this._targets = new Map();
    }

    start(path, directory) {
        this._targets.set(path, {directory, file: null, running: false, dirty: false});
        for (const [key, target] of this._targets) {
            if (this._targets.size <= MAX_TRACKED)
                break;
            if (!target.running)
                this._targets.delete(key);
        }
        this.sync(path);
    }

    sync(path) {
        const target = this._targets.get(path);
        if (!target)
            return;
        target.dirty = true;
        if (!target.running)
            this._run(path, target);
    }

    async _run(path, target) {
        target.running = true;
        try {
            while (target.dirty && this._active()) {
                target.dirty = false;
                try {
                    await this._withMount(target.directory, () => this._copy(path, target));
                } catch (error) {
                    if (!this._active())
                        return;
                    report('Could not copy snip to the network folder', error);
                    Main.notifyError('Could not save snip to the network folder',
                        `${error.message}\nIt is still in ${path}`);
                }
            }
        } finally {
            target.running = false;
        }
    }

    async _copy(path, target) {
        const input = await asyncCall(Gio.File.new_for_path(path), 'read_async', 'read_finish',
            [GLib.PRIORITY_DEFAULT, this._cancellable]);
        let output;
        try {
            if (!target.file) {
                target.file = await writeUnique(target.directory, input, Gio.FileCreateFlags.NONE,
                    this._cancellable, this._active);
                return;
            }
            checkActive(this._active);
            output = await asyncCall(target.file, 'replace_async', 'replace_finish',
                [null, false, Gio.FileCreateFlags.NONE, GLib.PRIORITY_DEFAULT, this._cancellable]);
            await asyncCall(output, 'splice_async', 'splice_finish',
                [input, Gio.OutputStreamSpliceFlags.CLOSE_SOURCE, GLib.PRIORITY_DEFAULT, this._cancellable]);
            await closeStream(output);
        } finally {
            if (!input.is_closed())
                await closeStream(input);
            if (output && !output.is_closed())
                await closeStream(output);
        }
    }

    async _withMount(directory, operation) {
        try {
            return await operation();
        } catch (error) {
            if (!error.matches?.(Gio.IOErrorEnum, Gio.IOErrorEnum.NOT_MOUNTED))
                throw error;
        }
        checkActive(this._active);
        try {
            await asyncCall(directory, 'mount_enclosing_volume', 'mount_enclosing_volume_finish',
                [Gio.MountMountFlags.NONE, mountOperation(), this._cancellable]);
        } catch (error) {
            if (!error.matches?.(Gio.IOErrorEnum, Gio.IOErrorEnum.ALREADY_MOUNTED))
                throw error;
        }
        return operation();
    }
}
