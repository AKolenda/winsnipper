import Gio from 'gi://Gio';
import GLib from 'gi://GLib';

// Explicit callbacks work both before and after Shell's Gio._promisify calls.
export function asyncCall(object, method, finish, args) {
    return new Promise((resolve, reject) => {
        object[method](...args, (source, result) => {
            try {
                resolve((source ?? object)[finish](result));
            } catch (error) {
                reject(error);
            }
        });
    });
}

export function checkActive(active) {
    if (!active())
        throw new Error('Operation cancelled because WinSnipper was disabled');
}

export async function makeDirectories(file, cancellable, active) {
    checkActive(active);
    try {
        await asyncCall(file, 'make_directory_async', 'make_directory_finish',
            [GLib.PRIORITY_DEFAULT, cancellable]);
    } catch (error) {
        if (error.matches(Gio.IOErrorEnum, Gio.IOErrorEnum.EXISTS))
            return;
        if (!error.matches(Gio.IOErrorEnum, Gio.IOErrorEnum.NOT_FOUND))
            throw error;
        const parent = file.get_parent();
        if (!parent)
            throw error;
        await makeDirectories(parent, cancellable, active);
        checkActive(active);
        await asyncCall(file, 'make_directory_async', 'make_directory_finish',
            [GLib.PRIORITY_DEFAULT, cancellable]).catch(e => {
            if (!e.matches(Gio.IOErrorEnum, Gio.IOErrorEnum.EXISTS))
                throw e;
        });
    }
}

export async function closeStream(stream) {
    // Never use synchronous close on filesystem streams, even in cleanup.
    await asyncCall(stream, 'close_async', 'close_finish',
        [GLib.PRIORITY_DEFAULT, null]);
}
