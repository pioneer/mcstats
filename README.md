# mcstats

A CLI tool for evaluating [MeshCore](https://meshcore.io/) LoRa mesh repeater placement quality. It connects to a USB companion device, traces paths through a chosen **repeater of interest** (ROI), discovers the ROI's zero-hop neighbours, and measures bidirectional SNR between the ROI and each neighbour.

## How it works

```
USB Client ──(LoRa)──▶ [intermediates…] ──▶ ROI ──▶ Neighbour
                                           ◀──────
```

1. **Path establishment** — finds a route from your client device to the ROI (direct trace → protocol path discovery → BFS through known repeaters).
2. **Neighbour discovery** — sends a trace through the ROI to every other known repeater; those that respond are zero-hop neighbours.
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
```

## Configuration

Copy or edit `config.yaml` in the project root:

```yaml
serial_port: "COM7"
baud_rate: 115200

repeater_of_interest: "MyRepeater_R1"

# Manual path to ROI (skip auto-discovery):
#   ""         — auto-discover (default)
#   "direct"   — ROI is directly reachable
#   "aa,bb"    — hex hashes of intermediate hops (client → aa → bb → ROI)
repeater_of_interest_path: ""

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
invoke list-repeaters --verbose
```

### Discover neighbours

Trace through the ROI to find its zero-hop neighbours; results are cached in `.cache/`:

```bash
invoke discover
invoke discover --roi "SomeOther_R1"
```

### Full scan

Discover + measure SNR in one step:

```bash
invoke scan
invoke scan --roi "MyRepeater_R1" --samples 5
```

### Measure SNR (cached)

Re-measure SNR using previously discovered neighbours (no re-discovery):

```bash
invoke measure-snr
invoke measure-snr --neighbour "Repeater_A,Repeater_B" --samples 10
```

### Common flags

| Flag | Description |
|---|---|
| `--roi NAME` | Override `repeater_of_interest` from config |
| `--config PATH` | Use a different config file (default: `config.yaml`) |
| `--samples N` | Number of SNR trace samples per neighbour |
| `--verbose` | Enable debug-level meshcore logging |

## Project structure

```
mcstats/
├── __init__.py
├── cache.py        # Per-ROI neighbour cache (JSON)
├── config.py       # YAML config loader with defaults + CLI overrides
├── connection.py   # Async serial connection context manager
├── display.py      # Rich table rendering (SNR report, repeater list)
└── scanner.py      # Core logic: path establishment, discovery, SNR measurement
config.yaml         # User configuration
tasks.py            # Invoke task definitions
pyproject.toml      # Package metadata
```

## License

Unlicensed — personal project.
