"""CommonMark + explicit HTML allowlist; never fetch linked images or URLs."""
from __future__ import annotations

from urllib.parse import urlsplit

import bleach
from markdown_it import MarkdownIt

TAGS = {'p', 'br', 'hr', 'pre', 'code', 'blockquote', 'ul', 'ol', 'li', 'strong',
        'em', 'b', 'i', 'del', 's', 'a', 'img', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
        'table', 'thead', 'tbody', 'tr', 'th', 'td', 'span', 'div'}
ATTRS = {'a': ['href', 'title'], 'img': ['src', 'alt', 'title', 'width', 'height'],
         'code': ['class'], 'ol': ['start']}
MD = MarkdownIt('commonmark', {'html': True, 'breaks': True})


def sanitize(html: str) -> str:
    return bleach.clean(html, tags=TAGS, attributes=ATTRS,
                        protocols={'http', 'https', 'mailto'}, strip=True, strip_comments=True)


def render(text: str, mode: str = 'markdown') -> str:
    text = text.removeprefix('<!--markdown-->')
    return sanitize(MD.render(text) if mode == 'markdown' else text)


def safe_link(link: str) -> str:
    try:
        parsed = urlsplit(link)
        return link if (parsed.scheme.lower() in {'http', 'https'} and parsed.hostname
                        and not parsed.username and not any(ord(c) < 32 for c in link)) else ''
    except ValueError:
        return ''
