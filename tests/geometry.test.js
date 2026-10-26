import assert from 'node:assert/strict';
import {test} from 'node:test';
import {clampRect, hitTestWindows, logicalToTexture, networkUri, normalizeRect,
    toastPosition, uniqueFilename} from '../extension/lib/geometry.js';

const bounds = {x: 0, y: 0, width: 1920, height: 1080};

test('normalize every drag direction, decimal positions and zero-area clicks', () => {
    const expected = {x: 10, y: 20, width: 30, height: 40};
    for (const points of [[10, 20, 40, 60], [40, 60, 10, 20], [40, 20, 10, 60], [10, 60, 40, 20]])
        assert.deepEqual(normalizeRect(...points), expected);
    assert.deepEqual(normalizeRect(-1.5, 0.25, 2.5, 1.75), {x: -1.5, y: 0.25, width: 4, height: 1.5});
    assert.deepEqual(normalizeRect(7, 8, 7, 8), {x: 7, y: 8, width: 0, height: 0});
});

test('clamp intersecting and wholly outside rectangles, including negative monitor origins', () => {
    assert.deepEqual(clampRect({x: -10, y: -20, width: 50, height: 60}, bounds),
        {x: 0, y: 0, width: 40, height: 40});
    assert.deepEqual(clampRect({x: 1900, y: 1070, width: 50, height: 60}, bounds),
        {x: 1900, y: 1070, width: 20, height: 10});
    assert.deepEqual(clampRect({x: -50, y: 20, width: 10, height: 30}, bounds),
        {x: 0, y: 20, width: 0, height: 30});
    assert.deepEqual(clampRect({x: 2000, y: 1200, width: 10, height: 10}, bounds),
        {x: 1920, y: 1080, width: 0, height: 0});
    const negative = {x: -1920, y: -200, width: 1920, height: 1080};
    assert.deepEqual(clampRect({x: -2000, y: -300, width: 4000, height: 2000}, negative), negative);
    const original = {...bounds};
    assert.deepEqual(clampRect(bounds, bounds), bounds);
    assert.deepEqual(bounds, original);
});

test('logical crop uses capture scale for normal, HiDPI and mixed/fractional layouts', () => {
    const logical = {x: 100, y: 52, width: 300, height: 200};
    for (const scale of [1, 1.25, 1.5, 1.75, 2, 3]) {
        assert.deepEqual(logicalToTexture(logical, scale, 5760, 3240),
            {x: 100 * scale, y: 52 * scale, width: 300 * scale, height: 200 * scale});
    }
    assert.deepEqual(logicalToTexture({x: 1, y: 3, width: 5, height: 7}, 1.25, 100, 100),
        {x: 1, y: 3, width: 7, height: 10});
    assert.deepEqual(logicalToTexture({x: 0.5, y: 0.5, width: 0.25, height: 0.25}, 1.5, 100, 100),
        {x: 0, y: 0, width: 2, height: 2});
    assert.deepEqual(logicalToTexture({x: -5, y: -5, width: 100, height: 100}, 2, 150, 120),
        {x: 0, y: 0, width: 150, height: 120});
    assert.deepEqual(logicalToTexture(bounds, 1.25, 2399, 1349),
        {x: 0, y: 0, width: 2399, height: 1349});
});

test('invalid capture scales/dimensions are rejected before reaching Cogl', () => {
    for (const scale of [0, -1, NaN, Infinity])
        assert.throws(() => logicalToTexture(bounds, scale, 100, 100), RangeError);
    assert.throws(() => logicalToTexture(bounds, 1, 0, 100), RangeError);
});

test('window hover selects the topmost snapshot and uses exclusive right/bottom boundaries', () => {
    const back = {x: 0, y: 0, width: 200, height: 200};
    const front = {x: 50, y: 50, width: 100, height: 100};
    const windows = [back, front];
    assert.equal(hitTestWindows(windows, 50, 50), front);
    assert.equal(hitTestWindows(windows, 149.9, 149.9), front);
    assert.equal(hitTestWindows(windows, 150, 150), back);
    assert.equal(hitTestWindows(windows, 0, 0), back);
    assert.equal(hitTestWindows(windows, 200, 50), null);
    assert.equal(hitTestWindows(windows, 50, 200), null);
    assert.equal(hitTestWindows(windows, -1, 50), null);
    assert.equal(hitTestWindows([], 0, 0), null);
    assert.deepEqual(windows, [back, front]);
});

test('toast respects primary work area offsets, panel struts and margins', () => {
    assert.deepEqual(toastPosition({x: 1920, y: 24, width: 1920, height: 1016}, 400, 100),
        {x: 3424, y: 924});
    assert.deepEqual(toastPosition({x: -1920, y: 0, width: 1920, height: 1040}, 400, 100),
        {x: -416, y: 924});
    assert.deepEqual(toastPosition({x: 10, y: 20, width: 100, height: 50}, 400, 100), {x: 10, y: 20});
    assert.deepEqual(toastPosition(bounds, 400, 100, 0), {x: 1520, y: 980});
});

test('local date filename format, zero-padding, collision numbering and no date mutation', () => {
    const date = new Date(2026, 0, 2, 3, 4, 5);
    const before = date.getTime();
    const base = 'Screenshot From 2026-01-02 03-04-05';
    assert.equal(uniqueFilename(date, () => false), `${base}.png`);
    const names = new Set([`${base}.png`, `${base} (2).png`, `${base} (3).png`, `${base} (5).png`]);
    assert.equal(uniqueFilename(date, value => names.has(value)), `${base} (4).png`);
    assert.equal(date.getTime(), before);
    assert.equal(uniqueFilename(new Date(2026, 11, 31, 23, 59, 59), () => false),
        'Screenshot From 2026-12-31 23-59-59.png');
});

test('crop property checks keep rounded pixels inside texture bounds', () => {
    for (const scale of [1, 1.25, 1.5, 2]) {
        for (let i = -10; i < 30; i++) {
            const logical = {x: i + 0.1, y: i + 0.2, width: 7.3, height: 9.8};
            const result = logicalToTexture(logical, scale, 40, 40);
            assert.ok(Number.isInteger(result.x) && Number.isInteger(result.y));
            assert.ok(result.width >= 0 && result.height >= 0);
            assert.ok(result.x >= 0 && result.y >= 0);
            assert.ok(result.x + result.width <= 40 && result.y + result.height <= 40);
        }
    }
});

test('Windows shares and URIs are network locations; local paths are not', () => {
    assert.equal(networkUri('\\\\10.40.40.4\\screenshots\\Screenshots'), 'smb://10.40.40.4/screenshots/Screenshots');
    assert.equal(networkUri('\\\\\\10.40.40.4\\screenshots\\Screenshots\\'), 'smb://10.40.40.4/screenshots/Screenshots');
    assert.equal(networkUri(' \\\\nas\\My Shots '), 'smb://nas/My%20Shots');
    assert.equal(networkUri('\\\\nas'), null);
    assert.equal(networkUri('smb://nas/share/'), 'smb://nas/share');
    assert.equal(networkUri('sftp://me@host/pics'), 'sftp://me@host/pics');
    for (const local of ['', '~/Pictures', '/mnt/share', 'Pictures/Shots', 'file:///tmp'])
        assert.equal(networkUri(local), null);
});
