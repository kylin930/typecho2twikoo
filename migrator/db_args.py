"""Database argument definitions; no optional driver imports."""
from pathlib import Path
from typing import Any

COMMANDS = ['db-check', 'db-info', 'db-backup', 'db-import', 'db-verify', 'db-rollback-plan']


def register(commands: Any) -> None:
    for name in COMMANDS:
        sub = commands.add_parser(name)
        sub.add_argument('--env-file', type=Path, default=Path('.env'))
        sub.add_argument('--database', help='显式目标库（可覆盖 URI 默认库以使用独立测试库）')
        if name == 'db-backup':
            sub.add_argument('--backup-dir', type=Path, default=Path('backups'))
        if name in {'db-import', 'db-verify'}:
            sub.add_argument('--file', type=Path, default=Path('output/twikoo-comments.jsonl'))
            sub.add_argument('--map', type=Path, default=Path('output/url-map.csv'))
            sub.add_argument('--nekomi', type=Path, required=True)
            sub.add_argument('--site', required=True)
            sub.add_argument('--base')
            sub.add_argument('--nekomi-config')
            sub.add_argument('--posts-dir')
            sub.add_argument('--pages-dir')
            sub.add_argument('--journal-dir', type=Path, default=Path('output/db-migrations'))
        if name == 'db-import':
            mode = sub.add_mutually_exclusive_group()
            mode.add_argument('--dry-run', action='store_true', help='默认：只读预检')
            mode.add_argument('--commit', action='store_true', help='显式插入，必须有备份与目标确认')
            sub.add_argument('--confirm-database')
            sub.add_argument('--confirm-collection')
            sub.add_argument('--backup-receipt', type=Path)
            sub.add_argument('--batch-size', type=int, default=200)
            sub.add_argument('--resume', help='保留原输入与日志，从此批次恢复')
            sub.add_argument('--report', type=Path, default=Path('output/db-preview.json'))
        if name in {'db-verify', 'db-rollback-plan'}:
            sub.add_argument('--batch-id', required=True)
        if name == 'db-rollback-plan':
            sub.add_argument('--journal-dir', type=Path, default=Path('output/db-migrations'))
            sub.add_argument('--report', type=Path, default=Path('output/db-rollback-plan.json'))
