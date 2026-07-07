"""Invoke tasks for mcstats — MeshCore repeater placement analysis."""

from __future__ import annotations

import asyncio

from invoke import task


def _run(coro):
    """Run an async coroutine from synchronous invoke context."""
    asyncio.run(coro)


@task(
    help={
        "config": "Path to config YAML file (default: config.yaml)",
        "verbose": "Enable debug-level meshcore logging",
        "prefix": "Only show repeaters whose name starts with this prefix",
        "exclude": "Comma-separated repeater names to exclude",
    },
)
def list_repeaters(c, config="config.yaml", verbose=False, prefix=None, exclude=None):
    """List all repeater contacts on the device."""

    async def _inner():
        from mcstats.config import load_config
        from mcstats.connection import connect
        from mcstats.display import show_repeaters
        from mcstats.scanner import get_repeaters, _filter_repeaters

        cfg = load_config(config, verbose=verbose, repeater_prefix=prefix, exclude_repeaters=exclude)
        async with connect(cfg) as mc:
            repeaters = await get_repeaters(mc)
            repeaters = _filter_repeaters(
                repeaters,
                cfg.get("repeater_prefix", ""),
                cfg.get("exclude_repeaters", ""),
            )
            show_repeaters(repeaters)

    _run(_inner())


@task(
    help={
        "roi": "Name or hex hash of the repeater of interest (overrides config)",
        "config": "Path to config YAML file (default: config.yaml)",
        "verbose": "Enable debug-level meshcore logging",
        "prefix": "Only include repeaters whose name starts with this prefix",
        "exclude": "Comma-separated repeater names to exclude",
    },
)
def discover(c, roi=None, config="config.yaml", verbose=False, prefix=None, exclude=None):
    """Discover zero-hop neighbours of the target repeater and save to cache."""

    async def _inner():
        from mcstats.config import load_config
        from mcstats.connection import connect
        from mcstats.scanner import run_discover

        cfg = load_config(
            config,
            repeater_of_interest=roi,
            verbose=verbose,
            repeater_prefix=prefix,
            exclude_repeaters=exclude,
        )

        if not cfg["repeater_of_interest"]:
            raise SystemExit(
                "No repeater of interest specified. "
                "Use --roi <name> or set repeater_of_interest in config."
            )

        async with connect(cfg) as mc:
            await run_discover(mc, cfg)

    _run(_inner())


@task(
    help={
        "roi": "Name or hex hash of the repeater of interest (overrides config)",
        "samples": "Number of SNR samples per neighbour",
        "config": "Path to config YAML file (default: config.yaml)",
        "verbose": "Enable debug-level meshcore logging",
        "prefix": "Only include repeaters whose name starts with this prefix",
        "exclude": "Comma-separated repeater names to exclude",
        "csv": "Path to write CSV output file",
    },
)
def scan(c, roi=None, samples=None, config="config.yaml", verbose=False, prefix=None, exclude=None, csv=None):
    """Full scan: discover neighbours of the target repeater, gather SNR."""

    async def _inner():
        from mcstats.config import load_config
        from mcstats.connection import connect
        from mcstats.display import show_stats
        from mcstats.scanner import run_scan

        cfg = load_config(
            config,
            repeater_of_interest=roi,
            snr_samples=int(samples) if samples is not None else None,
            verbose=verbose,
            repeater_prefix=prefix,
            exclude_repeaters=exclude,
        )

        if not cfg["repeater_of_interest"]:
            raise SystemExit(
                "No repeater of interest specified. "
                "Use --roi <name> or set repeater_of_interest in config."
            )

        async with connect(cfg) as mc:
            stats = await run_scan(mc, cfg)
            from mcstats.scanner import get_roi_display
            roi_name, roi_h = await get_roi_display(mc, cfg["repeater_of_interest"])
            show_stats(stats, cfg["timeout_penalty_db"], roi_name, roi_h)
            if csv:
                from mcstats.csv_export import write_csv
                p = write_csv(stats, cfg["timeout_penalty_db"], csv, roi_name)
                from rich.console import Console
                Console().print(f"  CSV written → [dim]{p}[/]")

    _run(_inner())


@task(
    help={
        "roi": "Name or hex hash of the repeater of interest (overrides config)",
        "neighbour": "Comma-separated list of specific neighbour names to measure (optional)",
        "samples": "Number of SNR samples per neighbour",
        "config": "Path to config YAML file (default: config.yaml)",
        "verbose": "Enable debug-level meshcore logging",
        "prefix": "Only include repeaters whose name starts with this prefix",
        "exclude": "Comma-separated repeater names to exclude",
        "csv": "Path to write CSV output file",
    },
)
def measure_snr(c, roi=None, neighbour=None, samples=None, config="config.yaml", verbose=False, prefix=None, exclude=None, csv=None):
    """Measure SNR using cached neighbours. Run 'invoke discover' first."""

    async def _inner():
        from mcstats.config import load_config
        from mcstats.connection import connect
        from mcstats.display import show_stats
        from mcstats.scanner import run_measure

        cfg = load_config(
            config,
            repeater_of_interest=roi,
            snr_samples=int(samples) if samples is not None else None,
            verbose=verbose,
            repeater_prefix=prefix,
            exclude_repeaters=exclude,
        )

        if not cfg["repeater_of_interest"]:
            raise SystemExit(
                "No repeater of interest specified. "
                "Use --roi <name> or set repeater_of_interest in config."
            )

        nbr_names = [n.strip() for n in neighbour.split(",")] if neighbour else None

        async with connect(cfg) as mc:
            stats = await run_measure(mc, cfg, nbr_names)
            from mcstats.scanner import get_roi_display
            roi_name, roi_h = await get_roi_display(mc, cfg["repeater_of_interest"])
            show_stats(stats, cfg["timeout_penalty_db"], roi_name, roi_h)
            if csv:
                from mcstats.csv_export import write_csv
                p = write_csv(stats, cfg["timeout_penalty_db"], csv, roi_name)
                from rich.console import Console
                Console().print(f"  CSV written → [dim]{p}[/]")

    _run(_inner())
