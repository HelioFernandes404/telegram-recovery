"""Private local files and exclusive process locks (Linux/macOS)."""

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path


class RecoveryError(Exception):
    """A deliberately safe, user-facing error, never a raw provider exception."""

    def __init__(self, message: str, *, code: str = "recovery_error"):
        super().__init__(message)
        self.code = code


def private_directory(path: Path) -> None:
    if path.is_symlink():
        raise RecoveryError("A pasta de dados/sessão não pode ser um link simbólico.")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def private_file(path: Path) -> None:
    if path.is_symlink():
        raise RecoveryError("O arquivo de dados/sessão não pode ser um link simbólico.")
    if path.exists():
        path.chmod(0o600)


@contextmanager
def exclusive_lock(path: Path):
    private_directory(path.parent)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RecoveryError(
                "Outra operação está usando este manifesto ou sessão.", code="already_running"
            ) from None
        yield
    finally:
        os.close(fd)
