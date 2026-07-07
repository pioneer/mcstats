# mcstats

A CLI tool for evaluating [MeshCore](https://meshcore.io/) LoRa mesh repeater placement quality. It connects to a USB companion device, traces paths through a chosen **repeater of interest** (ROI), discovers the ROI's zero-hop neighbours, and measures bidirectional SNR between the ROI and each neighbour.

## How it works

```
USB Client ──(LoRa)──▶ [intermediates…] ──▶ ROI ──▶ Neighbour
                                           ◀──────
```

1. **Path establishment** — finds a route from your client device to the ROI (direct trace → protocol path discovery → BFS through known repeaters).
2. **Neighbour discovery** — queries the ROI for its known neighbours (via the binary protocol), merges them with your local contacts, then traces through the ROI to each candidate; those that respond are confirmed zero-hop neighbours.
3. **SNR measurement** — sends round-trip traces through each neighbour and extracts outbound (ROI → neighbour) and inbound (neighbour → ROI) SNR from each hop.

Results are presented in a Rich table with per-attempt values, colour-coded averages, and timeout penalties.

## Requirements

- Python 3.10+
- A MeshCore companion radio connected via USB serial
- The radio must have repeater contacts already synced

## Installation

```bash
git clone <repo-url> && cd mcstats
python -m venv .venv
.venv\Scripts\activate   # Windows
# source .venv/bin/activate  # Linux/macOS
pip install -e .
cp config.yaml.example config.yaml   # then edit
```

## Configuration

Edit `config.yaml` (copied from `config.yaml.example`):

```yaml
serial_port: "COM7"
baud_rate: 115200

repeater_of_interest: "MyRepeater_R1"  # or hex hash, e.g. "b0"

# Manual path to ROI (skip auto-discovery):
#   ""         — auto-discover (default)
#   "direct"   — ROI is directly reachable
#   "aa,bb"    — hex hashes of intermediate hops (client → aa → bb → ROI)
repeater_of_interest_path: ""

# Only include repeaters whose name starts with this prefix (empty = all)
repeater_prefix: ""

# Comma-separated repeater names to exclude from discovery/measurement
exclude_repeaters: ""

# One or more ordered corridors of known waypoints for `findpath` (route order, ';' between corridors, e.g. "55,b0,e5;55,b0,c0,e5"); tests each + discovers unknown tail beyond it
path_candidates: ""

# Repeaters to try when discovering the tail beyond a corridor (`findpath`); tokens match by name, hex hash, or name prefix, e.g. "Kyiv_Troieshchyna,14". Empty = whole pool
tail_candidates: ""

snr_samples: 3             # traces per neighbour
timeout_penalty_db: -30    # dB penalty for timed-out samples in averages
flood_retries: 3           # retries per trace/flood
trace_timeout: 15          # seconds to wait for a trace response
max_path_hops: 4           # max intermediate hops searched by `findpath`
```

All settings can be overridden via CLI flags.

## Usage

All commands use [Invoke](https://www.pyinvoke.org/):

### List repeaters

Show all repeater-type contacts on your device:

```bash
invoke list-repeaters
invoke list-repeaters --prefix "Kyiv_"
invoke list-repeaters --exclude "BadRepeater_R1,OldRepeater_R2"
```

### Discover neighbours

Trace through the ROI to find its zero-hop neighbours; results are cached in `.cache/`:

```bash
invoke discover
invoke discover --roi "SomeOther_R1"
invoke discover --prefix "Kyiv_" --exclude "Kyiv_Old_R1"
```

### Find a path

When the path to a repeater is unknown, exhaustively search for a working route. This tries a direct trace, protocol path discovery, and a thorough SNR-guided BFS through known repeaters (each trace retried per `flood_retries`, up to `max_path_hops` intermediate hops). Results are ranked by stability best-first — highest minimum (weakest-hop) SNR, then highest average SNR, with hop count only as a final tiebreaker:

```bash
invoke findpath --roi "MyRepeater_R1"
invoke findpath --roi "b0" --max-hops 5 --exhaustive
invoke findpath --roi "MyRepeater_R1" --via "55,b0,e5;55,b0,c0,e5"  # two ordered corridors
invoke findpath --roi "MyRepeater_R1" --save      # write best path into config.yaml
```

- `--exhaustive` searches all hop depths and reports every working path (otherwise it stops at the first depth that yields a path).
- `--via` supplies one or more **ordered corridors** of known waypoints (names or hex hashes), each listed in route order from you outward. Waypoints within a corridor are comma-separated; separate multiple corridors with `;` — e.g. `--via "55,b0,e5;55,b0,c0,e5"`. findpath first tests each corridor and its ordered subsequences directly (catching the case where the ROI hangs off a known waypoint), and if none reach the ROI it then **discovers the unknown tail hops anchored at the corridor's far end** — probing candidates *through* the known crossing rather than from your device. Use `--tail` to scope which repeaters that tail discovery may try (see below), so it doesn't fan out across every contact. This is what makes `--via` a time-saver: you don't re-discover the part of the route you already know, and discovery reaches across an otherwise-unreachable stretch. Multiple corridors let you try partly-different routes (e.g. an alternate bridge `c0`). List your reachable entry node first, since the first hop must be a repeater your device can reach directly. Use `--exhaustive` to also discover tails even when a known route already works. Also settable as `path_candidates` in config.
- `--tail` scopes the repeaters tried when discovering the unknown tail beyond a corridor — e.g. `--tail "Kyiv_Troieshchyna,14"`. Each comma-separated token matches by **exact name, hex hash, or name prefix**, so a single option covers both "a direct list of repeaters" and "a set of name prefixes" (a full name is just an exact prefix). This is usually easier than a large `--exclude` list when only repeaters in the ROI's part of town can plausibly relay onward. Empty = try the whole pool. Also settable as `tail_candidates` in config.
- `--save` writes the best route to `repeater_of_interest_path` in your config so later `discover`/`scan`/`measure-snr` runs reuse it.
- `--prefix` / `--exclude` restrict which repeaters are considered as intermediate hops.

### Full scan

Discover + measure SNR in one step:

```bash
invoke scan
invoke scan --roi "MyRepeater_R1" --samples 5
invoke scan --csv results.csv
```

### Measure SNR (cached)

Re-measure SNR using previously discovered neighbours (no re-discovery):

```bash
invoke measure-snr
invoke measure-snr --neighbour "Repeater_A,Repeater_B" --samples 10
invoke measure-snr --csv report.csv
```

### Common flags

| Flag | Description |
|---|---|
| `--roi NAME` | Override `repeater_of_interest` from config (name or hex hash) |
| `--config PATH` | Use a different config file (default: `config.yaml`) |
| `--samples N` | Number of SNR trace samples per neighbour |
| `--prefix TEXT` | Only include repeaters whose name starts with this prefix |
| `--exclude LIST` | Comma-separated repeater names to exclude |
| `--csv PATH` | Write results to a CSV file (scan/measure only) |
| `--max-hops N` | Max intermediate hops to search (findpath only) |
| `--via LIST` | Known corridor(s) (';'-separated); tests each + discovers unknown tail beyond it (findpath only) |
| `--tail LIST` | Scope tail-discovery repeaters by name, hash, or name prefix (findpath only) |
| `--exhaustive` | Search all hop depths, report every path (findpath only) |
| `--save` | Write the best path into config.yaml (findpath only) |
| `--verbose` | Enable debug-level meshcore logging |

## Project structure

```
mcstats/
├── __init__.py
├── cache.py        # Per-ROI neighbour cache (JSON)
├── config.py       # YAML config loader with defaults + CLI overrides
├── connection.py   # Async serial connection context manager
├── csv_export.py   # CSV output for SNR results
├── display.py      # Rich table rendering (SNR report, repeater list)
└── scanner.py      # Core logic: path establishment, discovery, SNR measurement
tests/
└── test_core.py    # Unit tests (pytest)
config.yaml.example # Example configuration (copy to config.yaml)
tasks.py            # Invoke task definitions
pyproject.toml      # Package metadata
```

## Testing

```bash
pip install -e ".[dev]"
pytest tests/ -v
```

## Known limitations

- **ROI neighbour query**: The binary `req_neighbours` protocol (command 0x06) does not receive responses from most repeater firmware versions. Neighbour discovery falls back to tracing through all known contacts. This is handled gracefully — a warning is shown and local contacts are used as candidates.

## License

Unlicensed — personal project.
