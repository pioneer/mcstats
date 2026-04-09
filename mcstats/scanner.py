from __future__ import annotations

import dataclasses
import random
from typing import Any

from meshcore import EventType, MeshCore
from rich.console import Console

from mcstats.cache import load_neighbours, save_neighbours

console = Console()

CONTACT_TYPE_REPEATER = 2


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class SnrSample:
    """One SNR measurement attempt."""
    value: float | None = None  # None ⟹ timed out
    timed_out: bool = False


@dataclasses.dataclass
class NeighbourStats:
    """Aggregated stats for one zero-hop neighbour of the ROI."""
    name: str
    pub_key: str
    out_snr_samples: list[SnrSample] = dataclasses.field(default_factory=list)
    in_snr_samples: list[SnrSample] = dataclasses.field(default_factory=list)

    def _avg(self, samples: list[SnrSample], penalty: float) -> float | None:
        if not samples:
            return None
        vals = [s.value if s.value is not None else penalty for s in samples]
        return sum(vals) / len(vals)

    def avg_out(self, penalty: float) -> float | None:
        return self._avg(self.out_snr_samples, penalty)

    def avg_in(self, penalty: float) -> float | None:
        return self._avg(self.in_snr_samples, penalty)


@dataclasses.dataclass
class RoiPath:
    """Discovered path from the client device to the ROI."""
    roi_hash: str                    # hash of the ROI
    intermediate_hashes: list[str]   # hashes of hops between client and ROI
    hash_len: int = 1                # bytes per hash

    @property
    def prefix(self) -> str:
        """Comma-separated intermediate hops (empty string if direct)."""
        return ",".join(self.intermediate_hashes) if self.intermediate_hashes else ""

    def trace_to(self, target_hash: str) -> str:
        """Build trace path: [intermediates..., ROI, target, ROI, ...intermediates_reversed].

        Full round-trip so the trace packet returns via the same route.
        """
        fwd = self.intermediate_hashes + [self.roi_hash, target_hash]
        ret = [self.roi_hash] + list(reversed(self.intermediate_hashes))
        return ",".join(fwd + ret)

    def trace_roundtrip(self, target_hash: str) -> str:
        """Alias for trace_to — all traces are round-trips."""
        return self.trace_to(target_hash)

    def trace_to_roi(self) -> str:
        """Build trace path to ROI and back: [intermediates..., ROI, ...intermediates_reversed]."""
        fwd = self.intermediate_hashes + [self.roi_hash]
        ret = list(reversed(self.intermediate_hashes))
        return ",".join(fwd + ret) if ret else ",".join(fwd)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _contact_hash(contact: dict, hash_len: int = 1) -> str:
    """Derive the mesh path hash for a contact from its public key.

    MeshCore uses the first *hash_len* bytes of the public key as the
    routing hash shown in trace paths.
    """
    pk: str = contact.get("public_key", contact.get("pubkey", ""))
    return pk[: hash_len * 2]  # hex chars = 2 × bytes


def _split_path_hashes(out_path: str, hash_len: int, count: int) -> list[str]:
    """Split a concatenated hex path into individual hop hashes."""
    chars_per_hash = hash_len * 2
    return [out_path[i * chars_per_hash : (i + 1) * chars_per_hash]
            for i in range(count)]


async def _send_trace_and_wait(
    mc: MeshCore,
    path_str: str,
    timeout: float,
    verbose: bool = False,
) -> dict | None:
    """Send a trace along *path_str* and return TRACE_DATA payload or None."""
    tag = random.randint(1, 0xFFFF_FFFF)
    if verbose:
        console.print(f"    [dim]send_trace path={path_str} tag={tag}[/]")
    res = await mc.commands.send_trace(path=path_str, tag=tag)
    if verbose:
        console.print(f"    [dim]send_trace → type={res.type} payload={res.payload}[/]")
    if res.type == EventType.ERROR:
        return None
    suggested = res.payload.get("suggested_timeout", 0)
    wait = max(timeout, suggested / 1000 * 1.2) if suggested else timeout
    if verbose:
        console.print(f"    [dim]waiting {wait:.1f}s for TRACE_DATA (tag={tag}) …[/]")
    evt = await mc.wait_for_event(
        EventType.TRACE_DATA,
        attribute_filters={"tag": tag},
        timeout=wait,
    )
    if verbose:
        console.print(f"    [dim]trace evt = {evt}[/]")
    return evt.payload if evt else None


# ---------------------------------------------------------------------------
# Step 1 — Fetch & filter repeaters
# ---------------------------------------------------------------------------

async def get_repeaters(mc: MeshCore) -> list[dict]:
    """Return all contacts that are repeaters."""
    res = await mc.commands.get_contacts()
    if res.type == EventType.ERROR:
        console.print(f"[red]Failed to fetch contacts:[/] {res.payload}")
        return []
    contacts: dict[str, dict] = res.payload
    return [
        c for c in contacts.values()
        if c.get("type") == CONTACT_TYPE_REPEATER
    ]


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Step 2 — Establish a reliable path to the Repeater Of Interest (ROI)
# ---------------------------------------------------------------------------

async def establish_path_to_roi(
    mc: MeshCore,
    roi: dict,
    timeout: float,
    verbose: bool = False,
) -> RoiPath | None:
    """Establish a working path to *roi*.

    Strategy:
      1. Try a direct trace first (fast path for directly-reachable ROI).
      2. If that fails, do full path discovery (reset → flood → discover).
      3. Verify the discovered path with a trace.

    Returns a RoiPath on success, or None if unreachable.
    """
    name = roi.get("adv_name", "?")
    roi_hash_1b = _contact_hash(roi)
    console.print(f"\n[bold]Establishing path to ROI [cyan]{name}[/] (hash [dim]{roi_hash_1b}[/]) …[/]")

    # 1. Try direct trace first
    direct_path = RoiPath(roi_hash=roi_hash_1b, intermediate_hashes=[], hash_len=1)
    console.print(f"  Trying direct trace → [dim]{direct_path.trace_to_roi()}[/]")
    trace = await _send_trace_and_wait(mc, direct_path.trace_to_roi(), timeout, verbose=verbose)
    if trace:
        path_data = trace.get("path", [])
        snr_strs = [
            f"{h.get('snr'):+.1f}" if isinstance(h.get("snr"), (int, float)) else "?"
            for h in path_data
        ]
        console.print(f"  [green]ROI reachable directly.[/] SNR per hop: {' → '.join(snr_strs)}")
        return direct_path

    console.print("  [yellow]Direct trace failed — running path discovery …[/]")

    # 2. Full path discovery
    console.print("  Resetting path …")
    reset_res = await mc.commands.reset_path(roi)
    if verbose:
        console.print(f"    [dim]reset_path → type={reset_res.type} payload={reset_res.payload}[/]")

    console.print("  Sending flood advertisement …")
    advert_res = await mc.commands.send_advert(flood=True)
    if verbose:
        console.print(f"    [dim]send_advert → type={advert_res.type} payload={advert_res.payload}[/]")

    console.print("  Discovering path …")
    if verbose:
        console.print(f"    [dim]ROI contact: {roi}[/]")
    res = await mc.commands.send_path_discovery(roi)
    if verbose:
        console.print(f"    [dim]send_path_discovery → type={res.type} payload={res.payload}[/]")
    if res.type == EventType.ERROR:
        console.print(f"  [red]Path discovery failed:[/] {res.payload}")
        return None

    suggested = res.payload.get("suggested_timeout", 0)
    wait = max(timeout, suggested / 1000 * 1.2) if suggested else timeout
    if verbose:
        console.print(f"    [dim]waiting {wait:.1f}s for PATH_RESPONSE …[/]")
    path_evt = await mc.wait_for_event(EventType.PATH_RESPONSE, timeout=wait)
    if verbose:
        console.print(f"    [dim]path_evt = {path_evt}[/]")

    if not path_evt:
        console.print("  [red]No path response — ROI is unreachable.[/]")
        return None

    out_path = path_evt.payload.get("out_path", "")
    out_path_len = path_evt.payload.get("out_path_len", 0)
    out_path_hash_len = path_evt.payload.get("out_path_hash_len", 1)

    if verbose:
        console.print(f"    [dim]out_path={out_path!r} len={out_path_len} hash_len={out_path_hash_len}[/]")
        console.print(f"    [dim]full payload: {path_evt.payload}[/]")

    # Parse intermediate hops
    intermediate_hashes = _split_path_hashes(out_path, out_path_hash_len, out_path_len)
    hash_len = out_path_hash_len if out_path_len > 0 else 1
    roi_hash = _contact_hash(roi, hash_len)

    roi_path = RoiPath(
        roi_hash=roi_hash,
        intermediate_hashes=intermediate_hashes,
        hash_len=hash_len,
    )

    console.print(
        f"  Path found — [green]{out_path_len}[/] intermediate hop(s), "
        f"trace to ROI: [cyan]{roi_path.trace_to_roi()}[/]"
    )

    # Apply discovered outbound path
    if out_path:
        await mc.commands.change_contact_path(roi, out_path)

    # 3. Verify with a trace
    console.print("  Verifying with trace …")
    trace = await _send_trace_and_wait(mc, roi_path.trace_to_roi(), timeout, verbose=verbose)
    if trace:
        path_data = trace.get("path", [])
        snr_strs = [
            f"{h.get('snr'):+.1f}" if isinstance(h.get("snr"), (int, float)) else "?"
            for h in path_data
        ]
        console.print(f"  [green]Path verified.[/] SNR per hop: {' → '.join(snr_strs)}")
        return roi_path
    else:
        console.print("  [red]Trace to ROI failed — path not working.[/]")
        return None


# ---------------------------------------------------------------------------
# Step 3 — Discover zero-hop neighbours of the ROI
# ---------------------------------------------------------------------------

async def discover_neighbours(
    mc: MeshCore,
    roi_path: RoiPath,
    candidates: list[dict],
    retries: int,
    timeout: float,
    verbose: bool = False,
) -> list[dict]:
    """For each candidate, trace [intermediates..., ROI, candidate].

    A successful trace means the candidate is a direct (zero-hop) neighbour
    of the ROI — because no extra hops between ROI and candidate.
    """
    neighbours: list[dict] = []

    for cand in candidates:
        cand_hash = _contact_hash(cand, roi_path.hash_len)
        name = cand.get("adv_name", "?")
        trace_path = roi_path.trace_to(cand_hash)
        console.print(f"  Tracing [cyan]{name}[/] via ROI  path=[dim]{trace_path}[/]")

        found = False
        for attempt in range(1, retries + 1):
            if verbose and attempt > 1:
                console.print(f"    [dim]retry {attempt}/{retries} …[/]")

            trace = await _send_trace_and_wait(mc, trace_path, timeout, verbose=verbose)

            if verbose:
                console.print(f"    [dim]trace result: {trace}[/]")

            if trace is not None:
                path_data = trace.get("path", [])
                snr_info = ""
                for hop in reversed(path_data):
                    if "hash" in hop and hop.get("snr") is not None:
                        snr_info = f" SNR={hop['snr']:+.1f} dB"
                        break
                console.print(f"  [cyan]{name}[/] → [green]reachable[/]{snr_info} [dim](attempt {attempt})[/]")
                neighbours.append(cand)
                found = True
                break

        if not found:
            console.print(f"  [cyan]{name}[/] → [red]not reachable[/]")

    return neighbours


# ---------------------------------------------------------------------------
# Step 4 — Gather bidirectional SNR stats
# ---------------------------------------------------------------------------

async def gather_snr(
    mc: MeshCore,
    roi_path: RoiPath,
    neighbours: list[dict],
    samples: int,
    timeout: float,
    penalty: float,
    verbose: bool = False,
) -> list[NeighbourStats]:
    """Collect *samples* trace-based SNR readings per neighbour.

    Each trace is a full round-trip:
      [intermediates..., ROI, N, ROI, ...intermediates_reversed]

    From the trace path hops:
      • Outbound SNR (ROI → N): hop at the neighbour position
      • Inbound  SNR (N → ROI): hop at the ROI-return position
    Both are extracted from a single trace.
    """
    all_stats: list[NeighbourStats] = []
    n_intermediates = len(roi_path.intermediate_hashes)

    # In the round-trip trace result hops:
    #   [int0, int1, ..., ROI, N, ROI, ..., int1, int0, (client)]
    # Outbound SNR at N:         index = n_intermediates + 1
    # Inbound SNR at ROI return: index = n_intermediates + 2
    nbr_hop_idx = n_intermediates + 1
    roi_return_idx = n_intermediates + 2

    for nbr in neighbours:
        nbr_hash = _contact_hash(nbr, roi_path.hash_len)
        name = nbr.get("adv_name", "?")
        pk = nbr.get("public_key", nbr.get("pubkey", ""))
        stats = NeighbourStats(name=name, pub_key=pk)

        trace_path_str = roi_path.trace_to(nbr_hash)
        console.print(f"\n  Measuring [cyan]{name}[/] ({samples} samples)")
        console.print(f"    trace path: [dim]{trace_path_str}[/]")

        for i in range(1, samples + 1):
            trace = await _send_trace_and_wait(mc, trace_path_str, timeout, verbose=verbose)
            if trace:
                path_data = trace.get("path", [])
                if verbose:
                    console.print(f"    [dim]trace hops: {path_data}[/]")

                # Outbound: SNR at neighbour (receiving from ROI)
                if nbr_hop_idx < len(path_data):
                    out_val = path_data[nbr_hop_idx].get("snr")
                    stats.out_snr_samples.append(SnrSample(value=out_val))
                    out_disp = f"{out_val:+.1f}" if out_val is not None else "?"
                else:
                    stats.out_snr_samples.append(SnrSample(timed_out=True))
                    out_disp = "SHORT"

                # Inbound: SNR at ROI (receiving from neighbour)
                if roi_return_idx < len(path_data):
                    in_val = path_data[roi_return_idx].get("snr")
                    stats.in_snr_samples.append(SnrSample(value=in_val))
                    in_disp = f"{in_val:+.1f}" if in_val is not None else "?"
                else:
                    stats.in_snr_samples.append(SnrSample(timed_out=True))
                    in_disp = "SHORT"
            else:
                stats.out_snr_samples.append(SnrSample(timed_out=True))
                stats.in_snr_samples.append(SnrSample(timed_out=True))
                out_disp = "TIMEOUT"
                in_disp = "TIMEOUT"

            console.print(f"    sample {i}: out={out_disp} dB  in={in_disp} dB")

        all_stats.append(stats)

    return all_stats


# ---------------------------------------------------------------------------
# Orchestrators
# ---------------------------------------------------------------------------

async def run_discover(mc: MeshCore, cfg: dict[str, Any]) -> list[dict]:
    """Discover neighbours of the ROI and save them to the cache."""
    roi_name: str = cfg["repeater_of_interest"]
    retries: int = cfg["flood_retries"]
    timeout: float = cfg["trace_timeout"]
    cache_dir: str = cfg.get("cache_dir", ".cache")
    verbose: bool = cfg.get("verbose", False)

    # 1. Fetch repeaters
    console.print("\n[bold]Fetching repeaters …[/]")
    repeaters = await get_repeaters(mc)
    if not repeaters:
        console.print("[red]No repeaters found.[/]")
        return []
    console.print(f"  Found [green]{len(repeaters)}[/] repeater(s)")

    # Identify ROI
    roi = next((r for r in repeaters if r.get("adv_name") == roi_name), None)
    if roi is None:
        console.print(f"[red]ROI '{roi_name}' not found in contacts.[/]")
        return []

    # 2. Establish path to ROI
    roi_path = await establish_path_to_roi(mc, roi, timeout, verbose=verbose)
    if roi_path is None:
        console.print("[red]Cannot reach ROI — aborting.[/]")
        return []

    # 3. Discover zero-hop neighbours
    candidates = [r for r in repeaters if r.get("adv_name") != roi_name]
    console.print(f"\n[bold]Discovering neighbours of ROI via {len(candidates)} candidate(s) …[/]")
    neighbours = await discover_neighbours(mc, roi_path, candidates, retries, timeout, verbose=verbose)
    if not neighbours:
        console.print("[red]No zero-hop neighbours found.[/]")
        return []
    console.print(f"  [green]{len(neighbours)}[/] neighbour(s) found")

    # 4. Save to cache
    path = save_neighbours(roi_name, neighbours, cache_dir)
    console.print(f"\n  Cache saved → [dim]{path}[/]")

    return neighbours


async def run_scan(mc: MeshCore, cfg: dict[str, Any]) -> list[NeighbourStats]:
    """Full scan: discover neighbours then immediately measure SNR."""
    neighbours = await run_discover(mc, cfg)
    if not neighbours:
        return []
    return await _measure_with_neighbours(mc, cfg, neighbours)


async def run_measure(
    mc: MeshCore,
    cfg: dict[str, Any],
    neighbour_names: list[str] | None = None,
) -> list[NeighbourStats]:
    """Measure SNR using cached neighbours (or explicit list)."""
    roi_name: str = cfg["repeater_of_interest"]
    cache_dir: str = cfg.get("cache_dir", ".cache")
    verbose: bool = cfg.get("verbose", False)

    # Load from cache unless explicit names given
    if neighbour_names:
        # Resolve names → contact dicts from device
        repeaters = await get_repeaters(mc)
        neighbours = [r for r in repeaters if r.get("adv_name") in neighbour_names]
    else:
        cached = load_neighbours(roi_name, cache_dir)
        if cached is None:
            console.print(
                f"[red]No cached neighbours for '{roi_name}'.[/]\n"
                f"  Run [bold]invoke discover[/] first, or use [bold]invoke scan[/]."
            )
            return []
        console.print(f"  Loaded [green]{len(cached)}[/] neighbour(s) from cache")
        neighbours = cached

    if not neighbours:
        console.print("[red]No neighbours to measure.[/]")
        return []

    return await _measure_with_neighbours(mc, cfg, neighbours)


async def _measure_with_neighbours(
    mc: MeshCore,
    cfg: dict[str, Any],
    neighbours: list[dict],
) -> list[NeighbourStats]:
    """Establish path to ROI, then gather SNR for *neighbours*."""
    roi_name: str = cfg["repeater_of_interest"]
    timeout: float = cfg["trace_timeout"]
    samples: int = cfg["snr_samples"]
    penalty: float = cfg["timeout_penalty_db"]
    verbose: bool = cfg.get("verbose", False)

    repeaters = await get_repeaters(mc)
    roi = next((r for r in repeaters if r.get("adv_name") == roi_name), None)
    if roi is None:
        console.print(f"[red]ROI '{roi_name}' not found.[/]")
        return []

    roi_path = await establish_path_to_roi(mc, roi, timeout, verbose=verbose)
    if roi_path is None:
        console.print("[red]Cannot reach ROI.[/]")
        return []

    console.print(f"\n[bold]Measuring SNR for {len(neighbours)} neighbour(s) …[/]")
    return await gather_snr(mc, roi_path, neighbours, samples, timeout, penalty, verbose=verbose)
