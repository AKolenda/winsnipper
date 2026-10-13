import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';
import vm from 'node:vm';
import {clampRect} from '../extension/lib/geometry.js';

const source = file => readFileSync(new URL(file, import.meta.url), 'utf8')
    .replace(/^import .+;\n/gm, '').replace(/^export /gm, '');

test('editor uses standard Open and keeps parameterless plus path prewarm', () => {
    const calls = [];
    const EditorClient = vm.runInNewContext(`${source('../extension/lib/dbus.js')}\nEditorClient`, {
        Gio: {File: {new_for_path: path => ({get_uri: () => `file://${path}`})}},
        GLib: {Variant: class { constructor(signature, values) { Object.assign(this, {signature, values}); } }},
    });
    const client = new EditorClient(null, () => true);
    client._call = (...args) => calls.push(args);
    client.open('/tmp/shot.png');
    assert.equal(calls[0][0], 'Open');
    assert.equal(calls[0][1].signature, '(asa{sv})');
    assert.equal(calls[0][1].values.length, 2);
    assert.equal(calls[0][1].values[0][0], 'file:///tmp/shot.png');
    client.prewarm();
    assert.equal(calls[1][1].values[0], 'prewarm');
    assert.equal(calls[1][1].values[1].length, 0);
    client.prewarm('/tmp/shot.png');
    assert.equal(calls[2][1].values[0], 'prewarm-path');
    assert.equal(calls[2][1].values[1][0].signature, 's');
    assert.equal(calls[2][1].values[1][0].values, '/tmp/shot.png');
});

test('editor launcher fallback probes asynchronously and skips D-Bus timeouts', async () => {
    const spawned = [];
    let executable = ['/home/u/.local/bin/winsnipper-editor', '/usr/bin/winsnipper-editor'];
    const IOErrorEnum = {TIMED_OUT: 24, CANCELLED: 19};
    let failure;
    const EditorClient = vm.runInNewContext(`${source('../extension/lib/dbus.js')}\nEditorClient`, {
        Gio: {
            IOErrorEnum, DBusError: {TIMEOUT: 1, TIMED_OUT: 2, NO_REPLY: 3}, DBusCallFlags: {NONE: 0},
            DBus: {session: {call(...args) { args.at(-1)({call_finish() { throw failure; }}, null); }}},
            File: {new_for_path: path => ({path})}, FileQueryInfoFlags: {NONE: 0}, FileType: {REGULAR: 1},
        },
        GLib: {
            build_filenamev: parts => parts.join('/'), get_home_dir: () => '/home/u',
            file_test: () => assert.fail('Synchronous filesystem probe'), FileTest: {IS_EXECUTABLE: 64},
            find_program_in_path: () => assert.fail('Synchronous PATH search'),
            spawn_async: (_dir, argv) => spawned.push(argv), SpawnFlags: {DEFAULT: 0, SEARCH_PATH: 4},
            PRIORITY_DEFAULT: 0,
        },
        asyncCall: async file => ({get_file_type: () => 1,
            get_attribute_boolean: () => executable.includes(file.path)}),
        warn() {},
    });
    const client = new EditorClient(null, () => true);
    failure = {matches: (domain, code) => domain === IOErrorEnum && code === IOErrorEnum.TIMED_OUT};
    client._call('Open', null, ['/tmp/shot.png']);
    assert.deepEqual(spawned, [], 'a slow activation may still open the editor');
    failure = {matches: () => false};
    client._call('Open', null, ['/tmp/shot.png']);
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(spawned.map(argv => [...argv]), [['/home/u/.local/bin/winsnipper-editor', '/tmp/shot.png']]);
    executable = ['/usr/bin/winsnipper-editor'];
    client._call('Open', null, ['/tmp/shot.png']);
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual([...spawned[1]], ['winsnipper-editor', '/tmp/shot.png'], 'spawn searches PATH for the system launcher');
});

function overlayHarness() {
    const events = [];
    const Overlay = vm.runInNewContext(`${source('../extension/lib/overlay.js')}\nSelectionOverlay`, {
        GObject: {registerClass: klass => klass}, St: {Widget: class {}},
        Main: {
            uiGroup: {add_child: () => events.push('attach')},
            layoutManager: {emit: name => events.push(name)},
            pushModal: () => { events.push('grab'); return {get_seat_state: () => 1}; },
        },
        Shell: {ActionMode: {NONE: 0}}, Clutter: {GrabState: {ALL: 1}},
        Meta: {Cursor: {CROSSHAIR: 0}}, global: {display: {set_cursor() {}}}, clampRect,
    });
    const overlay = new Overlay();
    overlay.grab_key_focus = () => {};
    return {overlay, events};
}

test('overlay closes shell popup grabs before acquiring its modal grab', () => {
    const {overlay, events} = overlayHarness();
    overlay.open();
    assert.deepEqual(events, ['attach', 'system-modal-opened', 'grab']);
});

test('unchanged rounded selection label skips layout but still repositions', () => {
    const {overlay} = overlayHarness();
    let texts = 0;
    let measures = 0;
    const positions = [];
    const actor = () => ({set_position() {}, set_size() {}});
    Object.assign(overlay, {
        _bounds: {x: 0, y: 0, width: 1000, height: 800},
        _shades: Array.from({length: 4}, actor), _border: actor(),
        _label: {
            set_text() { texts++; },
            get_preferred_width() { measures++; return [0, 80]; },
            get_preferred_height() { measures++; return [0, 20]; },
            set_position(x, y) { positions.push([x, y]); },
        },
    });
    overlay._setSelection({x: 30, y: 80, width: 100.1, height: 40.1});
    overlay._setSelection({x: 50, y: 90, width: 100.2, height: 40.2});
    assert.equal(texts, 1);
    assert.equal(measures, 2);
    assert.notDeepEqual(positions[0], positions[1]);
    overlay._setSelection({x: 50, y: 90, width: 101, height: 40});
    assert.equal(texts, 2);
    assert.equal(measures, 4);
});
