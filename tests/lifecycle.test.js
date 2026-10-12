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
        constructor() { events.push('toast:new'); }
        connect() {}
        showToast() { events.push('toast:show'); }
        close() { events.push('toast:close'); }
    }
    const context = {
        Extension: Base,
        Gio: {Cancellable: class { cancel() { events.push('io:cancel'); } }},
        Meta: {KeyBindingFlags: {NONE: 0}},
        Shell: {Screenshot: class {}, ActionMode: {NORMAL: 1, OVERVIEW: 2}},
        Main: {layoutManager: layout, wm: {
            addKeybinding() { events.push('keybinding:add'); },
            removeKeybinding() { events.push('keybinding:remove'); },
        }},
        global: {stage: {width: 1920, height: 1080}, display},
        EditorClient: class { prewarm() { events.push('editor:prewarm'); } },
        ShellService: class { destroy() { events.push('dbus:destroy'); } },
        SelectionOverlay: Overlay,
        SnipToast: Toast,
        requestCapture() { events.push('capture'); const value = deferred(); captures.push(value); return value.promise; },
        snapshotWindows() { events.push('snapshot'); return snapshot(); },
        encodeCapture(...args) { events.push('encode'); return encoder(...args); },
        saveDirectory() { return '/tmp'; },
        saveCapture(...args) { events.push('save'); return saver(...args); },
        report(message) { events.push(`error:${message}`); },
    };
    const Controller = vm.runInNewContext(`${source}\nWinSnipper`, context);
    const extension = new Controller();
    extension.enable();
    events.length = 0;
    return {extension, events, captures, layout,
        encoder(value) { encoder = value; }, saver(value) { saver = value; },
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

test('completion closes overlay before encoding, writes before toast and prewarm, then unlocks', async () => {
    const {extension, events, captures} = harness();
    extension._startSnip();
    captures[0].resolve(['frozen texture', 2]);
    await flush();
    extension._overlay.complete();
    extension._startSnip();
    assert.equal(captures.length, 1, 'shortcut remains locked while completion is pending');
    await flush();
    assert.deepEqual(events.slice(4), ['overlay:close', 'encode', 'save', 'toast:new', 'toast:show', 'editor:prewarm']);
    extension._startSnip();
    assert.equal(captures.length, 2);
    extension.disable();
    captures[1].resolve(['late texture', 1]);
    await flush();
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
        'disconnect:workareas-changed', 'dbus:destroy'])
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

test('disable during file writing prevents toast and prewarm', async () => {
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
