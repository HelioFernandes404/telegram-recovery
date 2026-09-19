"""Private filesystem operations for owned partials and non-overwriting publication."""

import ctypes
import errno
import hashlib
import os
import stat
from pathlib import Path, PurePosixPath

from .security import RecoveryError, private_directory


def destination(root: Path, relative: str, channel_id: int, *, create: bool = True) -> Path:
    parts = PurePosixPath(relative).parts
    if (
        len(parts) != 4
        or parts[0] != f"channel_{channel_id}"
        or any(p in (".", "..") or "\\" in p for p in parts)
    ):
        raise RecoveryError("Caminho local inválido; arquivos preservados.", code="path_invalid")
    current = root
    for part in ("downloads", *parts[:-1]):
        current /= part
        if create:
            private_directory(current)
        elif current.is_symlink() or (current.exists() and not current.is_dir()):
            raise RecoveryError("Pasta de destino inválida; preservada.", code="path_invalid")
    return current / parts[-1]


def file_size(path: Path) -> int | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise RecoveryError("Destino não é arquivo regular; preservado.", code="file_conflict")
    return info.st_size


def sha256_file(path: Path) -> str:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise RecoveryError("Arquivo não regular; preservado.", code="file_conflict")
        digest = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def finalize(part: Path, final: Path):
    """Linux renameat2(RENAME_NOREPLACE), with atomic hard-link fallback."""
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, "renameat2", None)
    if rename is not None:
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        if rename(-100, os.fsencode(part), -100, os.fsencode(final), 1) == 0:
            sync_directory(final.parent)
            return
        code = ctypes.get_errno()
        if code not in (errno.ENOSYS, errno.EINVAL, errno.ENOTSUP):
            raise OSError(code, os.strerror(code))
    # Creating a hard link is atomic and fails if the destination already exists.
    os.link(part, final, follow_symlinks=False)
    part.unlink()  # Only the temporary name owned by this download is removed.
    sync_directory(final.parent)


def sync_directory(path: Path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
