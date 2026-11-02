"""Source-verified Nekomi / Astro 7.4.2 identity and URL mapping."""
from __future__ import annotations

import bisect
import csv
import io
import json
import re
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import yaml

from .common import MigrationError, integer
from .source import Source

RANGES = json.loads(Path(__file__).with_name('slugger-ranges.json').read_text())
STARTS = [r[0] for r in RANGES]
RESERVED = {'posts', 'page', 'friends', 'categories', 'tags', 'archive', '404',
            '404.html', 'article.html', 'rss.xml', 'robots.txt', 'sitemap.xml',
            'sitemap-index.xml', '__nekomi', '_astro', '_image'}
FIELDS = ['cid', 'source_type', 'source_slug', 'target', 'url', 'href', 'status', 'reason']


class UniqueLoader(yaml.SafeLoader):
    pass


def unique_mapping(loader: UniqueLoader, node: Any) -> dict[Any, Any]:
    pairs = loader.construct_pairs(node, deep=True)
    result = {}
    for key, value in pairs:
        if not isinstance(key, str) or key in result:
            raise MigrationError('Frontmatter: 重复键或非字符串键')
        result[key] = value
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


def encode(value: str) -> str:
    """JavaScript encodeURIComponent, including !'()* as unescaped characters."""
    return quote(value, safe="~()*!.'-_")


def github_slug(value: str) -> str:
    def retained(char: str) -> bool:
        cp = ord(char)
        at = bisect.bisect_right(STARTS, cp) - 1
        return at < 0 or cp > RANGES[at][1]
    return ''.join(c for c in value.lower() if retained(c)).replace(' ', '-')


def content_id(relative: str, data: dict[str, Any]) -> str:
    if data.get('slug'):
        if not isinstance(data['slug'], str):
            raise MigrationError('Frontmatter slug 必须为字符串')
        return data['slug']
    value = '/'.join(github_slug(s) for s in relative[:-3].split('/'))
    return value[:-6] if value.endswith('/index') else value


def thread_path(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r'/(?!/)[^?#\s\\]*', value):
        raise MigrationError('Twikoo url 必须为不含域名、空白、反斜杠、查询和 Hash 的路径')
    return value


def site_origin(site: str) -> str:
    try:
        parsed = urlsplit(site)
    except ValueError:
        raise MigrationError('--site 不是合法 URL') from None
    if (parsed.scheme not in {'https', 'http'} or not parsed.netloc or parsed.username
            or parsed.password or parsed.query or parsed.fragment or '\\' in site or any(c.isspace() for c in site) or parsed.path not in {'', '/'}):
        raise MigrationError('--site 必须为 HTTP(S) 站点源地址；子路径用 --base')
    return site.rstrip('/')


def base_path(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r'/(?:[A-Za-z0-9_.~\-]+/)*', value) or any(part in {'.', '..'} for part in value.split('/')):
        raise MigrationError('base 必须为 / 或 /Nekomi/ 形式的路径')
    return value


def page_slug(value: str) -> bool:
    return (0 < len(value) <= 160 and value.split('/')[0].lower() not in RESERVED and
            all(segment and segment[0].isalnum() and
                all(c.isalnum() or c in '_-' for c in segment) for segment in value.split('/')))


@dataclass(frozen=True)
class Target:
    target: str
    file: str
    id: str
    kind: str
    slug: str
    url: str
    href: str
    enabled: bool
    draft: bool


def frontmatter(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding='utf-8-sig')
    lines = text.splitlines()
    if not lines or lines[0] != '---':
        raise MigrationError('内容文件缺少 YAML Frontmatter')
    end = next((i for i in range(1, len(lines)) if lines[i] in {'---', '...'}), None)
    if end is None:
        raise MigrationError('内容文件 Frontmatter 未闭合')
    try:
        data = yaml.load('\n'.join(lines[1:end]), Loader=UniqueLoader)
    except yaml.YAMLError:
        raise MigrationError('Frontmatter YAML 无法解析（错误值未打印）') from None
    if not isinstance(data, dict):
        raise MigrationError('Frontmatter 必须是对象')
    for key in ['comments', 'draft']:
        if key in data and type(data[key]) is not bool:
            raise MigrationError(f'Frontmatter {key} 必须是布尔值')
    for key in ['commentId', 'commentPath', 'slug']:
        if key in data and (not isinstance(data[key], str) or not data[key]):
            raise MigrationError(f'Frontmatter {key} 必须是非空字符串')
    return data


def scan(root: Path, site: str, *, base: str | None = None, config: str | None = None,
         posts_dir: str | None = None, pages_dir: str | None = None) -> dict[str, Target]:
    origin = site_origin(site)
    config_path = Path(config) if config else root / 'site.config.json'
    try:
        settings = json.loads(config_path.read_text(encoding='utf-8'))
        comments = settings.get('comments', {})
        base = base_path(base if base is not None else settings.get('publishing', {}).get('base', '/'))
        prefixes = {'post': comments.get('keyPrefix', '/posts/'),
                    'page': comments.get('pageKeyPrefix', '/pages/')}
    except (ValueError, AttributeError, TypeError):
        raise MigrationError('无法读取 Nekomi JSON 配置') from None
    for prefix in prefixes.values():
        base_path(prefix)
    targets: dict[str, Target] = {}
    keys: set[str] = set()
    page_slugs: set[str] = set()
    for kind, directory in [('post', Path(posts_dir) if posts_dir else root / 'src/content/posts'),
                            ('page', Path(pages_dir) if pages_dir else root / 'src/content/pages')]:
        if not directory.is_dir():
            raise MigrationError('Nekomi 内容目录不存在；外部目录请显式指定')
        for path in sorted(directory.rglob('*.md')):
            relative = path.relative_to(directory).as_posix()
            data = frontmatter(path)
            ident = content_id(relative, data)
            if not ident:
                raise MigrationError('Astro 内容 ID 为空')
            slug = unicodedata.normalize('NFC', data.get('slug') or ident) if kind == 'page' else ident
            if kind == 'page':
                if not page_slug(slug) or slug.lower() in page_slugs:
                    raise MigrationError('页面 slug 无效、保留或重复')
                page_slugs.add(slug.lower())
            url = thread_path(data.get('commentPath') or prefixes[kind] + encode(data.get('commentId') or ident) + '/')
            target = kind + ':' + ident
            if target in targets:
                raise MigrationError('重复 Astro 内容 ID')
            draft = data.get('draft', False)
            enabled = data.get('comments', kind == 'post') and not draft
            # Twikoo queries both trailing slash variants; treat those as collisions too.
            if not draft and (kind == 'post' or enabled):
                key = url.rstrip('/')
                if key in keys:
                    raise MigrationError('重复 Twikoo 评论键（含尾斜杠等价形式）')
                keys.add(key)
            route = 'posts/' + encode(ident) if kind == 'post' else '/'.join(map(encode, slug.split('/')))
            targets[target] = Target(target, f'{kind}s/{relative}', ident, kind, slug,
                                     url, origin + base + route + '/', enabled, draft)
    return targets


def prepare_rows(source: Source, targets: dict[str, Target]) -> list[dict[str, str]]:
    rows = []
    for cid, content in sorted(source.contents.items()):
        kind, slug = str(content['type']), str(content['slug'])
        row = dict.fromkeys(FIELDS, '')
        row.update(cid=str(cid), source_type=kind, source_slug=slug, status='unmapped', reason='no_exact_match')
        # Exact slug-to-id equality is a candidate, not a title or numeric-ID guess.
        matches = [t for t in targets.values() if t.kind == kind and (t.id == slug or (kind == 'page' and t.slug == slug))]
        if len(matches) == 1:
            t = matches[0]
            row.update(target=t.target, url=t.url, href=t.href,
                       status='review' if t.enabled else 'disabled',
                       reason='exact_slug_candidate' if t.enabled else 'comments_disabled_or_draft')
        elif len(matches) > 1:
            row['reason'] = 'ambiguous'
        rows.append(row)
    return rows


def csv_text(rows: list[dict[str, str]]) -> str:
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, FIELDS, lineterminator='\n')
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue()


def read_map(path: Path, source: Source, targets: dict[str, Target]) -> dict[int, Target]:
    result: dict[int, Target] = {}
    seen: set[int] = set()
    used: set[str] = set()
    with path.open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != FIELDS:
            raise MigrationError('CSV 表头不符合当前格式，请重新 prepare')
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise MigrationError('CSV 列数不符')
            cid = integer(row['cid'], 'map.cid', minimum=1)
            if cid in seen or cid not in source.contents:
                raise MigrationError(f'映射重复或不存在 cid={cid}')
            seen.add(cid)
            content = source.contents[cid]
            if row['source_slug'] != str(content['slug']) or row['source_type'] != str(content['type']):
                raise MigrationError(f'映射源信息过期 cid={cid}')
            if row['status'] not in {'ready', 'review', 'unmapped', 'disabled', 'skip'}:
                raise MigrationError(f'映射状态无效 cid={cid}')
            if row['status'] != 'ready':
                continue
            t = targets.get(row['target'])
            if (not t or not t.enabled or t.kind != content['type'] or
                    row['url'] != t.url or row['href'] != t.href):
                raise MigrationError(f'映射目标不存在、关闭或 url/href 与 Nekomi 不符 cid={cid}')
            if t.target in used:
                raise MigrationError('多个 cid 指向同一目标，可能串帖；请先人工合并来源')
            if content['status'] != 'publish':
                raise MigrationError(f'不能公开非 publish 内容 cid={cid}')
            used.add(t.target)
            result[cid] = t
    return result


def catalog_text(targets: dict[str, Target]) -> list[dict[str, Any]]:
    return [asdict(t) for t in targets.values()]
