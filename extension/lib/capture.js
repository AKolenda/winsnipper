import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Shell from 'gi://Shell';

import {logicalToTexture, uniqueFilename} from './geometry.js';
import {asyncCall, checkActive, closeStream, makeDirectories} from './io.js';
import {warn} from './log.js';

export function requestCapture(shooter) {
    return asyncCall(shooter, 'screenshot_stage_to_content',
        'screenshot_stage_to_content_finish', []);
}

export function snapshotWindows() {
    const workspace = global.workspace_manager.get_active_workspace();
    const windows = global.get_window_actors()
        .filter(actor => actor.visible && !actor.meta_window.minimized &&
            actor.meta_window.located_on_workspace(workspace))
        .map(actor => actor.meta_window);
    return global.display.sort_windows_by_stacking(windows).map(window => {
        const rect = window.get_frame_rect();
        return {x: rect.x, y: rect.y, width: rect.width, height: rect.height};
    });
}

export async function encodeCapture(content, scale, rect, active) {
    checkActive(active);
    const texture = content.get_texture();
    const crop = logicalToTexture(rect, scale, texture.get_width(), texture.get_height());
    if (crop.width < 1 || crop.height < 1)
        throw new Error('The screenshot selection is empty');
    const stream = Gio.MemoryOutputStream.new_resizable();
    try {
        const pixbuf = await asyncCall(Shell.Screenshot, 'composite_to_stream',
            'composite_to_stream_finish', [texture, crop.x, crop.y,
                crop.width, crop.height, scale, null, 0, 0, 1, stream]);
        // Closing an in-memory stream performs no file IO (as in ui/screenshot.js).
        stream.close(null);
        checkActive(active);
        const bytes = stream.steal_as_bytes();
        return {bytes, pixbuf};
    } finally {
        if (!stream.is_closed())
            stream.close(null);
    }
}

export function saveDirectory(settings) {
    if (!settings.get_boolean('save-to-disk'))
        return GLib.build_filenamev([GLib.get_user_cache_dir(), 'winsnipper']);
    const custom = settings.get_string('save-directory');
    if (custom) {
        if (custom === '~' || custom.startsWith('~/'))
            return GLib.build_filenamev([GLib.get_home_dir(), custom.slice(2)]);
        return GLib.path_is_absolute(custom) ? custom :
            GLib.build_filenamev([GLib.get_home_dir(), custom]);
    }
    const pictures = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_PICTURES) ??
        GLib.build_filenamev([GLib.get_home_dir(), 'Pictures']);
    return GLib.build_filenamev([pictures, 'Screenshots']);
}

export async function saveCapture(bytes, directory, cancellable, active) {
    const dir = Gio.File.new_for_path(directory);
    await makeDirectories(dir, cancellable, active);
    const date = new Date();
    const collisions = new Set();
    for (;;) {
        checkActive(active);
        const name = uniqueFilename(date, candidate => collisions.has(candidate));
        const file = dir.get_child(name);
        let stream;
        try {
            // Exclusive creation handles races with other snips/processes without
            // any synchronous exists() check or overwriting an existing screenshot.
            stream = await asyncCall(file, 'create_async', 'create_finish',
                [Gio.FileCreateFlags.PRIVATE, GLib.PRIORITY_DEFAULT, cancellable]);
        } catch (error) {
            if (!error.matches(Gio.IOErrorEnum, Gio.IOErrorEnum.EXISTS))
                throw error;
            collisions.add(name);
            continue;
        }
        try {
            checkActive(active);
            // Not write_all_async: GJS frees the copied buffer before the write
            // runs, saving freed memory. The input stream keeps the GBytes alive.
            await asyncCall(stream, 'splice_async', 'splice_finish',
                [Gio.MemoryInputStream.new_from_bytes(bytes),
                    Gio.OutputStreamSpliceFlags.CLOSE_SOURCE, GLib.PRIORITY_DEFAULT, cancellable]);
            await closeStream(stream);
            checkActive(active);
            return file.get_path();
        } catch (error) {
            try {
                if (!stream.is_closed())
                    await closeStream(stream);
                await asyncCall(file, 'delete_async', 'delete_finish',
                    [GLib.PRIORITY_DEFAULT, null]);
            } catch (cleanupError) {
                warn('Could not remove incomplete screenshot', cleanupError);
            }
            throw error;
        }
    }
}
