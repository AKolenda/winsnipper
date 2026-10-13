import Gio from 'gi://Gio';
import GLib from 'gi://GLib';

import {asyncCall, checkActive, closeStream} from './io.js';

const MAX_IMAGE_BYTES = 256 * 1024 * 1024;
const READER_PATH = Gio.File.new_for_uri(import.meta.url).get_parent()
    .get_child('read-clipboard.py').get_path();

export async function readClipboardImage(path, cancellable, active) {
    checkActive(active);
    if (!GLib.path_is_absolute(path) || !path.endsWith('.png'))
        throw new Error('Expected an absolute .png path');
    // Gio.File.read_async can block a worker forever opening a replaced FIFO;
    // cancelling that task does not interrupt open(). Open with NONBLOCK and
    // validate the descriptor in a killable helper, keeping path IO out of Shell.
    const process = Gio.Subprocess.new(['python3', '-I', '-S', READER_PATH, path],
        Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE);
    const stream = process.get_stdout_pipe();
    const errors = process.get_stderr_pipe();
    const output = Gio.MemoryOutputStream.new_resizable();
    let reaped = false;
    let total = 0;
    try {
        for (;;) {
            const bytes = await asyncCall(stream, 'read_bytes_async', 'read_bytes_finish',
                [1024 * 1024, GLib.PRIORITY_DEFAULT, cancellable]);
            checkActive(active);
            if (!bytes.get_size())
                break;
            total += bytes.get_size();
            if (total >= MAX_IMAGE_BYTES)
                throw new Error('PNG grew beyond the 256 MiB limit');
            output.write_bytes(bytes, null); // In-memory only.
        }
        await asyncCall(process, 'wait_async', 'wait_finish', [cancellable]);
        reaped = true;
        checkActive(active);
        if (!process.get_successful()) {
            // The isolated helper emits at most 256 characters; never retain
            // an unbounded error stream or include screenshot bytes in logs.
            const detail = await asyncCall(errors, 'read_bytes_async', 'read_bytes_finish',
                [1024, GLib.PRIORITY_DEFAULT, cancellable]);
            const message = new TextDecoder().decode(detail.get_data()).trim();
            throw new Error(message || 'Could not read the PNG screenshot');
        }
        if (total < 8)
            throw new Error('PNG is empty');
        output.close(null);
        return output.steal_as_bytes();
    } finally {
        if (!output.is_closed())
            output.close(null);
        if (!reaped) {
            process.force_exit();
            // Do not release the service's single read slot until its helper
            // has exited. Cleanup is independent of request cancellation.
            await asyncCall(process, 'wait_async', 'wait_finish', [null]);
        }
        await Promise.all([closeStream(stream), closeStream(errors)]);
    }
}
