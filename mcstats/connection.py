from __future__ import annotations

import contextlib
import logging
from typing import Any

from meshcore import MeshCore
from rich.console import Console
from rich.logging import RichHandler

_console = Console(stderr=True)


def _setup_logging(verbose: bool = False) -> None:
    """Route meshcore logs through Rich so they always start on a new line."""
    mc_logger = logging.getLogger("meshcore")
    mc_logger.handlers.clear()
    mc_logger.propagate = False
    mc_logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    handler = RichHandler(
        console=_console,
        show_path=False,
        show_time=False,
        markup=True,
    )
    handler.setLevel(logging.DEBUG)
    mc_logger.addHandler(handler)


@contextlib.asynccontextmanager
async def connect(cfg: dict[str, Any]):
    """Async context manager that yields a connected MeshCore instance."""
    _setup_logging(verbose=cfg.get("verbose", False))
    mc = await MeshCore.create_serial(cfg["serial_port"], cfg["baud_rate"])
    try:
        yield mc
    finally:
        await mc.disconnect()
