"""Explicit, scope-bound storage for variables, cases, and effects."""

from contextlib import contextmanager
from contextvars import ContextVar


class RuntimeContext:
    """A single execution group's runtime state, independent of other instances."""

    def __init__(self):
        self.data = {
            "variables": {},
            "cases": {},
            "effects": {},
        }


_current_runtime_context: ContextVar[RuntimeContext | None] = ContextVar(
    "current_runtime_context", default=None
)


def get_runtime_context() -> RuntimeContext:
    """Return the active context, requiring callers to establish a scope."""
    runtime_context = _current_runtime_context.get()
    if runtime_context is None:
        raise RuntimeError("No active RuntimeContext")
    return runtime_context


@contextmanager
def bind_runtime_context(runtime_context: RuntimeContext):
    """Bind a context for this scope and restore the previous binding on exit."""
    if not isinstance(runtime_context, RuntimeContext):
        raise TypeError("runtime_context must be a RuntimeContext")
    token = _current_runtime_context.set(runtime_context)
    try:
        yield runtime_context
    finally:
        _current_runtime_context.reset(token)
