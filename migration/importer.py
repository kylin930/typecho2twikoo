"""Insert-only, journaled batches. Ambiguous writes are reconciled on explicit resume."""
from __future__ import annotations

import json
import os
import re
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from migrator.common import MigrationError, json_text, write_private
from migrator.validate import validate

from .backup import require_backup
from .mongodb import Settings, classify
from .validator import META, chunks, preflight, tagged, verify_stored


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def journal_path(directory: Path, batch_id: str) -> Path:
    if not re.fullmatch('[0-9a-f]{32}', batch_id):
        raise MigrationError('批次 ID 必须是 32 位小写十六进制字符串。')
    return directory / (batch_id + '.migration-journal.json')


@contextmanager
def target_lock(directory: Path, settings: Settings) -> Iterator[None]:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (settings.target_id + '.lock')
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise MigrationError('该日志目录的目标锁已存在；先确认没有运行中的迁移，再人工处理遗留锁。') from None
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(str(os.getpid()))
        yield
    finally:
        path.unlink(missing_ok=True)


def load_journal(path: Path, settings: Settings, input_sha: str, ids: list[str]) -> dict[str, Any]:
    try:
        state = json.loads(path.read_text(encoding='utf-8'))
        if (state['schema'] != 1 or state['target_id'] != settings.target_id or
            state['input_sha256'] != input_sha or state['planned_ids'] != ids or
            state['batch_id'] != path.name.split('.')[0]):
            raise ValueError
        return state
    except (OSError, ValueError, KeyError, TypeError):
        raise MigrationError('恢复日志与输入/目标不匹配；不得创建新批次掩盖部分写入。') from None


def import_comments(db: Any, settings: Settings, docs: list[dict[str, Any]], *, input_sha: str,
                    backup_receipt: Path, confirm_database: str, confirm_collection: str,
                    journal_dir: Path, batch_size: int = 200, resume: str | None = None,
                    progress: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    if settings.database_source == 'driver_default_test_unconfirmed':
        raise MigrationError('不能对隐式 test 库写入；显式设置 MONGODB_DATABASE 或 --database。')
    if confirm_database != settings.database or confirm_collection != settings.collection:
        raise MigrationError('必须通过 --confirm-database 和 --confirm-collection 精确确认目标。')
    if not 1 <= batch_size <= 1000:
        raise MigrationError('batch-size 必须为 1–1000。')
    validate(docs)
    backup = require_backup(backup_receipt, settings)
    ids = [doc['_id'] for doc in docs]
    batch_id = resume or uuid.uuid4().hex
    path = journal_path(journal_dir, batch_id)
    with target_lock(journal_dir, settings):
        if resume:
            state = load_journal(path, settings, input_sha, ids)
        else:
            state = {'schema': 1, 'batch_id': batch_id, 'target_id': settings.target_id,
                     'database': settings.database, 'collection': settings.collection,
                     'input_sha256': input_sha, 'planned_ids': ids, 'created_at': timestamp(),
                     'backup_sha256': backup['sha256'], 'status': 'planned', 'acknowledged_ids': [],
                     'pending_ids': [], 'events': []}
        plan = preflight(db, settings, docs, input_sha=input_sha, batch_id=batch_id if resume else None)
        if plan['blocked']:
            raise MigrationError('预检发现重复 ID/不兼容结构/索引；生成预览和修改计划后人工处理，未写入。')
        # Acknowledged IDs absent on resume may have been intentionally deleted; never recreate silently.
        if set(state['acknowledged_ids']) - set(plan['resumable_ids']):
            raise MigrationError('曾确认写入的记录已缺失；可能被人工删除，停止恢复并核对。')
        already = set(plan['resumable_ids'])
        state['acknowledged_ids'] = sorted(already)
        state['status'] = 'writing'
        write_private(path, json_text(state))  # durable plan BEFORE the first network write
        if progress:
            progress({'batch_id': batch_id, 'status': 'planned', 'total': len(docs), 'already_present': len(already)})
        pending = [doc for doc in docs if doc['_id'] not in already]
        for part in chunks(pending, batch_size):
            state['pending_ids'] = [doc['_id'] for doc in part]
            state['status'] = 'writing'
            write_private(path, json_text(state))
            try:
                # Only INSERT. No upsert/update/delete/drop or collection/index changes.
                db[settings.collection].insert_many([tagged(d, batch_id, input_sha) for d in part], ordered=False)
                # Majority acknowledgement alone does not prove content remained intact.
                verify_stored(db, settings, part, batch_id, input_sha)
            except Exception as exc:
                error = classify(exc)
                state['status'] = 'interrupted_or_partial'
                state['events'].append({'at': timestamp(), 'category': error.category,
                                        'pending_count': len(part)})
                write_private(path, json_text(state))
                raise MigrationError(f'批次 {batch_id} 部分/未知写入；错误类别 {error.category}。用同一输入及 --resume {batch_id} 恢复；未自动重试。') from None
            already.update(doc['_id'] for doc in part)
            state['acknowledged_ids'] = sorted(already)
            state['pending_ids'] = []
            state['events'].append({'at': timestamp(), 'acknowledged': len(already)})
            write_private(path, json_text(state))
            if progress:
                progress({'batch_id': batch_id, 'acknowledged': len(already), 'total': len(docs)})
        result = verify_stored(db, settings, docs, batch_id, input_sha)
        state.update(status='storage_verified', pending_ids=[], completed_at=timestamp(), **result)
        write_private(path, json_text(state))
        return {'batch_id': batch_id, 'journal': str(path), 'target': settings.public(),
                'new_documents': len(pending), 'already_present': len(docs) - len(pending), **result}


def rollback_plan(db: Any, settings: Settings, path: Path) -> dict[str, Any]:
    try:
        state = json.loads(path.read_text(encoding='utf-8'))
        if state['schema'] != 1 or state['target_id'] != settings.target_id:
            raise ValueError
        batch_id, ids, input_sha = state['batch_id'], state['planned_ids'], state['input_sha256']
        journal_path(path.parent, batch_id)
    except (OSError, ValueError, TypeError, KeyError):
        raise MigrationError('回滚日志不合法或目标不匹配。') from None
    owned, foreign, missing = [], [], []
    for part in chunks(ids, 500):
        found = {d['_id']: d for d in db[settings.collection].find({'_id': {'$in': part}})}
        for ident in part:
            if ident not in found:
                missing.append(ident)
            elif found[ident].get(META) == {'tool': 'typecho2twikoo', 'version': 1,
                                           'batch_id': batch_id, 'input_sha256': input_sha}:
                owned.append(ident)
            else:
                foreign.append(ident)
    # A plan, not a deletion routine. Even owned records may have changed since import.
    return {'batch_id': batch_id, 'target': settings.public(), 'tagged_ids_to_review': sorted(owned),
            'foreign_ids_never_delete': sorted(foreign), 'missing_ids': sorted(missing),
            'writes_performed': False, 'requires_manual_content_and_new_reply_review': True,
            'action': 'review only; prefer restoring a full backup into a separate recovery database'}
