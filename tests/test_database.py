"""Database tests use synthetic in-memory fakes, never a live MongoDB."""
from __future__ import annotations

import contextlib
import copy
import gzip
import io
import json
import os
import ssl
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

try:
    from bson import ObjectId
    from pymongo import errors
    from migration import diagnostics
    from migration.backup import create_backup, require_backup, tools_uri
    from migration.importer import import_comments, journal_path, rollback_plan, target_lock
    from migration.mongodb import DatabaseError, Settings, classify, connect
    from migration.validator import database_info, input_documents, preflight, tagged, verify_stored
except ModuleNotFoundError:
    raise unittest.SkipTest('Optional database tests require requirements-db.txt')

from migrator.cli import main
from migrator.common import MigrationError, json_text, write_private
from migrator.convert import convert
from migrator.nekomi import csv_text, prepare_rows, scan
from tests.helpers import comment, nekomi, source


class Cursor(list):
    def limit(self, count):
        return Cursor(self[:count])


class Collection:
    def __init__(self):
        self.rows = {}
        self.write_calls = 0
        self.indexes = [{'key': {'_id': 1}, 'unique': True}]
        self.rules = {}
        self.fail_once = None
    def find(self, query, projection=None):
        ids = query.get('_id', {}).get('$in')
        rows = [copy.deepcopy(r) for k, r in self.rows.items() if ids is None or k in ids]
        if projection:
            rows = [{k:v for k,v in r.items() if k in projection} for r in rows]
        return Cursor(rows)
    def options(self): return self.rules
    def count_documents(self, query): return len(self.rows)
    def list_indexes(self): return self.indexes
    def insert_many(self, docs, ordered=False):
        self.write_calls += 1
        count = len(docs) if self.fail_once is None else self.fail_once
        for doc in docs[:count]:
            if doc['_id'] in self.rows:
                raise errors.DuplicateKeyError('synthetic conflict')
            self.rows[doc['_id']] = copy.deepcopy(doc)
        if self.fail_once is not None:
            self.fail_once = None
            raise errors.AutoReconnect('synthetic disconnect; never log password=SECRET')


class Database:
    def __init__(self): self.collection = Collection(); self.exists = True
    def __getitem__(self, name): return self.collection
    def list_collection_names(self): return ['comment'] if self.exists else []
    def command(self, name, **kwargs): return {'ok': 1, 'authInfo': {'authenticatedUsers': [{'user': 'synthetic'}]}}


class Setup(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = Settings.from_env(env_file=None, environ={
            'MONGODB_URI': 'mongodb://synthetic:TOPSECRET@official.example.invalid/source?authSource=admin',
            'MONGODB_DATABASE': 'source'})
        self.db = Database()
        self.nk = nekomi(self.root / 'Nekomi')
        self.targets = scan(self.nk, 'https://example.invalid')
        s = source([comment(1), comment(2, parent=1), comment(3, parent=2), comment(4, cid=30)])
        self.docs, _ = convert(s, {10:self.targets['post:hello'],30:self.targets['page:about']}, namespace='database-tests')
        self.file = self.root / 'comments.jsonl'
        write_private(self.file, ''.join(json.dumps(d, ensure_ascii=False) + '\n' for d in self.docs))
        rows = prepare_rows(s, self.targets)
        for r in rows: r['status'] = 'ready'
        self.mapping = self.root / 'map.csv'
        write_private(self.mapping, csv_text(rows))
        self.docs, self.sha, _ = input_documents(self.file, self.mapping, self.targets)
        self.directory = self.root / 'journals'

    def backup(self):
        def run(command, **kwargs):
            if '--version' in command:
                return SimpleNamespace(returncode=0, stdout=b'mongodump version: 100.13.0\n')
            config = Path(command[command.index('--config') + 1])
            self.last_config = config
            self.assertNotIn('TOPSECRET', ' '.join(command))
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
            archive = Path(next(a.split('=',1)[1] for a in command if a.startswith('--archive=')))
            with gzip.open(archive, 'wb') as stream: stream.write(b'SYNTHETIC-ARCHIVE-' * 100)
            return SimpleNamespace(returncode=0)
        with patch('migration.backup.shutil.which', return_value='/fake/mongodump'), patch('migration.backup.subprocess.run', side_effect=run):
            receipt = create_backup(self.settings, self.root / 'backups')
        self.assertFalse(self.last_config.exists())
        return Path(receipt['receipt'])

    def commit(self, **kwargs):
        return import_comments(self.db, self.settings, self.docs, input_sha=self.sha,
            backup_receipt=kwargs.pop('backup_receipt', self.backup()),
            confirm_database=kwargs.pop('confirm_database', 'source'),
            confirm_collection=kwargs.pop('confirm_collection', 'comment'),
            journal_dir=self.directory, **kwargs)


class ConfigTests(Setup):
    def test_no_credentials_and_no_secret_repr(self):
        with self.assertRaisesRegex(DatabaseError, 'credentials_missing'):
            Settings.from_env(env_file=None, environ={})
        self.assertNotIn('TOPSECRET', repr(self.settings))
        self.assertNotIn('TOPSECRET', json.dumps(self.settings.public()))
    def test_default_test_is_unconfirmed_and_overrides(self):
        s = Settings.from_env(env_file=None, environ={'MONGODB_URI':'mongodb://a.example.invalid/'})
        self.assertEqual(s.database, 'test')
        self.assertEqual(s.database_source, 'driver_default_test_unconfirmed')
        s = Settings.from_env(database='migration_test', env_file=None, environ={'MONGODB_URI':self.settings.uri})
        self.assertEqual(s.database, 'migration_test')
    def test_tls_bypass_rejected(self):
        for option in ['tlsAllowInvalidCertificates=true','tlsAllowInvalidHostnames=true','tlsInsecure=true','tls=false','ssl=false']:
            with self.subTest(option=option), self.assertRaises(DatabaseError):
                Settings.from_env(env_file=None, environ={'MONGODB_URI':'mongodb://a.invalid/db?' + option})
    def test_env_file_no_interpolation_and_process_priority(self):
        p = self.root / '.env'
        p.write_text('MONGODB_URI=mongodb://a.example.invalid/from_file\nMONGODB_DATABASE=from_file\nMONGODB_COLLECTION=comment\n')
        s = Settings.from_env(env_file=p, environ={'MONGODB_DATABASE':'from_process'})
        self.assertEqual(s.database, 'from_process')
    def test_timeout_validation(self):
        with self.assertRaises(DatabaseError):
            Settings.from_env(env_file=None, environ={'MONGODB_URI':self.settings.uri,'MONGODB_CONNECT_TIMEOUT_MS':'0'})
    def test_srv_has_no_guessed_hosts(self):
        s = Settings.from_env(env_file=None, environ={'MONGODB_URI':'mongodb+srv://official.example.invalid/db'})
        self.assertEqual(s.seeds, [('official.example.invalid',27017)])
        for uri in ['mongodb+srv://a.invalid:27017/db','mongodb+srv://a.invalid,b.invalid/db']:
            with self.assertRaises(DatabaseError): Settings.from_env(env_file=None, environ={'MONGODB_URI':uri})
    def test_connect_only_ping_and_closes_with_bounded_options(self):
        client = MagicMock()
        client.get_database.return_value = self.db
        with patch('migration.mongodb.MongoClient', return_value=client) as constructor:
            with connect(self.settings) as db: self.assertIs(db, self.db)
        kwargs = constructor.call_args.kwargs
        self.assertTrue(kwargs['tls'])
        self.assertFalse(kwargs['tlsAllowInvalidCertificates'])
        self.assertFalse(kwargs['retryWrites'])
        self.assertTrue(kwargs['retryReads'])
        self.assertEqual(kwargs['maxPoolSize'],5)
        self.assertEqual(self.db.collection.write_calls,0)
        client.close.assert_called_once()
    def test_errors_are_classified_without_credentials(self):
        cases = [(errors.OperationFailure('TOPSECRET',18),'authentication'),
                 (errors.OperationFailure('TOPSECRET',13),'authorization'),
                 (ssl.SSLCertVerificationError('TOPSECRET'),'tls_certificate'),
                 (errors.ServerSelectionTimeoutError('TOPSECRET'),'timeout'),
                 (errors.ConfigurationError('DNS NXDOMAIN TOPSECRET'),'dns'),
                 (errors.AutoReconnect('TOPSECRET'),'network')]
        for exc, expected in cases:
            with self.subTest(expected=expected):
                error=classify(exc)
                self.assertEqual(error.category, expected)
                self.assertNotIn('TOPSECRET',str(error))
    def test_context_preserves_actionable_migration_error(self):
        client=MagicMock(); client.get_database.return_value=self.db
        with patch('migration.mongodb.MongoClient', return_value=client), self.assertRaisesRegex(MigrationError,'resume batch'):
            with connect(self.settings): raise MigrationError('resume batch')
    def test_tools_database_override_preserves_auth(self):
        s=Settings.from_env(database='migration_test', env_file=None, environ={'MONGODB_URI':'mongodb://u:p@a.invalid/original'})
        value=tools_uri(s)
        self.assertIn('/migration_test?',value)
        self.assertIn('authSource=original',value)
        self.assertIn('tls=true',value)


class DiagnosticsTests(Setup):
    def test_srv_failure_stops_without_node_or_ping_retries(self):
        settings=Settings.from_env(env_file=None,environ={'MONGODB_URI':'mongodb+srv://official.example.invalid/db'})
        with patch('migration.diagnostics.dns_query',return_value=([], 'timeout')) as dns, patch('migration.diagnostics.connect') as connection:
            report=diagnostics.diagnose(settings)
        self.assertEqual(report['category'],'dns')
        self.assertEqual(dns.call_count,2)  # SRV and TXT, each once
        connection.assert_not_called()
    def test_authoritative_srv_and_a_aaaa_tcp_tls_ping(self):
        settings=Settings.from_env(env_file=None,environ={'MONGODB_URI':'mongodb+srv://cluster.example.invalid/db'})
        record=SimpleNamespace(target='node.example.invalid.',port=27017)
        responses=[([record],'ok'),([], 'no_answer'),(['192.0.2.1'],'ok'),([], 'no_answer')]
        sock=MagicMock(); context=MagicMock()
        with patch('migration.diagnostics.dns_query',side_effect=responses), patch('migration.diagnostics.socket.create_connection',return_value=sock) as tcp, patch('migration.diagnostics.ssl.create_default_context',return_value=context), patch('migration.diagnostics.connect',return_value=contextlib.nullcontext(self.db)):
            report=diagnostics.diagnose(settings)
        self.assertEqual(tcp.call_args.args[0],('node.example.invalid',27017))
        self.assertEqual(report['category'],'ok')
        self.assertFalse(report['writes_performed'])
        context.wrap_socket.assert_called_once()
    def test_untrusted_srv_target_not_contacted(self):
        settings=Settings.from_env(env_file=None,environ={'MONGODB_URI':'mongodb+srv://cluster.example.invalid/db'})
        record=SimpleNamespace(target='attacker.other.invalid.',port=27017)
        with patch('migration.diagnostics.dns_query',side_effect=[([record],'ok'),([], 'no_answer')]),patch('migration.diagnostics.socket.create_connection') as tcp:
            report=diagnostics.diagnose(settings)
        self.assertEqual(report['category'],'dns_untrusted_srv_target')
        tcp.assert_not_called()
    def test_tls_certificate_failure_never_bypasses_verification(self):
        sock=MagicMock(); context=MagicMock()
        context.wrap_socket.side_effect=ssl.SSLCertVerificationError('TOPSECRET')
        with patch('migration.diagnostics.dns_query',return_value=(['addr'],'ok')),patch('migration.diagnostics.socket.create_connection',return_value=sock),patch('migration.diagnostics.ssl.create_default_context',return_value=context),patch('migration.diagnostics.connect') as connection:
            report=diagnostics.diagnose(self.settings)
        self.assertEqual(report['nodes'][0]['category'],'tls_certificate')
        connection.assert_not_called()
        self.assertNotIn('TOPSECRET',json.dumps(report))
    def test_tcp_failure_does_not_claim_ip_whitelist_cause(self):
        with patch('migration.diagnostics.dns_query',return_value=(['addr'],'ok')),patch('migration.diagnostics.socket.create_connection',side_effect=TimeoutError('TOPSECRET')):
            report=diagnostics.diagnose(self.settings)
        self.assertEqual(report['nodes'][0]['category'],'timeout')
        self.assertIn('not_observable',report['ip_access_list'])


class BackupTests(Setup):
    def test_receipt_and_integrity_and_no_uri_in_receipt(self):
        p=self.backup()
        receipt=require_backup(p,self.settings)
        self.assertNotIn('TOPSECRET',p.read_text())
        self.assertFalse(receipt['restore_verified'])
        self.assertGreater(receipt['bytes'],32)
    def test_missing_mongodump_blocks_backup(self):
        with patch('migration.backup.shutil.which',return_value=None), self.assertRaises(MigrationError): create_backup(self.settings,self.root)
    def test_failed_mongodump_no_success_receipt(self):
        def run(command,**kwargs):
            return SimpleNamespace(returncode=0,stdout=b'mongodump version: 100.13.0') if '--version' in command else SimpleNamespace(returncode=1)
        with patch('migration.backup.shutil.which',return_value='/fake/mongodump'),patch('migration.backup.subprocess.run',side_effect=run),self.assertRaises(MigrationError):
            create_backup(self.settings,self.root/'backups')
        self.assertEqual(list((self.root/'backups').glob('*.json')),[])
    def test_corrupt_expired_or_wrong_target_receipt_blocks(self):
        p=self.backup(); original=json.loads(p.read_text())
        for key,value in [('target_id','other'),('sha256','bad'),('created_at',(datetime.now(timezone.utc)-timedelta(days=2)).isoformat())]:
            changed={**original,key:value}; write_private(p,json_text(changed))
            with self.subTest(key=key),self.assertRaises(MigrationError): require_backup(p,self.settings)
        write_private(p,json_text(original)); Path(original['archive']).write_bytes(b'corrupt')
        with self.assertRaises(MigrationError): require_backup(p,self.settings)


class ImportTests(Setup):
    def test_dry_run_and_schema_info_no_writes(self):
        report=preflight(self.db,self.settings,self.docs,input_sha=self.sha)
        self.assertFalse(report['blocked']); self.assertEqual(report['to_insert'],4)
        self.assertEqual(self.db.collection.write_calls,0)
        self.assertNotIn('TOPSECRET',json.dumps(report))
    def test_wrong_confirmation_or_no_backup_never_writes(self):
        for kwargs in [{'confirm_database':'other'},{'confirm_collection':'comments'}, {'backup_receipt':self.root/'missing'}]:
            with self.subTest(kwargs=kwargs),self.assertRaises(MigrationError): self.commit(**kwargs)
        self.assertEqual(self.db.collection.write_calls,0)
    def test_batch_insert_verify_and_repeat_conflict(self):
        result=self.commit(batch_size=2)
        self.assertTrue(result['storage_verified']); self.assertFalse(result['twikoo_e2e_verified'])
        self.assertEqual(self.db.collection.write_calls,2)
        self.assertEqual(len(self.db.collection.rows),4)
        journal=json.loads(journal_path(self.directory,result['batch_id']).read_text())
        self.assertEqual(journal['status'],'storage_verified')
        self.assertEqual(len(journal['acknowledged_ids']),4)
        with self.assertRaises(MigrationError): self.commit()
        self.assertEqual(self.db.collection.write_calls,2)
    def test_existing_comments_merged_without_modification(self):
        old=copy.deepcopy(self.docs[0]); old['_id']='f'*32
        self.db.collection.rows[old['_id']]=copy.deepcopy(old)
        self.commit()
        self.assertEqual(len(self.db.collection.rows),5)
        self.assertEqual(self.db.collection.rows[old['_id']],old)
    def test_connection_loss_after_partial_write_resume(self):
        self.db.collection.fail_once=1
        with self.assertRaisesRegex(MigrationError,'--resume'): self.commit(batch_size=2)
        files=list(self.directory.glob('*.migration-journal.json')); self.assertEqual(len(files),1)
        state=json.loads(files[0].read_text()); bid=state['batch_id']
        self.assertEqual(state['status'],'interrupted_or_partial')
        self.assertNotIn('TOPSECRET',files[0].read_text())
        self.assertEqual(len(self.db.collection.rows),1)
        result=self.commit(batch_size=2,resume=bid)
        self.assertEqual(result['already_present'],1); self.assertEqual(result['new_documents'],3)
        self.assertEqual(len(self.db.collection.rows),4)
        self.assertTrue(result['storage_verified'])
    def test_unknown_write_all_committed_before_disconnect(self):
        self.db.collection.fail_once=4
        with self.assertRaises(MigrationError): self.commit(batch_size=4)
        state=json.loads(next(self.directory.glob('*.migration-journal.json')).read_text())
        self.assertEqual(self.commit(resume=state['batch_id'])['new_documents'],0)
    def test_modified_resumable_document_is_conflict(self):
        self.db.collection.fail_once=1
        with self.assertRaises(MigrationError): self.commit()
        state=json.loads(next(self.directory.glob('*.migration-journal.json')).read_text())
        self.db.collection.rows[self.docs[0]['_id']]['nick']='modified'
        with self.assertRaises(MigrationError): self.commit(resume=state['batch_id'])
    def test_acknowledged_deleted_document_never_recreated(self):
        result=self.commit()
        self.db.collection.rows.pop(self.docs[0]['_id'])
        with self.assertRaisesRegex(MigrationError,'已缺失'): self.commit(resume=result['batch_id'])
        self.assertEqual(len(self.db.collection.rows),3)
    def test_changed_input_or_target_cannot_resume(self):
        result=self.commit()
        path=journal_path(self.directory,result['batch_id']); state=json.loads(path.read_text()); state['input_sha256']='changed';write_private(path,json_text(state))
        with self.assertRaises(MigrationError): self.commit(resume=result['batch_id'])
    def test_incompatible_existing_schema_and_custom_index_plan(self):
        old=copy.deepcopy(self.docs[0]);old['_id']=ObjectId()
        self.db.collection.rows[old['_id']]=old
        plan=preflight(self.db,self.settings,self.docs,input_sha=self.sha)
        self.assertTrue(plan['blocked']); self.assertEqual(self.db.collection.write_calls,0)
        self.db.collection.rows.clear()
        self.db.collection.indexes.append({'key':{'url':1},'unique':True})
        self.assertTrue(preflight(self.db,self.settings,self.docs,input_sha=self.sha)['blocked'])
    def test_validator_and_capped_collection_block(self):
        self.db.collection.rows[self.docs[0]['_id']]=self.docs[0]
        for option in [{'validator':{'$jsonSchema':{}}},{'capped':True}]:
            self.db.collection.rules=option
            self.assertTrue(database_info(self.db,self.settings)['blockers'])
    def test_mapping_rechecked_before_database_use(self):
        self.docs[0]['url']='/wrong/'
        write_private(self.file,''.join(json.dumps(d)+'\n' for d in self.docs))
        with self.assertRaises(MigrationError): input_documents(self.file,self.mapping,self.targets)
    def test_rollback_plan_is_readonly_and_excludes_foreign_ids(self):
        result=self.commit(); calls=self.db.collection.write_calls
        self.db.collection.rows[self.docs[0]['_id']].pop('_migration')
        plan=rollback_plan(self.db,self.settings,journal_path(self.directory,result['batch_id']))
        self.assertEqual(len(plan['tagged_ids_to_review']),3)
        self.assertEqual(plan['foreign_ids_never_delete'],[self.docs[0]['_id']])
        self.assertEqual(self.db.collection.write_calls,calls)
    def test_lock_rejects_concurrent_local_import(self):
        with target_lock(self.directory,self.settings),self.assertRaises(MigrationError):
            with target_lock(self.directory,self.settings): pass
    def test_batch_id_path_traversal_rejected(self):
        with self.assertRaises(MigrationError): journal_path(self.directory,'../../other')


class DatabaseCliTests(Setup):
    def test_missing_credentials_safe_exit_two(self):
        output=io.StringIO()
        with patch.dict(os.environ,{},clear=True),contextlib.redirect_stderr(output):
            code=main(['db-check','--env-file',str(self.root/'absent')])
        self.assertEqual(code,2);self.assertIn('credentials_missing',output.getvalue())
    def test_default_import_is_readonly_and_writes_preview_only(self):
        env={'MONGODB_URI':self.settings.uri,'MONGODB_DATABASE':'source'}
        command=['db-import','--file',str(self.file),'--map',str(self.mapping),'--nekomi',str(self.nk),'--site','https://example.invalid','--report',str(self.root/'preview.json')]
        with patch.dict(os.environ,env,clear=True),patch('migration.cli.connect',return_value=contextlib.nullcontext(self.db)),contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(command),0)
        self.assertEqual(self.db.collection.write_calls,0)
        self.assertTrue((self.root/'preview.json').is_file())
    def test_commit_without_target_confirmation_stops_before_connection(self):
        command=['db-import','--commit','--file',str(self.file),'--map',str(self.mapping),'--nekomi',str(self.nk),'--site','https://example.invalid']
        with patch.dict(os.environ,{'MONGODB_URI':self.settings.uri},clear=True),patch('migration.cli.connect') as connection,contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(command),2)
        connection.assert_not_called()


class SafetyRegressionTests(Setup):
    def test_new_database_info_remains_readonly(self):
        self.db.exists=False
        report=database_info(self.db,self.settings)
        self.assertFalse(report['collection_exists']); self.assertEqual(report['existing_comments'],0)
        self.assertEqual(self.db.collection.write_calls,0)
    def test_bson_double_mutation_prevents_resume(self):
        result=self.commit()
        self.db.collection.rows[self.docs[0]['_id']]['created']=float(self.docs[0]['created'])
        with self.assertRaises(MigrationError): self.commit(resume=result['batch_id'])
    def test_unconfirmed_default_test_write_rejected(self):
        implicit=Settings.from_env(env_file=None,environ={'MONGODB_URI':'mongodb://a.example.invalid/'})
        with self.assertRaisesRegex(MigrationError,'隐式 test'):
            import_comments(self.db,implicit,self.docs,input_sha=self.sha,backup_receipt=self.root/'missing',confirm_database='test',confirm_collection='comment',journal_dir=self.directory)
        self.assertEqual(self.db.collection.write_calls,0)
    def test_race_unique_id_never_overwrites_foreign_data(self):
        original=self.db.collection.insert_many
        foreign=copy.deepcopy(self.docs[0]);foreign['nick']='existing-foreign'
        def racing(docs,ordered=False):
            self.db.collection.rows[foreign['_id']]=copy.deepcopy(foreign)
            return original(docs,ordered)
        self.db.collection.insert_many=racing
        with self.assertRaises(MigrationError): self.commit()
        self.assertEqual(self.db.collection.rows[foreign['_id']],foreign)
    def test_backup_temporary_config_removed_when_process_times_out(self):
        seen=[]
        def run(command,**kwargs):
            if '--version' in command:return SimpleNamespace(stdout=b'mongodump version: 100.13.0',returncode=0)
            seen.append(Path(command[command.index('--config')+1]));raise subprocess.TimeoutExpired('safe',1)
        with patch('migration.backup.shutil.which',return_value='/fake/mongodump'),patch('migration.backup.subprocess.run',side_effect=run),self.assertRaises(MigrationError):
            create_backup(self.settings,self.root/'backups',timeout=1)
        self.assertFalse(seen[0].exists())
    def test_ping_without_authentication_is_not_success(self):
        class Unauthenticated(Database):
            def command(self,name,**kwargs): return {'ok':1,'authInfo':{'authenticatedUsers':[]}}
        with patch('migration.diagnostics.dns_query',return_value=(['address'],'ok')),patch('migration.diagnostics.socket.create_connection',return_value=MagicMock()),patch('migration.diagnostics.ssl.create_default_context',return_value=MagicMock()),patch('migration.diagnostics.connect',return_value=contextlib.nullcontext(Unauthenticated())):
            report=diagnostics.diagnose(self.settings)
        self.assertFalse(report['authenticated']);self.assertEqual(report['category'],'authentication_unconfigured')
    def test_tls_insecure_false_normalized_before_driver(self):
        s=Settings.from_env(env_file=None,environ={'MONGODB_URI':'mongodb://a.example.invalid/db?tlsInsecure=false'})
        self.assertNotIn('tlsInsecure',s.uri)


class CliWorkflowRegressionTests(Setup):
    def test_explicit_commit_verify_and_rollback_plan_cli(self):
        receipt=self.backup()
        env={'MONGODB_URI':self.settings.uri,'MONGODB_DATABASE':'source'}
        shared=['--file',str(self.file),'--map',str(self.mapping),'--nekomi',str(self.nk),
                '--site','https://example.invalid','--journal-dir',str(self.directory)]
        command=['db-import',*shared,'--commit','--confirm-database','source','--confirm-collection','comment','--backup-receipt',str(receipt)]
        output=io.StringIO()
        with patch.dict(os.environ,env,clear=True),patch('migration.cli.connect',side_effect=lambda _:contextlib.nullcontext(self.db)),contextlib.redirect_stdout(output):
            self.assertEqual(main(command),0)
            state=json.loads(next(self.directory.glob('*.migration-journal.json')).read_text())
            self.assertEqual(main(['db-verify',*shared,'--batch-id',state['batch_id']]),0)
            self.assertEqual(main(['db-rollback-plan','--batch-id',state['batch_id'],'--journal-dir',str(self.directory),'--report',str(self.root/'rollback.json')]),0)
        self.assertEqual(len(self.db.collection.rows),len(self.docs))
        self.assertNotIn('TOPSECRET',output.getvalue())
        self.assertEqual(json.loads((self.root/'rollback.json').read_text())['writes_performed'],False)


class FinalGuardTests(Setup):
    def test_module_rejects_invalid_input_before_any_write(self):
        self.docs[1]['rid']='f'*32
        with self.assertRaises(MigrationError): self.commit()
        self.assertEqual(self.db.collection.write_calls,0)
    def test_database_check_authentication_error_is_redacted(self):
        @contextlib.contextmanager
        def rejected(settings):
            raise errors.OperationFailure('mongodb://name:TOPSECRET@host/',18)
            yield
        with patch('migration.diagnostics.dns_query',return_value=(['address'],'ok')),patch('migration.diagnostics.socket.create_connection',return_value=MagicMock()),patch('migration.diagnostics.ssl.create_default_context',return_value=MagicMock()),patch('migration.diagnostics.connect',rejected):
            report=diagnostics.diagnose(self.settings)
        self.assertEqual(report['category'],'authentication')
        self.assertNotIn('TOPSECRET',json.dumps(report))
