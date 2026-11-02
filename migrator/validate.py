"""Offline checks for this tool's Mongo import contract; no database connections."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .common import MigrationError
from .nekomi import thread_path
from .render import safe_link, sanitize

STRINGS = {'_id', 'nick', 'mail', 'mailMd5', 'link', 'ua', 'url', 'href', 'comment'}
REQUIRED = STRINGS | {'master', 'isSpam', 'created', 'updated'}
ALLOWED = REQUIRED | {'pid', 'rid'}


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj: dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            raise MigrationError('JSONL: 重复对象字段')
        obj[key] = value
    return obj


def load_documents(path: Path) -> list[dict[str, Any]]:
    docs = []
    with path.open(encoding='utf-8') as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                raise MigrationError(f'JSONL: 第 {number} 行为空')
            try:
                doc = json.loads(line, object_pairs_hook=unique_object)
            except ValueError:
                raise MigrationError(f'JSONL: 第 {number} 行不是合法 JSON') from None
            if not isinstance(doc, dict):
                raise MigrationError(f'JSONL: 第 {number} 行必须为对象')
            docs.append(doc)
    return docs


def validate(docs: list[dict[str, Any]]) -> dict[str, int]:
    if not docs:
        raise MigrationError('没有评论；空文件不能证明转换成功')
    by_id: dict[str, dict[str, Any]] = {}
    for line, doc in enumerate(docs, 1):
        def fail(message: str) -> None:
            raise MigrationError(f'文档第 {line} 行: {message}')
        if not REQUIRED <= doc.keys() or doc.keys() - ALLOWED:
            fail('字段缺失或出现不支持的字段')
        if any(not isinstance(doc[k], str) for k in STRINGS):
            fail('字符串字段类型错误（不允许 BSON ObjectId）')
        if not re.fullmatch('[0-9a-f]{32}', doc['_id']):
            fail('_id 必须为 32 位十六进制字符串')
        if doc['_id'] in by_id:
            fail('重复 _id')
        by_id[doc['_id']] = doc
        if type(doc['master']) is not bool or doc['isSpam'] is not False:
            fail('master/isSpam 类型或审核状态错误')
        for key in ['created', 'updated']:
            if type(doc[key]) is not int or not 0 <= doc[key] <= 253402300799999:
                fail('时间戳必须为范围内的毫秒整数')
        thread_path(doc['url'])
        try:
            href = urlsplit(doc['href'])
            if (href.scheme not in {'http', 'https'} or not href.netloc or href.username or
                    href.password or href.query or href.fragment or '\\' in doc['href'] or
                    any(c.isspace() for c in doc['href'])):
                fail('href 必须为无凭据的 HTTP(S) 文章地址')
        except ValueError:
            fail('href 无效')
        if doc['link'] and safe_link(doc['link']) != doc['link']:
            fail('个人网站链接不安全')
        if doc['comment'] != sanitize(doc['comment']):
            fail('正文含非允许 HTML，必须重新转换')
        if ('pid' in doc) != ('rid' in doc):
            fail('pid/rid 必须同时存在或同时省略')
        for key in ['pid', 'rid']:
            if key in doc and (not isinstance(doc[key], str) or not re.fullmatch('[0-9a-f]{32}', doc[key])):
                fail('回复引用必须为字符串 ID')
    root_cache: dict[str, str] = {}
    for ident, doc in by_id.items():
        chain: list[str] = []
        visited: set[str] = set()
        at = ident
        while at not in root_cache:
            if at in visited:
                raise MigrationError('回复链存在循环')
            visited.add(at)
            chain.append(at)
            current = by_id[at]
            if 'pid' not in current:
                root_cache[at] = at
                break
            parent = by_id.get(current['pid'])
            if not parent:
                raise MigrationError('父评论不存在')
            if parent['url'] != current['url'] or parent['href'] != current['href']:
                raise MigrationError('回复引用跨文章')
            at = current['pid']
        root = root_cache[at]
        for item in chain:
            root_cache[item] = root
        if 'pid' in doc and doc['rid'] != root:
            raise MigrationError('rid 未指向直接父链的根评论')
    return {'documents': len(docs), 'roots': sum('pid' not in d for d in docs),
            'replies': sum('pid' in d for d in docs), 'threads': len({d['url'] for d in docs})}


def check_existing_ids(docs: list[dict[str, Any]], path: Path) -> int:
    incoming = {d['_id'] for d in docs}
    existing = load_documents(path)
    overlaps = 0
    for item in existing:
        ident = item.get('_id')
        if isinstance(ident, str):
            overlaps += ident in incoming
        elif not (isinstance(ident, dict) and set(ident) == {'$oid'} and
                  isinstance(ident['$oid'], str) and re.fullmatch('[0-9a-fA-F]{24}', ident['$oid'])):
            raise MigrationError('已有 ID 快照含不支持的主键类型')
    if overlaps:
        raise MigrationError(f'与已有集合存在 {overlaps} 条 _id 冲突；禁止直接重复导入')
    return len(existing)
