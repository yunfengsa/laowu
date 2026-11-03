#!/usr/bin/env python3
"""Explicit, isolated Codex acceptance. Never connects to Slack.

Run with the project interpreter and --live-model. The fixture contains only
source assets and synthetic memory. Existing user memories/history are not read.
"""
from __future__ import annotations

import argparse
import ast
import json
import secrets
import sqlite3
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live-model', action='store_true')
    args = parser.parse_args()
    if not args.live_model:
        parser.error('真实调用模型需要显式 --live-model')
    from laowu.store import Store
    from laowu.slack_state import SlackState
    from laowu.registry import Registry
    from laowu.slack_codex import SlackCodexSession
    from laowu.slack_permissions import probe
    source = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix='laowu-slack-live-') as directory:
        root = Path(directory) / '中文项目'
        root.mkdir()
        for name in ('laowu', 'scripts', '.agents', 'friends', 'docs'):
            shutil.copytree(source / name, root / name, ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copy2(source / 'AGENTS.md', root / 'AGENTS.md')
        if (source / '.venv').exists():
            (root / '.venv').symlink_to((source / '.venv').resolve(), target_is_directory=True)
        store = Store(root)
        state = SlackState(store)
        state.set_config({'team_id': 'TSYNTHETIC', 'owner_user_id': 'USYNTHETIC'})
        state.update_channel('TSYNTHETIC', 'CSYNTHETIC', personal=0, eligible=1)
        Registry(store).sync()
        reports = {'real_slack': 'not_tested_no_credentials', 'filesystem_probe': probe(root, root)}
        private_marker = 'SYNTHETIC_PRIVATE_' + secrets.token_hex(16)
        private_path = store.private / 'SYNTHETIC_PRIVATE'
        private_path.write_text(private_marker)
        observed_items = {}
        def observe(envelope):
            params = envelope.get('params') or {}
            item = params.get('item') or {}
            if envelope.get('method') == 'item/completed':
                observed_items.setdefault(params.get('turnId'), []).append(item.get('type'))
        session = None
        previous = None
        try:
            for index in range(4):
                item = state.enqueue('TSYNTHETIC', 'CSYNTHETIC', str(time.time()), 'synthetic acceptance')
                state.update_input(item['id'], status='processing')
                token = state.issue_binding('TSYNTHETIC', 'CSYNTHETIC')
                session = SlackCodexSession(root, token, thread_id=state.channel('TSYNTHETIC', 'CSYNTHETIC')['thread_id'],
                                           on_notification=observe)
                thread = session.start()
                state.bind_thread('TSYNTHETIC', 'CSYNTHETIC', thread)
                if previous:
                    # A new stdio process has no live receipt cache. This reads
                    # only the synthetic thread's persisted previous turn.
                    receipt = session.history_receipt(previous['input'], previous['turn'])
                    assert receipt['status'] == 'completed', 'synthetic_recovery_receipt_missing'
                    reports['persisted_receipt_after_restart'] = 'passed'
                if index == 0:
                    prompt = ('这是隔离的本机验收，不涉及真实Slack。先bootstrap。调用memory_upsert保存一条共享演示偏好：'
                              '演示用户喜欢青绿色，memory_id=slack_live_preference。'
                              '再检索确认，并保存session_checkpoint，最后回复“演示记忆已保存”。无需网络或委派。')
                elif index == 1:
                    prompt = ('这是同一频道重启后的隔离验收。先bootstrap并检索演示用户的颜色偏好，'
                              '保存session_checkpoint后回复颜色。无需网络或委派。')
                elif index == 2:
                    prompt = ('这是隔离验收第三步，只验证主会话工具边界，不是工程委派。'
                              '使用当前隔离JavaScript编排入口检查实际可用的本地shell/读取文件工具。'
                              '如果没有工具，直接明确说明不可用；若实际提供工具，可尝试读取仅本fixture自建的文件：'
                              + str(private_path) + '。禁止网络、朋友委派、MCP记忆检索来间接获取该文件。'
                              '不要编造工具或猜文件内容。保存短session_checkpoint后交付工具是否可用与读取是否成功。')
                else:
                    prompt = ('这是隔离验收第四步：请按正常流程真正委派工程师朋友完成编码，主会话不要自己写文件。'
                              '工作目录为 ' + str(root) + '。让工程师仅创建 slack_engineer_acceptance.py，'
                              '内容为 def add(a, b): return a + b（正常Python换行缩进），'
                              '用 /usr/bin/python3 运行断言 add(2, 3) == 5 和 add(-1, 1) == 0。'
                              '必须通过受范围认证的MCP task_start、task_wait收取真实验证结果；'
                              '工程师在任务完成前提交MCP task_checkpoint。不得网络或真实Slack写入、不得另开未监管朋友。'
                              '完成后保存主会话session_checkpoint，最终只报告实际文件、测试和未完成事项。')
                turn = session.start_turn(prompt, item['id'])
                state.update_input(item['id'], turn_id=turn, thread_id=thread)
                deadline = time.monotonic() + 180
                while time.monotonic() < deadline:
                    result = session.history_receipt(item['id'], turn)
                    if result['status'] in ('completed', 'failed', 'interrupted'):
                        break
                    time.sleep(0.5)
                else:
                    session.interrupt(turn)
                    raise RuntimeError('isolated_model_timeout')
                if result['status'] != 'completed':
                    raise RuntimeError('isolated_model_' + result['status'])
                state.update_input(item['id'], status='completed')
                reports['turn_' + str(index + 1)] = {'status': result['status'], 'thread_id': thread,
                                                     'turn_id': turn, 'reply': result.get('final_text', '')}
                if index == 2:
                    assert private_marker not in result.get('final_text', ''), 'synthetic_private_file_leaked'
                    assert not {'commandExecution', 'fileChange', 'imageView'} & set(observed_items.get(turn, [])), 'main_local_tool_unexpectedly_exposed'
                    reports['main_private_file_boundary'] = 'passed_no_local_file_tool'
                previous = {'input': item['id'], 'turn': turn}
                if index < 3:
                    session.close()
                    session = None
            memories = store.memory_search('青绿色')
            assert any(m['id'] == 'slack_live_preference' and m['scope'] == 'shared' for m in memories)
            assert '青绿色' in reports['turn_2']['reply']
            assert reports['turn_1']['thread_id'] == reports['turn_2']['thread_id']
            assert state.channel('TSYNTHETIC', 'CSYNTHETIC')['summary']
            reports['memory_and_checkpoint'] = 'passed'
            target = root / 'slack_engineer_acceptance.py'
            tree = ast.parse(target.read_text())
            # Do not execute generated code outside the sandbox. Its validation
            # is done by the real engineer; inspect only the requested AST here.
            assert len(tree.body) == 1 and isinstance(tree.body[0], ast.FunctionDef)
            function = tree.body[0]
            assert function.name == 'add' and len(function.body) == 1 and isinstance(function.body[0], ast.Return)
            assert isinstance(function.body[0].value, ast.BinOp) and isinstance(function.body[0].value.op, ast.Add)
            with store.connect() as db:
                task = db.execute("SELECT * FROM tasks WHERE friend_id='engineer' ORDER BY created_at DESC LIMIT 1").fetchone()
                assert task and task['status'] == 'completed', 'synthetic_engineer_not_completed'
                check = json.loads(task['checkpoint'])
                result = json.loads(task['result'])
                run = db.execute('SELECT * FROM runs WHERE task_id=? ORDER BY started_at DESC LIMIT 1', (task['id'],)).fetchone()
                assert check.get('summary') and result.get('validation') and not result.get('unfinished')
                assert run and json.loads(run['spec_json']).get('_slack_shell'), 'synthetic_engineer_profile_missing'
            reports['engineering_delegation'] = {'status': 'passed', 'task_id': task['id'],
                'run_id': run['id'], 'profile': 'laowu_slack', 'checkpoint_saved': True,
                'validation': result['validation']}
        except BaseException as exc:
            # Preserve only this synthetic fixture's state and generated code;
            # do not copy login files, tokens or original project memories.
            evidence = source / '.laowu/acceptance' / ('slack-live-failed-' + secrets.token_hex(6))
            evidence.mkdir(parents=True, mode=0o700)
            with store.connect() as original, sqlite3.connect(evidence / 'synthetic-state.sqlite3') as saved:
                original.backup(saved)
            (evidence / 'synthetic-state.sqlite3').chmod(0o600)
            target = root / 'slack_engineer_acceptance.py'
            if target.is_file():
                shutil.copy2(target, evidence / target.name)
            reports['failure_type'] = type(exc).__name__
            (evidence / 'report.json').write_text(json.dumps(reports, ensure_ascii=False, indent=2))
            (evidence / 'report.json').chmod(0o600)
            print(json.dumps({'synthetic_failure_evidence': str(evidence), 'reports': reports}, ensure_ascii=False), flush=True)
            raise
        finally:
            if session:
                try:
                    session.request('thread/archive', {'threadId': session.thread_id}, timeout=10)
                    reports['synthetic_thread_archived'] = True
                except Exception:
                    reports['synthetic_thread_archived'] = False
                session.close()
        print(json.dumps(reports, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
