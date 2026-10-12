// Optional real-GJS boundary checks: gjs -m tests/gjs-io-smoke.js
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import {asyncCall, closeStream, makeDirectories} from '../extension/lib/io.js';

function assert(value, message) {
    if (!value)
        throw new Error(message);
}
const path = GLib.dir_make_tmp('winsnipper-gjs-XXXXXX');
const directory = Gio.File.new_for_path(`${path}/a/b`);
const cancel = new Gio.Cancellable();
await makeDirectories(directory, cancel, () => true);
await makeDirectories(directory, cancel, () => true);
const file = directory.get_child('image.png');
const output = await asyncCall(file, 'create_async', 'create_finish', [Gio.FileCreateFlags.NONE, 0, cancel]);
const bytes = GLib.Bytes.new(new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10]));
const result = await asyncCall(output, 'write_all_async', 'write_all_finish', [bytes.get_data(), 0, cancel]);
assert(result[1] === 8, 'write_all_finish bytes written');
await closeStream(output);
const info = await asyncCall(file, 'query_info_async', 'query_info_finish', ['standard::type,standard::size', Gio.FileQueryInfoFlags.NOFOLLOW_SYMLINKS, 0, cancel]);
assert(info.get_file_type() === Gio.FileType.REGULAR && info.get_size() === 8, 'regular file metadata');
const input = await asyncCall(file, 'read_async', 'read_finish', [0, cancel]);
const openedInfo = await asyncCall(input, 'query_info_async', 'query_info_finish', ['standard::type,standard::size', 0, cancel]);
assert(openedInfo.get_file_type() === Gio.FileType.REGULAR, 'opened stream regular file metadata');
const read = await asyncCall(input, 'read_bytes_async', 'read_bytes_finish', [1024, 0, cancel]);
assert(read.get_size() === 8, 'read_bytes_finish size');
await closeStream(input);
// Explicit callbacks must still work with global Shell-style promisification.
Gio._promisify(Gio.File.prototype, 'query_info_async', 'query_info_finish');
const again = await asyncCall(file, 'query_info_async', 'query_info_finish', ['standard::size', 0, 0, cancel]);
assert(again.get_size() === 8, 'callback after promisify');
const memory = Gio.MemoryOutputStream.new_resizable();
memory.write_bytes(bytes, null);
memory.close(null);
assert(memory.steal_as_bytes().get_size() === 8, 'memory stream byte transfer');
assert(new GLib.Variant('(sava{sv})', ['prewarm', [], {}]).get_type_string() === '(sava{sv})', 'prewarm variant');
assert(new GLib.Variant('(assa{sv})', [['file:///tmp/a.png'], '', {}]).get_type_string() === '(assa{sv})', 'Open variant');
await asyncCall(file, 'delete_async', 'delete_finish', [0, null]);
for (const dir of [directory, directory.get_parent(), Gio.File.new_for_path(path)])
    await asyncCall(dir, 'delete_async', 'delete_finish', [0, null]);
print('GJS IO and activation-variant smoke checks passed');
