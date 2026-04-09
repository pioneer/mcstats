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

snr_samples: 3             # traces per neighbour
timeout_penalty_db: -30    # dB penalty for timed-out samples in averages
flood_retries: 3           # retries per trace/flood
trace_timeout: 15          # seconds to wait for a trace response
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
