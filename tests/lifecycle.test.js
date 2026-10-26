import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';
import vm from 'node:vm';

// Run the actual extension controller with compositor boundaries stubbed out.
// The tests assert observable lifecycle behavior, not actor implementation details.
const source = readFileSync(new URL('../extension/extension.js', import.meta.url), 'utf8')
    .replace(/^import .+;\n/gm, '').replace('export default class', 'class');

function deferred() {
    let resolve;
    let reject;
    const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
    return {promise, resolve, reject};
}
const flush = () => new Promise(resolve => setImmediate(resolve));

function harness() {
    const events = [];
    const captures = [];
    let encoder = async () => ({bytes: 'PNG', pixbuf: 'preview'});
    let saver = async () => '/tmp/shot.png';
    let snapshot = () => [];
    let network = null;
    const signals = () => ({
        connect(name, handler) { events.push(`connect:${name}`); this[name] = handler; return name; },
        disconnect(id) { events.push(`disconnect:${id}`); delete this[id]; },
    });
    const layout = signals();
    const display = signals();
    const settings = {get_string: () => 'rectangle', get_int: () => 7};
    class Base { getSettings() { return settings; } }
    class Overlay {
        constructor(content, bounds, windows, mode, finish, cancel) {
            events.push('overlay:new');
            Object.assign(this, {content, bounds, windows, mode, finish, cancel});
        }
        open() { events.push('overlay:open'); }
        close() { events.push('overlay:close'); }
        complete() { this.close(); this.finish({x: 1, y: 2, width: 3, height: 4}); }
    }
    class Toast {
        constructor(_pixbuf, _timeout, open) { events.push('toast:new'); this.open = open; }
        connect() {}
        showToast() { events.push('toast:show'); }
        close() { events.push('toast:close'); }
    }
    const context = {
        Extension: Base,
        Gio: {Cancellable: class { cancel() { events.push('io:cancel'); } }},
        Meta: {KeyBindingFlags: {IGNORE_AUTOREPEAT: 1}},
        Shell: {Screenshot: class {}, ActionMode: {NORMAL: 1, OVERVIEW: 2}},
        Main: {layoutManager: layout, overview: {visible: false}, wm: {
            addKeybinding(_name, _settings, flags) { assert.equal(flags, 1); events.push('keybinding:add'); },
            removeKeybinding() { events.push('keybinding:remove'); },
        }},
        global: {stage: {width: 1920, height: 1080}, display},
        EditorClient: class {
            prewarm(path = '') { events.push(path ? `editor:prewarm:${path}` : 'editor:prewarm'); }
            open(path) { events.push(`editor:open:${path}`); }
        },
        ShellService: class {
            setClipboardBytes(bytes) { assert.equal(bytes, 'PNG'); events.push('clipboard:set'); }
            destroy() { events.push('dbus:destroy'); }
        },
        SelectionOverlay: Overlay,
        SnipToast: Toast,
        requestCapture() { events.push('capture'); const value = deferred(); captures.push(value); return value.promise; },
        snapshotWindows() { events.push('snapshot'); return snapshot(); },
        encodeCapture(...args) { events.push('encode'); return encoder(...args); },
        saveDirectory() { return '/tmp'; },
        networkDirectory: () => network,
        Uploader: class {
            start(path, directory) { events.push(`upload:${path}:${directory}`); }
            sync(path) { events.push(`sync:${path}`); }
        },
        saveCapture(...args) { events.push('save'); return saver(...args); },
        report(message) { events.push(`error:${message}`); },
    };
    const Controller = vm.runInNewContext(`${source}\nWinSnipper`, context);
    const extension = new Controller();
    extension.enable();
    events.length = 0;
    return {extension, events, captures, layout, display, context,
        encoder(value) { encoder = value; }, saver(value) { saver = value; },
        network(value) { network = value; },
        snapshot(value) { snapshot = value; }};
}

test('capture is requested before snapshot/overlay, and reentrant shortcuts are ignored', async () => {
    const {extension, events, captures} = harness();
    extension._startSnip();
    extension._startSnip();
    assert.deepEqual(events, ['capture', 'snapshot']);
    assert.equal(captures.length, 1);
    captures[0].resolve(['frozen texture', 1.25]);
    await flush();
    assert.deepEqual(events.slice(2), ['overlay:new', 'overlay:open']);
    extension._startSnip();
    assert.equal(captures.length, 1);
    extension.disable();
});

test('completion closes overlay before encoding, shows toast and warms the saved path, then unlocks', async () => {
    const {extension, events, captures} = harness();
    extension._startSnip();
    captures[0].resolve(['frozen texture', 2]);
    await flush();
    extension._overlay.complete();
    extension._startSnip();
    assert.equal(captures.length, 1, 'shortcut remains locked while completion is pending');
    await flush();
    assert.deepEqual(events.slice(4), ['overlay:close', 'encode', 'clipboard:set', 'save', 'toast:new', 'toast:show', 'editor:prewarm', 'editor:prewarm:/tmp/shot.png']);
    extension._startSnip();
    assert.equal(captures.length, 2);
    extension.disable();
    captures[1].resolve(['late texture', 1]);
    await flush();
});

test('a network folder is uploaded after the local save; the toast and editor stay local', async () => {
    const {extension, events, captures, network} = harness();
    network('smb://nas/shots');
    extension._startSnip();
    captures[0].resolve(['frozen texture', 1]);
    await flush();
    extension._overlay.complete();
    await flush();
    assert.deepEqual(events.slice(4), ['overlay:close', 'encode', 'clipboard:set', 'save', 'toast:new',
        'toast:show', 'editor:prewarm', 'upload:/tmp/shot.png:smb://nas/shots', 'editor:prewarm:/tmp/shot.png']);
    await extension._toast.open();
    assert.equal(events.at(-1), 'editor:open:/tmp/shot.png');
    extension.disable();
});

test('disable during capture prevents overlay and releases every registered resource', async () => {
    const {extension, events, captures} = harness();
    extension._startSnip();
    extension.disable();
    const afterDisable = events.slice();
    captures[0].resolve(['late texture', 1]);
    await flush();
    assert.deepEqual(events, afterDisable);
    for (const event of ['io:cancel', 'keybinding:remove', 'disconnect:monitors-changed',
        'dbus:destroy'])
        assert.ok(events.includes(event), event);
});

test('old capture cannot attach UI after disable and re-enable', async () => {
    const {extension, events, captures} = harness();
    extension._startSnip();
    extension.disable();
    extension.enable();
    extension._startSnip();
    events.length = 0;
    captures[0].resolve(['old texture', 1]);
    await flush();
    assert.deepEqual(events, []);
    captures[1].resolve(['current texture', 2]);
    await flush();
    assert.deepEqual(events, ['overlay:new', 'overlay:open']);
    extension.disable();
});

test('disable during encoding prevents all later save/toast/editor activity', async () => {
    const h = harness();
    const encoding = deferred();
    h.encoder(() => encoding.promise);
    h.extension._startSnip();
    h.captures[0].resolve(['frozen texture', 1]);
    await flush();
    h.extension._overlay.complete();
    h.extension.disable();
    const afterDisable = h.events.slice();
    encoding.resolve({bytes: 'PNG', pixbuf: 'preview'});
    await flush();
    assert.deepEqual(h.events, afterDisable);
});

test('disable during file writing closes the early toast and prevents later prewarm', async () => {
    const h = harness();
    const writing = deferred();
    h.saver(() => writing.promise);
    h.extension._startSnip();
    h.captures[0].resolve(['frozen texture', 1]);
    await flush();
    h.extension._overlay.complete();
    await flush();
    assert.ok(h.events.includes('save'));
    h.extension.disable();
    const afterDisable = h.events.slice();
    writing.resolve('/tmp/shot.png');
    await flush();
    assert.deepEqual(h.events, afterDisable);
});

test('monitor changes discard pending capture and wait for it before permitting another', async () => {
    const {extension, events, captures, layout} = harness();
    extension._startSnip();
    layout['monitors-changed']();
    extension._startSnip();
    assert.equal(captures.length, 1);
    captures[0].resolve(['stale dimensions', 1]);
    await flush();
    assert.ok(!events.includes('overlay:new'));
    extension._startSnip();
    assert.equal(captures.length, 2);
    captures[1].resolve(['current dimensions', 1]);
    await flush();
    assert.ok(events.includes('overlay:new'));
    extension.disable();
});

test('capture or encoding failure logs an error and permits retry', async () => {
    const h = harness();
    h.extension._startSnip();
    h.captures[0].reject(new Error('capture failed'));
    await flush();
    assert.ok(h.events.includes('error:Snip failed'));
    h.extension._startSnip();
    h.captures[1].resolve(['texture', 1]);
    await flush();
    h.encoder(async () => { throw new Error('encoder failed'); });
    h.extension._overlay.complete();
    await flush();
    assert.ok(h.events.includes('error:Could not encode or save snip'));
    h.extension._startSnip();
    assert.equal(h.captures.length, 3);
    h.extension.disable();
    h.captures[2].resolve(['late', 1]);
    await flush();
});


test('toast appears before save resolves; an early click awaits the saved path', async () => {
    const h = harness();
    const writing = deferred();
    h.saver(() => writing.promise);
    h.extension._startSnip();
    h.captures[0].resolve(['texture', 1]);
    await flush();
    h.extension._overlay.complete();
    await flush();
    assert.ok(h.events.includes('toast:show'));
    assert.ok(!h.events.some(e => e.startsWith('editor:open:')));
    const clicked = h.extension._toast.open();
    await flush();
    assert.ok(!h.events.some(e => e.startsWith('editor:open:')));
    writing.resolve('/tmp/ready.png');
    await clicked;
    await flush();
    assert.ok(h.events.includes('editor:open:/tmp/ready.png'));
    assert.ok(h.events.includes('editor:prewarm:/tmp/ready.png'));
    h.extension.disable();
});

test('failed save reports once and never opens or decodes a path after an early click', async () => {
    const h = harness();
    const writing = deferred();
    h.saver(() => writing.promise);
    h.extension._startSnip();
    h.captures[0].resolve(['texture', 1]);
    await flush();
    h.extension._overlay.complete();
    await flush();
    const clicked = h.extension._toast.open();
    writing.reject(new Error('disk full'));
    await clicked;
    await flush();
    assert.equal(h.events.filter(e => e === 'error:Could not encode or save snip').length, 1);
    assert.ok(!h.events.some(e => e.startsWith('editor:open:') || e.startsWith('editor:prewarm:')));
    assert.equal(h.extension._busy, false);
    h.extension.disable();
});

test('disable while an early click awaits save prevents editor activation', async () => {
    const h = harness();
    const writing = deferred();
    h.saver(() => writing.promise);
    h.extension._startSnip();
    h.captures[0].resolve(['texture', 1]);
    await flush();
    h.extension._overlay.complete();
    await flush();
    const clicked = h.extension._toast.open();
    h.extension.disable();
    writing.resolve('/tmp/late.png');
    await clicked;
    assert.ok(!h.events.some(e => e.startsWith('editor:open:')));
});

test('workarea changes are not connected; monitor changes still cancel a live selection', async () => {
    const h = harness();
    assert.equal(h.display['workareas-changed'], undefined);
    h.extension._startSnip();
    h.captures[0].resolve(['texture', 1]);
    await flush();
    h.layout['monitors-changed']();
    assert.ok(h.events.includes('overlay:close'));
    h.extension.disable();
});

test('overview snips offer no window targets (frame rects do not match the clones)', async () => {
    const {extension, events, captures, context} = harness();
    context.Main.overview.visible = true;
    extension._startSnip();
    assert.deepEqual(events, ['capture']);
    captures[0].resolve(['frozen texture', 1]);
    await flush();
    assert.equal(extension._overlay.windows.length, 0);
    extension.disable();
});
