"""A restricted MySQL dump reader, not an SQL executor.

Lexes comments, strings and statement boundaries; parses CREATE column order and
literal INSERT tuples. Anything affecting target data outside that grammar fails
closed. Input is loaded in memory. SQL values are never echoed in errors.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .common import MigrationError, integer


@dataclass(frozen=True)
class Token:
    kind: str
    value: str


ESCAPES = {'0': '\0', 'b': '\b', 'n': '\n', 'r': '\r', 't': '\t',
           'Z': '\x1a', "'": "'", '"': '"', '\\': '\\', '%': '\\%', '_': '\\_'}


def lex(text: str) -> Iterator[Token]:
    i = 0
    while i < len(text):
        c = text[i]
        if c.isspace():
            i += 1
        elif c == '#' or (text.startswith('--', i) and
                          (i + 2 == len(text) or text[i + 2].isspace())):
            end = text.find('\n', i)
            i = len(text) if end < 0 else end + 1
        elif text.startswith('/*', i):
            end = text.find('*/', i + 2)
            if end < 0:
                raise MigrationError('SQL: 未闭合注释')
            inside = text[i + 2:end]
            # MariaDB 11.x adds a client-only sandbox directive, not SQL data.
            if re.fullmatch(r'M!\d+\\- enable the sandbox mode\s*', inside):
                i = end + 2
                continue
            if inside.startswith('!') or inside.startswith('M!'):
                # mysqldump executable directives: do not silently discard modes.
                inside = re.sub(r'^(?:M)?!\d*\s*', '', inside)
                yield from lex(inside)
            i = end + 2
        elif c in "'\"`":
            quote, result = c, []
            i += 1
            while i < len(text):
                c = text[i]
                if c == quote:
                    if i + 1 < len(text) and text[i + 1] == quote:
                        result.append(quote)
                        i += 2
                        continue
                    i += 1
                    break
                if c == '\\' and quote != '`':
                    i += 1
                    if i >= len(text):
                        raise MigrationError('SQL: 字符串转义不完整')
                    result.append(ESCAPES.get(text[i], text[i]))
                else:
                    result.append(c)
                i += 1
            else:
                raise MigrationError('SQL: 未闭合字符串或标识符')
            yield Token('id' if quote == '`' else 'str', ''.join(result))
        elif c.isascii() and (c.isalnum() or c in '_$'):
            start = i
            while i < len(text) and text[i].isascii() and (text[i].isalnum() or text[i] in '_$.'):
                i += 1
            yield Token('word', text[start:i])
        else:
            yield Token('symbol', c)
            i += 1


def statements(text: str) -> Iterator[list[Token]]:
    current: list[Token] = []
    for token in lex(text):
        if token.value == ';' and token.kind == 'symbol':
            if current:
                yield current
            current = []
        else:
            current.append(token)
    if current:
        raise MigrationError('SQL: 末条语句没有分号（可能备份被截断）')


def groups(tokens: list[Token]) -> list[list[Token]]:
    """Split top-level comma groups while retaining nested SQL type syntax."""
    result: list[list[Token]] = [[]]
    depth = 0
    for token in tokens:
        if token.kind == 'symbol':
            if token.value == '(':
                depth += 1
            elif token.value == ')':
                depth -= 1
            elif token.value == ',' and depth == 0:
                result.append([])
                continue
        if depth < 0:
            raise MigrationError('SQL: 括号不平衡')
        result[-1].append(token)
    if depth:
        raise MigrationError('SQL: 括号不平衡')
    return result


def parenthesized(tokens: list[Token], start: int) -> tuple[list[Token], int]:
    if start >= len(tokens) or tokens[start].kind != 'symbol' or tokens[start].value != '(':
        raise MigrationError('SQL: 预期括号')
    depth = 1
    for i in range(start + 1, len(tokens)):
        if tokens[i].kind == 'symbol':
            depth += (tokens[i].value == '(') - (tokens[i].value == ')')
        if depth == 0:
            return tokens[start + 1:i], i + 1
    raise MigrationError('SQL: 缺少闭括号')


def literal(tokens: list[Token]) -> Any:
    if len(tokens) == 1:
        t = tokens[0]
        if t.kind == 'str':
            return t.value
        if t.kind == 'word' and t.value.upper() == 'NULL':
            return None
        if t.kind == 'word' and re.fullmatch(r'\d+', t.value):
            return int(t.value)
    if len(tokens) == 2 and tokens[0].value == '-' and tokens[1].value.isdecimal():
        return -int(tokens[1].value)
    raise MigrationError('SQL: INSERT 仅支持字符串、整数、NULL；表达式/十六进制/浮点请用本地 MariaDB 备用导出')


@dataclass
class Source:
    contents: dict[int, dict[str, Any]]
    comments: dict[int, dict[str, Any]]


def index_rows(tables: dict[str, list[dict[str, Any]]]) -> Source:
    result: dict[str, dict[int, dict[str, Any]]] = {}
    required = {'contents': {'cid', 'slug', 'type', 'status'},
                'comments': {'coid', 'cid', 'parent', 'created', 'author', 'text', 'status', 'type'}}
    for table, key in [('contents', 'cid'), ('comments', 'coid')]:
        result[table] = {}
        for row in tables[table]:
            if not required[table] <= row.keys():
                raise MigrationError(f'{table}: 缺少必需列')
            for field in required[table] - {'cid', 'coid', 'parent', 'created'}:
                if not isinstance(row[field], str):
                    raise MigrationError(f'{table}: 文本必需列必须为字符串')
            ident = integer(row[key], key, minimum=1)
            if ident in result[table]:
                raise MigrationError(f'{table}: 重复 {key}={ident}')
            result[table][ident] = row
    return Source(result['contents'], result['comments'])


def load_sql(path: Path, prefix: str) -> Source:
    tables: dict[str, list[dict[str, Any]]] = {'contents': [], 'comments': []}
    names = {prefix + key: key for key in tables}
    schemas: dict[str, list[str]] = {}
    seen: set[str] = set()
    text = path.read_text(encoding='utf-8-sig')
    for ts in statements(text):
        head = ts[0].value.upper()
        words = [t.value.upper() for t in ts if t.kind != 'symbol']
        if head == 'SET':
            if any('NO_BACKSLASH_ESCAPES' in w or 'ANSI_QUOTES' in w for w in words):
                raise MigrationError('SQL: 不支持此 SQL_MODE；请用本地 MariaDB 备用导出')
            if 'NAMES' in words and not any(w in {'UTF8', 'UTF8MB4'} for w in words):
                raise MigrationError('SQL: 仅接受 UTF-8/utf8mb4 NAMES；请先本地转换编码')
            continue
        if head == 'CREATE' and len(ts) > 3 and ts[1].value.upper() == 'TABLE':
            start = 2
            if ts[start].value.upper() == 'IF':
                start += 3
            name = ts[start].value
            if name not in names:
                continue
            if tables[names[name]]:
                raise MigrationError('SQL: 数据之后出现目标表定义，请仅导出一个数据库')
            seen.add(name)
            body, _ = parenthesized(ts, start + 1)
            schema = []
            for group in groups(body):
                if not group:
                    raise MigrationError('SQL: 空列定义')
                if group[0].value.upper() not in {'PRIMARY', 'KEY', 'UNIQUE', 'CONSTRAINT', 'INDEX', 'FULLTEXT', 'CHECK', 'FOREIGN'}:
                    schema.append(group[0].value)
            if len(schema) != len(set(schema)):
                raise MigrationError('SQL: 重复列定义')
            if name in schemas:
                raise MigrationError('SQL: 重复目标表定义；请仅导出一个数据库')
            schemas[name] = schema
        elif head == 'INSERT':
            if len(ts) < 4 or ts[1].value.upper() != 'INTO':
                raise MigrationError('SQL: 仅支持 INSERT INTO ... VALUES')
            name, start = ts[2].value, 3
            if start < len(ts) and ts[start].value == '.':
                raise MigrationError('SQL: 不支持数据库限定表名，请导出单库或使用本地备用导出')
            # Qualified target names are intentionally unsupported.
            if name not in names:
                if any(n in name for n in names):
                    raise MigrationError('SQL: 不支持带数据库限定的目标表名')
                continue
            seen.add(name)
            columns = schemas.get(name)
            if ts[start].value == '(':
                body, start = parenthesized(ts, start)
                column_groups = groups(body)
                if any(len(g) != 1 or g[0].kind not in {'id', 'word'} for g in column_groups):
                    raise MigrationError('SQL: 无效列清单')
                columns = [g[0].value for g in column_groups]
            if not columns or len(columns) != len(set(columns)):
                raise MigrationError('SQL: 无列清单且缺少 CREATE TABLE，或存在重复列')
            if start >= len(ts) or ts[start].value.upper() != 'VALUES':
                raise MigrationError('SQL: 不支持 INSERT SELECT/SET')
            start += 1
            while start < len(ts):
                body, start = parenthesized(ts, start)
                values = [literal(g) for g in groups(body)]
                if len(values) != len(columns):
                    raise MigrationError('SQL: INSERT 值与列数不符')
                tables[names[name]].append(dict(zip(columns, values)))
                if start == len(ts):
                    break
                if ts[start].value != ',' or start + 1 == len(ts):
                    raise MigrationError('SQL: 不支持 INSERT 后缀（例如 ON DUPLICATE KEY）')
                start += 1
        elif head in {'DROP', 'LOCK', 'UNLOCK', 'ALTER', 'USE', 'START', 'COMMIT', 'BEGIN'}:
            # Only dump scaffolding is allowed; don't silently ignore ALTER data transforms.
            if head == 'DROP' and any(t.value in names and tables[names[t.value]] for t in ts):
                raise MigrationError('SQL: 数据之后出现 DROP，拒绝合并多份备份')
            if head == 'ALTER' and not ('KEYS' in words and ('DISABLE' in words or 'ENABLE' in words)):
                raise MigrationError('SQL: 仅支持 ALTER TABLE ENABLE/DISABLE KEYS')
            if head == 'DROP' and (len(ts) < 2 or ts[1].value.upper() != 'TABLE'):
                raise MigrationError('SQL: 仅支持 DROP TABLE 备份脚手架')
        else:
            raise MigrationError('SQL: 不支持的语句；请用本地 MariaDB 备用导出')
    if not set(names) <= seen:
        raise MigrationError('SQL: 未找到完整 contents/comments 表，请检查 --prefix 和备份范围')
    return index_rows(tables)


def load_jsonl(path: Path) -> Source:
    tables: dict[str, list[dict[str, Any]]] = {'contents': [], 'comments': []}
    seen: set[str] = set()
    for line_no, line in enumerate(path.read_text(encoding='utf-8-sig').splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
            if item['table'] not in tables:
                raise ValueError
            seen.add(item['table'])
            if item.get('schema') == 1:  # table-presence marker also represents empty tables
                continue
            if not isinstance(item['row'], dict):
                raise ValueError
            tables[item['table']].append(item['row'])
        except (ValueError, KeyError, TypeError):
            raise MigrationError(f'源 JSONL: 第 {line_no} 行格式错误') from None
    if seen != set(tables):
        raise MigrationError('源 JSONL: 必须包含两个表（空表用 schema 标记）')
    return index_rows(tables)


def load_source(sql: str | None, source_jsonl: str | None, prefix: str) -> Source:
    if not re.fullmatch(r'[A-Za-z0-9_]*', prefix):
        raise MigrationError('--prefix 只能含 ASCII 字母、数字、下划线')
    return load_sql(Path(sql), prefix) if sql else load_jsonl(Path(source_jsonl or ''))
