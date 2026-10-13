"""Stream one regular PNG through stdout without blocking on substituted FIFOs.

Executed by the Shell extension with Python's isolated/no-site flags. Keep this
helper dependency-free: its stdout is image data and stderr is bounded diagnostics.
"""
import os
import stat
import sys

MAX_IMAGE_BYTES = 256 * 1024 * 1024
PNG_SIGNATURE = b'\x89PNG\r\n\x1a\n'


def copy_image(path, target):
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd, 'rb') as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size < MAX_IMAGE_BYTES:
            raise ValueError('PNG must be a regular file smaller than 256 MiB')
        header = source.read(len(PNG_SIGNATURE))
        if header != PNG_SIGNATURE:
            raise ValueError('File does not have a PNG signature')
        target.write(header)
        total = len(header)
        while chunk := source.read(1024 * 1024):
            total += len(chunk)
            if total >= MAX_IMAGE_BYTES:
                raise ValueError('PNG grew beyond the 256 MiB limit')
            target.write(chunk)
        target.flush()


if __name__ == '__main__':
    try:
        copy_image(sys.argv[1], sys.stdout.buffer)
    except (OSError, ValueError) as error:
        print(str(error)[:256], file=sys.stderr)
        raise SystemExit(1)
