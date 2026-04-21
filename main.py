import argparse
import hashlib
import logging
import math
import sys
from pathlib import Path
from datetime import datetime, timezone
from csv import DictWriter, QUOTE_ALL

# Seconds between the Unix epoch (1970-01-01) and the FIT epoch (1989-12-31).
# Add this to any FIT timestamp to obtain a standard Unix timestamp.
FIT_EPOCH_S = 631065600

# The FIT specification reserves field number 253 for the timestamp in every
# message type. Messages whose type is absent from the public profile are
# decoded with numeric keys, so we must check this number as a fallback.
_TIMESTAMP_FIELD_NUM = "253"

# Columns added by this script, inserted before the SDK data columns.
# The parsed_ prefix distinguishes them from Garmin SDK fields.
PARSED_COLS = [
    "parsed_source_filename",
    "parsed_source_hash_sha256",
    "parsed_message_type",
    "parsed_timestamp_source",
    "parsed_fit_timestamp",
    "parsed_unix_timestamp",
    "parsed_utc_timestamp",
    "parsed_ref_fit_timestamp",
    "parsed_ref_row",
]


def setup_logging(results_root: Path, verbose: bool) -> logging.Logger:
    """Configure console and file logging for a run.

    The console handler respects --verbose; the file handler always writes at
    DEBUG level so the full trace is available for forensic review after the run.
    """
    logger = logging.getLogger("fit_parser")
    logger.setLevel(logging.DEBUG)
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # The log file always captures DEBUG level regardless of --verbose,
    # so the full trace is available for forensic review after the run.
    file_handler = logging.FileHandler(results_root / "parse.log", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger


def sha256_of_file(path: Path) -> str:
    """Return the hex-encoded SHA-256 digest of a file.

    Reads in 64 KB chunks to avoid loading large FIT files into memory at once.
    """
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def load_sdk(sdk_path: Path):
    """Import and return (Decoder, Stream) from the Garmin FIT Python SDK.

    sdk_path must point to the directory that contains the garmin_fit_sdk
    package (e.g. FitSDKRelease_*/py). It is inserted at the front of sys.path
    so it takes priority over any other version already installed in the environment.

    Raises ImportError with a descriptive message if the package cannot be found.
    """
    # Insert at position 0 so this SDK takes priority over any other
    # garmin_fit_sdk version that might already be installed in the environment.
    sys.path.insert(0, str(sdk_path.resolve()))
    try:
        from garmin_fit_sdk import Decoder, Stream
        return Decoder, Stream
    except ImportError as e:
        raise ImportError(f"Cannot import garmin_fit_sdk from '{sdk_path}': {e}") from e


def decode_fit(fit_path: Path, Decoder, Stream, logger: logging.Logger):
    """Decode a FIT file using the Garmin Python SDK.

    Returns (messages_dict, errors_list) on success, or (None, []) if the file
    is not a valid FIT file. SDK decode errors are non-fatal and returned in
    errors_list for the caller to log.

    Timestamps are kept as raw FIT integers (convert_datetimes_to_dates=False)
    so that the full conversion chain can be recorded in the output CSV.
    Heart rate merging is disabled to preserve the original message structure.
    """
    stream = Stream.from_file(str(fit_path))
    decoder = Decoder(stream)

    if not decoder.is_fit():
        logger.error(f"  Not a valid FIT file: {fit_path}")
        return None, []

    messages, errors = decoder.read(
        apply_scale_and_offset=True,    # Convert raw integers to physical values (e.g. altitude in metres)
        convert_datetimes_to_dates=False,  # Keep timestamps as raw FIT integers for traceability
        convert_types_to_strings=True,  # Decode enums to human-readable strings (e.g. 'cycling')
        expand_sub_fields=True,         # Expand conditional sub-fields into their own keys
        expand_components=True,         # Expand bit-packed component fields into separate keys
        merge_heart_rates=False,        # Preserve original message structure; do not alter record_mesgs
    )
    return messages, errors


def _resolve_timestamp(row: dict) -> tuple[int | None, str]:
    """Return (fit_timestamp, source_field) for a decoded message dict.

    Checks the named 'timestamp' field first, then falls back to numeric key
    '253' for messages whose type is absent from the public FIT profile.
    """
    ts = row.get("timestamp") or row.get(_TIMESTAMP_FIELD_NUM)
    # The SDK uses NaN to represent a missing numeric field; treat it as absent.
    if ts is not None and not (isinstance(ts, float) and math.isnan(ts)):
        return int(ts), "timestamp"
    return None, ""


def process_fit(
    fit_path: Path,
    output_path: Path,
    source_hash: str,
    Decoder,
    Stream,
    logger: logging.Logger,
) -> bool:
    """Decode a FIT file and write the enriched CSV to output_path.

    All message types are flattened into a single CSV. Each row receives the
    parsed_ provenance and timestamp columns in addition to the SDK fields.
    Returns True on success, False if decoding or writing fails.
    """
    logger.info(f"  Decoding {fit_path}")
    messages, errors = decode_fit(fit_path, Decoder, Stream, logger)
    if messages is None:
        return False

    for err in errors:
        logger.warning(f"  SDK error in {fit_path.name}: {err}")

    # Flatten all message types into an ordered list of (type_name, field_dict).
    # Keys are normalised to strings so numeric field numbers (proprietary
    # messages) and named fields are handled uniformly by DictWriter.
    all_rows: list[tuple[str, dict]] = []
    for mesg_key, mesg_list in messages.items():
        mesg_type = mesg_key.removesuffix("_mesgs") if isinstance(mesg_key, str) else str(mesg_key)
        for mesg in mesg_list:
            all_rows.append((mesg_type, {str(k): v for k, v in mesg.items()}))

    # Build the superset of all data field names across every message type.
    # This becomes the CSV header; columns absent from a given row are left empty.
    data_fields: set[str] = set()
    for _, row in all_rows:
        data_fields.update(row.keys())

    fieldnames = PARSED_COLS + sorted(data_fields)

    try:
        with open(output_path, "w", encoding="utf-8", newline="") as f:
            # extrasaction="ignore" is a safety net: the parsed_* keys are added
            # directly to the row dict, so without this DictWriter would raise
            # on any key not declared in fieldnames.
            writer = DictWriter(f, fieldnames=fieldnames, quoting=QUOTE_ALL, extrasaction="ignore")
            writer.writeheader()

            base_fit_ts: int = 0
            base_fit_ts_row: int | None = None

            # Row numbering starts at 2 to match spreadsheet convention
            # (row 1 is the header).
            for row_num, (mesg_type, row) in enumerate(all_rows, start=2):
                row["parsed_source_filename"] = fit_path.name
                row["parsed_source_hash_sha256"] = source_hash
                row["parsed_message_type"] = mesg_type
                row["parsed_timestamp_source"] = ""
                row["parsed_fit_timestamp"] = ""
                row["parsed_unix_timestamp"] = ""
                row["parsed_utc_timestamp"] = ""
                row["parsed_ref_fit_timestamp"] = ""
                row["parsed_ref_row"] = ""

                fit_ts, ts_source = _resolve_timestamp(row)

                if fit_ts is not None:
                    # Absolute timestamp — update the reference used by subsequent timestamp_16 rows.
                    base_fit_ts = fit_ts
                    base_fit_ts_row = row_num
                    unix_ts = fit_ts + FIT_EPOCH_S
                    utc_dt = datetime.fromtimestamp(unix_ts, tz=timezone.utc)
                    logger.debug(
                        f"  {fit_path.name}: base_timestamp -> {fit_ts} ({utc_dt}) at row {row_num}"
                    )
                    row["parsed_timestamp_source"] = ts_source
                    row["parsed_fit_timestamp"] = fit_ts
                    row["parsed_unix_timestamp"] = unix_ts
                    row["parsed_utc_timestamp"] = utc_dt
                    row["parsed_ref_fit_timestamp"] = fit_ts
                    row["parsed_ref_row"] = row_num

                elif (ts16_raw := row.get("timestamp_16")) is not None:
                    # 16-bit relative timestamp (explicit field, e.g. monitoring messages).
                    # The Python SDK does not reconstruct compressed timestamp headers from
                    # the binary record header — those appear as resolved `timestamp` fields.
                    if not base_fit_ts:
                        logger.warning(
                            f"  {fit_path.name} row {row_num}: timestamp_16 with no base_timestamp set — skipping"
                        )
                    else:
                        ts16_int = int(ts16_raw)
                        # Rollover-safe 16-bit offset from base_timestamp.
                        # The masking ensures correct handling when the low 16 bits of
                        # base_fit_ts wrap past 0xFFFF between two consecutive records.
                        adjusted = base_fit_ts + ((ts16_int - (base_fit_ts & 0xFFFF)) & 0xFFFF)
                        unix_ts = adjusted + FIT_EPOCH_S
                        utc_dt = datetime.fromtimestamp(unix_ts, tz=timezone.utc)
                        row["parsed_timestamp_source"] = "timestamp_16"
                        row["parsed_fit_timestamp"] = adjusted
                        row["parsed_unix_timestamp"] = unix_ts
                        row["parsed_utc_timestamp"] = utc_dt
                        row["parsed_ref_fit_timestamp"] = base_fit_ts
                        row["parsed_ref_row"] = base_fit_ts_row

                elif mesg_type == "stress_level" and (slt := row.get("stress_level_time")) is not None:
                    # stress_level messages use a dedicated absolute timestamp field
                    # instead of the standard timestamp field 253.
                    fit_ts = int(slt)
                    unix_ts = fit_ts + FIT_EPOCH_S
                    utc_dt = datetime.fromtimestamp(unix_ts, tz=timezone.utc)
                    row["parsed_timestamp_source"] = "stress_level_time"
                    row["parsed_fit_timestamp"] = fit_ts
                    row["parsed_unix_timestamp"] = unix_ts
                    row["parsed_utc_timestamp"] = utc_dt
                    row["parsed_ref_fit_timestamp"] = fit_ts
                    row["parsed_ref_row"] = row_num

                writer.writerow(row)

        return True

    except Exception as e:
        logger.error(f"  Failed to write {output_path}: {e}")
        return False


def main() -> None:
    """Entry point: parse CLI arguments, discover FIT files, and process each one."""
    parser = argparse.ArgumentParser(
        description=(
            "Forensic parser for Garmin FIT files using the Python SDK. "
            "Decodes FIT files to enriched CSV with full timestamp traceability. No Java required."
        )
    )
    parser.add_argument("input", type=Path, help="Root folder containing .fit files to process")
    parser.add_argument("output", type=Path, help="Root folder for results (mirrors input tree)")
    parser.add_argument(
        "--sdk",
        type=Path,
        required=True,
        help="Path to the garmin_fit_sdk package directory (e.g. FitSDKRelease_21_105_00/py)",
    )
    parser.add_argument("--verbose", action="store_true", help="Print DEBUG-level messages to console")
    args = parser.parse_args()

    if not args.input.is_dir():
        print(f"Error: input folder '{args.input}' does not exist.", file=sys.stderr)
        sys.exit(1)

    try:
        Decoder, Stream = load_sdk(args.sdk)
    except ImportError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    args.output.mkdir(parents=True, exist_ok=True)
    logger = setup_logging(args.output, args.verbose)
    logger.info(
        f"FIT Parser started — input: {args.input.resolve()}, "
        f"output: {args.output.resolve()}, sdk: {args.sdk.resolve()}"
    )

    fit_files = sorted(p for p in args.input.rglob("*") if p.is_file() and p.suffix.lower() == ".fit")

    if not fit_files:
        logger.warning("No .fit files found in the input folder.")
        return

    total = len(fit_files)
    logger.info(f"Found {total} .fit file(s) to process")

    # Create all output directories up front so the processing loop is not
    # interrupted by a missing parent directory mid-run.
    for fit_path in fit_files:
        result_dir = args.output / fit_path.relative_to(args.input).parent
        result_dir.mkdir(parents=True, exist_ok=True)

    parsed_ok = 0

    for idx, fit_path in enumerate(fit_files, start=1):
        logger.info(f"[{idx}/{total}] {fit_path}")

        result_dir = args.output / fit_path.relative_to(args.input).parent
        parsed_path = result_dir / f"{fit_path.stem}_parsed.csv"

        source_hash = sha256_of_file(fit_path)
        logger.info(f"  SHA256: {source_hash}")

        if process_fit(fit_path, parsed_path, source_hash, Decoder, Stream, logger):
            parsed_ok += 1

    failed = total - parsed_ok
    logger.info(
        f"FIT Parser finished — {total} found, {parsed_ok} parsed successfully, {failed} failed"
    )
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
