// Native GIO cancellation/cleanup coverage: gjs -m tests/gjs-clipboard-reader-smoke.js
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import {readClipboardImage} from '../extension/lib/clipboard-reader.js';

function assert(value, message) {
    if (!value)
        throw new Error(message);
}
const directory = GLib.dir_make_tmp('winsnipper-reader-XXXXXX');
const path = `${directory}/shot.png`;
const signature = new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10]);
function fixture(code) {
    // Synchronous operations here are test setup only, outside the extension.
    Gio.Subprocess.new(['python3', '-I', '-S', '-c', code, path], Gio.SubprocessFlags.NONE).wait_check(null);
}
async function rejects(operation) {
    let rejected = false;
    try {
        await operation;
    } catch (_error) {
        rejected = true;
    }
    assert(rejected, 'Expected the invalid/cancelled read to reject');
}
try {
    GLib.file_set_contents(path, signature);
    const initial = await readClipboardImage(path, new Gio.Cancellable(), () => true);
    assert(initial.get_size() === signature.length, 'The valid PNG bytes must survive transport');
    fixture('import os,sys; os.unlink(sys.argv[1]); os.mkfifo(sys.argv[1])');
    await rejects(readClipboardImage(path, new Gio.Cancellable(), () => true));
    fixture('import os,sys; os.unlink(sys.argv[1]); '
        + 'f=open(sys.argv[1], "wb"); f.write(bytes([137,80,78,71,13,10,26,10])); '
        + 'f.truncate(128*1024*1024); f.close()');
    const cancellable = new Gio.Cancellable();
    const reading = readClipboardImage(path, cancellable, () => !cancellable.is_cancelled());
    GLib.timeout_add(GLib.PRIORITY_DEFAULT, 5, () => {
        cancellable.cancel();
        return GLib.SOURCE_REMOVE;
    });
    await rejects(reading);
    // A cancelled read has reaped its subprocess before the next request starts.
    GLib.file_set_contents(path, signature);
    const next = await readClipboardImage(path, new Gio.Cancellable(), () => true);
    assert(next.get_size() === signature.length, 'A cancelled read cannot wedge the next clipboard update');
    print('GJS reader: valid PNG, FIFO rejection, cancellation and subsequent read passed');
} finally {
    Gio.File.new_for_path(path).delete(null);
    Gio.File.new_for_path(directory).delete(null);
}
