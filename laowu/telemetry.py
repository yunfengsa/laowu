"""Optional observations must never control execution or resource cleanup."""


def best_effort(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except Exception:
        # No exception bodies: providers may include sensitive request content.
        return None
