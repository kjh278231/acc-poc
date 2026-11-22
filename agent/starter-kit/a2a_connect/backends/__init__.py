# SPDX-License-Identifier: MIT-0
"""Backends: the one interface a partner implements to plug in their agent."""

from .base import (
    Backend,
    Turn,
    TextChunk,
    ToolCall,
    ModelCall,
    Finish,
)

# Registered backend names. Each entry is the dotted module under this package
# that exposes a ``make_backend`` factory. Resolve a name with
# :func:`make_backend_module`; it imports the module lazily, so an optional
# dependency (for example ``strands-agents`` for the ``strands`` backend) is
# only required when that backend is actually selected.
BACKEND_MODULES = {
    "echo": "a2a_connect.backends.echo",
    "bedrock": "a2a_connect.backends.bedrock",
    "strands": "a2a_connect.backends.strands",
}


def make_backend_module(name: str):
    """Return the module object for a registered backend name (lazy import)."""
    import importlib

    try:
        module_path = BACKEND_MODULES[name]
    except KeyError:
        raise ValueError(f"unknown backend {name!r}; "
                         f"known: {', '.join(sorted(BACKEND_MODULES))}") from None
    return importlib.import_module(module_path)


_CUSTOM_NAME = __import__("re").compile(r"^[a-z][a-z0-9_]*$")


def load_custom_backend(name: str) -> Backend:
    """Build a partner backend that is not one of the shipped ones.

    Put a module named ``<name>.py`` in this package (``a2a_connect/backends/``)
    with a ``make_backend()`` function that takes no arguments and returns the
    backend; read any settings it needs from environment variables. Then select
    it with ``--backend <name>`` (or ``A2A_BACKEND=<name>`` in a deployment).
    """
    import importlib

    if name in BACKEND_MODULES:
        raise ValueError(f"{name!r} is a shipped backend, not a custom one")
    if not _CUSTOM_NAME.match(name) or name == "base":
        raise ValueError(f"backend name {name!r} must be a lowercase module name "
                         f"(letters, digits, underscores), not 'base'")
    try:
        module = importlib.import_module(f"{__name__}.{name}")
    except ModuleNotFoundError as e:
        if e.name != f"{__name__}.{name}":
            raise
        raise ValueError(f"unknown backend {name!r}; shipped: "
                         f"{', '.join(sorted(BACKEND_MODULES))}; for your own, add "
                         f"a2a_connect/backends/{name}.py with make_backend()") from None
    factory = getattr(module, "make_backend", None)
    if not callable(factory):
        raise ValueError(f"a2a_connect/backends/{name}.py has no make_backend() function")
    return factory()


__all__ = ["Backend", "Turn", "TextChunk", "ToolCall", "ModelCall", "Finish",
           "BACKEND_MODULES", "make_backend_module", "load_custom_backend"]
