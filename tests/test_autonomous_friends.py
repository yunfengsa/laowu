"""Autonomous roles: temporary SQLite, authenticated synthetic Slack, no models."""
import json
import shutil
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from laowu.acceptance import finish
from laowu.common import json_text, now
from laowu.context import Context
from laowu.friend_policy import TEMPLATES
from laowu.registry import Registry
from laowu.runtime import Manager, task_record
from laowu.service import Service, visible_tools
from laowu.slack_state import SlackState
from laowu.store import Store


class AutonomousFriendsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'project'
        self.root.mkdir()
        source = Path(__file__).resolve().parents[1]
        shutil.copytree(source / '.agents', self.root / '.agents')
        self.scheduler = patch.object(Manager, '_schedule', return_value=None)
        self.scheduler.start()
        self.addCleanup(self.scheduler.stop)
        self.services = []
        self.gui = self.service()
        self.state = SlackState(self.gui.store)
        self.a = self.slack('CA')
        self.b = self.slack('CB')
        self.private = self.slack('DP', personal=True)
        self.write_policy()
        (self.root / 'config/features.json').write_text(json.dumps({'mcp_budget': True, 'task_acceptance': True}))
        self.addCleanup(self.close_services)

    def service(self, **kwargs):
        result = Service(self.root, **kwargs)
        self.services.append(result)
        return result

    def close_services(self):
        for service in self.services:
            service.close()

    def slack(self, channel, personal=False):
        self.state.update_channel('TEAM', channel, personal=int(personal), eligible=1)
        service = self.service(slack_token_file=self.state.issue_binding('TEAM', channel))
        record = self.state.enqueue('TEAM', channel, '1.00', 'ordinary request')
        self.state.update_input(record['id'], status='running')
        return service

    def write_policy(self, enabled=True):
        path = self.root / 'config/friend-policy.json'
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps({'enabled': enabled, 'templates': {k: dict(enabled=True, **v) for k, v in TEMPLATES.items()}}))

    def request(self, **fields):
        return dict(template_id='researcher', name='资料核验', description='核验一般资料',
                    instructions='只做通用证据核验，按任务授权工作。', idempotency_key='create-one', **fields)

    def create(self, service=None, **fields):
        args = self.request()
        args.update(fields)
        return (service or self.a).call('friend_create', args)

    def task(self, friend, service=None):
        return (service or self.a).call('task_start', {'friend_id': friend['id'], 'request': '合成任务 CANARY_TASK'})

    def claim(self, service):
        item = service.manager._claim()
        self.assertIsNotNone(item)
        return self.service(worker_run=item[2], worker_token=item[3]), item

    def complete(self, item):
        # Fake client completion uses the same handoff validator/acceptance writer.
        from laowu.worker import handoff
        result = handoff(json_text(dict(result='synthetic completed', artifacts=[], validation=['fake client'], unfinished=[], memory_candidates=[])))
        with self.gui.store.transaction() as db:
            db.execute("UPDATE runs SET status='completed' WHERE id=?", (item[2],))
            db.execute("UPDATE tasks SET status='completed',result=? WHERE id=?", (json_text(result), item[0]['id']))
            finish(db, item[0]['id'], item[2], result)

    def test_ordinary_slack_creation_global_reuse_and_review(self):
        friend = self.create()
        self.assertTrue(friend['dynamic'])
        self.assertEqual(friend['version'], 1)
        self.assertFalse((self.root / 'friends' / friend['id']).exists())
        for service in (self.a, self.b, self.gui):
            rows = service.call('friend_list', {})
            self.assertEqual([r['id'] for r in rows], [friend['id']])
            self.assertNotIn('conversation_id', json.dumps(rows))
            self.assertNotIn('slack:TEAM:CA', json.dumps(service.call('friend_get', {'friend_id': friend['id']})))
        catalog = self.a.call('friend_list', {'include_templates': True})
        self.assertEqual({t['template_id'] for t in catalog['templates']}, set(TEMPLATES))
        task = self.task(friend)
        worker, item = self.claim(self.a)
        self.assertNotIn('CANARY_TASK', json.dumps(friend))
        self.assertNotIn('read', item[1]['tools'])
        self.assertEqual(worker.call('memory_search', {}), [])
        self.complete(item)
        self.a.call('task_review', dict(task_id=task['id'], run_id=item[2], decision='accepted', summary='已核对合成交接', evidence=['fake client completion']))
        self.assertEqual(task_record(self.gui.store, task['id'])['acceptance']['state'], 'accepted')
        self.assertEqual(self.task(friend, self.b)['friend_id'], friend['id'])
        self.assertEqual(self.task(friend, self.gui)['friend_id'], friend['id'])

    def test_persistent_normalized_idempotency_conflict_and_concurrent_create(self):
        barrier = threading.Barrier(2)
        def call(service):
            barrier.wait()
            return self.create(service)
        with ThreadPoolExecutor(2) as pool:
            result = list(pool.map(call, (self.a, self.b)))
        self.assertEqual(result[0]['id'], result[1]['id'])
        restarted = self.service()
        same = self.create(restarted, name='  资料核验  ', tools=list(reversed(TEMPLATES['researcher']['tools'])))
        self.assertEqual(same['id'], result[0]['id'])
        with self.assertRaisesRegex(ValueError, '冲突'):
            self.create(instructions='different')
        with self.gui.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM friend_versions').fetchone()[0], 1)
            columns = {r[1] for r in db.execute('PRAGMA table_info(friend_creations)')}
            self.assertEqual(columns, {'key_hash', 'request_hash', 'friend_id', 'revoked'})

    def test_cross_channel_cas_and_disabled_bootstrap(self):
        friend = self.create()
        barrier = threading.Barrier(2)
        def change(pair):
            service, name = pair
            barrier.wait()
            try:
                return service.call('friend_update', dict(friend_id=friend['id'], expected_version=1, name=name))
            except ValueError:
                return None
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(change, ((self.a, 'one'), (self.b, 'two'))))
        self.assertEqual(sum(r is not None for r in results), 1)
        off = self.b.call('friend_set_active', dict(friend_id=friend['id'], expected_version=2, active=False))
        self.assertEqual(off['version'], 3)
        self.assertFalse(self.a.call('friend_list', {})[0]['active'])
        self.assertEqual(self.a.call('friend_list', {'include_inactive': False}), [])
        self.assertFalse(self.a.call('bootstrap', {})['friends'][0]['active'])
        on = self.gui.call('friend_set_active', dict(friend_id=friend['id'], expected_version=3, active=True))
        self.assertEqual(on['version'], 4)
        self.assertTrue(on['active'])

    def test_run_freeze_and_disable_prevents_dispatch_resume_not_live_access(self):
        friend = self.create(template_id='confidant')
        task = self.task(friend, self.private)
        worker, item = self.claim(self.private)
        self.a.call('friend_update', dict(friend_id=friend['id'], expected_version=1, instructions='new general duty'))
        self.a.call('friend_set_active', dict(friend_id=friend['id'], expected_version=2, active=False))
        with self.gui.store.connect() as db:
            run = db.execute('SELECT * FROM runs WHERE id=?', (item[2],)).fetchone()
            self.assertEqual(run['friend_version'], 1)
            self.assertEqual(json.loads(run['spec_json'])['instructions'], friend['instructions'])
            self.assertEqual(json.loads(run['memory_policy_json']), {'personal': True})
        self.gui.store.memory_upsert('private', scope='personal')
        self.assertTrue(worker.call('memory_search', {'scope': 'personal'}))
        with self.assertRaisesRegex(ValueError, '停用'):
            self.task(friend)
        self.private.call('task_cancel', {'task_id': task['id']})
        with self.assertRaisesRegex(ValueError, '停用'):
            self.private.call('task_resume', {'task_id': task['id']})
        self.a.call('friend_set_active', dict(friend_id=friend['id'], expected_version=3, active=True))
        self.private.call('task_resume', {'task_id': task['id']})
        _, next_item = self.claim(self.private)
        self.assertEqual(next_item[1]['version'], 4)
        self.assertEqual(next_item[1]['instructions'], 'new general duty')

    def test_switch_off_blocks_changes_but_retains_query_run_and_replay(self):
        friend = self.create()
        self.write_policy(enabled=False)
        self.assertEqual(self.create()['id'], friend['id'])
        self.assertEqual(self.gui.call('friend_get', {'friend_id': friend['id']})['id'], friend['id'])
        self.task(friend)
        self.claim(self.a)
        for name, args in (
            ('friend_create', dict(self.request(), idempotency_key='new')),
            ('friend_update', dict(friend_id=friend['id'], expected_version=1, name='new')),
            ('friend_set_active', dict(friend_id=friend['id'], expected_version=1, active=False))):
            with self.subTest(name=name), self.assertRaises(PermissionError):
                self.a.call(name, args)
        (self.root / 'config/friend-policy.json').unlink()
        with self.assertRaises(PermissionError):
            self.create(idempotency_key='missing-policy')

    def test_ceiling_frozen_protected_roles_external_mcp_and_untrusted_metadata(self):
        friend = self.create()
        for name, args in (
            ('friend_create', dict(self.request(), id='laowu')),
            ('friend_create', dict(self.request(), personal=True)),
            ('friend_create', dict(self.request(), origin={'kind': 'native'})),
            ('friend_create', dict(self.request(), mcp_servers={'extra': {'url': 'https://example.invalid'}})),
            ('friend_update', dict(friend_id=friend['id'], expected_version=1, template_id='engineer')),
            ('friend_update', dict(friend_id=friend['id'], expected_version=1, tools=['shell'])),
            ('friend_update', dict(friend_id=friend['id'], expected_version=1, skills=['engineering'])),
            ('friend_update', dict(friend_id=friend['id'], expected_version=1, can_write=True))):
            with self.subTest(name=name, fields=list(args)), self.assertRaises((ValueError, PermissionError)):
                self.a.call(name, args)
        # File maintenance cannot promote a dynamic ID, including sync.
        spec = dict(id=friend['id'], name='overwrite', description='static', instructions='overwrite')
        with self.assertRaises(PermissionError):
            self.gui.call('friend_ensure', {'spec': spec})
        path = self.root / 'friends' / friend['id']
        path.mkdir(parents=True)
        (path / 'AGENTS.md').write_text('overwrite')
        (path / 'friend.json').write_text(json.dumps(spec))
        with self.assertRaises(PermissionError):
            self.gui.registry.sync()
        shutil.rmtree(path)
        with self.assertRaises(PermissionError):
            self.gui.registry.ensure(dict(spec, id='laowu'))
        for ident in ('engineer', 'researcher', 'confidant', 'local_custom'):
            self.gui.registry.ensure(dict(spec, id=ident))
            for method, fields in (('friend_update', {'name': 'overwrite'}), ('friend_set_active', {'active': False})):
                with self.assertRaises(PermissionError):
                    self.a.call(method, dict(friend_id=ident, expected_version=1, **fields))
        self.assertEqual(self.a.call('friend_get', {'friend_id': friend['id']})['version'], 1)

    def test_worker_management_and_reserved_identity_rejected(self):
        friend = self.create()
        self.task(friend)
        worker, item = self.claim(self.a)
        names = {'friend_create', 'friend_get', 'friend_update', 'friend_set_active', 'friend_list', 'friend_ensure'}
        self.assertFalse(names & {t[0] for t in visible_tools(worker_run=item[2])})
        for name in names:
            with self.subTest(name=name), self.assertRaises(PermissionError):
                worker.call(name, {})
        with self.gui.store.transaction() as db:
            db.execute("UPDATE runs SET friend_id='laowu' WHERE id=?", (item[2],))
        with self.assertRaises(PermissionError):
            worker.call('memory_search', {})
        with self.assertRaises(PermissionError):
            self.service(worker_run=item[2], worker_token=item[3])

    def test_create_and_update_failure_are_atomic_and_auth_rechecked(self):
        save = self.gui.registry._save_dynamic
        def fail(*args):
            save(*args)
            raise OSError('synthetic transaction failure')
        with patch.object(self.gui.registry, '_save_dynamic', fail), self.assertRaises(OSError):
            self.create(self.gui)
        with self.gui.store.connect() as db:
            for table in ('friend_registry', 'friend_versions', 'friend_management', 'friend_creations'):
                self.assertEqual(db.execute('SELECT count(*) FROM ' + table).fetchone()[0], 0)
        friend = self.create(self.gui)
        with patch.object(self.gui.registry, '_save_dynamic', fail), self.assertRaises(OSError):
            self.gui.call('friend_update', dict(friend_id=friend['id'], expected_version=1, name='bad'))
        self.assertEqual(self.gui.registry.describe(friend['id'])['version'], 1)
        with self.gui.store.transaction() as db:
            db.execute('UPDATE slack_bindings SET revoked=1 WHERE token_hash=?', (self.a.access.token_hash,))
        with self.assertRaises(PermissionError):
            self.a.registry.create(**dict(self.request(), idempotency_key='revoked'), authorize=self.a._friend_authorize)
        self.gui.close()
        with self.assertRaises(ValueError):
            self.create(self.gui, idempotency_key='closed')

    def test_engineering_still_requires_actual_isolation_probe(self):
        friend = self.create(template_id='engineer')
        with patch('laowu.slack_permissions.probe', return_value=False) as probe:
            with self.assertRaises(PermissionError):
                self.task(friend)
            probe.assert_called()
        # No task row is left after a preflight denial.
        with self.gui.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM tasks').fetchone()[0], 0)

    def test_template_must_be_approved_installed_and_cannot_raise_existing_ceiling(self):
        path = self.root / 'config/friend-policy.json'
        settings = json.loads(path.read_text())
        settings['templates']['engineer'].update(can_write=False, tools=['memory', 'checkpoint'])
        settings['templates']['researcher']['enabled'] = False
        path.write_text(json.dumps(settings))
        with self.assertRaises(PermissionError):
            self.create()
        friend = self.create(template_id='engineer')
        self.write_policy()  # A later local approval cannot raise this role's ceiling.
        with self.assertRaises(PermissionError):
            self.a.call('friend_update', dict(friend_id=friend['id'], expected_version=1, can_write=True, tools=['write']))
        with patch('laowu.registry.new_id', return_value=friend['id']), self.assertRaisesRegex(ValueError, 'ID 冲突'):
            self.create(idempotency_key='collision')
        (self.root / '.agents/skills/research/SKILL.md').unlink()
        with self.assertRaisesRegex(ValueError, '未安装'):
            self.create(idempotency_key='missing-skill')
        with self.gui.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM friend_registry').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT count(*) FROM friend_creations').fetchone()[0], 1)

    def test_deactivation_between_submission_and_claim_blocks_new_run(self):
        friend = self.create()
        task = self.task(friend)
        self.b.call('friend_set_active', dict(friend_id=friend['id'], expected_version=1, active=False))
        self.assertIsNone(self.a.manager._claim())
        self.assertEqual(task_record(self.gui.store, task['id'])['error'], 'friend_unavailable')
        with self.gui.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM runs').fetchone()[0], 0)

    def test_skill_file_and_each_ancestor_symlink_are_rejected(self):
        spec = dict(id='linked_skill', name='linked', description='synthetic',
                    instructions='general duty', skills=['research'])
        for relative in ('.agents', '.agents/skills', '.agents/skills/research',
                         '.agents/skills/research/SKILL.md'):
            with self.subTest(path=relative):
                selected = self.root / relative
                external = Path(self.temp.name) / 'external-skill'
                selected.rename(external)
                selected.symlink_to(external, target_is_directory=external.is_dir())
                try:
                    with self.assertRaisesRegex(ValueError, '符号链接'):
                        self.gui.registry.ensure(spec, write_files=False)
                    with self.assertRaisesRegex(ValueError, '符号链接'):
                        self.create(idempotency_key='linked-skill')
                finally:
                    selected.unlink()
                    external.rename(selected)
        with self.gui.store.connect() as db:
            for table in ('friend_registry', 'friend_versions', 'friend_management', 'friend_creations'):
                self.assertEqual(db.execute('SELECT count(*) FROM ' + table).fetchone()[0], 0)

    def test_static_service_config_is_private_in_api_and_full_context_paging(self):
        from laowu.policy import settings
        canary = 'SYNTHETIC_CONFIG_CANARY'
        servers = {'extra': {'url': 'https://example.invalid/?access_token=' + canary}}
        self.gui.registry.ensure(dict(id='local_extra', name='local', description='general duty',
                                      instructions='通用职责说明。' * 3000, skills=['research'],
                                      mcp_servers=servers), write_files=False)
        # Unknown future launch fields must also stay private; projections use
        # an allowlist rather than just removing today's mcp_servers field.
        with self.gui.store.transaction() as db:
            spec = self.gui.registry.get('local_extra', db=db)
            spec['launch_options'] = {'value': canary}
            db.execute('UPDATE friend_registry SET spec_json=? WHERE id=?', (json_text(spec), 'local_extra'))
            db.execute('UPDATE friend_management SET origin_json=? WHERE friend_id=?',
                       (json_text({'conversation_id': 'slack:TEAM:CA'}), 'local_extra'))
        self.assertEqual(self.gui.registry.get('local_extra')['mcp_servers'], servers)
        for service in (self.gui, self.a, self.b):
            with self.subTest(principal=service.manager.id):
                args = {'friend_id': 'local_extra'}
                value = service.call('friend_get', args)
                self.assertNotIn('mcp_servers', value)
                self.assertNotIn('launch_options', value)
                self.assertNotIn(canary, json.dumps(value))
                self.assertNotIn('slack:TEAM:CA', json.dumps(value))
                context = Context(service)
                self.assertEqual(context.object('friend_definition', 'local_extra'), value)
                shown = context.present('friend_get', args, value)
                instruction_ref = shown['data']['instructions']['ref']
                # Also force the whole-object reference so paging reconstructs
                # every public field, not just the long instructions field.
                with patch('laowu.context.settings', return_value=dict(settings(service.store), response_bytes=800)):
                    root_ref = context.present('friend_get', args, value)['data']['ref']
                for reference, expected in ((instruction_ref, value['instructions']), (root_ref, value)):
                    fragments, cursor = [], None
                    while True:
                        page = service.call('context_read', {'ref': reference, 'cursor': cursor})
                        fragments.append(page['data'])
                        cursor = page['next_cursor']
                        if not cursor:
                            break
                    self.assertGreater(len(fragments), 1)
                    encoded = ''.join(fragments)
                    self.assertEqual(json.loads(encoded), expected)
                    self.assertNotIn(canary, encoded)
                    self.assertNotIn('slack:TEAM:CA', encoded)
                    self.assertNotIn('mcp_servers', encoded)
        # Public projection does not strip the stored execution configuration.
        self.assertEqual(self.gui.registry.get('local_extra')['mcp_servers'], servers)

    def test_large_definition_and_disabled_bootstrap_page_without_source_leak(self):
        friend = self.create(instructions='通用职责说明。' * 3000)
        context = Context(self.b)
        value = self.b.call('friend_get', {'friend_id': friend['id']})
        shown = context.present('friend_get', {'friend_id': friend['id']}, value)
        reference = shown['data']['instructions']['ref']
        self.assertIn('通用职责', context.read(reference)['data'])
        self.a.call('friend_set_active', dict(friend_id=friend['id'], expected_version=1, active=False))
        with self.assertRaisesRegex(ValueError, '版本已变化'):
            context.read(reference)
        bootstrap = self.b.call('bootstrap', {})
        # Force the same persistent descriptor route used when bootstrap exceeds
        # its byte budget, without constructing dozens of duplicate profiles.
        descriptor = context.descriptor(bootstrap, 'bootstrap', {})
        self.assertEqual(context.resolve(descriptor), bootstrap)
        self.assertFalse(bootstrap['friends'][0]['active'])
        self.assertNotIn('origin_json', json.dumps(bootstrap))

    def test_dynamic_standard_roles_private_entry_have_no_personal_grant(self):
        secret = self.gui.store.memory_upsert('synthetic personal', scope='personal')
        for template in ('engineer', 'researcher'):
            friend = self.create(template_id=template, idempotency_key=template,
                                 tools=['memory', 'checkpoint'], can_write=False)
            self.task(friend, self.private)
            worker, item = self.claim(self.private)
            self.assertEqual(worker.call('memory_search', {'scope': 'personal'}), [])
            with self.assertRaises(PermissionError):
                Context(worker).object('memory', secret['id'])
            self.complete(item)

    def test_memory_search_and_context_filter_legacy_parent_ids_identically(self):
        friend = self.create(template_id='confidant')
        self.task(friend)
        worker, _ = self.claim(self.a)
        parent = self.gui.store.memory_upsert('legacy general task', scope='tasks')
        child = self.gui.store.memory_upsert('shared derived record', scope='shared', derived_from=[parent['id']])
        value = worker.call('memory_search', {'scope': 'shared'})[0]
        self.assertEqual(value['derived_from'], [])
        self.assertEqual(Context(worker).object('memory', child['id']), value)

    def test_dynamic_personal_intersection_search_paging_and_revocation(self):
        friend = self.create(template_id='confidant')
        private = self.gui.store.memory_upsert('合成私密文字' * 6000, scope='personal')
        other = self.task(friend, self.b)
        self.gui.store.memory_upsert('other task', scope='task:' + other['id'])
        task = self.task(friend, self.private)
        worker, item = self.claim(self.private)
        self.gui.store.memory_upsert('own task', scope='task:' + task['id'])
        scopes = {r['scope'] for r in worker.call('memory_search', {})}
        self.assertEqual(scopes, {'personal', 'task:' + task['id']})
        context = Context(worker)
        value = worker.call('memory_search', {'scope': 'personal'})
        shown = context.present('memory_search', {'scope': 'personal'}, value)
        def ref(node):
            if isinstance(node, dict):
                if 'ref' in node:
                    return node['ref']
                for item in node.values():
                    found = ref(item)
                    if found:
                        return found
            if isinstance(node, list):
                for item in node:
                    found = ref(item)
                    if found:
                        return found
        reference = ref(shown)
        self.assertIsNotNone(reference)
        first = worker.call('context_read', {'ref': reference})
        self.assertTrue(first['next_cursor'])
        fragments = [first['data']]
        cursor = first['next_cursor']
        while cursor:
            page = worker.call('context_read', {'ref': reference, 'cursor': cursor})
            fragments.append(page['data'])
            cursor = page['next_cursor']
        self.assertIn('合成私密文字', json.dumps(json.loads(''.join(fragments)), ensure_ascii=False))
        with self.assertRaises(PermissionError):
            context.object('memory', self.gui.store.memory_search(scope='task:' + other['id'])[0]['id'])
        self.state.update_channel('TEAM', 'DP', generation=1)
        for name, args in (('memory_search', {'scope': 'personal'}), ('context_read', {'ref': reference, 'cursor': first['next_cursor']})):
            with self.assertRaises(PermissionError):
                worker.call(name, args)
        self.complete(item)
        self.task(friend)
        shared_worker, shared_item = self.claim(self.a)
        self.assertEqual(shared_worker.call('memory_search', {'scope': 'personal'}), [])
        with self.assertRaises(PermissionError):
            Context(shared_worker).object('memory', private['id'])
        self.complete(shared_item)
        standard = self.create(template_id='researcher', idempotency_key='standard')
        self.task(standard, self.gui)
        native, native_item = self.claim(self.gui)
        self.assertEqual(native.call('memory_search', {'scope': 'personal'}), [])
        with self.assertRaises(PermissionError):
            Context(native).object('memory', private['id'])
        self.complete(native_item)
        self.task(friend, self.gui)
        native, _ = self.claim(self.gui)
        own = self.gui.store.memory_upsert('own role', scope='friend:' + friend['id'])
        self.assertIn(own['id'], {r['id'] for r in native.call('memory_search', {})})
        self.assertTrue(native.call('memory_search', {'scope': 'personal'}))

    def test_forget_scrubs_current_history_runs_and_old_backup_not_new_roles(self):
        friend = self.create(self.gui, instructions='FORGET_CANARY general instructions')
        self.task(friend, self.gui)
        _, item = self.claim(self.gui)
        self.complete(item)
        self.gui.call('friend_update', dict(friend_id=friend['id'], expected_version=1, instructions='FORGET_CANARY second version'))
        fact = self.gui.store.memory_upsert('FORGET_CANARY')
        old = Path(self.temp.name) / 'old.zip'
        shutil.copyfile(self.gui.store.backup()['path'], old)
        self.close_services()
        self.gui.store.memory_forget(fact['id'])
        erased = self.gui.registry.describe(friend['id'])
        self.assertFalse(erased['active'])
        self.assertTrue(erased['needs_definition'])
        with self.gui.store.connect() as db:
            for table in ('friend_registry', 'friend_versions', 'runs'):
                self.assertNotIn('FORGET_CANARY', str([tuple(r) for r in db.execute('SELECT spec_json FROM ' + table)]))
        live = self.service()
        with self.assertRaisesRegex(ValueError, '遗忘'):
            self.create(live, instructions='FORGET_CANARY general instructions')
        with self.assertRaisesRegex(ValueError, '重新定义'):
            live.call('friend_set_active', dict(friend_id=friend['id'], expected_version=erased['version'], active=True))
        rewritten = live.call('friend_update', dict(friend_id=friend['id'], expected_version=erased['version'], name='重新定义', description='通用资料', instructions='新的通用职责'))
        self.assertFalse(rewritten['active'])
        live.call('friend_set_active', dict(friend_id=friend['id'], expected_version=rewritten['version'], active=True))
        with self.assertRaisesRegex(ValueError, '遗忘'):
            self.create(live)
        fresh = self.create(live, idempotency_key='fresh', instructions='FRESH_GENERAL_DUTY')
        live.close()
        new = Path(self.temp.name) / 'new.zip'
        shutil.copyfile(self.gui.store.backup()['path'], new)
        self.gui.store.restore(old)
        self.assertTrue(self.gui.registry.describe(friend['id'])['needs_definition'])
        self.assertEqual(self.gui.registry.describe(friend['id'])['instructions'], '')
        for _ in range(2):
            self.gui.store.restore(new)
            self.assertEqual(self.gui.registry.describe(fresh['id'])['instructions'], 'FRESH_GENERAL_DUTY')
            self.assertTrue(self.gui.registry.describe(fresh['id'])['active'])
            Store(self.root)  # Startup ledger replay must preserve post-forget work.

    def test_old_layout_migrates_with_backup_static_roles_and_future_rejected(self):
        for ident in TEMPLATES:
            self.gui.registry.ensure(dict(id=ident, name=ident, description='static', instructions='static'))
        self.close_services()
        with self.gui.store.transaction() as db:
            db.execute('DROP TABLE friend_creations')
            db.execute('DROP TABLE friend_management')
            db.execute("UPDATE memory_meta SET value='1' WHERE key='phase1_layout_revision'")
        before = set(self.gui.store.backups.glob('before_migration_*.sqlite3'))
        migrated = Store(self.root)
        backups = set(migrated.backups.glob('before_migration_*.sqlite3')) - before
        self.assertEqual(len(backups), 1)
        with sqlite3.connect(str(next(iter(backups)))) as db:
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='friend_management'").fetchone())
        registry = Registry(migrated)
        for role in registry.list():
            self.assertFalse(role['dynamic'])
            self.assertFalse(role['editable'])
        with migrated.connect() as db:
            self.assertEqual(registry.memory_policy(db, 'confidant'), {'personal': True})
            self.assertEqual(registry.memory_policy(db, 'engineer'), {'personal': False})
        Store(self.root)
        self.assertEqual(set(migrated.backups.glob('before_migration_*.sqlite3')) - before, backups)
        from laowu.migrations import LAYOUT_REVISION
        with migrated.transaction() as db:
            db.execute("UPDATE memory_meta SET value=? WHERE key='phase1_layout_revision'", (str(LAYOUT_REVISION + 1),))
        with self.assertRaisesRegex(ValueError, '布局'):
            Store(self.root)
        with migrated.transaction() as db:
            db.execute("UPDATE memory_meta SET value=? WHERE key='phase1_layout_revision'", (str(LAYOUT_REVISION),))

    def test_restore_predating_creation_preserves_revoked_key_without_role_text(self):
        old = Path(self.temp.name) / 'before-creation.zip'
        shutil.copyfile(self.gui.store.backup()['path'], old)
        friend = self.create(self.gui, instructions='ERASED_ROLE_TEXT')
        fact = self.gui.store.memory_upsert('ERASED_ROLE_TEXT')
        self.close_services()
        self.gui.store.memory_forget(fact['id'])
        live = self.service()
        version = live.registry.describe(friend['id'])['version']
        live.call('friend_update', dict(friend_id=friend['id'], expected_version=version,
                                       name='new general role', description='new general role', instructions='NEWER_TEXT_NOT_IN_OLD_BACKUP'))
        live.close()
        for _ in range(2):
            self.gui.store.restore(old)
            erased = self.gui.registry.describe(friend['id'])
            self.assertTrue(erased['needs_definition'])
            self.assertFalse(erased['active'])
            self.assertEqual(erased['instructions'], '')
            live = self.service()
            with self.assertRaisesRegex(ValueError, '遗忘'):
                self.create(live, instructions='ERASED_ROLE_TEXT')
            live.close()


if __name__ == '__main__':
    unittest.main()
