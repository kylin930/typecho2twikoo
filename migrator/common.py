"""Shared errors, deterministic serialization and atomic individual-file writes."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


class MigrationError(ValueError):
    """Safe error suitable for terminal output (never include source values)."""


def integer(value: Any, field: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise MigrationError(f'{field}: 必须为整数')
    try:
        number = int(value)
    except (ValueError, TypeError):
        raise MigrationError(f'{field}: 必须为整数') from None
    if str(number) != str(value).strip() or not minimum <= number <= 2**53 - 1:
        raise MigrationError(f'{field}: 超出范围或不是规范整数')
    return number


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + '\n'


def write_private(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.migration-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
