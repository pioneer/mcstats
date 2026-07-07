from __future__ import annotations

from rich.console import Console
from rich.table import Table

from .scanner import NeighbourStats, SnrSample, _contact_hash

console = Console()


def _fmt_sample(s: SnrSample) -> str:
    if s.timed_out:
        return "[red]TOUT[/]"
    if s.value is None:
        return "[yellow]?[/]"
    return f"{s.value:+.1f}"


def _fmt_avg(val: float | None) -> str:
    if val is None:
        return "—"
    return f"{val:+.1f}"


def _color_avg(val: float | None) -> str:
    """Wrap the average in a colour based on rough signal quality."""
    if val is None:
        return "—"
    txt = f"{val:+.1f}"
    if val >= 5:
        return f"[green]{txt}[/]"
    if val >= -5:
        return f"[yellow]{txt}[/]"
    return f"[red]{txt}[/]"


def show_stats(stats: list[NeighbourStats], penalty: float, roi_name: str = "", roi_hash: str = "") -> None:
    """Render a rich table showing per-attempt and average SNR."""
    if not stats:
        console.print("[yellow]No stats to display.[/]")
        return

    max_samples = max(
        max(len(s.out_snr_samples) for s in stats),
        max(len(s.in_snr_samples) for s in stats),
    )

    if roi_name:
        label = f"{roi_name} ({roi_hash})" if roi_hash else roi_name
        title = f"SNR Report \u2014 Target Repeater: {label}"
    else:
        title = "Repeater Neighbour SNR Report"
    table = Table(
        title=title,
        show_lines=True,
        title_style="bold",
    )
    table.add_column("Repeater", style="cyan", no_wrap=True)

    for i in range(1, max_samples + 1):
        table.add_column(f"Out #{i}", justify="right")
    table.add_column("Out Avg", justify="right", style="bold")

    for i in range(1, max_samples + 1):
        table.add_column(f"In #{i}", justify="right")
    table.add_column("In Avg", justify="right", style="bold")

    for s in stats:
        h = s.pub_key[:2] if s.pub_key else "??"
        row: list[str] = [f"{s.name} ({h})"]

        # Outbound samples
        for i in range(max_samples):
            if i < len(s.out_snr_samples):
                row.append(_fmt_sample(s.out_snr_samples[i]))
            else:
                row.append("—")
        row.append(_color_avg(s.avg_out(penalty)))

        # Inbound samples
        for i in range(max_samples):
            if i < len(s.in_snr_samples):
                row.append(_fmt_sample(s.in_snr_samples[i]))
            else:
                row.append("—")
        row.append(_color_avg(s.avg_in(penalty)))

        table.add_row(*row)

    console.print()
    console.print(table)
    console.print(f"\n  [dim]Timeout penalty applied to averages: {penalty} dB[/]")


def show_repeaters(repeaters: list[dict]) -> None:
    """Simple table listing matching repeaters."""
    table = Table(title="Repeaters", show_lines=True, title_style="bold")
    table.add_column("#", justify="right")
    table.add_column("Name", style="cyan")
    table.add_column("Path Len", justify="right")
    table.add_column("Path", style="dim")
    table.add_column("Pub Key (prefix)", style="dim")

    for idx, r in enumerate(repeaters, 1):
        path_len = r.get("out_path_len", "?")
        if path_len == -1:
            path_len_str = "flood"
        elif path_len == 0:
            path_len_str = "direct"
        else:
            path_len_str = str(path_len)

        pk = r.get("public_key", r.get("pubkey", ""))
        h = _contact_hash(r)
        table.add_row(
            str(idx),
            f"{r.get('adv_name', '?')} ({h})",
            path_len_str,
            r.get("out_path", "") or "—",
            pk[:12] + "…" if len(pk) > 12 else pk,
        )

    console.print()
    console.print(table)
