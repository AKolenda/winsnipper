// Pure stage-coordinate math. No GNOME imports: also consumed by node --test.
export function normalizeRect(x1, y1, x2, y2) {
    return {x: Math.min(x1, x2), y: Math.min(y1, y2),
        width: Math.abs(x2 - x1), height: Math.abs(y2 - y1)};
}

export function clampRect(rect, bounds) {
    const x = Math.max(bounds.x, Math.min(bounds.x + bounds.width, rect.x));
    const y = Math.max(bounds.y, Math.min(bounds.y + bounds.height, rect.y));
    const right = Math.max(x, Math.min(bounds.x + bounds.width, rect.x + rect.width));
    const bottom = Math.max(y, Math.min(bounds.y + bounds.height, rect.y + rect.height));
    return {x, y, width: right - x, height: bottom - y};
}

export function logicalToTexture(rect, scale, textureWidth, textureHeight) {
    if (!Number.isFinite(scale) || scale <= 0 || textureWidth <= 0 || textureHeight <= 0)
        throw new RangeError('Invalid capture dimensions or scale');
    // GI composite_to_stream takes integer pixels. Round the two edges outward
    // instead of truncating width independently, then clamp to the real texture.
    const x = Math.floor(rect.x * scale);
    const y = Math.floor(rect.y * scale);
    const right = Math.ceil((rect.x + rect.width) * scale);
    const bottom = Math.ceil((rect.y + rect.height) * scale);
    return clampRect({x, y, width: right - x, height: bottom - y},
        {x: 0, y: 0, width: textureWidth, height: textureHeight});
}

// The list is bottom-to-top, as returned by Meta.Display.sort_windows_by_stacking.
// Right/bottom edges are exclusive, matching adjacent monitor/window boundaries.
export function hitTestWindows(windows, x, y) {
    for (let i = windows.length - 1; i >= 0; i--) {
        const rect = windows[i];
        if (x >= rect.x && y >= rect.y &&
            x < rect.x + rect.width && y < rect.y + rect.height)
            return rect;
    }
    return null;
}

export function toastPosition(workArea, width, height, margin = 16) {
    return {
        x: Math.max(workArea.x, workArea.x + workArea.width - width - margin),
        y: Math.max(workArea.y, workArea.y + workArea.height - height - margin),
    };
}

export function uniqueFilename(date, exists) {
    const pad = value => String(value).padStart(2, '0');
    const name = `Screenshot From ${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}-${pad(date.getMinutes())}-${pad(date.getSeconds())}`;
    for (let index = 1; ; index++) {
        const candidate = `${name}${index === 1 ? '' : ` (${index})`}.png`;
        if (!exists(candidate))
            return candidate;
    }
}

// A Windows share (\\server\share\folder) becomes smb://server/share/folder.
// Other URIs except file:// pass through; local paths return null.
export function networkUri(location) {
    const text = location.trim();
    if (/^\\\\/.test(text)) {
        const parts = text.split(/[\\/]+/).filter(Boolean);
        return parts.length < 2 ? null : `smb://${parts.map(encodeURIComponent).join('/')}`;
    }
    if (/^[a-z][a-z0-9+.-]*:\/\//i.test(text) && !/^file:/i.test(text))
        return text.replace(/\/+$/, '');
    return null;
}
