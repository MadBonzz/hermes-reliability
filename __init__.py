"""Hermes plugin entry point. Registration is lazy and performs no I/O."""


def register(ctx):
    from .src.plugin.integration import register as register_plugin

    register_plugin(ctx)
