"""CSV export for SNR measurement results."""
from __future__ import annotations

import csv
import pathlib
from datetime import datetime, timezone

from .scanner import NeighbourStats, SnrSample


def write_csv(
    stats: list[NeighbourStats],
    penalty: float,
    path: str | pathlib.Path,
    roi_name: str = "",
) -> pathlib.Path:
    """Write SNR stats to a CSV file.  Returns the path written."""
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)

    max_samples = 0
    if stats:
        max_samples = max(
            max(len(s.out_snr_samples) for s in stats),
            max(len(s.in_snr_samples) for s in stats),
        )

    header = ["repeater", "hash"]
    for i in range(1, max_samples + 1):
        header.append(f"out_{i}")
    header.append("out_avg")
    for i in range(1, max_samples + 1):
        header.append(f"in_{i}")
    header.append("in_avg")

    with p.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        # Metadata row
        writer.writerow([
            f"# roi={roi_name}",
            f"penalty={penalty}",
            f"timestamp={datetime.now(timezone.utc).isoformat()}",
        ])
        writer.writerow(header)
        for s in stats:
            h = s.pub_key[:2] if s.pub_key else "??"
            row: list[str] = [s.name, h]
            for i in range(max_samples):
                if i < len(s.out_snr_samples):
                    row.append(_fmt_val(s.out_snr_samples[i]))
                else:
                    row.append("")
            row.append(_fmt_avg(s.avg_out(penalty)))
            for i in range(max_samples):
                if i < len(s.in_snr_samples):
                    row.append(_fmt_val(s.in_snr_samples[i]))
                else:
                    row.append("")
            row.append(_fmt_avg(s.avg_in(penalty)))
            writer.writerow(row)

    return p


def _fmt_val(s: SnrSample) -> str:
    if s.timed_out:
        return "TOUT"
    if s.value is None:
        return ""
    return f"{s.value:.1f}"


def _fmt_avg(val: float | None) -> str:
    if val is None:
        return ""
    return f"{val:.1f}"
