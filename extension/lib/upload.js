import Gio from 'gi://Gio';
import GLib from 'gi://GLib';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';

import {writeUnique} from './capture.js';
import {asyncCall, checkActive, closeStream, makeDirectories} from './io.js';
import {warn} from './log.js';

// Older snips can no longer be edited in practice; forget them eventually.
// Snips still waiting to upload are never forgotten.
const MAX_TRACKED = 100;
const FIRST_RETRY_SECONDS = 30;
const MAX_RETRY_SECONDS = 600;

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

// A queue that copies staged snips to a network folder one at a time, and
// again whenever the editor saves an edit. While the folder is unreachable,
// snips wait in the queue (saved to disk, so they survive a logout) and are
// retried with backoff, or as soon as the network changes. Everything is
// async GIO, so nothing here ever blocks the compositor.
export class Uploader {
    constructor(stateDirectory, cancellable, active) {
        this._cancellable = cancellable;
        this._active = active;
        this._stateDirectory = Gio.File.new_for_path(stateDirectory);
        this._stateFile = this._stateDirectory.get_child('uploads.json');
        // Local path -> {directory, remote, dirty}; directory and remote are URIs.
        this._entries = new Map();
        this._running = false;
        this._retryId = 0;
        this._retrySeconds = FIRST_RETRY_SECONDS;
        this._offline = false;
        this._saving = false;
        this._saveAgain = false;
        this._monitor = Gio.NetworkMonitor.get_default();
        this._networkId = this._monitor.connect('network-changed', (_monitor, available) => {
            if (available && this._retryId)
                this._retryNow();
        });
        this._load().catch(error => {
            if (this._active())
                warn('Could not read the upload queue', error);
        });
    }

    start(path, directory) {
        this._entries.set(path, {directory: directory.get_uri(), remote: null, dirty: true});
        for (const [key, entry] of this._entries) {
            if (this._entries.size <= MAX_TRACKED)
                break;
            if (!entry.dirty)
                this._entries.delete(key);
        }
        this._changed();
    }

    sync(path) {
        const entry = this._entries.get(path);
        if (!entry)
            return;
        entry.dirty = true;
        this._changed();
    }

    destroy() {
        this._monitor.disconnect(this._networkId);
        if (this._retryId)
            GLib.source_remove(this._retryId);
        this._retryId = 0;
    }

    _changed() {
        this._save();
        // While waiting to retry, new snips just join the queue.
        if (!this._retryId)
            this._pump();
    }

    _retryNow() {
        GLib.source_remove(this._retryId);
        this._retryId = 0;
        this._pump();
    }

    async _pump() {
        if (this._running || !this._active())
            return;
        this._running = true;
        try {
            for (;;) {
                const next = [...this._entries].find(([, entry]) => entry.dirty);
                if (!next || !this._active())
                    break;
                const [path, entry] = next;
                entry.dirty = false;
                try {
                    await this._withMount(Gio.File.new_for_uri(entry.directory),
                        () => this._copy(path, entry));
                } catch (error) {
                    if (!this._active())
                        return;
                    if (error.stagedSnipMissing) {
                        // The staged snip was deleted; there is nothing left to upload.
                        this._entries.delete(path);
                        continue;
                    }
                    entry.dirty = true;
                    this._wait(error);
                    break;
                }
                this._retrySeconds = FIRST_RETRY_SECONDS;
                if (this._offline && ![...this._entries.values()].some(other => other.dirty)) {
                    this._offline = false;
                    Main.notify('WinSnipper', 'Queued snips were saved to the network folder');
                }
            }
        } finally {
            this._running = false;
            this._save();
        }
    }

    _wait(error) {
        warn(`Network folder unavailable, retrying in ${this._retrySeconds}s`, error);
        if (!this._offline) {
            this._offline = true;
            Main.notify('WinSnipper',
                `Could not reach the network folder (${error.message}). Snips are queued ` +
                'and will be copied when it is back. Until then they are in ~/.cache/winsnipper.');
        }
        this._retryId = GLib.timeout_add_seconds(GLib.PRIORITY_LOW, this._retrySeconds, () => {
            this._retryId = 0;
            this._pump();
            return GLib.SOURCE_REMOVE;
        });
        this._retrySeconds = Math.min(this._retrySeconds * 2, MAX_RETRY_SECONDS);
    }

    async _copy(path, entry) {
        let input;
        try {
            input = await asyncCall(Gio.File.new_for_path(path), 'read_async', 'read_finish',
                [GLib.PRIORITY_LOW, this._cancellable]);
        } catch (error) {
            error.stagedSnipMissing = error.matches?.(Gio.IOErrorEnum, Gio.IOErrorEnum.NOT_FOUND);
            throw error;
        }
        let output;
        try {
            if (!entry.remote) {
                const file = await writeUnique(Gio.File.new_for_uri(entry.directory), input,
                    Gio.FileCreateFlags.NONE, this._cancellable, this._active);
                entry.remote = file.get_uri();
                return;
            }
            checkActive(this._active);
            output = await asyncCall(Gio.File.new_for_uri(entry.remote), 'replace_async', 'replace_finish',
                [null, false, Gio.FileCreateFlags.NONE, GLib.PRIORITY_LOW, this._cancellable]);
            await asyncCall(output, 'splice_async', 'splice_finish',
                [input, Gio.OutputStreamSpliceFlags.CLOSE_SOURCE, GLib.PRIORITY_LOW, this._cancellable]);
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

    async _load() {
        let contents;
        try {
            [, contents] = await asyncCall(this._stateFile, 'load_contents_async',
                'load_contents_finish', [this._cancellable]);
        } catch (error) {
            if (error.matches?.(Gio.IOErrorEnum, Gio.IOErrorEnum.NOT_FOUND))
                return;
            throw error;
        }
        checkActive(this._active);
        const saved = JSON.parse(new TextDecoder().decode(contents));
        // Snips taken since enable() are newer; keep them after the saved ones.
        const current = this._entries;
        this._entries = new Map();
        for (const {path, directory, remote, dirty} of saved) {
            if (typeof path === 'string' && typeof directory === 'string' && !current.has(path))
                this._entries.set(path, {directory, remote: remote ?? null, dirty: !!dirty});
        }
        for (const [path, entry] of current)
            this._entries.set(path, entry);
        // Snips left over from the last session: say so once they are copied.
        this._offline ||= [...this._entries.values()].some(entry => entry.dirty);
        this._changed();
    }

    // Coalesced: at most one write in flight, plus one more with the newest state.
    async _save() {
        if (this._saving) {
            this._saveAgain = true;
            return;
        }
        this._saving = true;
        try {
            do {
                this._saveAgain = false;
                const state = [...this._entries].map(([path, entry]) => ({path, ...entry}));
                const bytes = new GLib.Bytes(new TextEncoder().encode(JSON.stringify(state)));
                await makeDirectories(this._stateDirectory, this._cancellable, this._active);
                await asyncCall(this._stateFile, 'replace_contents_bytes_async',
                    'replace_contents_finish', [bytes, null, false, Gio.FileCreateFlags.PRIVATE,
                        this._cancellable]);
            } while (this._saveAgain && this._active());
        } catch (error) {
            if (this._active())
                warn('Could not save the upload queue', error);
        } finally {
            this._saving = false;
        }
    }
}
