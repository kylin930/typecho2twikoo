"""Opt-in database CLI, kept separate from the offline dependency path."""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from migrator.common import MigrationError, json_text, write_private
from migrator.nekomi import scan
from .backup import create_backup
from .diagnostics import diagnose
from .importer import import_comments, journal_path, load_journal, rollback_plan
from .mongodb import Settings, connect
from .validator import database_info, input_documents, preflight, verify_stored




def run(args: argparse.Namespace) -> int:
    settings = Settings.from_env(database=args.database, env_file=args.env_file)
    if args.command == 'db-check':
        report = diagnose(settings)
        print(json_text(report), end='')
        return 0 if report.get('category') == 'ok' else 2
    if args.command == 'db-backup':
        with connect(settings):  # verify credentials/target before invoking external tool
            result = create_backup(settings, args.backup_dir)
        print(json_text(result), end='')
        return 0
    if args.command in {'db-import', 'db-verify'}:
        targets = scan(args.nekomi, args.site, base=args.base, config=args.nekomi_config,
                       posts_dir=args.posts_dir, pages_dir=args.pages_dir)
        docs, input_sha, summary = input_documents(args.file, args.map, targets)
        if args.command == 'db-import' and args.commit:
            if (args.confirm_database != settings.database or args.confirm_collection != settings.collection
                or not args.backup_receipt):
                raise MigrationError('--commit 必须同时提供有效备份凭据和精确目标库/集合确认。')
        with connect(settings) as db:
            if args.command == 'db-verify':
                path = journal_path(args.journal_dir, args.batch_id)
                load_journal(path, settings, input_sha, [d['_id'] for d in docs])
                result = verify_stored(db, settings, docs, args.batch_id, input_sha)
            elif args.commit:
                result = import_comments(db, settings, docs, input_sha=input_sha,
                    backup_receipt=args.backup_receipt, confirm_database=args.confirm_database,
                    confirm_collection=args.confirm_collection, journal_dir=args.journal_dir,
                    batch_size=args.batch_size, resume=args.resume,
                    progress=lambda data: print(json_text(data), end=''))
            else:
                if args.resume:
                    load_journal(journal_path(args.journal_dir, args.resume), settings, input_sha, [d['_id'] for d in docs])
                result = {**preflight(db, settings, docs, input_sha=input_sha, batch_id=args.resume),
                          'input_validation': summary}
                write_private(args.report, json_text(result))
        print(json_text(result), end='')
        return 2 if result.get('blocked') else 0
    with connect(settings) as db:
        if args.command == 'db-info':
            result = database_info(db, settings)
        else:
            result = rollback_plan(db, settings, journal_path(args.journal_dir, args.batch_id))
            write_private(args.report, json_text(result))
    print(json_text(result), end='')
    return 0
