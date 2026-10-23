"""Ổ đĩa đầy: nhận diện lỗi và kiểm tra dung lượng còn trống."""
from __future__ import annotations

import errno
import shutil
import sqlite3
from pathlib import Path
from typing import Optional

# Windows: ERROR_DISK_FULL = 112, ERROR_HANDLE_DISK_FULL = 39.
_WIN_DISK_FULL = {112, 39}


class DiskFullError(Exception):
    """Không đủ dung lượng ổ đĩa. Thử lại cũng vô ích cho tới khi dọn ổ."""

    kind = "disk_full"


def is_disk_full(exc: Optional[BaseException]) -> bool:
    """Lỗi (hoặc lỗi gốc của nó) có phải do ổ đầy không."""
    seen = 0
    while exc is not None and seen < 10:
        if isinstance(exc, DiskFullError):
            return True
        if isinstance(exc, OSError):
            if exc.errno == errno.ENOSPC or getattr(exc, "winerror", None) in _WIN_DISK_FULL:
                return True
        if isinstance(exc, sqlite3.OperationalError) and "disk is full" in str(exc).lower():
            return True
        exc = exc.__cause__ or exc.__context__
        seen += 1
    return False


def _existing_ancestor(path: Path) -> Optional[Path]:
    p = Path(path)
    for candidate in (p, *p.parents):
        if candidate.exists():
            return candidate
    return None


def free_mb(path: Path | str) -> Optional[float]:
    """Dung lượng còn trống (MB) của ổ chứa `path` (thư mục chưa tồn tại thì đo thư mục cha gần nhất)."""
    anchor = _existing_ancestor(Path(path))
    if anchor is None:
        return None
    try:
        return shutil.disk_usage(anchor).free / (1024 * 1024)
    except OSError:
        return None


def drive_of(path: Path | str) -> str:
    p = Path(path)
    return p.anchor or str(p)


def check_free(path: Path | str, min_mb: float) -> None:
    """Raise DiskFullError nếu ổ chứa `path` còn dưới `min_mb` MB."""
    if min_mb <= 0:
        return
    free = free_mb(path)
    if free is not None and free < min_mb:
        raise DiskFullError(f"Ổ {drive_of(path)} chỉ còn {free:.0f} MB (cần tối thiểu {min_mb:.0f} MB). "
                            f"Dọn bớt ổ đĩa rồi chạy lại job.")
