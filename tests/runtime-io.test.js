import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {mkdtemp, open, rm, stat, unlink} from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {test} from 'node:test';
import vm from 'node:vm';
import {uniqueFilename} from '../extension/lib/geometry.js';

const source = file => readFileSync(new URL(file, import.meta.url), 'utf8')
    .replace(/^import .+;\n/gm, '').replace(/^export /gm, '');
const flush = () => new Promise(resolve => setImmediate(resolve));

test('capture files remain private with a permissive desktop umask', async () => {
    const directory = await mkdtemp(path.join(os.tmpdir(), 'winsnipper-private-'));
    const previous = process.umask(0o022);
    const finish = result => {
        if (result instanceof Error)
            throw result;
        return result;
    };
    function file(filename) {
        return {
            get_path: () => filename,
            get_child: name => file(path.join(filename, name)),
            create_async(flags, _priority, _cancel, callback) {
                open(filename, 'wx', flags & 1 ? 0o600 : 0o666).then(handle => {
                    let closed = false;
                    const stream = {
                        write_all_async(bytes, _prio, _cancellable, done) {
                            handle.writeFile(bytes).then(() => done(stream, [true, bytes.length]),
                                error => done(stream, error));
                        },
                        write_all_finish: finish,
                        close_async(_prio, _cancellable, done) {
                            handle.close().then(() => { closed = true; done(stream, true); },
                                error => done(stream, error));
                        },
                        close_finish: finish,
                        is_closed: () => closed,
                    };
                    callback(this, stream);
                }, error => callback(this, error));
            },
            create_finish: finish,
            delete_async(_priority, _cancel, callback) {
                unlink(filename).then(() => callback(this, true), error => callback(this, error));
            },
            delete_finish: finish,
        };
    }
    const context = {
        Gio: {File: {new_for_path: file}, FileCreateFlags: {NONE: 0, PRIVATE: 1}},
        GLib: {PRIORITY_DEFAULT: 0}, uniqueFilename, warn() {},
    };
    const helpers = vm.runInNewContext(`${source('../extension/lib/io.js')}\n({asyncCall, closeStream, checkActive})`, context);
    const saveCapture = vm.runInNewContext(`${source('../extension/lib/capture.js')}\nsaveCapture`, {
        ...context, ...helpers, makeDirectories: async () => {},
    });
    try {
        const saved = await saveCapture({get_data: () => Buffer.from('private screenshot')}, directory, null, () => true);
        assert.equal((await stat(saved)).mode & 0o777, 0o600);
        assert.equal(readFileSync(saved, 'utf8'), 'private screenshot');
    } finally {
        process.umask(previous);
        await rm(directory, {recursive: true, force: true});
    }
});

function serviceHarness() {
    const reads = [];
    const clipboard = [];
    class Cancellable {
        cancel() { this.cancelled = true; }
        is_cancelled() { return this.cancelled ?? false; }
    }
    const ShellService = vm.runInNewContext(`${source('../extension/lib/dbus.js')}
        readClipboardImage = readImage;
        ShellService`, {
        Gio: {
            Cancellable,
            DBusExportedObject: {wrapJSObject: () => ({export() {}, unexport() {}})},
            DBus: {session: {}}, BusNameOwnerFlags: {NONE: 0},
            bus_own_name_on_connection: () => 1, bus_unown_name() {},
        },
        St: {Clipboard: {get_default: () => ({set_content(_type, _mime, bytes) { clipboard.push(bytes); }})},
            ClipboardType: {CLIPBOARD: 1}},
        readImage: (filename, cancellable) => new Promise(resolve => reads.push({filename, cancellable, resolve})),
        checkActive(active) { if (!active()) throw new Error('Inactive request'); }, warn() {},
    });
    const service = new ShellService(() => {}, new Cancellable(), () => true);
    const invoke = filename => {
        const replies = [];
        service.SetClipboardImageAsync([filename], {
            return_value: value => replies.push({value}),
            return_dbus_error: (name, message) => replies.push({name, message}),
        });
        return replies;
    };
    return {service, invoke, reads, clipboard};
}

test('clipboard reads are bounded and coalesce to the newest request', async () => {
    const h = serviceHarness();
    const first = h.invoke('/first.png');
    const second = h.invoke('/second.png');
    const third = h.invoke('/third.png');
    assert.equal(h.reads.length, 1, 'only one potentially 256 MiB read may run');
    assert.equal(second.length, 1, 'superseded pending invocation must be answered');
    assert.ok(second[0].name.endsWith('.Cancelled'));
    h.reads[0].resolve('first bytes');
    await flush();
    assert.equal(first.length, 1);
    assert.equal(h.reads.length, 2);
    assert.equal(h.reads[1].filename, '/third.png');
    h.reads[1].resolve('third bytes');
    await flush();
    assert.deepEqual(h.clipboard, ['third bytes']);
    assert.equal(third.length, 1);
    h.service.destroy();
});

test('destroy cancels current clipboard IO and answers pending requests', async () => {
    const h = serviceHarness();
    const first = h.invoke('/first.png');
    const second = h.invoke('/second.png');
    h.service.destroy();
    assert.equal(h.reads[0].cancellable.is_cancelled(), true);
    assert.equal(second.length, 1);
    h.reads[0].resolve('late bytes');
    await flush();
    assert.deepEqual(h.clipboard, []);
    assert.equal(first.length, 1);
    assert.equal(h.reads.length, 1);
});

test('a new capture cancels editor reads before taking the clipboard', async () => {
    const h = serviceHarness();
    const first = h.invoke('/old-edit.png');
    const second = h.invoke('/old-pending-edit.png');
    h.service.setClipboardBytes('new capture bytes');
    assert.equal(second[0].name, 'io.github.winsnipper.Shell.Cancelled');
    assert.equal(h.reads[0].cancellable.is_cancelled(), true);
    h.reads[0].resolve('old edit bytes');
    await flush();
    assert.equal(first[0].name, 'io.github.winsnipper.Shell.Cancelled');
    assert.equal(h.reads.length, 1);
    assert.deepEqual(h.clipboard, ['new capture bytes']);
    h.service.destroy();
});
