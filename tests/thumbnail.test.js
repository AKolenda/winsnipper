import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../extension/lib/thumbnail.js', import.meta.url), 'utf8')
    .replace(/^import .+;\n/gm, '').replace(/^export /gm, '');
const thumbnailPixbuf = vm.runInNewContext(`${source}\nthumbnailPixbuf`, {
    GdkPixbuf: {InterpType: {NEAREST: 'nearest', BILINEAR: 'bilinear'}},
});

function pixbuf(width, height, calls = []) {
    return {width, height, calls, scale_simple(w, h, interpolation) {
        calls.push({width: w, height: h, interpolation});
        return pixbuf(w, h, calls);
    }};
}

test('5K toast bounds both bilinear input and uploaded preview at HiDPI', () => {
    const capture = pixbuf(5120, 2880);
    const preview = thumbnailPixbuf(capture, 2);
    assert.deepEqual(capture.calls, [
        {width: 640, height: 360, interpolation: 'nearest'},
        {width: 160, height: 90, interpolation: 'bilinear'},
    ]);
    assert.equal(preview.width * preview.height * 4, 57600);
    assert.equal(capture.width, 5120, 'the saved/clipboard capture retains full resolution');
});

test('small snips remain untouched and moderate snips use one quality reduction', () => {
    const small = pixbuf(30, 80);
    assert.equal(thumbnailPixbuf(small), small);
    const moderate = pixbuf(120, 240);
    thumbnailPixbuf(moderate);
    assert.deepEqual(moderate.calls, [{width: 40, height: 80, interpolation: 'bilinear'}]);
});

test('panoramic and narrow previews keep positive dimensions and bounded allocation', () => {
    for (const [width, height] of [[100000, 1], [1, 100000], [10000, 10000]]) {
        for (const scale of [1, 1.25, 2, 4, 100, NaN]) {
            const capture = pixbuf(width, height);
            const preview = thumbnailPixbuf(capture, scale);
            assert.ok(preview.width >= 1 && preview.height >= 1);
            assert.ok(Math.max(preview.width, preview.height) <= 320);
            assert.ok(capture.calls.every(call => Math.max(call.width, call.height) <= 1280));
        }
    }
});
