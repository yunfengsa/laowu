"""Local operator commands. No token arguments or automatic Slack writes."""
from __future__ import annotations

import importlib.metadata
import json
import os
import stat
from pathlib import Path

from .slack_state import SlackState


def execute_slack(store, args):
    state = SlackState(store)
    if args.action == 'setup':
        if args.credentials_file:
            path = Path(args.credentials_file).expanduser()
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise PermissionError('凭据来源须归当前用户所有且权限为 0600')
                with os.fdopen(fd, encoding='utf-8') as stream:
                    fd = -1
                    credentials = json.loads(stream.read(16385))
                state.save_credentials(credentials)
            finally:
                if fd >= 0:
                    os.close(fd)
        result = state.set_config({'team_id': args.team_id, 'owner_user_id': args.owner_user_id, 'enabled': False})
        return {'configured': True, 'config': result, 'manifest': str(store.root / 'config/slack-manifest.json'),
                'next': '运行 slack doctor 核实配置，安装应用并配置令牌后 slack enable'}
    if args.action == 'run':
        from .slack_supervisor import run_gateway
        run_gateway(store)
        return None
    if args.action in ('enable', 'disable'):
        return state.set_enabled(args.action == 'enable')
    if args.action == 'channels':
        if not args.channel_id:
            if args.mode or args.personal:
                raise ValueError('修改模式时须提供频道 ID')
            return state.channels()
        config = state.config()
        if not config.get('team_id'):
            raise ValueError('请先 slack setup')
        team = config['team_id']
        row = state.channel(team, args.channel_id, create=True)
        fields = {}
        if args.mode:
            fields['mode'] = args.mode
        if args.personal is not None:
            # A configuration change never directly grants personal reads.
            state.reset(team, args.channel_id)
            fields.update(private_approved=int(args.personal == 'yes'), personal=0)
        return state.update_channel(team, args.channel_id, **fields) if fields else row
    if args.action == 'reset':
        config = state.config()
        if not config.get('team_id'):
            raise ValueError('请先 slack setup')
        return state.reset(config['team_id'], args.channel_id)
    result = state.status()
    if args.action == 'doctor':
        result['dependencies'] = {}
        for name in ('slack-bolt', 'slack-sdk'):
            try:
                result['dependencies'][name] = importlib.metadata.version(name)
            except importlib.metadata.PackageNotFoundError:
                result['dependencies'][name] = 'missing: install .[slack]'
        try:
            state.credentials()
            result['credentials'] = 'configured_local_only'
        except FileNotFoundError:
            result['credentials'] = 'missing'
        except (ValueError, PermissionError):
            result['credentials'] = 'invalid_format_or_permissions'
        from .background import daemon_status
        result['background'] = daemon_status(store)
        result['native_history_backup'] = '不含 Codex 原生对话；异机使用频道摘要重建'
    return result
