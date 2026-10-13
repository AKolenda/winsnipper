// Real pixbuf allocation check: gjs -m tests/gjs-thumbnail-smoke.js
import GdkPixbuf from 'gi://GdkPixbuf';
import GLib from 'gi://GLib';
import {thumbnailPixbuf} from '../extension/lib/thumbnail.js';

const capture = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, true, 8, 5120, 2880);
capture.fill(0x123456ff);
const started = GLib.get_monotonic_time();
const preview = thumbnailPixbuf(capture, 2);
const elapsed = (GLib.get_monotonic_time() - started) / 1000;
if (preview.width !== 160 || preview.height !== 90 || preview.get_byte_length() !== 57600)
    throw new Error('HiDPI toast preview did not respect its pixel budget');
if (capture.width !== 5120 || capture.height !== 2880)
    throw new Error('Thumbnail changed the original capture');
const bytes = preview.read_pixel_bytes().get_data();
if (bytes[0] !== 0x12 || bytes[1] !== 0x34 || bytes[2] !== 0x56 || bytes[3] !== 0xff)
    throw new Error('Thumbnail did not preserve screenshot colours');
print(`5K preview: ${capture.get_byte_length()} → ${preview.get_byte_length()} bytes in ${elapsed.toFixed(2)} ms`);
