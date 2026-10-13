import GdkPixbuf from 'gi://GdkPixbuf';

// The toast displays an 80-logical-pixel icon. Avoid uploading the full snip
// (almost 60 MB for a 5K screen) just to display that small preview.
export function thumbnailPixbuf(pixbuf, displayScale = 1) {
    const scale = Number.isFinite(displayScale) ? Math.max(1, Math.min(4, displayScale)) : 1;
    const limit = 80 * scale;
    const longest = Math.max(pixbuf.width, pixbuf.height);
    if (longest <= limit)
        return pixbuf;
    const dimensions = max => [
        Math.max(1, Math.round(pixbuf.width * max / longest)),
        Math.max(1, Math.round(pixbuf.height * max / longest)),
    ];
    // Bilinear reduction directly from a large capture visits the entire image
    // on the compositor thread. Bound that work with a cheap first reduction.
    const reduced = longest > limit * 4
        ? pixbuf.scale_simple(...dimensions(limit * 4), GdkPixbuf.InterpType.NEAREST)
        : pixbuf;
    return reduced.scale_simple(...dimensions(limit), GdkPixbuf.InterpType.BILINEAR);
}
