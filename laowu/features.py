"""Central defaults. Operators opt in through config/features.json after review."""
import json

DEFAULTS = {'mcp_budget': False, 'task_acceptance': False, 'monitoring': False,
            'read_retries': False, 'metrics': False}


def settings(store):
    path = store.root / 'config' / 'features.json'
    store._safe(path)
    values = json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(values, dict) or set(values) - DEFAULTS.keys() or any(type(v) is not bool for v in values.values()):
        raise ValueError('features.json 仅允许已知布尔开关')
    return DEFAULTS | values


def enabled(store, name):
    return settings(store)[name]
