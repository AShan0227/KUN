"""Builtin executable skills.

Importing this package registers the six bundled skills with the dispatcher.
Modules are imported lazily by ``dispatcher.autoload_builtins()`` so callers
that don't want skills can skip the cost.
"""
