// Optional real-GJS check of the network upload queue: gjs -m tests/gjs-upload-smoke.js [folder-uri]
// Without an argument the "network folder" is a local file:// folder. Pass e.g.
// smb://server/share/some-test-folder to also copy through gvfs (the folder is removed afterwards).
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';

function assert(value, message) {
    if (!value)
        throw new Error(message);
}

// upload.js imports Shell modules that only exist inside gnome-shell; stub them.
const lib = GLib.build_filenamev([GLib.path_get_dirname(GLib.filename_from_uri(import.meta.url)[0]),
    '..', 'extension', 'lib']);
const copy = GLib.dir_make_tmp('winsnipper-lib-XXXXXX');
const notices = [];
globalThis.winsnipperNotices = notices;
for (const name of ['upload.js', 'capture.js', 'io.js', 'log.js', 'geometry.js']) {
    let [, source] = GLib.file_get_contents(`${lib}/${name}`);
    source = new TextDecoder().decode(source)
        .replace("import Shell from 'gi://Shell';", 'const Shell = null;')
        .replace("import * as Main from 'resource:///org/gnome/shell/ui/main.js';",
            'const Main = {notify: (...args) => globalThis.winsnipperNotices.push(args)};');
    GLib.file_set_contents(`${copy}/${name}`, source);
}
const {Uploader} = await import(`file://${copy}/upload.js`);

const wait = (test, message, seconds = 20) => new Promise((resolve, reject) => {
    const started = Date.now();
    GLib.timeout_add(GLib.PRIORITY_DEFAULT, 50, () => {
        if (test()) {
            resolve();
            return GLib.SOURCE_REMOVE;
        }
        if (Date.now() - started > seconds * 1000) {
            reject(new Error(`Timed out: ${message}`));
            return GLib.SOURCE_REMOVE;
        }
        return GLib.SOURCE_CONTINUE;
    });
});
const read = file => {
    const [, contents] = file.load_contents(null);
    return new TextDecoder().decode(contents);
};
const listNames = folder => {
    const names = [];
    const children = folder.enumerate_children('standard::name', Gio.FileQueryInfoFlags.NONE, null);
    for (let info; (info = children.next_file(null));)
        names.push(info.get_name());
    return names;
};

async function main() {
    const root = GLib.dir_make_tmp('winsnipper-upload-XXXXXX');
    const stage = `${root}/stage`;
    GLib.mkdir_with_parents(stage, 0o700);
    const snip = `${stage}/snip.png`;
    GLib.file_set_contents(snip, 'first');
    const blocked = `${root}/blocked`;
    GLib.mkdir_with_parents(blocked, 0o500);
    const target = Gio.File.new_for_path(`${blocked}/Screenshots`);
    const cancellable = new Gio.Cancellable();
    const active = () => true;

    // An unwritable folder: the snip is queued and the queue is saved.
    let uploader = new Uploader(stage, cancellable, active);
    uploader.start(snip, target);
    await wait(() => uploader._retryId && !uploader._running, 'failed copy schedules a retry');
    assert(notices.length === 1 && notices[0][1].includes('queued'), 'one queued notice');
    await wait(() => GLib.file_test(`${stage}/uploads.json`, GLib.FileTest.EXISTS) && !uploader._saving,
        'queue saved');
    assert(JSON.parse(read(Gio.File.new_for_path(`${stage}/uploads.json`)))[0].dirty, 'saved as pending');
    uploader.destroy();

    // A new session loads the queue; once the folder is writable, it uploads.
    GLib.chmod(blocked, 0o700);
    uploader = new Uploader(stage, cancellable, active);
    await wait(() => !uploader._running && [...uploader._entries.values()].every(e => !e.dirty) &&
        uploader._entries.size === 1, 'queued snip uploads after restart');
    const uploaded = Gio.File.new_for_uri(uploader._entries.get(snip).remote);
    assert(read(uploaded) === 'first', 'uploaded the staged bytes');
    assert(notices.at(-1)[1].includes('saved to the network folder'), 'recovery notice');

    // An editor save replaces the same remote file.
    GLib.file_set_contents(snip, 'edited');
    uploader.sync(snip);
    await wait(() => !uploader._running && read(uploaded) === 'edited', 'edit re-uploaded');
    assert(listNames(target).length === 1, 'edits replace, never duplicate');
    uploader.destroy();

    // Optional: the same through a real network folder.
    const uri = ARGV[0];
    if (uri) {
        const remote = Gio.File.new_for_uri(uri);
        uploader = new Uploader(`${root}/stage2`, cancellable, active);
        uploader.start(snip, remote);
        await wait(() => !uploader._running && uploader._entries.get(snip)?.remote, 'network upload', 60);
        const file = Gio.File.new_for_uri(uploader._entries.get(snip).remote);
        assert(read(file) === 'edited', 'network copy matches');
        GLib.file_set_contents(snip, 'edited twice');
        uploader.sync(snip);
        await wait(() => !uploader._running && read(file) === 'edited twice', 'network edit', 60);
        uploader.destroy();
        file.delete(null);
        remote.delete(null);
        print(`network folder ok: ${uri}`);
    }
    print('upload queue ok');
}

await main();
