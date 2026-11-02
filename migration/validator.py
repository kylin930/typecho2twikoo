"""Read-only target inspection, source/map checks, and resume ownership checks."""
from __future__ import annotations

import csv
import hashlib
from collections import Counter
from pathlib import Path
from typing import Any, Iterator

from bson import BSON, ObjectId, Int64

from migrator.common import MigrationError
from migrator.nekomi import FIELDS, Target
from migrator.validate import validate

from .mongodb import Settings

META = '_migration'


def chunks(items: list[Any], size: int) -> Iterator[list[Any]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


def sorted_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: sorted_value(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [sorted_value(v) for v in value]
    return value


def same_document(a: dict[str, Any], b: dict[str, Any]) -> bool:
    # Compare BSON types as well as values; Python 1 == 1.0 must not conceal mutations.
    return BSON.encode(sorted_value(a)) == BSON.encode(sorted_value(b))


def input_documents(path: Path, mapping: Path, targets: dict[str, Target]) -> tuple[list[dict[str, Any]], str, dict[str, int]]:
    payload = path.read_bytes()
    # Hash the same snapshot that is decoded and validated (not a second read).
    import json
    from migrator.validate import unique_object
    try:
        docs = [json.loads(line, object_pairs_hook=unique_object) for line in payload.decode('utf-8').splitlines()]
    except (UnicodeError, ValueError):
        raise MigrationError('迁移输入必须为 UTF-8 JSONL；不能包含空行或无效 JSON。') from None
    if any(not isinstance(d, dict) for d in docs):
        raise MigrationError('迁移输入每行必须是对象。')
    summary = validate(docs)
    approved: set[tuple[str, str]] = set()
    seen: set[str] = set()
    used: set[str] = set()
    with mapping.open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != FIELDS:
            raise MigrationError('路径映射表头不符。')
        for row in reader:
            if None in row or any(v is None for v in row.values()) or row['cid'] in seen:
                raise MigrationError('路径映射列数错误或 cid 重复。')
            seen.add(row['cid'])
            if row['status'] != 'ready':
                continue
            target = targets.get(row['target'])
            if (not target or not target.enabled or row['source_type'] != target.kind or
                row['url'] != target.url or row['href'] != target.href or row['target'] in used):
                raise MigrationError('ready 映射与当前 Nekomi 内容不一致或重复。')
            used.add(row['target'])
            approved.add((target.url, target.href))
    if any((d['url'], d['href']) not in approved for d in docs):
        raise MigrationError('评论路径不在经确认的 Nekomi 映射中。')
    return docs, hashlib.sha256(payload).hexdigest(), summary


def bson_type(value: Any) -> str:
    if isinstance(value, ObjectId):
        return 'objectId'
    if isinstance(value, str):
        return 'string'
    if type(value) is bool:
        return 'bool'
    if isinstance(value, Int64):
        return 'int64'
    if isinstance(value, int):
        return 'int32' if -(2**31) <= value < 2**31 else 'int64'
    if isinstance(value, float):
        return 'double'
    if value is None:
        return 'null'
    return type(value).__name__


def database_info(db: Any, settings: Settings) -> dict[str, Any]:
    collection = db[settings.collection]
    names = db.list_collection_names()
    exists = settings.collection in names
    options = collection.options() if exists else {}
    fields = ['_id', 'url', 'href', 'pid', 'rid', 'created', 'updated', 'isSpam', 'master']
    hist = {key: Counter() for key in fields}
    examined = 0
    blockers: Counter[str] = Counter()
    for row in collection.find({}, {key: 1 for key in fields}).limit(1000):
        examined += 1
        for key in fields:
            hist[key][bson_type(row[key]) if key in row else 'missing'] += 1
        if not isinstance(row.get('_id'), str):
            blockers['legacy_or_nonstring_id_requires_plan'] += 1
        if not isinstance(row.get('url'), str) or (not isinstance(row.get('created'), (int, float)) or isinstance(row.get('created'), bool)):
            blockers['incompatible_core_fields_requires_plan'] += 1
        if 'isSpam' in row and type(row['isSpam']) is not bool:
            blockers['incompatible_moderation_requires_plan'] += 1
        for key in ['pid', 'rid']:
            if row.get(key) is not None and not isinstance(row[key], str):
                blockers['incompatible_reference_requires_plan'] += 1
    indexes = []
    if exists:
        for spec in collection.list_indexes():
            keys = list(spec['key'].items())
            indexes.append({'keys': keys, 'unique': bool(spec.get('unique', False) or keys == [('_id', 1)]),
                            'sparse': bool(spec.get('sparse', False)),
                            'partial': 'partialFilterExpression' in spec})
            if spec.get('unique') and keys != [('_id', 1)]:
                blockers['custom_unique_index_requires_plan'] += 1
    if options.get('validator'):
        blockers['collection_validator_requires_plan'] += 1
    if options.get('capped'):
        blockers['capped_collection_not_supported'] += 1
    return {'target': settings.public(), 'collection_exists': exists,
            'comment_collection_exists': 'comment' in names,
            'existing_comments': collection.count_documents({}), 'sampled_documents': examined,
            'sample_limit': 1000, 'sample_field_types': {key: dict(value) for key, value in hist.items()},
            'indexes': indexes, 'blockers': dict(blockers), 'modification_plan': [{'issue': issue, 'count': count,
                'action': 'export scoped snapshot and review against Twikoo 2.0.17; no automatic mutation'}
                for issue, count in sorted(blockers.items())],
            'writes_performed': False}


def tagged(doc: dict[str, Any], batch_id: str, input_sha: str) -> dict[str, Any]:
    return {**doc, META: {'tool': 'typecho2twikoo', 'version': 1,
                         'batch_id': batch_id, 'input_sha256': input_sha}}


def preflight(db: Any, settings: Settings, docs: list[dict[str, Any]], *, input_sha: str,
              batch_id: str | None = None) -> dict[str, Any]:
    info = database_info(db, settings)
    ids = [doc['_id'] for doc in docs]
    by_id = {doc['_id']: doc for doc in docs}
    found: dict[str, Any] = {}
    for part in chunks(ids, 500):
        for doc in db[settings.collection].find({'_id': {'$in': part}}):
            found[doc['_id']] = doc
    conflicts, resumable = [], []
    for ident, actual in found.items():
        if batch_id and same_document(actual, tagged(by_id[ident], batch_id, input_sha)):
            resumable.append(ident)
        else:
            conflicts.append(ident)
    return {'target': settings.public(), 'input_sha256': input_sha, 'input_count': len(docs),
            'existing_count': info['existing_comments'], 'existing_schema': info,
            'conflicts': sorted(conflicts), 'resumable_ids': sorted(resumable),
            'to_insert': len(docs) - len(found), 'blocked': bool(conflicts or info['blockers']),
            'merge_strategy': 'insert_new_ids_only; never update/delete existing comments',
            'writes_performed': False, 'twikoo_version': '2.0.17', 'twikoo_e2e_verified': False}


def verify_stored(db: Any, settings: Settings, docs: list[dict[str, Any]], batch_id: str, input_sha: str) -> dict[str, Any]:
    # Full input already passed pid/rid/thread checks. Compare EVERY stored document to it.
    plan = preflight(db, settings, docs, input_sha=input_sha, batch_id=batch_id)
    if plan['conflicts'] or len(plan['resumable_ids']) != len(docs):
        raise MigrationError('写后核对发现缺失或已修改文档；保留批次日志，不宣称完成。')
    return {'storage_verified': True, 'documents': len(docs), 'twikoo_e2e_verified': False}
