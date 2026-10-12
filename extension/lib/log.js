export function warn(message, error = null) {
    console.warn(`[winsnipper] ${message}${error ? `: ${error.message ?? error}` : ''}`);
}

export function report(message, error) {
    console.error(`[winsnipper] ${message}: ${error?.stack ?? error}`);
}
