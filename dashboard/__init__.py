"""Read-only terminal dashboard for the BTC 5-min Polymarket bot.

Import surface:
    TerminalState  - the observed-state store
    probe.install  - attach read-only wrappers + capture stdout
    layout.build   - pure frame builder
    make_renderer  - TTY or plain-log renderer

Nothing in this package makes a network call, holds a lock the bot needs,
or changes a trading decision.
"""
from .layout import build, snapshot
from .controls import Keys
from .renderer import PlainRenderer, Renderer, make_renderer
from .state import TerminalState
from .theme import glyphs

__all__ = [
    "TerminalState", "build", "snapshot", "Renderer", "PlainRenderer",
    "make_renderer", "glyphs", "Keys",
]
