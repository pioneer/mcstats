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

    @staticmethod
    def _dedup(hops: list[str]) -> list[str]:
        """Collapse consecutive duplicate hops (e.g. when a hop equals the ROI)."""
        result: list[str] = []
        for h in hops:
            if not result or result[-1] != h:
                result.append(h)
        return result

    @property
    def prefix(self) -> str:
        """Comma-separated intermediate hops (empty string if direct)."""
        return ",".join(self.intermediate_hashes) if self.intermediate_hashes else ""

    @property
    def hops_to_roi_len(self) -> int:
        """Number of hops (deduped) in the forward path up to and including the ROI."""
        return len(self._dedup(self.intermediate_hashes + [self.roi_hash]))

    def trace_to(self, target_hash: str) -> str:
        """Build trace path: [intermediates..., ROI, target, ROI, ...intermediates_reversed].

        Full round-trip so the trace packet returns via the same route.
        """
        fwd = self.intermediate_hashes + [self.roi_hash, target_hash]
        ret = [self.roi_hash] + list(reversed(self.intermediate_hashes))
        return ",".join(self._dedup(fwd + ret))

    def trace_roundtrip(self, target_hash: str) -> str:
        """Alias for trace_to — all traces are round-trips."""
        return self.trace_to(target_hash)

    def trace_to_roi(self) -> str:
        """Build trace path to ROI and back: [intermediates..., ROI, ...intermediates_reversed]."""
        fwd = self.intermediate_hashes + [self.roi_hash]
        ret = list(reversed(self.intermediate_hashes))
        return ",".join(self._dedup(fwd + ret))


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


def _filter_repeaters(repeaters: list[dict], prefix: str, exclude: str) -> list[dict]:
    """Filter repeaters by name prefix and exclusion list."""
    result = repeaters
    if prefix:
        result = [r for r in result if r.get("adv_name", "").startswith(prefix)]
    if exclude:
        excluded = {n.strip() for n in exclude.split(",") if n.strip()}
        result = [r for r in result if r.get("adv_name", "") not in excluded]
    return result


def _select_candidates(repeaters: list[dict], candidates: str) -> list[dict]:
    """Keep only repeaters matching the *candidates* allowlist.

    ``candidates`` is a comma-separated list of repeater names or 2-char hex
    hashes. An empty string means "no restriction" (all repeaters kept).
    Order follows the allowlist, and entries that match nothing are ignored.
    """
    specs = [s.strip() for s in candidates.split(",") if s.strip()]
    if not specs:
        return repeaters

    selected: list[dict] = []
    seen: set[int] = set()
    for spec in specs:
        match = _find_roi(repeaters, spec)
        if match is not None and id(match) not in seen:
            selected.append(match)
            seen.add(id(match))
    return selected


def _parse_corridors(spec: str, repeaters: list[dict]) -> list[list[str]]:
    """Parse a ``--via`` spec into one or more ordered waypoint corridors.

    Corridors are separated by ``;`` and each corridor is a comma-separated,
    ordered list of repeater names or 2-char hex hashes, e.g.::

        "55,b0,e5;55,b0,c0,e5"

    Returns a list of corridors, each a list of resolved 2-char hashes in the
    given order. Unresolvable waypoints are skipped; empty corridors dropped.
    Duplicate corridors are removed while preserving first-seen order.
    """
    corridors: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()
    for chunk in spec.split(";"):
        if not chunk.strip():
            continue
        reps = _select_candidates(repeaters, chunk)
        hashes = [_contact_hash(r) for r in reps]
        key = tuple(hashes)
        if hashes and key not in seen:
            corridors.append(hashes)
            seen.add(key)
    return corridors


def _select_tail_candidates(repeaters: list[dict], spec: str) -> list[dict]:
    """Select tail-discovery candidates by exact hash, exact name, or name prefix.

    ``spec`` is a comma-separated list of tokens. A repeater matches a token
    when its 2-char hex hash equals the token (case-insensitive) *or* its
    advertisement name starts with the token. A full name is therefore also a
    valid (exact) prefix, so the same option covers both "a direct list of
    repeaters" and "a set of name prefixes".

    An empty spec means "no narrowing" — all repeaters are returned. Order
    follows *repeaters* and duplicates are removed.
    """
    tokens = [t.strip() for t in spec.split(",") if t.strip()]
    if not tokens:
        return repeaters

    selected: list[dict] = []
    seen: set[int] = set()
    for r in repeaters:
        name = r.get("adv_name", "") or ""
        h = _contact_hash(r).lower()
        for tok in tokens:
            if (h == tok.lower() or name.startswith(tok)) and id(r) not in seen:
                selected.append(r)
                seen.add(id(r))
                break
    return selected


def _find_roi(repeaters: list[dict], roi_spec: str) -> dict | None:
    """Find a repeater by advertisement name or 2-char hex hash."""
    # Try exact name match first
    roi = next((r for r in repeaters if r.get("adv_name") == roi_spec), None)
    if roi:
        return roi
    # Try as hex hash (case-insensitive)
    spec_lower = roi_spec.strip().lower()
    for r in repeaters:
        if _contact_hash(r).lower() == spec_lower:
            return r
    return None


def _split_path_hashes(out_path: str, hash_len: int, count: int) -> list[str]:
    """Split a concatenated hex path into individual hop hashes."""
    chars_per_hash = hash_len * 2
    return [out_path[i * chars_per_hash : (i + 1) * chars_per_hash]
            for i in range(count)]


def _roi_path_from_config(roi_hash: str, manual_path: str) -> RoiPath | None:
    """Build a RoiPath from the ``repeater_of_interest_path`` config value.

    Returns None when the value is empty (meaning auto-discover).
    ``"direct"`` → direct path (no intermediates).
    ``"aa,bb"`` → use those hex hashes as intermediate hops.
    """
    value = manual_path.strip()
    if not value:
        return None
    if value.lower() == "direct":
        return RoiPath(roi_hash=roi_hash, intermediate_hashes=[], hash_len=1)
    hashes = [h.strip() for h in value.split(",") if h.strip()]
    return RoiPath(roi_hash=roi_hash, intermediate_hashes=hashes, hash_len=1)


async def _sync_firmware_path(
    mc: MeshCore,
    roi: dict,
    roi_path: RoiPath,
    verbose: bool = False,
) -> None:
    """Update the firmware's contact table so binary requests can route to the ROI."""
    path_hex = "".join(roi_path.intermediate_hashes)
    if path_hex:
        if verbose:
            console.print(f"  [dim]change_contact_path → {path_hex}[/]")
        await mc.commands.change_contact_path(roi, path_hex)
    else:
        if verbose:
            console.print("  [dim]reset_path (direct)[/]")
        await mc.commands.reset_path(roi)


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


async def get_roi_hash(mc: MeshCore, roi_spec: str) -> str:
    """Look up the 2-char hex hash for a ROI by name or hash. Returns '' if not found."""
    repeaters = await get_repeaters(mc)
    roi = _find_roi(repeaters, roi_spec)
    return _contact_hash(roi) if roi else ""


async def get_roi_display(mc: MeshCore, roi_spec: str) -> tuple[str, str]:
    """Return (display_name, hex_hash) for a ROI spec. Falls back to (roi_spec, '')."""
    repeaters = await get_repeaters(mc)
    roi = _find_roi(repeaters, roi_spec)
    if roi is None:
        return roi_spec, ""
    return roi.get("adv_name", roi_spec), _contact_hash(roi)


async def fetch_roi_neighbours(
    mc: MeshCore,
    roi: dict,
    timeout: float,
    verbose: bool = False,
) -> list[dict]:
    """Ask the ROI for its neighbour list via the binary protocol.

    Returns a list of lightweight contact dicts (pubkey prefix + SNR only)
    that can be used as trace candidates even if they are not in our
    local contact list.
    """
    name = roi.get("adv_name", "?")
    console.print(f"\n[bold]Fetching neighbour list from target repeater [cyan]{name}[/] …[/]")
    try:
        result = await mc.commands.req_neighbours_sync(roi, timeout=timeout)
    except Exception as exc:
        console.print(f"  [yellow]req_neighbours_sync failed: {exc}[/]")
        return []

    if result is None:
        console.print("  [yellow]No response from target repeater.[/]")
        return []

    neighbours = result.get("neighbours", [])
    console.print(f"  Target repeater reports [green]{result.get('neighbours_count', '?')}[/] neighbour(s), "
                  f"received [green]{len(neighbours)}[/]")

    remote_contacts: list[dict] = []
    for n in neighbours:
        pk = n.get("pubkey", "")
        snr = n.get("snr")
        secs = n.get("secs_ago")
        snr_str = f" SNR={snr:+.1f}" if isinstance(snr, (int, float)) else ""
        age_str = f" {secs}s ago" if isinstance(secs, int) else ""
        if verbose:
            console.print(f"    [dim]{pk}{snr_str}{age_str}[/]")
        remote_contacts.append({
            "public_key": pk,
            "adv_name": f"remote_{pk[:4]}",
            "type": CONTACT_TYPE_REPEATER,
            "_remote": True,
        })

    return remote_contacts


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
    other_repeaters: list[dict] | None = None,
    max_hops: int = 3,
    verbose: bool = False,
) -> RoiPath | None:
    """Establish a working path to *roi*.

    Strategy:
      1. Try a direct trace first (fast path for directly-reachable ROI).
      2. If that fails, do full path discovery (reset → flood → discover).
      3. If path discovery also fails, BFS through known repeaters up to
         *max_hops* intermediate hops.

    Returns a RoiPath on success, or None if unreachable.
    """
    name = roi.get("adv_name", "?")
    roi_hash_1b = _contact_hash(roi)
    console.print(f"\n[bold]Establishing path to target repeater [cyan]{name}[/] (hash [dim]{roi_hash_1b}[/]) …[/]")

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
        console.print(f"  [green]Target repeater reachable directly.[/] SNR per hop: {' → '.join(snr_strs)}")
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
        console.print(f"    [dim]Target repeater contact: {roi}[/]")
    res = await mc.commands.send_path_discovery(roi)
    if verbose:
        console.print(f"    [dim]send_path_discovery → type={res.type} payload={res.payload}[/]")
    if res.type == EventType.ERROR:
        console.print(f"  [red]Path discovery failed:[/] {res.payload}")
        # Fall through to BFS below
    else:
        suggested = res.payload.get("suggested_timeout", 0)
        wait = max(timeout, suggested / 1000 * 1.2) if suggested else timeout
        if verbose:
            console.print(f"    [dim]waiting {wait:.1f}s for PATH_RESPONSE …[/]")
        roi_pubkey_pre = roi.get("public_key", roi.get("pubkey", ""))[:12]
        path_evt = await mc.wait_for_event(
            EventType.PATH_RESPONSE,
            attribute_filters={"pubkey_pre": roi_pubkey_pre} if roi_pubkey_pre else None,
            timeout=wait,
        )
        if verbose:
            console.print(f"    [dim]path_evt = {path_evt}[/]")

        if path_evt:
            return await _apply_discovered_path(mc, roi, path_evt, roi_hash_1b, timeout, verbose)

        console.print("  [yellow]No path response from protocol.[/]")

    # 3. BFS: try multi-hop paths through known repeaters
    if not other_repeaters:
        console.print("  [red]Target repeater is unreachable (no other repeaters to try).[/]")
        return None

    result = await _bfs_find_path(
        mc, roi_hash_1b, other_repeaters, timeout, max_hops, verbose,
    )
    if result:
        return result

    console.print("  [red]Target repeater is unreachable.[/]")
    return None


def _format_snr_hops(path_data: list[dict]) -> str:
    return " → ".join(
        f"{h.get('snr'):+.1f}" if isinstance(h.get("snr"), (int, float)) else "?"
        for h in path_data
    )


async def _apply_discovered_path(
    mc: MeshCore,
    roi: dict,
    path_evt,
    roi_hash_1b: str,
    timeout: float,
    verbose: bool,
) -> RoiPath | None:
    """Parse a PATH_RESPONSE, apply it, and verify with a trace."""
    out_path = path_evt.payload.get("out_path", "")
    out_path_len = path_evt.payload.get("out_path_len", 0)
    out_path_hash_len = path_evt.payload.get("out_path_hash_len", 1)

    if verbose:
        console.print(f"    [dim]out_path={out_path!r} len={out_path_len} hash_len={out_path_hash_len}[/]")
        console.print(f"    [dim]full payload: {path_evt.payload}[/]")

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

    if out_path:
        await mc.commands.change_contact_path(roi, out_path)

    console.print("  Verifying with trace …")
    trace = await _send_trace_and_wait(mc, roi_path.trace_to_roi(), timeout, verbose=verbose)
    if trace:
        console.print(f"  [green]Path verified.[/] SNR per hop: {_format_snr_hops(trace.get('path', []))}")
        return roi_path
    else:
        console.print("  [red]Trace to target repeater failed — path not working.[/]")
        return None


async def _bfs_find_path(
    mc: MeshCore,
    roi_hash: str,
    repeaters: list[dict],
    timeout: float,
    max_hops: int,
    verbose: bool,
) -> RoiPath | None:
    """BFS through known repeaters to find a multi-hop path to the ROI.

    1. Quick scan to find directly-reachable repeaters.
    2. Depth 1: try each reachable repeater as sole intermediate.
    3. Depth 2+: extend with any repeater not already in the chain.

    Only directly-reachable repeaters are used as the first hop.
    """
    hash_to_name: dict[str, str] = {}
    for r in repeaters:
        h = _contact_hash(r)
        hash_to_name[h] = r.get("adv_name", "?")
    all_hashes = list(hash_to_name.keys())

    # Quick reachability scan
    console.print(f"  [bold]Scanning reachability of {len(repeaters)} repeater(s) …[/]")
    reachable: set[str] = set()
    for r in repeaters:
        h = _contact_hash(r)
        probe = RoiPath(roi_hash=h, intermediate_hashes=[], hash_len=1)
        trace = await _send_trace_and_wait(mc, probe.trace_to_roi(), timeout, verbose=verbose)
        if trace:
            reachable.add(h)
            if verbose:
                console.print(f"    [dim]{hash_to_name[h]} ({h}) — reachable[/]")
        else:
            if verbose:
                console.print(f"    [dim]{hash_to_name[h]} ({h}) — not reachable[/]")
    console.print(f"  {len(reachable)} directly-reachable repeater(s)")

    if not reachable:
        return None

    # BFS by depth
    chains: list[list[str]] = []
    for depth in range(1, max_hops + 1):
        if depth == 1:
            chains = [[h] for h in reachable]
        else:
            new_chains: list[list[str]] = []
            for chain in chains:
                for h in all_hashes:
                    if h not in chain and h != roi_hash:
                        new_chains.append(chain + [h])
            chains = new_chains

        if not chains:
            continue

        console.print(f"  Trying {len(chains)} path(s) at depth {depth} …")
        for chain in chains:
            candidate = RoiPath(
                roi_hash=roi_hash,
                intermediate_hashes=chain,
                hash_len=1,
            )
            trace_str = candidate.trace_to_roi()
            if verbose:
                path_names = " → ".join(hash_to_name.get(h, h) for h in chain)
                console.print(f"    [dim]{path_names} → target repeater: {trace_str}[/]")
            trace = await _send_trace_and_wait(mc, trace_str, timeout, verbose=verbose)
            if trace:
                path_names = " → ".join(hash_to_name.get(h, h) for h in chain)
                console.print(
                    f"  [green]Target repeater reachable via {path_names}[/] "
                    f"({depth} hop(s)). SNR: {_format_snr_hops(trace.get('path', []))}"
                )
                return candidate

    return None


# ---------------------------------------------------------------------------
# Thorough path finding (invoke findpath)
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class PathResult:
    """A verified path to the target repeater and its quality metrics."""
    roi_path: RoiPath
    min_snr: float | None                       # weakest hop SNR (None if unknown)
    avg_snr: float | None = None                # mean hop SNR (None if unknown)
    hop_names: list[str] = dataclasses.field(default_factory=list)

    @property
    def n_hops(self) -> int:
        """Number of intermediate hops (0 = direct)."""
        return len(self.roi_path.intermediate_hashes)


def _path_min_snr(payload: dict | None) -> float | None:
    """Return the weakest per-hop SNR in a trace payload, or None if unavailable."""
    if not payload:
        return None
    snrs = [
        h.get("snr")
        for h in payload.get("path", [])
        if isinstance(h.get("snr"), (int, float))
    ]
    return min(snrs) if snrs else None


def _path_avg_snr(payload: dict | None) -> float | None:
    """Return the mean per-hop SNR in a trace payload, or None if unavailable."""
    if not payload:
        return None
    snrs = [
        h.get("snr")
        for h in payload.get("path", [])
        if isinstance(h.get("snr"), (int, float))
    ]
    return sum(snrs) / len(snrs) if snrs else None


def _score_path(
    min_snr: float | None,
    avg_snr: float | None = None,
    n_hops: int = 0,
) -> tuple[float, float, int]:
    """Sort key for ranking paths by stability (lower tuple = better).

    Primary key is the weakest hop SNR (a chain is only as stable as its
    weakest link), tie-broken by mean SNR, then by hop count. Hop count is
    only a last-resort tiebreaker — paths are ranked by signal, not length.
    ``None`` SNR values are treated as worst.
    """
    lo = min_snr if min_snr is not None else float("-inf")
    avg = avg_snr if avg_snr is not None else float("-inf")
    return (-lo, -avg, n_hops)


def _path_sort_key(pr: "PathResult") -> tuple[float, float, int]:
    """Ranking key for a :class:`PathResult`."""
    return _score_path(pr.min_snr, pr.avg_snr, pr.n_hops)



def _order_candidates(hashes: list[str], snr_map: dict[str, float | None]) -> list[str]:
    """Order candidate hashes by descending known SNR (unknown SNR last)."""
    def key(h: str) -> float:
        v = snr_map.get(h)
        return v if isinstance(v, (int, float)) else float("-inf")

    return sorted(hashes, key=key, reverse=True)


def _ordered_subsequences(items: list[str], max_len: int) -> list[list[str]]:
    """All non-empty ordered subsequences of *items* with length ≤ *max_len*.

    Ordering of the original list is preserved within each subsequence, and
    longer subsequences come first (they use more of the known corridor).
    Used to expand a ``--via`` corridor into concrete paths to test.
    """
    from itertools import combinations

    result: list[list[str]] = []
    n = len(items)
    for length in range(min(n, max_len), 0, -1):
        for combo in combinations(range(n), length):
            result.append([items[i] for i in combo])
    return result


async def _trace_with_retries(
    mc: MeshCore,
    path_str: str,
    timeout: float,
    retries: int,
    verbose: bool = False,
) -> dict | None:
    """Trace *path_str* up to *retries* times; return first successful payload."""
    for attempt in range(1, max(1, retries) + 1):
        if verbose and attempt > 1:
            console.print(f"      [dim]retry {attempt}/{retries} …[/]")
        trace = await _send_trace_and_wait(mc, path_str, timeout, verbose=verbose)
        if trace:
            return trace
    return None


async def _probe_reachability(
    mc: MeshCore,
    repeaters: list[dict],
    timeout: float,
    retries: int,
    verbose: bool = False,
    prefix: list[str] | None = None,
) -> dict[str, float | None]:
    """Trace each repeater (with retries); return {hash: best_min_snr}.

    Only reachable repeaters appear in the result. The SNR value is used later
    to prioritise which repeaters to try as intermediate hops.

    If *prefix* is given, each repeater is probed *through* that fixed chain of
    hops (``prefix → repeater``) instead of directly, so reachability is judged
    from the far end of a known ``--via`` corridor rather than from the device.
    """
    prefix = prefix or []
    reachable: dict[str, float | None] = {}
    for r in repeaters:
        h = _contact_hash(r)
        name = r.get("adv_name", "?")
        probe = RoiPath(roi_hash=h, intermediate_hashes=prefix, hash_len=1)
        trace = await _trace_with_retries(
            mc, probe.trace_to_roi(), timeout, retries, verbose=verbose
        )
        if trace:
            reachable[h] = _path_min_snr(trace)
            if verbose:
                console.print(f"    [dim]{name} ({h}) — reachable[/]")
        elif verbose:
            console.print(f"    [dim]{name} ({h}) — not reachable[/]")
    return reachable


async def _bfs_find_all_paths(
    mc: MeshCore,
    roi_hash: str,
    repeaters: list[dict],
    timeout: float,
    retries: int,
    max_hops: int,
    exhaustive: bool,
    verbose: bool,
    prefix: list[str] | None = None,
    tried: set[tuple[str, ...]] | None = None,
    hash_to_name_extra: dict[str, str] | None = None,
) -> list[PathResult]:
    """Thorough BFS: collect verified paths to the ROI, ordered by quality.

    - Each trace is retried up to *retries* times.
    - Reachable candidates (ordered by SNR) seed the first discovered hop.
    - Deeper chains extend with SNR-ordered candidates.
    - Without *exhaustive*, stops after the first depth that yields any path.
    - With *exhaustive*, scans all depths up to *max_hops* and returns everything.

    If *prefix* is given (a known ``--via`` corridor), every path is built as
    ``prefix → discovered tail → target``: candidates are probed for reachability
    *through* the corridor and *max_hops* counts the discovered tail hops beyond
    it. This lets ``--via`` cross a known-good stretch cheaply and only discover
    the unknown remainder.

    *tried* is a shared set of full intermediate-chain tuples already probed; any
    already in it is skipped (updated in place).
    """
    prefix = prefix or []
    prefix_set = set(prefix)
    tried = tried if tried is not None else set()
    hash_to_name: dict[str, str] = {}
    for r in repeaters:
        hash_to_name[_contact_hash(r)] = r.get("adv_name", "?")
    for h in prefix:
        hash_to_name.setdefault(h, h)
    if hash_to_name_extra:
        hash_to_name.update(hash_to_name_extra)

    # Candidates for discovered hops: exclude the corridor itself and the ROI.
    candidates = [
        r for r in repeaters
        if _contact_hash(r) not in prefix_set and _contact_hash(r) != roi_hash
    ]

    if prefix:
        prefix_route = " → ".join(hash_to_name.get(h, h) for h in prefix)
        console.print(
            f"  [bold]Discovering tail beyond {prefix_route} — probing "
            f"{len(candidates)} candidate(s) reachable through the corridor …[/]"
        )
    else:
        console.print(f"  [bold]Scanning reachability of {len(candidates)} repeater(s) …[/]")

    snr_map = await _probe_reachability(
        mc, candidates, timeout, retries, verbose=verbose, prefix=prefix,
    )
    where = "beyond the corridor" if prefix else "directly"
    console.print(f"  {len(snr_map)} repeater(s) reachable {where}")
    if not snr_map:
        return []

    cand_hashes = [_contact_hash(r) for r in candidates]
    ordered_all = _order_candidates(cand_hashes, snr_map)
    reachable_ordered = _order_candidates(list(snr_map.keys()), snr_map)

    results: list[PathResult] = []
    chains: list[list[str]] = []
    for depth in range(1, max_hops + 1):
        if depth == 1:
            chains = [[h] for h in reachable_ordered]
        else:
            new_chains: list[list[str]] = []
            for chain in chains:
                for h in ordered_all:
                    if h not in chain and h != roi_hash:
                        new_chains.append(chain + [h])
            chains = new_chains

        # Build full paths (prefix + discovered tail) and skip any already tried.
        pending: list[list[str]] = []
        for chain in chains:
            full = prefix + chain
            if tuple(full) in tried:
                continue
            pending.append(chain)

        if not pending:
            continue

        tier = f"tail-depth {depth}" if prefix else f"depth {depth}"
        console.print(f"  Trying {len(pending)} path(s) at {tier} …")
        for chain in pending:
            full = prefix + chain
            tried.add(tuple(full))
            candidate = RoiPath(roi_hash=roi_hash, intermediate_hashes=full, hash_len=1)
            names = [hash_to_name.get(h, h) for h in full]
            if verbose:
                console.print(f"    [dim]{' → '.join(names)} → target: {candidate.trace_to_roi()}[/]")
            trace = await _trace_with_retries(
                mc, candidate.trace_to_roi(), timeout, retries, verbose=verbose
            )
            if trace:
                min_snr = _path_min_snr(trace)
                avg_snr = _path_avg_snr(trace)
                snr_disp = f"{min_snr:+.1f}" if min_snr is not None else "?"
                avg_disp = f"{avg_snr:+.1f}" if avg_snr is not None else "?"
                console.print(
                    f"  [green]Reachable via {' → '.join(names)}[/] "
                    f"({len(full)} hop(s), min SNR {snr_disp}, avg SNR {avg_disp})"
                )
                results.append(
                    PathResult(roi_path=candidate, min_snr=min_snr, avg_snr=avg_snr, hop_names=names)
                )

        if results and not exhaustive:
            break

    results.sort(key=_path_sort_key)
    return results


async def _corridor_find_paths(
    mc: MeshCore,
    roi_hash: str,
    waypoints: list[str],
    hash_to_name: dict[str, str],
    timeout: float,
    retries: int,
    max_hops: int,
    verbose: bool,
    tried: set[tuple[str, ...]] | None = None,
) -> list[PathResult]:
    """Test an *ordered* ``--via`` corridor without probing the whole mesh.

    Given an ordered list of waypoint hashes (e.g. ``[55, b0, e5]``), trace the
    full corridor ``me → 55 → b0 → e5 → target`` and every ordered subsequence
    of it (``55→b0``, ``b0→e5``, ``55→e5``, …). All are verified with retries
    and scored by SNR stability, so a shorter subpath that avoids a weak hop can
    win over the full corridor.

    *tried* is a shared set of chains already probed by previous corridors; any
    subsequence in it is skipped so overlapping corridors never re-trace the
    same (sub)path. It is updated in place with every chain attempted here.

    This leverages the operator's knowledge of the route order to test only a
    handful of concrete paths instead of scanning every repeater.
    """
    if tried is None:
        tried = set()
    subseqs = _ordered_subsequences(waypoints, max_hops)
    corridor_names = " → ".join(hash_to_name.get(h, h) for h in waypoints)
    console.print(
        f"  [bold]Testing ordered corridor[/] {corridor_names} → target "
        f"([cyan]{len(subseqs)}[/] ordered path(s), longest first) …"
    )

    results: list[PathResult] = []
    for chain in subseqs:
        key = tuple(chain)
        if key in tried:
            if verbose:
                route = " → ".join(hash_to_name.get(h, h) for h in chain) or "direct"
                console.print(f"    [dim]{route} → target: already probed, skipping[/]")
            continue
        tried.add(key)

        candidate = RoiPath(roi_hash=roi_hash, intermediate_hashes=chain, hash_len=1)
        names = [hash_to_name.get(h, h) for h in chain]
        route = " → ".join(names) if names else "direct"
        if verbose:
            console.print(f"    [dim]{route} → target: {candidate.trace_to_roi()}[/]")
        trace = await _trace_with_retries(
            mc, candidate.trace_to_roi(), timeout, retries, verbose=verbose
        )
        if trace:
            min_snr = _path_min_snr(trace)
            avg_snr = _path_avg_snr(trace)
            snr_disp = f"{min_snr:+.1f}" if min_snr is not None else "?"
            avg_disp = f"{avg_snr:+.1f}" if avg_snr is not None else "?"
            console.print(
                f"  [green]Reachable via {route}[/] "
                f"({len(chain)} hop(s), min SNR {snr_disp}, avg SNR {avg_disp})"
            )
            results.append(
                PathResult(roi_path=candidate, min_snr=min_snr, avg_snr=avg_snr, hop_names=names)
            )

    results.sort(key=_path_sort_key)
    return results


async def find_path_thorough(
    mc: MeshCore,
    roi: dict,
    cfg: dict[str, Any],
    others: list[dict],
    timeout: float,
    verbose: bool,
    exhaustive: bool = False,
    corridors: list[list[str]] | None = None,
) -> list[PathResult]:
    """Extensively search for working paths to *roi*.

    Order of attempts:
      1. Direct trace (with retries).
      2. Protocol path discovery (reset → flood → discover → verify).
      3. Thorough BFS through known repeaters (SNR-ordered, retried).

    If *corridors* is given (one or more ordered ``--via`` routes), the
    mesh-wide search is skipped. For each corridor findpath first tests the
    corridor and its ordered subsequences directly (see
    :func:`_corridor_find_paths`), then — anchored at the corridor's far end —
    discovers any unknown tail hops needed to reach the ROI (see
    :func:`_bfs_find_all_paths` with a ``prefix``). All results are merged and
    ranked by stability.

    Returns a list of verified :class:`PathResult`, ranked most-stable first.
    Empty if the target repeater is unreachable.
    """
    retries: int = cfg.get("flood_retries", 1)
    max_hops: int = cfg.get("max_path_hops", 4)
    route_list = corridors or []
    name = roi.get("adv_name", "?")
    roi_hash = _contact_hash(roi)
    console.print(
        f"\n[bold]Finding path to target repeater [cyan]{name}[/] "
        f"(hash [dim]{roi_hash}[/]) …[/]"
    )

    # Ordered corridor mode — use the known route(s), then discover the tail.
    if route_list:
        hash_to_name = {_contact_hash(r): r.get("adv_name", "?") for r in others}
        for corridor in route_list:
            for h in corridor:
                hash_to_name.setdefault(h, h)
        plural = "s" if len(route_list) > 1 else ""
        console.print(
            f"  [dim]{len(route_list)} corridor{plural} set — testing each route + "
            f"subsequences, then discovering unknown tail hops beyond the corridor. "
            f"Skipping direct/protocol discovery and mesh-wide scan.[/]"
        )
        results: list[PathResult] = []
        tried: set[tuple[str, ...]] = set()

        def _merge(found: list[PathResult]) -> None:
            for pr in found:
                if not any(existing.roi_path.intermediate_hashes == pr.roi_path.intermediate_hashes
                           for existing in results):
                    results.append(pr)

        # Phase 1 — test each corridor and its ordered subsequences directly.
        # This is cheap and catches the case where the ROI hangs directly off a
        # known waypoint.
        for idx, corridor in enumerate(route_list, 1):
            if len(route_list) > 1:
                route_disp = " → ".join(hash_to_name.get(h, h) for h in corridor)
                console.print(f"  [bold]Corridor {idx}/{len(route_list)}:[/] {route_disp}")
            _merge(await _corridor_find_paths(
                mc, roi_hash, corridor, hash_to_name, timeout, retries,
                max_hops, verbose, tried=tried,
            ))

        # Phase 2 — if the known routes didn't reach the ROI (or --exhaustive),
        # discover the unknown tail anchored at each corridor's far end, so we
        # don't re-scan the known crossing. Candidates for the tail are scoped
        # by the `tail_candidates` spec (names / hashes / name-prefixes); if it
        # is empty the full pool is used.
        if exhaustive or not results:
            tail_spec: str = cfg.get("tail_candidates", "")
            tail_pool = _select_tail_candidates(others, tail_spec)
            if tail_spec:
                console.print(
                    f"  [dim]Tail discovery scoped to [green]{len(tail_pool)}[/] "
                    f"repeater(s) matching: {tail_spec}[/]"
                )
                if not tail_pool:
                    console.print(
                        "  [yellow]No repeaters matched the tail-candidate spec — "
                        "nothing to discover beyond the corridor.[/]"
                    )
            for idx, corridor in enumerate(route_list, 1):
                route_disp = " → ".join(hash_to_name.get(h, h) for h in corridor)
                console.print(
                    f"  [bold]Discovering tail beyond corridor {idx}/{len(route_list)}:[/] "
                    f"{route_disp} → ?"
                )
                _merge(await _bfs_find_all_paths(
                    mc, roi_hash, tail_pool, timeout, retries, max_hops,
                    exhaustive, verbose,
                    prefix=corridor, tried=tried, hash_to_name_extra=hash_to_name,
                ))

        results.sort(key=_path_sort_key)
        return results

    results: list[PathResult] = []

    # 1. Direct trace
    console.print("  Trying direct trace …")
    direct = RoiPath(roi_hash=roi_hash, intermediate_hashes=[], hash_len=1)
    trace = await _trace_with_retries(mc, direct.trace_to_roi(), timeout, retries, verbose=verbose)
    if trace:
        min_snr = _path_min_snr(trace)
        avg_snr = _path_avg_snr(trace)
        snr_disp = f"{min_snr:+.1f}" if min_snr is not None else "?"
        console.print(f"  [green]Directly reachable[/] (min SNR {snr_disp})")
        results.append(PathResult(roi_path=direct, min_snr=min_snr, avg_snr=avg_snr, hop_names=[]))
        if not exhaustive:
            return results

    # 2. Protocol path discovery
    console.print("  Trying protocol path discovery …")
    discovered = await establish_path_to_roi(
        mc, roi, timeout, other_repeaters=None, verbose=verbose
    )
    if discovered is not None and discovered.intermediate_hashes:
        # Verify quality with a fresh trace so we can score it
        vtrace = await _trace_with_retries(
            mc, discovered.trace_to_roi(), timeout, retries, verbose=verbose
        )
        min_snr = _path_min_snr(vtrace)
        avg_snr = _path_avg_snr(vtrace)
        names = list(discovered.intermediate_hashes)
        if not any(pr.roi_path.intermediate_hashes == discovered.intermediate_hashes
                   for pr in results):
            results.append(
                PathResult(roi_path=discovered, min_snr=min_snr, avg_snr=avg_snr, hop_names=names)
            )
        if results and not exhaustive:
            results.sort(key=_path_sort_key)
            return results

    # 3. Thorough BFS through known repeaters
    if others:
        bfs_results = await _bfs_find_all_paths(
            mc, roi_hash, others, timeout, retries, max_hops, exhaustive, verbose,
        )
        for pr in bfs_results:
            if not any(existing.roi_path.intermediate_hashes == pr.roi_path.intermediate_hashes
                       for existing in results):
                results.append(pr)
    elif not results:
        console.print("  [red]No other repeaters to try as intermediate hops.[/]")

    results.sort(key=_path_sort_key)
    return results


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
    # In the round-trip trace hops:
    #   [int0, int1, ..., ROI, N, ROI, ..., int1, int0, (client)]
    nbr_hop_idx = roi_path.hops_to_roi_len       # outbound SNR (ROI → N)
    roi_return_idx = roi_path.hops_to_roi_len + 1  # inbound SNR (N → ROI)

    for cand in candidates:
        cand_hash = _contact_hash(cand, roi_path.hash_len)
        name = cand.get("adv_name", "?")
        name_h = f"{name} ({cand_hash})"
        trace_path = roi_path.trace_to(cand_hash)
        console.print(f"  Tracing [cyan]{name_h}[/] via target repeater  path=[dim]{trace_path}[/]")

        found = False
        for attempt in range(1, retries + 1):
            if verbose and attempt > 1:
                console.print(f"    [dim]retry {attempt}/{retries} …[/]")

            trace = await _send_trace_and_wait(mc, trace_path, timeout, verbose=verbose)

            if verbose:
                console.print(f"    [dim]trace result: {trace}[/]")

            if trace is not None:
                path_data = trace.get("path", [])
                # Extract out SNR (ROI → N) and in SNR (N → ROI)
                out_snr = None
                in_snr = None
                if nbr_hop_idx < len(path_data):
                    out_snr = path_data[nbr_hop_idx].get("snr")
                if roi_return_idx < len(path_data):
                    in_snr = path_data[roi_return_idx].get("snr")
                out_str = f"{out_snr:+.1f}" if isinstance(out_snr, (int, float)) else "?"
                in_str = f"{in_snr:+.1f}" if isinstance(in_snr, (int, float)) else "?"
                console.print(
                    f"  [cyan]{name_h}[/] → [green]reachable[/]"
                    f"  out={out_str} dB  in={in_str} dB"
                    f" [dim](attempt {attempt})[/]"
                )
                neighbours.append(cand)
                found = True
                break

        if not found:
            console.print(f"  [cyan]{name_h}[/] → [red]not reachable[/]")

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

    # In the round-trip trace result hops:
    #   [int0, int1, ..., ROI, N, ROI, ..., int1, int0, (client)]
    # Outbound SNR at N:         index = hops_to_roi_len
    # Inbound SNR at ROI return: index = hops_to_roi_len + 1
    nbr_hop_idx = roi_path.hops_to_roi_len
    roi_return_idx = roi_path.hops_to_roi_len + 1

    for nbr in neighbours:
        nbr_hash = _contact_hash(nbr, roi_path.hash_len)
        name = nbr.get("adv_name", "?")
        pk = nbr.get("public_key", nbr.get("pubkey", ""))
        stats = NeighbourStats(name=name, pub_key=pk)

        trace_path_str = roi_path.trace_to(nbr_hash)
        console.print(f"\n  Measuring [cyan]{name} ({nbr_hash})[/] ({samples} samples)")
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


async def _resolve_roi_path(
    mc: MeshCore,
    roi: dict,
    roi_hash: str,
    cfg: dict[str, Any],
    others: list[dict],
    timeout: float,
    verbose: bool,
) -> RoiPath | None:
    """Resolve the RoiPath to use: configured (verified with retries) or auto-discovered.

    A manually configured path is only a guess, so it is verified with up to
    *flood_retries* trace attempts. If none succeed, the manual path is
    rejected and we fall back to auto-discovery.
    """
    manual_path = cfg.get("repeater_of_interest_path", "")
    retries: int = cfg.get("flood_retries", 1)
    roi_path = _roi_path_from_config(roi_hash, manual_path)

    if roi_path:
        console.print(
            f"\n[bold]Using configured path to target repeater:[/] "
            f"{'direct' if not roi_path.intermediate_hashes else ','.join(roi_path.intermediate_hashes)}"
        )
        for attempt in range(1, retries + 1):
            if verbose and attempt > 1:
                console.print(f"    [dim]verify retry {attempt}/{retries} …[/]")
            trace = await _send_trace_and_wait(mc, roi_path.trace_to_roi(), timeout, verbose=verbose)
            if trace:
                console.print(f"  [green]Path verified.[/] SNR per hop: {_format_snr_hops(trace.get('path', []))}")
                return roi_path

        console.print(
            f"  [red]Configured path did not respond after {retries} attempt(s) — "
            f"falling back to auto-discovery.[/]"
        )

    return await establish_path_to_roi(mc, roi, timeout, other_repeaters=others, verbose=verbose)


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
    for r in repeaters:
        h = _contact_hash(r)
        console.print(f"    {r.get('adv_name', '?')} [dim]({h})[/]")
    console.print(f"  Found [green]{len(repeaters)}[/] repeater(s)")

    # Identify target repeater
    roi = _find_roi(repeaters, roi_name)
    if roi is None:
        console.print(f"[red]Target repeater '{roi_name}' not found in contacts.[/]")
        return []

    # 2. Establish path to target repeater
    others = [r for r in repeaters if r is not roi]
    roi_hash = _contact_hash(roi)
    roi_path = await _resolve_roi_path(mc, roi, roi_hash, cfg, others, timeout, verbose)
    if roi_path is None:
        console.print("[red]Cannot reach target repeater — aborting.[/]")
        return []

    # Sync the firmware's contact table so binary requests can route
    await _sync_firmware_path(mc, roi, roi_path, verbose=verbose)

    # 3. Fetch the ROI's own neighbour list & merge with local candidates
    candidates = [r for r in repeaters if r is not roi]
    # Apply prefix / exclude filters
    prefix = cfg.get("repeater_prefix", "")
    exclude = cfg.get("exclude_repeaters", "")
    candidates = _filter_repeaters(candidates, prefix, exclude)
    local_hashes = {_contact_hash(r) for r in candidates}
    remote_nbrs = await fetch_roi_neighbours(mc, roi, timeout, verbose=verbose)
    new_remote = [r for r in remote_nbrs if _contact_hash(r) not in local_hashes
                  and _contact_hash(r) != _contact_hash(roi)]
    if new_remote:
        console.print(f"  [green]{len(new_remote)}[/] remote neighbour(s) not in local contacts — added as candidates")
        for r in new_remote:
            h = _contact_hash(r)
            console.print(f"    {r.get('adv_name', '?')} [dim]({h})[/]")
        candidates.extend(new_remote)

    # 4. Discover zero-hop neighbours
    console.print(f"\n[bold]Discovering neighbours of target repeater via {len(candidates)} candidate(s) …[/]")
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


async def run_findpath(
    mc: MeshCore,
    cfg: dict[str, Any],
    exhaustive: bool = False,
) -> list[PathResult]:
    """Orchestrate a thorough search for a path to the target repeater.

    Returns the verified paths (best first). Prints diagnostics along the way.
    """
    roi_name: str = cfg["repeater_of_interest"]
    timeout: float = cfg["trace_timeout"]
    verbose: bool = cfg.get("verbose", False)
    prefix: str = cfg.get("repeater_prefix", "")
    exclude: str = cfg.get("exclude_repeaters", "")
    candidates: str = cfg.get("path_candidates", "")

    console.print("\n[bold]Fetching repeaters …[/]")
    repeaters = await get_repeaters(mc)
    if not repeaters:
        console.print("[red]No repeaters found.[/]")
        return []

    roi = _find_roi(repeaters, roi_name)
    if roi is None:
        console.print(f"[red]Target repeater '{roi_name}' not found in contacts.[/]")
        return []

    others = [r for r in repeaters if r is not roi]
    others = _filter_repeaters(others, prefix, exclude)

    corridors: list[list[str]] = []
    if candidates:
        n_requested = len([c for c in candidates.split(";") if c.strip()])
        corridors = _parse_corridors(candidates, others)
        hash_to_name = {_contact_hash(r): r.get("adv_name", "?") for r in others}
        if corridors:
            plural = "s" if len(corridors) > 1 else ""
            console.print(
                f"  Corridor mode: testing [green]{len(corridors)}[/] of "
                f"[cyan]{n_requested}[/] requested ordered route{plural} "
                f"(and their ordered subsequences):"
            )
            for idx, corridor in enumerate(corridors, 1):
                route_disp = " → ".join(hash_to_name.get(h, h) for h in corridor)
                console.print(f"    [cyan]{idx}.[/] {route_disp} → target")
        if not corridors:
            console.print(
                "[red]None of the requested waypoint repeaters were found "
                "in contacts — aborting.[/]"
            )
            return []

    results = await find_path_thorough(
        mc, roi, cfg, others, timeout, verbose,
        exhaustive=exhaustive, corridors=corridors,
    )
    if not results:
        console.print("\n[red]No working path to the target repeater was found.[/]")
    return results


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
    roi = _find_roi(repeaters, roi_name)
    if roi is None:
        console.print(f"[red]Target repeater '{roi_name}' not found.[/]")
        return []

    others = [r for r in repeaters if r is not roi]
    roi_hash = _contact_hash(roi)
    roi_path = await _resolve_roi_path(mc, roi, roi_hash, cfg, others, timeout, verbose)
    if roi_path is None:
        console.print("[red]Cannot reach target repeater.[/]")
        return []

    await _sync_firmware_path(mc, roi, roi_path, verbose=verbose)

    console.print(f"\n[bold]Measuring SNR for {len(neighbours)} neighbour(s) …[/]")
    return await gather_snr(mc, roi_path, neighbours, samples, timeout, penalty, verbose=verbose)
