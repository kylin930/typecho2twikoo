"""Public command line interface."""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from .common import MigrationError, json_text, write_private
from .convert import convert, report_markdown
from .nekomi import catalog_text, csv_text, prepare_rows, read_map, scan
from .source import load_source
from .validate import load_documents, validate, check_existing_ids


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description='Typecho → Twikoo 2.0.17：离线转换与显式启用的 MongoDB 迁移')
    commands = p.add_subparsers(dest='command', required=True)
    for command in ['inspect', 'prepare', 'convert']:
        sub = commands.add_parser(command)
        source = sub.add_mutually_exclusive_group(required=True)
        source.add_argument('--sql', help='UTF-8 MySQL/MariaDB 备份')
        source.add_argument('--source-jsonl', help='本地 MariaDB 备用导出')
        sub.add_argument('--prefix', default='typecho_')
        if command != 'inspect':
            sub.add_argument('--nekomi', required=True, type=Path)
            sub.add_argument('--site', required=True)
            sub.add_argument('--base', help='覆盖 publishing.base，如 /Nekomi/')
            sub.add_argument('--nekomi-config', help='实际部署使用的 NEKOMI_CONFIG JSON')
            sub.add_argument('--posts-dir', help='对应 NEKOMI_CONTENT_DIR')
            sub.add_argument('--pages-dir', help='对应 NEKOMI_PAGES_DIR')
            sub.add_argument('--output', type=Path, default=Path('output'))
        if command == 'convert':
            sub.add_argument('--map', required=True, type=Path)
            sub.add_argument('--namespace', required=True, help='永久保留的来源名，例如 nekopara-typecho')
            sub.add_argument('--owner-id', action='append', type=int, default=[], help='已确认博主的 Typecho 用户 uid，可重复')
            sub.add_argument('--body-mode', choices=['markdown', 'html'], default='markdown')
            sub.add_argument('--allow-partial', action='store_true', help='已知缺失的 approved 评论仍返回成功；报告保留 incomplete')
    sub = commands.add_parser('validate')
    sub.add_argument('--file', required=True, type=Path)
    sub.add_argument('--existing-ids', type=Path, help='mongoexport 导出的目标集合 _id 快照，仅离线检测冲突')
    from .db_args import register
    register(commands)
    return p


def run(args: argparse.Namespace) -> int:
    if args.command.startswith('db-'):
        try:
            from migration.cli import run as run_database
        except ModuleNotFoundError:
            raise MigrationError('数据库命令需要可选依赖：python -m pip install -r requirements-db.txt') from None
        return run_database(args)
    if args.command == 'validate':
        docs = load_documents(args.file)
        result = validate(docs)
        if args.existing_ids:
            result['existing_ids_checked'] = check_existing_ids(docs, args.existing_ids)
        print(json_text(result), end='')
        return 0
    source = load_source(args.sql, args.source_jsonl, args.prefix)
    if args.command == 'inspect':
        # No author/email/IP/text/slug values in terminal output.
        status = Counter(r['status'] if r['status'] in {'approved', 'waiting', 'spam'} else 'other'
                         for r in source.comments.values())
        print(json_text({'contents': len(source.contents), 'comments': len(source.comments), 'statuses': status}), end='')
        return 0
    targets = scan(args.nekomi, args.site, base=args.base, config=args.nekomi_config,
                   posts_dir=args.posts_dir, pages_dir=args.pages_dir)
    if args.command == 'prepare':
        rows = prepare_rows(source, targets)
        write_private(args.output / 'url-map.csv', csv_text(rows))
        write_private(args.output / 'catalog.json', json_text(catalog_text(targets)))
        print('已生成 url-map.csv 和 catalog.json；请逐项核对，将确认的行 status 改为 ready。')
        return 0
    if not args.namespace.strip() or any(c.isspace() for c in args.namespace) or len(args.namespace) > 128:
        raise MigrationError('namespace 必须为 1–128 字符的无空白稳定来源标识')
    if any(i <= 0 for i in args.owner_id):
        raise MigrationError('owner-id 必须为正整数')
    mapping = read_map(args.map, source, targets)
    docs, report = convert(source, mapping, namespace=args.namespace,
                           owner_ids=set(args.owner_id), body_mode=args.body_mode)
    if docs:
        report['validation'] = validate(docs)
    else:
        report['validation'] = {'documents': 0}
    # Read, parse and validate everything before replacing any output file.
    write_private(args.output / 'twikoo-comments.jsonl', ''.join(
        json.dumps(d, ensure_ascii=False, sort_keys=True, allow_nan=False) + '\n' for d in docs))
    write_private(args.output / 'url-map.csv', csv_text(prepare_output_rows(args.map)))
    write_private(args.output / 'report.json', json_text(report))
    write_private(args.output / 'report.md', report_markdown(report))
    print(f"导出 {len(docs)} 条；跳过 {report['skipped']} 条；详情见输出目录 report.json / report.md。")
    if not docs or (report['incomplete'] and not args.allow_partial):
        print('转换有缺失或为空；文件仅供审查，暂勿导入。处理映射/异常后重跑，或明确 --allow-partial。', file=sys.stderr)
        return 2
    return 0


def prepare_output_rows(path: Path) -> list[dict[str, str]]:
    import csv
    with path.open(encoding='utf-8-sig', newline='') as stream:
        return list(csv.DictReader(stream))


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return run(args)
    except (MigrationError, OSError, UnicodeError):
        # OS paths and parser context can contain secrets; keep diagnostics deliberately bounded.
        error = sys.exc_info()[1]
        print(f'错误：{error}' if isinstance(error, MigrationError) else
              '错误：文件读写或 UTF-8 解码失败，请检查路径、权限、编码。', file=sys.stderr)
        return 2
