"""Numerical policy, separate from boolean feature flags; no enabling side effects."""
import json
import math

DEFAULTS = {
    'bootstrap_bytes': 32768, 'response_bytes': 16384, 'page_bytes': 8192,
    'reference_ttl_seconds': 900, 'monitor_scan_seconds': 15,
    'working_seconds': 600, 'tool_seconds': 1800, 'waiting_seconds': 600,
    'read_extra_attempts': 3, 'read_delays_seconds': [2, 10, 30],
    'read_window_seconds': 300,
}


def settings(store):
    path = store.root / 'config' / 'runtime-policy.json'
    store._safe(path)
    values = json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(values, dict) or set(values) - DEFAULTS.keys():
        raise ValueError('runtime-policy.json 含未知数值策略')
    policy = DEFAULTS | values
    for key, value in policy.items():
        numbers = value if key == 'read_delays_seconds' else [value]
        if not isinstance(numbers, list) or not numbers or any(type(n) not in (int, float) or not math.isfinite(n) or n <= 0 for n in numbers):
            raise ValueError('数值策略须为有限正数，重试间隔须为正数列表')
    for key in ('bootstrap_bytes', 'response_bytes', 'page_bytes', 'read_extra_attempts'):
        if type(policy[key]) is not int:
            raise ValueError('字节预算和次数须为整数')
    if not (1024 <= policy['page_bytes'] <= policy['response_bytes'] <= policy['bootstrap_bytes'] <= 1048576):
        raise ValueError('字节预算须为 1024 ≤ page ≤ response ≤ bootstrap ≤ 1048576')
    delays = policy['read_delays_seconds']
    if not 1 <= policy['read_extra_attempts'] <= 100 or len(delays) != policy['read_extra_attempts'] or delays != sorted(delays):
        raise ValueError('重试间隔须递增且与额外次数相等（1至100）')
    if sum(delays) > policy['read_window_seconds']:
        raise ValueError('重试间隔总和不能超过故障窗口')
    return policy
