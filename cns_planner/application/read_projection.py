"""Memoize small identities only during one synchronous read projection."""

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from functools import wraps

_active = ContextVar("read_projection_identities", default=None)


@contextmanager
def identity_projection_scope():
    token = _active.set({})
    try:
        yield
    finally:
        _active.reset(token)


def scoped_identity(method):
    @wraps(method)
    def read(self):
        cache = _active.get()
        if cache is None:
            return method(self)
        key = (id(self), method)
        if key not in cache:
            cache[key] = method(self)
        # Callers keep independent results, including nested fingerprint components.
        return deepcopy(cache[key])
    return read
