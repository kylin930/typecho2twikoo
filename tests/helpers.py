from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from migrator.source import Source, index_rows


def comment(coid: int = 1, cid: int = 10, parent: int = 0, **changes: Any) -> dict[str, Any]:
    row = dict(coid=coid, cid=cid, parent=parent, created=1700000000,
               author='合成访客😀', authorId=0, ownerId=1, mail='SYNTHETIC@example.invalid',
               url='https://example.invalid/', agent='Synthetic-UA/1.0',
               text='你好 **Markdown** 😀\n第二行', status='approved', type='comment')
    row.update(changes)
    return row


def content(cid: int = 10, slug: str = 'hello', **changes: Any) -> dict[str, Any]:
    return dict(cid=cid, slug=slug, type='post', status='publish', **changes)


def source(rows: list[dict[str, Any]] | None = None) -> Source:
    return index_rows({'contents': [content(), content(20, 'other'),
                                   dict(cid=30, slug='about', type='page', status='publish')],
                       'comments': [comment()] if rows is None else rows})


def nekomi(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / 'site.config.json').write_text(json.dumps({'publishing': {'base': '/'}}))
    post = root / 'src/content/posts'
    page = root / 'src/content/pages'
    post.mkdir(parents=True)
    page.mkdir(parents=True)
    (post / 'hello.md').write_text('---\ntitle: 合成文章\ndate: "2023-11-14"\n---\n', encoding='utf-8')
    (post / 'other.md').write_text('---\ntitle: 另一篇\ndate: "2023-11-14"\ncommentId: old-id\n---\n', encoding='utf-8')
    (page / 'renamed.md').write_text('---\ntitle: 关于\nslug: about\ncomments: true\ncommentPath: /old/about.html\n---\n', encoding='utf-8')
    return root


def quote_sql(value: Any) -> str:
    if value is None:
        return 'NULL'
    if isinstance(value, int):
        return str(value)
    return "'" + str(value).replace('\\', '\\\\').replace("'", "\\'").replace('\n', '\\n').replace('\r', '\\r').replace('\0', '\\0') + "'"


def sql_dump(data: Source, prefix: str = 'typecho_') -> str:
    statements = ['-- synthetic only', '/*!40101 SET NAMES utf8mb4 */;']
    for name, rows in [('contents', data.contents), ('comments', data.comments)]:
        columns = list(next(iter(rows.values())).keys())
        statements.append(f'CREATE TABLE `{prefix}{name}` (' + ',\n'.join(f'`{c}` text' for c in columns) + ',PRIMARY KEY (`' + columns[0] + '`)) ENGINE=InnoDB;')
        tuples = ['(' + ','.join(quote_sql(row.get(c)) for c in columns) + ')' for row in rows.values()]
        statements.append(f'INSERT INTO `{prefix}{name}` VALUES\n' + ',\n'.join(tuples) + ';')
    return '\n'.join(statements)
