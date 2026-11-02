"""Generate synthetic-only CLI demonstration inputs in an explicitly chosen folder."""
from __future__ import annotations

import argparse
from pathlib import Path

from tests.helpers import comment, nekomi, source, sql_dump


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=False)
    nekomi(args.directory / 'Nekomi')
    data = source([comment(1), comment(2, parent=1), comment(3, parent=2),
                   comment(4, cid=20), comment(5, cid=30),
                   comment(6, status='waiting'), comment(7, status='spam')])
    (args.directory / 'synthetic.sql').write_text(sql_dump(data), encoding='utf-8')
    print('合成示例已生成：7 条源评论，其中 5 条应导出。')


if __name__ == '__main__':
    main()
