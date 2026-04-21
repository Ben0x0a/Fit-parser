import argparse
import hashlib
import logging
import subprocess
import sys
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from csv import DictReader, DictWriter, QUOTE_ALL

# Seconds between the Unix epoch (1970-01-01) and the FIT epoch (1989-12-31).
# Add this to any FIT timestamp to obtain a standard Unix timestamp.
FIT_EPOCH_S = 631065600

# Columns added by this script, inserted after the first two SDK columns
# (Type, Local Number) for readability.
# The parsed_ prefix distinguishes them from Garmin SDK fields.
PARSED_COLS = [
    "parsed_source_filename",
    "parsed_source_hash_sha256",
    "parsed_utc_timestamp",
    "parsed_utc_timestamp_direct",
    "parsed_ref_timestamp",
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

    file_handler = logging.FileHandler(results_root / "parse.log", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger


def fit_ts_to_utc(timestamp: int) -> datetime:
    """Convert a FIT timestamp (seconds since FIT epoch) to a UTC-aware datetime."""
    return datetime.fromtimestamp((timestamp if timestamp else 0) + FIT_EPOCH_S, tz=timezone.utc)


def sha256_of_file(path: Path) -> str:
    """Return the hex-encoded SHA-256 digest of a file.

    Reads in 64 KB chunks to avoid loading large FIT files into memory at once.
    """
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def convert_fit_to_csv(fit_path: Path, csv_path: Path, sdk_path: Path, logger: logging.Logger) -> bool:
    """Convert a FIT file to raw CSV using FitCSVTool.jar.

    Invokes the Garmin Java SDK via subprocess. Returns True on success,
    False if the process returns a non-zero exit code or produces no output file.
    """
    logger.info(f"  SDK conversion: {fit_path} -> {csv_path}")
    result = subprocess.run(
        ["java", "-jar", str(sdk_path.resolve()), "-b", str(fit_path.resolve()), str(csv_path.resolve())],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        logger.error(f"  SDK conversion failed: {result.stderr.strip()}")
        return False
    if not csv_path.exists():
        logger.error("  SDK conversion produced no output file")
        return False
    return True


def parse_csv(
    csv_path: Path,
    output_path: Path,
    source_fit: Path,
    source_hash: str,
    logger: logging.Logger,
) -> bool:
    """Enrich the raw SDK CSV with parsed_ provenance and timestamp columns.

    Reads the intermediate CSV produced by FitCSVTool.jar and writes an
    enriched version to output_path. Handles all three FIT timestamp mechanisms:
    absolute timestamp, 16-bit relative timestamp_16, and stress_level_time.
    Returns True on success, False on any read/write error.
    """
    logger.info(f"  Parsing {csv_path} -> {output_path}")

    try:
        with open(csv_path, "r", encoding="utf-8") as input_file, \
             open(output_path, "w", encoding="utf-8", newline="") as output_file:

            reader = DictReader(input_file)
            fieldnames = list(reader.fieldnames)

            # Insert parsed_ columns right after the first two SDK columns
            # (Type and Local Number) so they appear near the left of the CSV.
            for i, col in enumerate(PARSED_COLS):
                fieldnames.insert(2 + i, col)

            writer = DictWriter(output_file, quoting=QUOTE_ALL, fieldnames=fieldnames)
            writer.writeheader()

            # Count how many Field N / Value N column pairs the SDK produced.
            # The SDK uses a variable number depending on the widest message.
            max_field_number = 0
            while f"Field {max_field_number + 1}" in reader.fieldnames:
                max_field_number += 1

            base_timestamp: int = 0
            base_timestamp_row: int | None = None

            # Row numbering starts at 2 to match spreadsheet convention
            # (row 1 is the header).
            for row_num, row in enumerate(reader, start=2):
                row["parsed_source_filename"] = source_fit.name
                row["parsed_source_hash_sha256"] = source_hash
                row["parsed_utc_timestamp"] = ""
                row["parsed_utc_timestamp_direct"] = ""
                row["parsed_ref_timestamp"] = ""
                row["parsed_ref_row"] = ""

                if row.get("\ufeffType") == "Data":
                    # The SDK CSV starts with a UTF-8 BOM (\ufeff), which Python's
                    # csv module attaches to the first column name rather than
                    # stripping it, hence the "\ufeffType" key instead of "Type".
                    for i in range(1, max_field_number + 1):
                        field = row.get(f"Field {i}", "")
                        value = row.get(f"Value {i}", "")

                        if not field or not value:
                            continue

                        is_stress = row["Message"] == "stress_level" and field == "stress_level_time"
                        is_ts = field == "timestamp"
                        is_ts16 = field == "timestamp_16"

                        # Only timestamp-related fields need integer conversion;
                        # skipping other fields avoids spurious warnings for
                        # legitimate float values like enhanced_altitude or distance.
                        if not (is_stress or is_ts or is_ts16):
                            continue

                        try:
                            int_value = int(value)
                        except ValueError:
                            logger.warning(
                                f"  {source_fit.name} row {row_num}: non-integer value '{value}' "
                                f"for field '{field}' — skipping field"
                            )
                            continue

                        if is_stress:
                            # stress_level messages use a dedicated absolute timestamp
                            # field instead of the standard timestamp field.
                            row["parsed_utc_timestamp"] = fit_ts_to_utc(int_value)
                            row["parsed_utc_timestamp_direct"] = datetime.fromtimestamp(
                                int_value + FIT_EPOCH_S, tz=timezone.utc
                            )
                            row["parsed_ref_timestamp"] = int_value
                            row["parsed_ref_row"] = row_num

                        elif is_ts:
                            # Absolute timestamp — update the reference used by subsequent timestamp_16 rows.
                            base_timestamp = int_value
                            base_timestamp_row = row_num
                            logger.debug(
                                f"  {source_fit.name}: base_timestamp -> {base_timestamp} "
                                f"({fit_ts_to_utc(base_timestamp)}) at row {row_num}"
                            )
                            row["parsed_utc_timestamp"] = fit_ts_to_utc(base_timestamp)
                            row["parsed_utc_timestamp_direct"] = datetime.fromtimestamp(
                                base_timestamp + FIT_EPOCH_S, tz=timezone.utc
                            )
                            row["parsed_ref_timestamp"] = base_timestamp
                            row["parsed_ref_row"] = row_num

                        elif is_ts16:
                            if not base_timestamp:
                                logger.warning(
                                    f"  {source_fit.name} row {row_num}: timestamp_16 with no base_timestamp set — skipping"
                                )
                            else:
                                # Rollover-safe 16-bit offset from base_timestamp.
                                # The masking ensures correct handling when the low 16 bits of
                                # base_timestamp wrap past 0xFFFF between two consecutive records.
                                adjusted = base_timestamp + ((int_value - (base_timestamp & 0xFFFF)) & 0xFFFF)
                                row["parsed_utc_timestamp"] = fit_ts_to_utc(adjusted)
                                row["parsed_utc_timestamp_direct"] = datetime.fromtimestamp(
                                    adjusted + FIT_EPOCH_S, tz=timezone.utc
                                )
                                row["parsed_ref_timestamp"] = base_timestamp
                                row["parsed_ref_row"] = base_timestamp_row

                writer.writerow(row)

        return True

    except Exception as e:
        logger.error(f"  Failed to parse {csv_path}: {e}")
        return False


def main() -> None:
    """Entry point: parse CLI arguments, discover FIT files, and process each one."""
    parser = argparse.ArgumentParser(
        description="Forensic parser for Garmin FIT files. Converts FIT files to enriched CSV with parsed timestamps."
    )
    parser.add_argument("input", type=Path, help="Root folder containing .fit files to process")
    parser.add_argument("output", type=Path, help="Root folder for results (mirrors input tree)")
    parser.add_argument("--sdk", type=Path, required=True, help="Path to FitCSVTool.jar (Garmin FIT SDK)")
    parser.add_argument("--verbose", action="store_true", help="Print DEBUG-level messages to console")
    args = parser.parse_args()

    if not args.input.is_dir():
        print(f"Error: input folder '{args.input}' does not exist.", file=sys.stderr)
        sys.exit(1)
    if not args.sdk.is_file():
        print(f"Error: SDK jar '{args.sdk}' not found.", file=sys.stderr)
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

    converted_ok = 0
    parsed_ok = 0

    for idx, fit_path in enumerate(fit_files, start=1):
        logger.info(f"[{idx}/{total}] {fit_path}")

        result_dir = args.output / fit_path.relative_to(args.input).parent
        parsed_path = result_dir / f"{fit_path.stem}_parsed.csv"

        source_hash = sha256_of_file(fit_path)
        logger.info(f"  SHA256: {source_hash}")

        # TemporaryDirectory is used for the intermediate SDK CSV so it is
        # deleted automatically on exit, even if an exception occurs.
        with tempfile.TemporaryDirectory() as tmp_dir:
            csv_path = Path(tmp_dir) / fit_path.with_suffix(".csv").name

            if not convert_fit_to_csv(fit_path, csv_path, args.sdk, logger):
                continue
            converted_ok += 1

            success = parse_csv(csv_path, parsed_path, fit_path, source_hash, logger)

        if success:
            parsed_ok += 1

    failed = total - parsed_ok
    logger.info(
        f"FIT Parser finished — {total} found, {converted_ok} converted, "
        f"{parsed_ok} parsed successfully, {failed} failed"
    )
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
