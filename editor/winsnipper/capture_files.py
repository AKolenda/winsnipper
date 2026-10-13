"""Import portal results into private files; never edit a portal-owned original."""
from datetime import datetime
import os
from pathlib import Path
import stat
import struct
from urllib.parse import unquote_to_bytes, urlsplit
import uuid

MAX_CAPTURE_BYTES = 256 * 1024 * 1024
MAX_CAPTURE_PIXELS = 32 * 1024 * 1024


def local_capture_path(uri):
    if not isinstance(uri, str):
        raise ValueError("The screenshot service did not return a local file.")
    parsed = urlsplit(uri)
    if (parsed.scheme != "file" or parsed.netloc not in ("", "localhost")
            or parsed.query or parsed.fragment):
        raise ValueError("The screenshot service did not return a local file.")
    path = os.fsdecode(unquote_to_bytes(parsed.path))
    if not os.path.isabs(path) or "\0" in path:
        raise ValueError("The screenshot service returned an invalid file path.")
    return Path(path)


def import_capture(uri, pictures, loader):
    """Worker operation returning (private path, decoded image).

    Pin input/output descriptors, reject links and special files, bound both
    compressed and decoded sizes, and remove incomplete copies on every failure.
    The source is never modified or deleted (including document-portal files).
    """
    source = local_capture_path(uri)
    pictures = Path(pictures).expanduser()
    pictures.mkdir(mode=0o700, parents=True, exist_ok=True)
    pictures = pictures.resolve()
    parent_fd = os.open(pictures, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    directory_fd = None
    output = None
    try:
        parent_info = os.fstat(parent_fd)
        if parent_info.st_uid != os.getuid() or parent_info.st_mode & 0o022:
            raise OSError("The Pictures folder must be owned by you and not writable by other users.")
        try:
            os.mkdir("WinSnipper", 0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
        directory_fd = os.open("WinSnipper", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                               | os.O_CLOEXEC, dir_fd=parent_fd)
        info = os.fstat(directory_fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise OSError("The WinSnipper folder must be owned by you with private permissions (0700).")
        source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        with os.fdopen(source_fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size < MAX_CAPTURE_BYTES:
                raise ValueError("The screenshot must be a regular PNG file smaller than 256 MiB.")
            header = stream.read(24)
            if len(header) != 24 or header[:16] != b"\x89PNG\r\n\x1a\n\0\0\0\rIHDR":
                raise ValueError("The screenshot service did not return a PNG image.")
            width, height = struct.unpack(">II", header[16:24])
            if width < 1 or height < 1 or width * height > MAX_CAPTURE_PIXELS:
                raise ValueError("The screenshot dimensions are too large (32 megapixel limit).")
            filename = f"Screenshot {datetime.now():%Y-%m-%d %H-%M-%S} {uuid.uuid4().hex[:12]}.png"
            fd = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                         | os.O_CLOEXEC, 0o600, dir_fd=directory_fd)
            output = pictures / "WinSnipper" / filename
            with os.fdopen(fd, "wb") as target:
                target.write(header)
                total = len(header)
                while chunk := stream.read(1024 * 1024):
                    total += len(chunk)
                    if total >= MAX_CAPTURE_BYTES:
                        raise ValueError("The screenshot grew beyond the 256 MiB limit.")
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
            after = os.fstat(stream.fileno())
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise OSError("The screenshot changed while it was being imported.")
        base = loader.load(output)
        return output, base
    except BaseException:
        if output is not None:
            os.unlink(output.name, dir_fd=directory_fd)
        raise
    finally:
        if directory_fd is not None:
            os.close(directory_fd)
        os.close(parent_fd)
