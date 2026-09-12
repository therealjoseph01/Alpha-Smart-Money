from __future__ import annotations

from asm.config import Mode, settings
from asm.execution.base import Executor
from asm.execution.live import LiveExecutor, ShadowExecutor
from asm.execution.paper import PaperExecutor


def get_executor(mode: Mode | None = None) -> Executor:
    """PRD 8 - the mode is runtime config, never a branch inside the gates."""
    mode = mode or settings.mode
    match mode:
        case Mode.LIVE:
            return LiveExecutor()
        case Mode.SHADOW:
            return ShadowExecutor()
        case _:
            return PaperExecutor()
