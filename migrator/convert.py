"""Deterministic conversion with quarantine of broken or unpublished ancestry."""
from __future__ import annotations

import hashlib
import uuid
from collections import Counter
from dataclasses import dataclass
from typing import Any

from .common import integer
from .nekomi import Target
from .render import render, safe_link
from .source import Source

TARGET_VERSION = '2.0.17'


def comment_id(namespace: str, coid: int) -> str:
    return uuid.uuid5(uuid.NAMESPACE_URL, f'typecho2twikoo:v1:{namespace}:comment:{coid}').hex


@dataclass(frozen=True)
class Node:
    cid: int
    parent: int
    created: int


def graph(nodes: dict[int, Node]) -> tuple[dict[int, int], dict[int, str]]:
    """Iterative memoized ancestry walk. Long chains don't exhaust Python's stack."""
    roots: dict[int, int] = {}
    errors: dict[int, str] = {}
    for start in sorted(nodes):
        chain: list[int] = []
        visiting: set[int] = set()
        at = start
        root, error = 0, ''
        while True:
            if at in roots:
                root = roots[at]
                break
            if at in errors:
                error = errors[at]
                break
            if at in visiting:
                error = 'cycle'
                break
            chain.append(at)
            visiting.add(at)
            parent = nodes[at].parent
            if not parent:
                root = at
                break
            if parent not in nodes:
                error = 'missing_parent'
                break
            if nodes[parent].cid != nodes[at].cid:
                error = 'cross_article_parent'
                break
            at = parent
        for ident in chain:
            if error:
                errors[ident] = error
            else:
                roots[ident] = root
    return roots, errors


def convert(source: Source, mapping: dict[int, Target], *, namespace: str,
            owner_ids: set[int] | None = None, body_mode: str = 'markdown') -> tuple[list[dict[str, Any]], dict[str, Any]]:
    owner_ids = owner_ids or set()
    nodes: dict[int, Node] = {}
    for coid, row in source.comments.items():
        created = integer(row['created'], f'comment {coid} created')
        if created > 253402300799:
            from .common import MigrationError
            raise MigrationError(f'comment {coid}: created 不像 Unix 秒时间戳')
        nodes[coid] = Node(integer(row['cid'], f'comment {coid} cid', minimum=1),
                           integer(row['parent'], f'comment {coid} parent'), created * 1000)
    roots, graph_errors = graph(nodes)
    status_counts: Counter[str] = Counter()
    type_counts: Counter[str] = Counter()
    base_errors: dict[int, str] = {}
    for coid, row in sorted(source.comments.items()):
        status = row['status'] if row['status'] in {'approved', 'waiting', 'spam'} else 'other'
        status_counts[status] += 1
        kind = row['type'] if row['type'] in {'comment', 'trackback', 'pingback'} else 'other'
        type_counts[kind] += 1
        cid = nodes[coid].cid
        if status != 'approved':
            base_errors[coid] = 'not_approved'
        elif kind != 'comment':
            base_errors[coid] = 'not_normal_comment'
        elif cid not in source.contents:
            base_errors[coid] = 'missing_content'
        elif source.contents[cid]['status'] != 'publish':
            base_errors[coid] = 'unpublished_content'
        elif cid not in mapping:
            base_errors[coid] = 'unmapped_content'
        elif coid in graph_errors:
            base_errors[coid] = graph_errors[coid]
    # Exclude descendants of filtered comments; never reveal a pending ancestor.
    blocked = dict(base_errors)
    accepted: set[int] = set()
    for start in sorted(nodes):
        at, chain = start, []
        while at not in blocked and at not in accepted:
            chain.append(at)
            if nodes[at].parent == 0:
                break
            at = nodes[at].parent
        if at in blocked:
            for ident in chain:
                blocked[ident] = 'excluded_ancestor'
        else:
            accepted.update(chain)
    documents: list[dict[str, Any]] = []
    for coid in sorted(accepted):
        row, node = source.comments[coid], nodes[coid]
        t = mapping[node.cid]
        mail = str(row.get('mail') or '').strip().lower()
        author_id = integer(row.get('authorId') or 0, f'comment {coid} authorId')
        doc: dict[str, Any] = {
            '_id': comment_id(namespace, coid),
            'nick': str(row.get('author') or '匿名'),
            'mail': mail,
            'mailMd5': hashlib.md5(mail.encode('utf-8')).hexdigest() if mail else '',
            'link': safe_link(str(row.get('url') or '')),
            'ua': str(row.get('agent') or ''),
            # No IP, uid or fabricated session/ownership token.
            'master': author_id > 0 and author_id in owner_ids,
            'url': t.url, 'href': t.href,
            'comment': render(str(row.get('text') or ''), body_mode),
            'isSpam': False,
            'created': node.created, 'updated': node.created,
        }
        if node.parent:
            doc.update(pid=comment_id(namespace, node.parent), rid=comment_id(namespace, roots[coid]))
        documents.append(doc)
    issues = [{'coid': coid, 'cid': nodes[coid].cid, 'code': code}
              for coid, code in sorted(blocked.items())]
    anomalies = [{'coid': coid, 'cid': nodes[coid].cid, 'code': code}
                 for coid, code in sorted(graph_errors.items())]
    anomalies += [{'coid': coid, 'cid': n.cid, 'code': 'missing_content'}
                  for coid, n in sorted(nodes.items()) if n.cid not in source.contents]
    expected = sum(r['status'] == 'approved' and r['type'] == 'comment'
                   for r in source.comments.values())
    report = {
        'schema': 1, 'target_twikoo': TARGET_VERSION, 'namespace': namespace,
        'source_contents': len(source.contents), 'source_comments': len(nodes),
        'status_counts': dict(sorted(status_counts.items())),
        'type_counts': dict(sorted(type_counts.items())),
        'exported': len(documents), 'skipped': len(blocked),
        'skipped_counts': dict(sorted(Counter(blocked.values()).items())),
        'issues': issues, 'anomalies': anomalies,
        'id_map': {str(coid): comment_id(namespace, coid) for coid in sorted(accepted)},
        'incomplete': len(documents) < expected,
        'master_policy': 'explicit_authenticated_typecho_authorId' if owner_ids else 'all_false',
        'body_mode': body_mode, 'ip_exported': False,
    }
    return documents, report


def report_markdown(report: dict[str, Any]) -> str:
    lines = ['# 离线迁移报告', '', f"目标存储契约：Twikoo {report['target_twikoo']}", '',
             f"源评论 {report['source_comments']}；导出 {report['exported']}；跳过 {report['skipped']}。", '',
             '仅导出 approved 普通评论；此报告不代表数据库导入或线上展示已验证。', '',
             '## 跳过原因', '']
    lines += [f'- {code}: {count}' for code, count in report['skipped_counts'].items()]
    lines += ['', '## 需检查的评论 ID', '', '| coid | cid | 原因 |', '| --- | --- | --- |']
    lines += [f"| {i['coid']} | {i['cid']} | {i['code']} |" for i in report['issues']]
    lines += ['', '详细结构异常（含未审核评论）和稳定 ID 映射见 report.json。', '']
    return '\n'.join(lines)
