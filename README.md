# FIT Parser — Garmin Forensic CSV Extractor

A command-line tool for forensic analysis of Garmin `.fit` files. It decodes FIT files using the official Garmin FIT SDK and produces enriched CSV files with parsed UTC timestamps, source file provenance, and full timestamp reference traceability.

One script, two backends:

| Mode | Backend | Requires |
|---|---|---|
| default | Garmin Python SDK | Python 3.10+ |
| `--jar` | Garmin Java SDK (`FitCSVTool.jar`) | Python 3.10+ · Java |

`--jar` mode is preserved for pedagogical use: its raw SDK CSV output exposes the `Type / Field N / Value N` structure of the FIT format, which is useful for teaching purposes.

---

## Features

### Both modes
- Mirror the input folder tree under a configurable output root
- Parse all Garmin timestamp mechanisms: `timestamp`, `timestamp_16` (with 16-bit rollover correction), and `stress_level_time`
- Append source file provenance to every row: filename and SHA-256 hash of the original `.fit` file
- Records which row set the `base_timestamp` used for each `timestamp_16` entry (`parsed_ref_row`, `parsed_ref_fit_timestamp`)
- Structured logging to console and to a per-run log file in the output folder
- Logs every `base_timestamp` change with file, row number, and UTC value

### Default mode (Python SDK) only
- Decodes `.fit` files directly in Python — no Java required, no intermediate files
- All message types (record, session, lap, event, …) in a single output CSV, one row per message
- Full timestamp conversion chain per row: FIT integer → Unix integer → UTC datetime
- Handles proprietary/unknown message types (numeric keys) transparently

### `--jar` mode only
- Converts `.fit` files to CSV via `FitCSVTool.jar`, then enriches the output
- Two independent UTC timestamp columns per row (`parsed_utc_timestamp` and `parsed_utc_timestamp_direct`) for cross-verification
- Removes intermediate raw SDK CSV files after parsing (only the enriched `_parsed.csv` is kept)

---

## Requirements

- **Python 3.10+**
- **Java** — only required for `--jar` mode
- **[git-lfs](https://git-lfs.com/)** — only required for `--jar` mode (see below)

### Why git-lfs?

`--jar` mode gets its `FitCSVTool.jar` from the `fit-sdk-tools/` git submodule (see [SDK sourcing](#sdk-sourcing) below). That upstream repo stores its binary files (`FitCSVTool.jar`, `Profile.xlsx`) via **Git LFS** instead of committing them directly. A plain `git clone`/`git submodule update` only checks out small LFS *pointer files* (~130 bytes of text), not the real binaries — `git-lfs` has to be installed and `git lfs pull` run to actually download them.

If you skip this, `FitCSVTool.jar` on disk is just a text pointer, and running with `--jar` fails with:
```
SDK conversion failed: Error: Invalid or corrupt jarfile /path/to/fit-sdk-tools/FitCSVTool/FitCSVTool.jar
```
That error means git-lfs content wasn't pulled — run `git lfs pull` (see [Installation](#installation)) to fix it.

Install git-lfs once per machine:
```bash
brew install git-lfs      # macOS
# or: apt install git-lfs / see https://git-lfs.com/ for other platforms
```

Default mode does not need git-lfs at all — it only depends on the `garmin-fit-sdk` PyPI package.

### SDK sourcing

Both modes are wired to the official Garmin FIT SDK, kept up to date instead of a one-off vendored copy:

- Default mode uses the [`garmin-fit-sdk`](https://pypi.org/project/garmin-fit-sdk/) PyPI package (source: [garmin/fit-python-sdk](https://github.com/garmin/fit-python-sdk)), pinned in `requirements.txt`.
- `--jar` mode uses `FitCSVTool.jar` from the [garmin/fit-sdk-tools](https://github.com/garmin/fit-sdk-tools) repo, vendored as a git submodule at `fit-sdk-tools/`, pinned to a release tag.

---

## Installation

```bash
git clone --recurse-submodules <this repo>
cd FIT-Parser
git lfs pull                          # required for --jar mode — see "Why git-lfs?" above
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt       # required for default mode
```

If you already cloned without `--recurse-submodules`:

```bash
git submodule update --init
git lfs pull
```

To bump the SDK versions later:

```bash
pip install -U garmin-fit-sdk         # and update requirements.txt
cd fit-sdk-tools && git fetch --tags && git checkout <new-tag> && cd ..
```

---

## Usage

### Default mode — Python SDK (recommended)

```bash
python main.py <input_folder> <output_folder>
```

```bash
python main.py ./data ./results
python main.py ./data ./results --verbose
```

### `--jar` mode — Java SDK (pedagogical)

```bash
python main.py <input_folder> <output_folder> --jar [path/to/FitCSVTool.jar]
```

```bash
python main.py ./data ./results --jar                                          # uses the vendored submodule jar
python main.py ./data ./results --jar ./some/other/FitCSVTool.jar --verbose    # uses a different jar
```

### Arguments

| Argument | Description |
|---|---|
| `input` | Root folder containing `.fit` files to process (searched recursively) |
| `output` | Root folder where results will be written (folder tree is mirrored from input) |
| `--jar [PATH]` | Switch to the Java backend. With no value, defaults to the vendored submodule jar (`fit-sdk-tools/FitCSVTool/FitCSVTool.jar`). Omit `--jar` entirely to use the default Python backend. |
| `--verbose` | Print DEBUG-level messages to the console (always written to the log file) |

---

## Output

For each `<name>.fit` file found, one file is produced in the mirrored output folder:

| File | Description |
|---|---|
| `<name>_parsed.csv` | Enriched CSV with all columns added by this script |

A `parse.log` file is written at the root of the output folder containing the full run log (always at DEBUG level, regardless of `--verbose`).

### Added columns (`parsed_*` prefix)

All columns added by this script use the `parsed_` prefix to distinguish them from columns produced by the Garmin SDK.

#### Default mode

| Column | Description |
|---|---|
| `parsed_row_number` | CSV row number of this row (matches spreadsheet row; row 1 is the header) |
| `parsed_source_filename` | Name of the original `.fit` file |
| `parsed_source_hash_sha256` | SHA-256 hash of the original `.fit` file |
| `parsed_message_type` | FIT message type (e.g. `record`, `session`, `lap`) |
| `parsed_timestamp_source` | Field used to compute the timestamp: `timestamp`, `timestamp_16`, or `stress_level_time` |
| `parsed_fit_timestamp` | Raw FIT epoch integer (seconds since 1989-12-31 00:00:00 UTC) |
| `parsed_unix_timestamp` | Unix timestamp (`parsed_fit_timestamp + 631065600`) |
| `parsed_utc_timestamp` | UTC datetime derived from `parsed_unix_timestamp` |
| `parsed_ref_fit_timestamp` | The `base_timestamp` value used to resolve this row's `timestamp_16` |
| `parsed_ref_row` | CSV row number where `parsed_ref_fit_timestamp` was established |

#### `--jar` mode

| Column | Description |
|---|---|
| `parsed_row_number` | CSV row number of this row (matches spreadsheet row; row 1 is the header) |
| `parsed_source_filename` | Name of the original `.fit` file |
| `parsed_source_hash_sha256` | SHA-256 hash of the original `.fit` file |
| `parsed_utc_timestamp` | UTC timestamp computed via `fit_ts_to_utc()` |
| `parsed_utc_timestamp_direct` | UTC timestamp computed independently via `datetime.fromtimestamp()` for cross-verification |
| `parsed_ref_timestamp` | The `base_timestamp` value used to resolve this row's `timestamp_16` |
| `parsed_ref_row` | CSV row number where `parsed_ref_timestamp` was established |

Rows without a resolvable timestamp leave the `parsed_utc_*`, `parsed_fit_*`, `parsed_unix_*`, `parsed_ref_*` columns empty.

---

## Timestamp decoding

The FIT format uses a custom epoch (Unix timestamp `631065600`, corresponding to `1989-12-31 00:00:00 UTC`). Three timestamp mechanisms are handled:

- **`timestamp`** — absolute 32-bit FIT timestamp; resets the `base_timestamp` reference used for subsequent rows
- **`timestamp_16`** — explicit 16-bit field (e.g. in `monitoring` messages); rollover-safe correction applied:
  ```
  adjusted = base_timestamp + ((timestamp_16 - (base_timestamp & 0xFFFF)) & 0xFFFF)
  ```
- **`stress_level_time`** — absolute timestamp specific to `stress_level` message records

> **Note (default mode):** The Python SDK resolves FIT compressed timestamp headers (a binary-level optimisation used in high-frequency messages such as `gps_metadata`) transparently into `timestamp` fields. It is therefore not possible to distinguish these from regular `timestamp` fields, and messages that rely solely on compressed headers with no fallback field (e.g. some `gps_metadata` rows) will have empty `parsed_utc_timestamp` columns.

Every `base_timestamp` update is logged at `DEBUG` level (file, row number, UTC value) to support audit trails.

---

## Forensic notes

- The SHA-256 hash in `parsed_source_hash_sha256` covers the original binary `.fit` file and can be used to verify file integrity throughout an investigation.
- Default mode exposes the full conversion chain per row: `parsed_fit_timestamp` (FIT integer) → `parsed_unix_timestamp` (Unix integer) → `parsed_utc_timestamp` (UTC string), making each step independently verifiable.
- `--jar` mode provides two independent UTC computations (`parsed_utc_timestamp` and `parsed_utc_timestamp_direct`) per row; a discrepancy would indicate a computation error.
- `timestamp_16` rows reference back to the exact row (`parsed_ref_row`) that set their `base_timestamp`, enabling full timestamp chain reconstruction.
- Both modes exit with code `1` if any file fails processing, making the script suitable for use in automated forensic pipelines.
