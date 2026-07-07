from __future__ import annotations

import pathlib
from typing import Any

import yaml

DEFAULTS: dict[str, Any] = {
    "serial_port": "COM3",
    "baud_rate": 115200,
    "repeater_of_interest": "",
    "repeater_of_interest_path": "",
    "repeater_prefix": "",
    "exclude_repeaters": "",
    "path_candidates": "",
    "tail_candidates": "",
    "snr_samples": 3,
    "timeout_penalty_db": -30,
    "flood_retries": 2,
    "trace_timeout": 15,
    "max_path_hops": 4,
    "cache_dir": ".cache",
}


def load_config(path: str | pathlib.Path = "config.yaml", **overrides: Any) -> dict[str, Any]:
    """Load YAML config, apply defaults, then apply CLI overrides (non-None only)."""
    cfg = dict(DEFAULTS)

    p = pathlib.Path(path)
    if not p.exists():
        raise SystemExit(
            f"Config file not found: {p}\n"
            f"  Copy config.yaml.example to config.yaml and edit it."
        )
    with p.open("r", encoding="utf-8") as f:
        file_cfg = yaml.safe_load(f) or {}
    cfg.update(file_cfg)

    # CLI overrides: only apply values that were explicitly provided
    for k, v in overrides.items():
        if v is not None:
            cfg[k] = v

    return cfg
