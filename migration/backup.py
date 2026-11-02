"""Official mongodump runner with private config, gzip validation and bound receipts."""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode, unquote
from pymongo.uri_parser import parse_uri

import yaml

from migrator.common import MigrationError, json_text, write_private
from .mongodb import Settings, classify


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(part)
    return digest.hexdigest()


def check_archive(path: Path) -> int:
    if not path.is_file() or path.stat().st_size < 32:
        raise MigrationError('备份文件缺失或过小。')
    # Decompress to EOF to check CRC/truncation; no database contents are printed.
    try:
        size = 0
        with gzip.open(path, 'rb') as stream:
            for part in iter(lambda: stream.read(1024 * 1024), b''):
                size += len(part)
        if size < 16:
            raise MigrationError('备份归档内容过小。')
    except (OSError, EOFError):
        raise MigrationError('备份 gzip 完整性验证失败。') from None
    return path.stat().st_size


def tools_uri(settings: Settings) -> str:
    """Match --db without changing the original driver's authentication database."""
    parts = urlsplit(settings.uri)
    options = dict(parse_qsl(parts.query, keep_blank_values=True))
    source = next((v for k,v in options.items() if k.lower() == 'authsource'), None)
    if source is None:
        if settings.srv:
            try:
                parsed = parse_uri(settings.uri, connect_timeout=settings.connect_ms / 1000)
                source = parsed['options'].get('authSource') or parsed['database'] or 'admin'
            except Exception as exc:
                raise classify(exc) from None
        else:
            source = unquote(parts.path.lstrip('/')) or 'admin'
    options = {k: v for k,v in options.items() if k.lower() not in {'authsource', 'tls', 'ssl'}}
    options.update(authSource=source, tls='true')
    return urlunsplit((parts.scheme, parts.netloc, '/' + settings.database, urlencode(options), ''))


def create_backup(settings: Settings, directory: Path, *, timeout: int = 600) -> dict[str, Any]:
    tool = shutil.which('mongodump')
    if not tool:
        raise MigrationError('未安装官方 mongodump；安装 Database Tools 100.3.0+ 后再备份，禁止正式导入。')
    try:
        version = subprocess.run([tool, '--version'], capture_output=True, stdin=subprocess.DEVNULL, timeout=10, check=True)
        match = re.search(rb'mongodump version: (?:r|v)?(\d+)\.(\d+)\.(\d+)', version.stdout)
        if not match or tuple(map(int, match.groups())) < (100, 3, 0):
            raise MigrationError('无法确认 mongodump 100.3.0+，拒绝使用不支持安全配置的版本。')
    except (subprocess.SubprocessError, OSError):
        raise MigrationError('mongodump 版本检查失败；错误原文未记录。') from None
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    archive = (directory / f'{stamp}-{uuid.uuid4().hex[:12]}.archive.gz').resolve()
    fd_archive = os.open(archive, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd_archive)
    # Keep credentials out of argv and console; remove secure temp file in finally.
    fd, config_path = tempfile.mkstemp(prefix='typecho-mongodump-', suffix='.yml')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as config:
            yaml.safe_dump({'uri': tools_uri(settings)}, config)
        command = [tool, '--config', config_path, '--db', settings.database,
                   '--archive=' + str(archive), '--gzip']
        if settings.ca_file:
            command += ['--sslCAFile', settings.ca_file]
        # Capture stdout/stderr in files, not unbounded memory or terminal; may contain secrets.
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            completed = subprocess.run(command, stdout=out, stderr=err, stdin=subprocess.DEVNULL, timeout=timeout)
        if completed.returncode:
            raise MigrationError('mongodump 失败；不生成成功凭据，禁止正式导入。')
        size = check_archive(archive)
        archive.chmod(0o600)
        receipt = {'schema': 1, 'kind': 'mongodump-database-backup', 'status': 'completed',
                   'created_at': datetime.now(timezone.utc).isoformat(), 'target_id': settings.target_id,
                   'database': settings.database, 'collection': settings.collection,
                   'archive': str(archive), 'bytes': size, 'sha256': file_hash(archive),
                   'tool_version': '.'.join(x.decode() for x in match.groups()),
                   'gzip_integrity_verified': True, 'restore_verified': False}
        receipt_path = archive.with_name(archive.name + '.backup-receipt.json')
        write_private(receipt_path, json_text(receipt))
        return {**receipt, 'receipt': str(receipt_path)}
    except (subprocess.SubprocessError, OSError):
        raise MigrationError('备份超时或本地文件/工具操作失败；禁止导入。') from None
    finally:
        Path(config_path).unlink(missing_ok=True)


def require_backup(receipt_path: Path, settings: Settings, *, maximum_age_hours: int = 24) -> dict[str, Any]:
    try:
        receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
        archive = Path(receipt['archive'])
        created = datetime.fromisoformat(receipt['created_at'])
        age = (datetime.now(timezone.utc) - created).total_seconds()
        valid = (receipt['schema'] == 1 and receipt['kind'] == 'mongodump-database-backup'
                 and receipt['status'] == 'completed' and receipt['target_id'] == settings.target_id
                 and receipt['database'] == settings.database and receipt['collection'] == settings.collection
                 and -300 <= age <= maximum_age_hours * 3600 and receipt['gzip_integrity_verified'] is True)
        if not valid or check_archive(archive) != receipt['bytes'] or file_hash(archive) != receipt['sha256']:
            raise ValueError
        return receipt
    except (OSError, ValueError, TypeError, KeyError, MigrationError):
        raise MigrationError('备份凭据无效、过期、目标不符或文件已更改；重新 db-backup 后再提交。') from None
