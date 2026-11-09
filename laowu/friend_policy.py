"""Locally approved capabilities. Model inputs never define a permission ceiling."""
import json
import os
import stat


MAX_POLICY_BYTES = 16 * 1024


# These initial templates have the same ceilings as the shipped file roles.
# Local policy must explicitly approve each capability and installed skill.
TEMPLATES = {
    'engineer': dict(skills=['engineering'], tools=['read', 'write', 'shell', 'search', 'memory', 'checkpoint'], can_write=True, personal=False),
    'researcher': dict(skills=['research'], tools=['read', 'search', 'web', 'memory', 'checkpoint'], can_write=False, personal=False),
    'confidant': dict(skills=['listening'], tools=['memory', 'checkpoint'], can_write=False, personal=True),
}


def _read_policy(root):
    # Anchor the file open to a non-symlink directory. O_NONBLOCK avoids hanging
    # on a FIFO before fstat can reject all non-regular files.
    directory = None
    try:
        directory = os.open(root / 'config', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        fd = os.open('friend-policy.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=directory)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError('friend-policy 必须是普通文件')
            if info.st_size > MAX_POLICY_BYTES:
                raise ValueError('friend-policy 文件过大')
            with os.fdopen(fd, 'rb', closefd=False) as stream:
                content = stream.read(MAX_POLICY_BYTES + 1)
            if len(content) > MAX_POLICY_BYTES:
                raise ValueError('friend-policy 文件过大')
        finally:
            os.close(fd)
        return json.loads(content.decode('utf-8'))
    except FileNotFoundError:
        return {'enabled': False, 'templates': {}}
    except OSError as exc:
        raise ValueError('friend-policy 目录或文件不可安全读取') from exc
    finally:
        if directory is not None:
            os.close(directory)


def policy(root):
    data = _read_policy(root)
    if (not isinstance(data, dict) or set(data) - {'enabled', 'templates'}
            or type(data.get('enabled')) is not bool or not isinstance(data.get('templates'), dict)):
        raise ValueError('friend-policy 配置无效')
    approved = {}
    for ident, value in data['templates'].items():
        if ident not in TEMPLATES or not isinstance(value, dict) or type(value.get('enabled')) is not bool:
            raise ValueError('friend-policy 模板无效')
        if not value['enabled']:
            continue
        base = TEMPLATES[ident]
        if set(value) != {'enabled', *base}:
            raise ValueError('批准模板必须明确列出技能、工具、写入及 personal 上限')
        for field in ('skills', 'tools'):
            if (not isinstance(value[field], list) or not all(isinstance(v, str) for v in value[field])
                    or not set(value[field]) <= set(base[field])):
                raise ValueError('批准模板超出内置能力上限')
        for field in ('can_write', 'personal'):
            if type(value[field]) is not bool or (value[field] and not base[field]):
                raise ValueError('批准模板超出内置权限上限')
        if 'write' in value['tools'] and not value['can_write']:
            raise ValueError('批准 write 需要 can_write')
        for slug in value['skills']:
            if not (root / '.agents' / 'skills' / slug / 'SKILL.md').is_file():
                raise ValueError('批准技能尚未安装：' + slug)
        approved[ident] = {k: sorted(set(value[k])) if k in ('skills', 'tools') else value[k] for k in base}
    return {'enabled': data['enabled'], 'templates': approved}


def catalog(root):
    value = policy(root)
    return {'enabled': value['enabled'], 'templates': [dict(template_id=k, **v) for k, v in sorted(value['templates'].items())]}
