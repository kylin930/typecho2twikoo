#!/usr/bin/env python3
"""Export a LOCAL restored Typecho DB via MariaDB CLI; no Python DB driver.

Credentials remain in the client's local defaults-extra-file, never argv/logs.
Only localhost / 127.0.0.1 / ::1 are accepted. Does not restore or execute a dump.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

COLUMNS = {
    'contents': ['cid', 'slug', 'type', 'status'],
    'comments': ['coid', 'cid', 'parent', 'created', 'author', 'authorId', 'ownerId',
                 'mail', 'url', 'agent', 'text', 'status', 'type'],
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--client', default='mariadb', help='本地 MariaDB/MySQL CLI 路径')
    parser.add_argument('--defaults-file', required=True, type=Path)
    parser.add_argument('--database', required=True)
    parser.add_argument('--host', choices=['localhost', '127.0.0.1', '::1'], default='127.0.0.1')
    parser.add_argument('--port', type=int, default=3306)
    parser.add_argument('--prefix', default='typecho_')
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if not all(re.fullmatch('[A-Za-z0-9_]+', x) for x in [args.database, args.prefix]):
        parser.error('数据库名/表前缀只能含 ASCII 字母、数字、下划线')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=args.output.parent, prefix='.typecho-export-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as out:
            for table, columns in COLUMNS.items():
                out.write(json.dumps({'table': table, 'schema': 1}) + '\n')
                pairs = ','.join(f"'{column}',`{column}`" for column in columns)
                sql = f"SELECT JSON_OBJECT({pairs}) FROM `{args.prefix}{table}` ORDER BY `{columns[0]}`"
                command = [args.client, '--defaults-extra-file=' + str(args.defaults_file.resolve()),
                           '--host=' + args.host, '--port=' + str(args.port), '--protocol=TCP',
                           '--default-character-set=utf8mb4', '--batch', '--raw', '--skip-column-names',
                           '--database=' + args.database, '--execute=' + sql]
                # No stderr is printed: client diagnostics can expose connection configuration.
                with tempfile.TemporaryFile() as errors:
                    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors,
                                               encoding='utf-8', errors='strict')
                    try:
                        assert process.stdout is not None
                        for line in process.stdout:
                            row = json.loads(line)
                            if not isinstance(row, dict) or set(row) != set(columns):
                                raise ValueError('unexpected local SQL output')
                            out.write(json.dumps({'table': table, 'row': row}, ensure_ascii=False) + '\n')
                    except BaseException:
                        process.terminate()
                        raise
                    finally:
                        process.stdout.close()
                        process.wait()
                    if process.returncode:
                        raise RuntimeError('client failed')
        os.replace(temporary, args.output)
        print('本地 Typecho JSONL 导出完成。请继续 inspect。')
        return 0
    except (OSError, ValueError, RuntimeError, UnicodeError):
        print('导出失败；请在本地检查客户端、临时数据库、字段及安全配置，未发布半成品。')
        return 2
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


if __name__ == '__main__':
    raise SystemExit(main())
