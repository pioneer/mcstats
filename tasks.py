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
        "config": "Path to config YAML file (default: config.yaml)",
        "verbose": "Enable debug-level meshcore logging",
        "prefix": "Only consider repeaters whose name starts with this prefix as hops",
        "exclude": "Comma-separated repeater names to exclude as hops",
        "via": "One or more ordered corridors of known waypoints for findpath. Waypoints in a corridor are comma-separated in route order; separate multiple corridors with ';', e.g. '55,b0,e5;55,b0,c0'. findpath tests each corridor + its subsequences, then discovers any unknown tail hops beyond the corridor's far end to reach the ROI",
        "tail": "Comma-separated repeaters to try when discovering the unknown tail beyond the corridor (findpath). Each token matches by exact name, hex hash, or name prefix, e.g. 'Kyiv_Troieshchyna,14'. Empty = try the whole pool",
        "max-hops": "Maximum number of intermediate hops to search (overrides config)",
        "exhaustive": "Search all hop depths and report every working path",
        "save": "Write the best discovered path to repeater_of_interest_path in the config",
    },
)
def findpath(
    c,
    roi=None,
    config="config.yaml",
    verbose=False,
    prefix=None,
    exclude=None,
    via=None,
    tail=None,
    max_hops=None,
    exhaustive=False,
    save=False,
):
    """Thoroughly search for a working path to the target repeater."""

    async def _inner():
        from mcstats.config import load_config
        from mcstats.connection import connect
        from mcstats.display import show_path_results
        from mcstats.scanner import get_roi_display, run_findpath

        cfg = load_config(
            config,
            repeater_of_interest=roi,
            verbose=verbose,
            repeater_prefix=prefix,
            exclude_repeaters=exclude,
            path_candidates=via,
            tail_candidates=tail,
            max_path_hops=int(max_hops) if max_hops is not None else None,
        )

        if not cfg["repeater_of_interest"]:
            raise SystemExit(
                "No repeater of interest specified. "
                "Use --roi <name> or set repeater_of_interest in config."
            )

        async with connect(cfg) as mc:
            results = await run_findpath(mc, cfg, exhaustive=exhaustive)
            if not results:
                return
            roi_name, roi_h = await get_roi_display(mc, cfg["repeater_of_interest"])
            show_path_results(results, roi_name, roi_h)

            if save:
                best = results[0]
                value = "direct" if not best.roi_path.intermediate_hashes else best.roi_path.prefix
                _save_path_to_config(config, value)
                from rich.console import Console
                Console().print(
                    f"  [green]Saved[/] repeater_of_interest_path: \"{value}\" → {config}"
                )

    _run(_inner())


def _save_path_to_config(config_path: str, value: str) -> None:
    """Update repeater_of_interest_path in the YAML config, preserving comments."""
    import pathlib
    import re

    p = pathlib.Path(config_path)
    text = p.read_text(encoding="utf-8")
    new_line = f'repeater_of_interest_path: "{value}"'
    pattern = re.compile(r'^repeater_of_interest_path:.*$', re.MULTILINE)
    if pattern.search(text):
        text = pattern.sub(new_line, text)
    else:
        text = text.rstrip("\n") + f"\n{new_line}\n"
    p.write_text(text, encoding="utf-8")


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
