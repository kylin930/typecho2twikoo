from __future__ import annotations

import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path

from migrator.cli import main
from migrator.common import MigrationError, write_private
from migrator.convert import comment_id, convert, graph, Node
from migrator.nekomi import (content_id, csv_text, encode, github_slug, prepare_rows,
                            read_map, scan)
from migrator.render import render
from migrator.source import index_rows, load_jsonl, load_sql
from migrator.validate import load_documents, validate
from tests.helpers import comment, content, nekomi, source, sql_dump


class Workspace(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.nekomi = nekomi(self.root / 'Nekomi')
        self.targets = scan(self.nekomi, 'https://example.invalid')
        self.mapping = {10: self.targets['post:hello'], 20: self.targets['post:other'],
                        30: self.targets['page:about']}

    def sql(self, text: str):
        path = self.root / 'data.sql'
        path.write_text(text, encoding='utf-8')
        return load_sql(path, 'typecho_')

    def converted(self, rows=None, **kwargs):
        return convert(source(rows), self.mapping, namespace='synthetic', **kwargs)


class SqlTests(Workspace):
    def test_mysql_extended_multiline_insert_roundtrip(self):
        data = source([comment(1, text="引号' 双引号\" 反斜杠\\ \n中文😀 ;,( ) -- # /*text*/\r\0"), comment(2)])
        self.assertEqual(self.sql(sql_dump(data)).comments, data.comments)

    def test_explicit_columns_no_ddl(self):
        dump = "INSERT INTO typecho_contents (cid,slug,type,status) VALUES (10,'a','post','publish');\n" + \
               "INSERT INTO typecho_comments (coid,cid,parent,created,author,text,status,type) VALUES (1,10,0,1,'a','it''s\n😀','approved','comment');"
        self.assertEqual(self.sql(dump).comments[1]['text'], "it's\n😀")

    def test_custom_prefix(self):
        path = self.root / 'custom.sql'
        path.write_text(sql_dump(source(), 'blog_'), encoding='utf-8')
        self.assertEqual(len(load_sql(path, 'blog_').comments), 1)

    def test_unknown_prefix(self):
        with self.assertRaises(MigrationError): self.sql(sql_dump(source(), 'other_'))

    def test_duplicate_comment_id(self):
        dump = sql_dump(source()) + "\nINSERT INTO typecho_comments (coid,cid,parent,created,author,text,status,type) VALUES (1,10,0,1,'x','x','approved','comment');"
        with self.assertRaisesRegex(MigrationError, '重复'): self.sql(dump)

    def test_duplicate_content_id(self):
        with self.assertRaises(MigrationError):
            index_rows({'contents': [content(), content()], 'comments': []})

    def test_unsupported_sql_fails_closed(self):
        for suffix in ["UPDATE typecho_comments SET text='oops';", 'DELIMITER $$',
                       "SET SQL_MODE='NO_BACKSLASH_ESCAPES';", "SET SQL_MODE='ANSI_QUOTES';",
                       "INSERT INTO typecho_comments (coid) VALUES (NOW());",
                       "INSERT INTO typecho_comments (coid) VALUES (0xff);",
                       "INSERT INTO typecho_comments (coid) VALUES (1) ON DUPLICATE KEY UPDATE coid=1;",
                       'ALTER TABLE typecho_comments ADD extra text;', 'SET NAMES latin1;']:
            with self.subTest(suffix=suffix), self.assertRaises(MigrationError):
                self.sql(sql_dump(source()) + '\n' + suffix)

    def test_unclosed_and_truncated(self):
        for text in ["INSERT INTO t VALUES ('unclosed);", '/* unclosed', sql_dump(source())[:-1]]:
            with self.subTest(text=text[:10]), self.assertRaises(MigrationError): self.sql(text)

    def test_jsonl_fallback(self):
        p = self.root / 'source.jsonl'
        rows = [{'table': name, 'row': row} for name, values in [('contents', source().contents), ('comments', source().comments)] for row in values.values()]
        p.write_text('\n'.join(json.dumps(r, ensure_ascii=False) for r in rows), encoding='utf-8')
        self.assertEqual(load_jsonl(p).comments, source().comments)

    def test_empty_jsonl_table_markers(self):
        p = self.root / 'empty.jsonl'
        p.write_text('{"table":"contents","schema":1}\n{"table":"comments","schema":1}\n')
        self.assertEqual(load_jsonl(p).comments, {})


class MappingTests(Workspace):
    def test_post_comment_id_and_page_comment_path(self):
        self.assertEqual(self.targets['post:other'].url, '/posts/old-id/')
        self.assertEqual(self.targets['page:about'].url, '/old/about.html')
        self.assertEqual(self.targets['page:about'].href, 'https://example.invalid/about/')

    def test_slug_changes_astro_id(self):
        self.assertEqual(content_id('renamed.md', {'slug': 'about'}), 'about')
        self.assertNotIn('page:renamed', self.targets)

    def test_default_id_normalization(self):
        self.assertEqual(content_id('Sub Dir/Hello_WORLD!.md', {}), 'sub-dir/hello_world')
        self.assertEqual(content_id('Folder/index.md', {}), 'folder')
        self.assertEqual(content_id('index.md', {}), 'index')
        self.assertEqual(github_slug('中文 😀 Test'), '中文--test')
        self.assertEqual(encode("中文/a !'()*"), "%E4%B8%AD%E6%96%87%2Fa%20!'()*")

    def test_site_base_does_not_change_thread(self):
        targets = scan(self.nekomi, 'https://example.invalid', base='/Nekomi/')
        self.assertEqual(targets['post:hello'].url, '/posts/hello/')
        self.assertEqual(targets['post:hello'].href, 'https://example.invalid/Nekomi/posts/hello/')

    def test_disabled_page_and_post(self):
        p = self.nekomi / 'src/content/pages/default.md'
        p.write_text('---\ntitle: 默认关闭\n---\n', encoding='utf-8')
        p = self.nekomi / 'src/content/posts/off.md'
        p.write_text('---\ntitle: off\ncomments: false\n---\n')
        targets = scan(self.nekomi, 'https://example.invalid')
        self.assertFalse(targets['page:default'].enabled)
        self.assertFalse(targets['post:off'].enabled)

    def test_no_title_or_numeric_guess(self):
        s = source()
        s.contents[10]['slug'] = 'unknown'
        rows = prepare_rows(s, self.targets)
        self.assertEqual(rows[0]['status'], 'unmapped')
        self.assertEqual(rows[0]['url'], '')

    def test_review_required_and_stale_or_wrong_mapping(self):
        rows = prepare_rows(source(), self.targets)
        p = self.root / 'url-map.csv'
        p.write_text(csv_text(rows), encoding='utf-8')
        self.assertEqual(read_map(p, source(), self.targets), {})
        rows[0]['status'] = 'ready'
        p.write_text(csv_text(rows), encoding='utf-8')
        self.assertEqual(set(read_map(p, source(), self.targets)), {10})
        for field, value in [('url', '/wrong/'), ('href', 'https://example.invalid/wrong/'),
                             ('target', 'post:missing'), ('source_slug', 'outdated'), ('cid', '999')]:
            changed = copy.deepcopy(rows)
            changed[0][field] = value
            p.write_text(csv_text(changed), encoding='utf-8')
            with self.subTest(field=field), self.assertRaises(MigrationError): read_map(p, source(), self.targets)

    def test_duplicate_keys_or_absolute_comment_path_rejected(self):
        p = self.nekomi / 'src/content/posts/bad.md'
        for fm in ['commentPath: /posts/hello/', 'commentPath: https://example.invalid/post',
                   'comments: true\ncomments: false', 'commentId: 123']:
            p.write_text('---\ntitle: bad\n' + fm + '\n---\n')
            with self.subTest(fm=fm), self.assertRaises(MigrationError): scan(self.nekomi, 'https://example.invalid')

    def test_page_comment_id(self):
        p = self.nekomi / 'src/content/pages/me.md'
        p.write_text('---\ntitle: me\ncomments: true\ncommentId: old-me\n---\n')
        self.assertEqual(scan(self.nekomi, 'https://example.invalid')['page:me'].url, '/pages/old-me/')


class ConversionTests(Workspace):
    def test_normal_and_timestamp_and_privacy(self):
        docs, report = self.converted()
        self.assertEqual(docs[0]['created'], 1700000000000)
        self.assertEqual(docs[0]['updated'], docs[0]['created'])
        self.assertEqual(docs[0]['mail'], 'synthetic@example.invalid')
        self.assertEqual(docs[0]['ua'], 'Synthetic-UA/1.0')
        self.assertNotIn('ip', docs[0])
        self.assertNotIn('uid', docs[0])
        self.assertFalse(docs[0]['master'])
        self.assertEqual(validate(docs)['roots'], 1)
        self.assertNotIn('synthetic@example.invalid', json.dumps(report))

    def test_two_three_level_replies_and_different_articles(self):
        docs, _ = self.converted([comment(1), comment(2, parent=1), comment(3, parent=2), comment(4, cid=20)])
        self.assertEqual(docs[1]['pid'], docs[0]['_id'])
        self.assertEqual(docs[2]['pid'], docs[1]['_id'])
        self.assertEqual(docs[2]['rid'], docs[0]['_id'])
        self.assertNotIn('rid', docs[3])
        self.assertEqual(validate(docs), {'documents': 4, 'roots': 2, 'replies': 2, 'threads': 2})

    def test_deleted_parent_cycle_cross_article_and_no_content(self):
        rows = [comment(1, parent=99), comment(2, parent=3), comment(3, parent=2),
                comment(4, cid=20), comment(5, parent=4), comment(6, cid=999), comment(7, parent=1)]
        docs, report = self.converted(rows)
        self.assertEqual(len(docs), 1)
        codes = {i['code'] for i in report['anomalies']}
        self.assertEqual(codes, {'missing_parent', 'cycle', 'cross_article_parent', 'missing_content'})
        self.assertTrue(report['incomplete'])

    def test_unapproved_and_descendants_never_public(self):
        docs, report = self.converted([comment(1, status='waiting'), comment(2, parent=1),
                                      comment(3, status='spam'), comment(4, type='pingback'), comment(5)])
        self.assertEqual(len(docs), 1)
        self.assertEqual(report['status_counts']['waiting'], 1)
        self.assertEqual(report['status_counts']['spam'], 1)
        self.assertEqual(report['skipped_counts']['excluded_ancestor'], 1)

    def test_only_unapproved_skips_are_complete(self):
        _, report = self.converted([comment(1), comment(2, status='waiting')])
        self.assertFalse(report['incomplete'])

    def test_explicit_owner_requires_authenticated_author_id(self):
        docs, _ = self.converted([comment(1, authorId=7), comment(2, ownerId=7), comment(3, authorId=8)], owner_ids={7})
        self.assertEqual([d['master'] for d in docs], [True, False, False])

    def test_determinism_and_namespace(self):
        a = self.converted([comment(3, parent=1), comment(1), comment(2)])
        b = self.converted([comment(2), comment(1), comment(3, parent=1)])
        self.assertEqual(a, b)
        self.assertNotEqual(comment_id('one', 1), comment_id('two', 1))

    def test_unmapped_never_fabricates_url(self):
        docs, report = convert(source(), {}, namespace='test')
        self.assertEqual(docs, [])
        self.assertEqual(report['skipped_counts'], {'unmapped_content': 1})

    def test_page(self):
        docs, _ = self.converted([comment(1, cid=30)])
        self.assertEqual(docs[0]['url'], '/old/about.html')
        self.assertEqual(docs[0]['href'], 'https://example.invalid/about/')

    def test_implausible_milliseconds_input(self):
        with self.assertRaises(MigrationError): self.converted([comment(created=1700000000000)])

    def test_long_reply_chain(self):
        nodes = {i: Node(10, i - 1, 0) for i in range(1, 5001)}
        roots, errors = graph(nodes)
        self.assertFalse(errors)
        self.assertEqual(roots[5000], 1)


class SanitizerTests(unittest.TestCase):
    def test_code_image_link_emoji_and_markdown(self):
        html = render('你好😀 **粗体**\n\n```python\nprint("<script>")\n```\n\n![表情](https://example.invalid/a.png) [链接](https://example.invalid/abc)')
        for token in ['😀', '<strong>', '<pre><code', '&lt;script&gt;', '<img', '<a href=']:
            self.assertIn(token, html)

    def test_xss_payloads(self):
        for payload in ['<img src=x onerror=alert(1)>', '<a href="javascript:alert(1)">x</a>',
                        '<svg><a xlink:href="javascript:alert(1)">x</a></svg>',
                        '<iframe srcdoc="<script>alert(1)</script>"></iframe>',
                        '<p style="background:url(javascript:foo)">x</p>',
                        '<math><mtext><img src=x onerror=alert(1)></mtext></math>',
                        '<a href="jav&#x61;script:alert(1)">x</a>', '<img src="data:image/svg+xml,x">']:
            result = render(payload)
            for bad in ['onerror=', 'javascript:', '<svg', '<iframe', 'srcdoc=', 'style=', '<math', 'data:']:
                with self.subTest(payload=payload, bad=bad): self.assertNotIn(bad, result)

    def test_html_mode(self):
        self.assertIn('<pre><code>', render('<pre><code>&lt;x&gt;</code></pre>', 'html'))


class ValidationTests(Workspace):
    def test_duplicate_id(self):
        docs, _ = self.converted()
        with self.assertRaisesRegex(MigrationError, '重复'): validate(docs * 2)

    def test_bson_and_unsafe_and_bad_types(self):
        docs, _ = self.converted()
        for key, value in [('_id', {'$oid': 'a' * 24}), ('created', True), ('isSpam', True),
                           ('url', 'https://example.invalid/a'), ('comment', '<img onerror="x">'),
                           ('link', 'javascript:alert(1)'), ('rid', 'a' * 32)]:
            broken = copy.deepcopy(docs)
            broken[0][key] = value
            with self.subTest(key=key), self.assertRaises(MigrationError): validate(broken)

    def test_wrong_root_dangling_cross_thread_and_cycle(self):
        docs, _ = self.converted([comment(1), comment(2, parent=1), comment(3, parent=2), comment(4, cid=20)])
        for field, value in [('rid', docs[1]['_id']), ('pid', 'a' * 32), ('pid', docs[3]['_id']), ('pid', docs[2]['_id'])]:
            broken = copy.deepcopy(docs)
            broken[2][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(MigrationError): validate(broken)

    def test_empty_and_duplicate_json_fields(self):
        with self.assertRaises(MigrationError): validate([])
        p = self.root / 'bad.jsonl'
        p.write_text('{"_id":"a","_id":"b"}\n')
        with self.assertRaises(MigrationError): load_documents(p)


class CliTests(Workspace):
    def call(self, *args):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return main(list(args))

    def test_full_workflow_and_byte_repeatability(self):
        sql = self.root / 'fixture.sql'
        sql.write_text(sql_dump(source([comment(1), comment(2, parent=1), comment(3, parent=2), comment(4, cid=30)])), encoding='utf-8')
        out = self.root / 'output'
        common = ['--sql', str(sql), '--nekomi', str(self.nekomi), '--site', 'https://example.invalid', '--output', str(out)]
        self.assertEqual(self.call('inspect', '--sql', str(sql)), 0)
        self.assertEqual(self.call('prepare', *common), 0)
        rows = prepare_rows(source(), self.targets)
        for row in rows: row['status'] = 'ready'
        path = out / 'url-map.csv'
        write_private(path, csv_text(rows))
        command = ['convert', *common, '--map', str(path), '--namespace', 'test']
        self.assertEqual(self.call(*command), 0)
        names = ['twikoo-comments.jsonl', 'report.json', 'report.md', 'url-map.csv']
        first = {n: (out / n).read_bytes() for n in names}
        self.assertEqual(self.call(*command), 0)
        self.assertEqual(first, {n: (out / n).read_bytes() for n in names})
        self.assertEqual(self.call('validate', '--file', str(out / 'twikoo-comments.jsonl')), 0)

    def test_unreviewed_mapping_exit_two_and_report(self):
        sql = self.root / 'fixture.sql'
        sql.write_text(sql_dump(source()), encoding='utf-8')
        out = self.root / 'output'
        common = ['--sql', str(sql), '--nekomi', str(self.nekomi), '--site', 'https://example.invalid', '--output', str(out)]
        self.assertEqual(self.call('prepare', *common), 0)
        self.assertEqual(self.call('convert', *common, '--map', str(out / 'url-map.csv'), '--namespace', 'test'), 2)
        self.assertTrue(json.loads((out / 'report.json').read_text())['incomplete'])


class RegressionTests(Workspace):
    def test_upstream_identity_expectations(self):
        cases = json.loads(Path(__file__).with_name('upstream-identity.json').read_text(encoding='utf-8'))
        for case in cases:
            ident = content_id(case['file'], case['data'])
            data = case['data']
            key = data.get('commentPath') or '/posts/' + encode(data.get('commentId') or ident) + '/'
            with self.subTest(file=case['file']):
                self.assertEqual((ident, key), (case['id'], case['key']))

    def test_qualified_and_post_data_ddl_rejected(self):
        for suffix in ['INSERT INTO `db`.`typecho_comments` VALUES (1);',
                       'DROP TABLE typecho_comments;',
                       'CREATE TABLE typecho_comments (coid int);']:
            with self.subTest(suffix=suffix), self.assertRaises(MigrationError):
                self.sql(sql_dump(source()) + suffix)

    def test_empty_prefix(self):
        p = self.root / 'noprefix.sql'
        p.write_text(sql_dump(source(), ''), encoding='utf-8')
        self.assertEqual(load_sql(p, '').contents, source().contents)

    def test_control_escape_variants(self):
        dump = sql_dump(source()).replace("'你好 **Markdown** 😀\\n第二行'", "'\\0\\b\\t\\r\\n\\Z\\\\\\\"\\_\\%'" )
        text = self.sql(dump).comments[1]['text']
        self.assertEqual(text, '\0\b\t\r\n\x1a\\"\\_\\%')

    def test_disabled_and_unpublished_manual_mapping(self):
        rows = prepare_rows(source(), self.targets)
        for row in rows: row['status'] = 'ready'
        p = self.root / 'map.csv'
        p.write_text(csv_text(rows), encoding='utf-8')
        s = source()
        s.contents[10]['status'] = 'draft'
        with self.assertRaises(MigrationError): read_map(p, s, self.targets)

    def test_malformed_csv_is_safe_cli_error(self):
        p = self.root / 'map.csv'
        p.write_text(csv_text(prepare_rows(source(), self.targets)) + '10\n', encoding='utf-8')
        with self.assertRaises(MigrationError): read_map(p, source(), self.targets)


class MariaDbTests(Workspace):
    def test_mariadb_11_client_sandbox_directive(self):
        data = self.sql('/*M!999999\\- enable the sandbox mode */\n' + sql_dump(source()))
        self.assertEqual(data.comments, source().comments)
