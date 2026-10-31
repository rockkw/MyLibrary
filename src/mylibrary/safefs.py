"""The only module allowed to change the filesystem.

It can do exactly three things:
  * make_dirs()      create folders
  * snapshot_link()  create an extra hard link (a second name for the same data)
  * safe_move()      rename a file, refusing to overwrite or to cross drives

There is no delete, no copy-then-remove, and no fallback. If the kernel's
no-replace rename is unavailable, safe_move() refuses instead of degrading.
"""

import ctypes
import ctypes.util
import errno
import os
import sys

_AT_FDCWD = -100
_RENAME_NOREPLACE = 1      # Linux renameat2 flag
_RENAME_EXCL = 0x00000004  # macOS renamex_np flag

_libc = ctypes.CDLL(ctypes.util.find_library("c") or None, use_errno=True)


class RefusedError(OSError):
    """Raised when an operation is refused to protect existing data."""


def _rename_noreplace(src: bytes, dst: bytes) -> int:
    if sys.platform.startswith("linux"):
        return _libc.renameat2(_AT_FDCWD, src, _AT_FDCWD, dst, _RENAME_NOREPLACE)
    if sys.platform == "darwin":
        return _libc.renamex_np(src, dst, _RENAME_EXCL)
    raise RefusedError(errno.ENOSYS, "no atomic no-replace rename on this platform")


def make_dirs(path: str | os.PathLike) -> None:
    os.makedirs(path, exist_ok=True)


def safe_move(src: str | os.PathLike, dst: str | os.PathLike) -> None:
    """Atomically rename src -> dst. Never overwrites; never crosses filesystems."""
    src, dst = os.fspath(src), os.fspath(dst)
    if not os.path.lexists(src):
        raise RefusedError(errno.ENOENT, "source does not exist", src)
    if os.path.lexists(dst):
        raise RefusedError(errno.EEXIST, "destination already exists", dst)
    make_dirs(os.path.dirname(dst) or ".")
    if os.stat(src).st_dev != os.stat(os.path.dirname(dst) or ".").st_dev:
        raise RefusedError(errno.EXDEV, "cross-drive move refused", dst)
    if _rename_noreplace(os.fsencode(src), os.fsencode(dst)) != 0:
        e = ctypes.get_errno()
        raise RefusedError(e, os.strerror(e), dst)


def snapshot_link(src: str | os.PathLike, link_path: str | os.PathLike) -> None:
    """Create an extra hard link to src. Fails if link_path exists or is cross-drive."""
    make_dirs(os.path.dirname(os.fspath(link_path)))
    os.link(src, link_path)
