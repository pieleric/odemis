#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on 2026-04-02

Copyright © 2026 Delmic

This file is part of Odemis.

Odemis is free software: you can redistribute it and/or modify it under the
terms of the GNU General Public License version 2 as published by the Free
Software Foundation.

Odemis is distributed in the hope that it will be useful, but WITHOUT ANY
WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
PARTICULAR PURPOSE. See the GNU General Public License for more details.

You should have received a copy of the GNU General Public License along with
Odemis. If not, see http://www.gnu.org/licenses/.

Script to measure the repeatability/precision of the focus stage (z-axis) on a
METEOR system.

The focus stage is moved to z=0 once, then 10 round trips of +5 mm / -5 mm are
performed.  After each round trip the actual z position is read back and stored.
The final report contains the raw positions, their values in nanometres, the
normalised deviation from the first reading, and the standard deviation of those
normalised values.

Usage:
    python3 scripts/meteor-focus-stage-precision.py

For testing without hardware (TEST_NOHW=1):
    TEST_NOHW=1 python3 scripts/meteor-focus-stage-precision.py

Output file:
    $HOME/Documents/OdemisTestReports/<hostname>/focus-stage-precision-YYYYMMDD.csv
"""

import logging
import os
import random
import statistics
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional, Tuple

from odemis import model
from Pyro4.errors import CommunicationError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# Number of round-trip moves to perform.
N_ITERATIONS = 10

# Step size for the relative moves
STEP_SIZE = 5e-3  # m


def get_output_path() -> Path:
    """
    Build the output file path based on hostname and current date.

    The directory is created if it does not exist.

    :return: Path to the output TSV file.
    """
    today = date.today().strftime("%Y%m%d")
    directory = Path.home() / "Documents" / "OdemisTestReports" / os.uname().nodename
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"focus-stage-precision-{today}.csv"


def run_precision_test(
    focus: Optional[model.HwComponent],
) -> List[Tuple[str, float]]:
    """
    Execute the focus stage precision test.

    Moves the focus stage to z=0, then performs N_ITERATIONS round trips of
    +STEP_SIZE / -STEP_SIZE.  After each round trip the actual z position is
    read and returned together with a timestamp string.

    If focus is None, simulated positions (Gaussian noise around 0) are returned
    instead of performing real hardware moves.

    :param focus: Odemis focus component (role="focus"), or None to simulate.
    :return: List of (timestamp_str, position_m) tuples, one per iteration.
    """
    measurements: List[Tuple[str, float]] = []

    if focus is None:
        logging.warning("No focus component provided; returning simulated positions.")
        for i in range(N_ITERATIONS):
            ts = datetime.now().strftime("%H:%M:%S")
            pos = random.gauss(0.0, 50e-9)
            measurements.append((ts, pos))
            logging.info("Iteration %d/%d: simulated z = %.9f m", i + 1, N_ITERATIONS, pos)
        return measurements

    # Move to the reference position before starting.
    logging.info("Moving focus stage to z=0 ...")
    focus.moveAbsSync({"z": 0})

    for i in range(N_ITERATIONS):
        logging.info("Iteration %d/%d: moving +%.1f mm ...", i + 1, N_ITERATIONS, STEP_SIZE * 1e3)
        focus.moveRelSync({"z": +STEP_SIZE})
        logging.info("Iteration %d/%d: moving -%.1f mm ...", i + 1, N_ITERATIONS, STEP_SIZE * 1e3)
        focus.moveRelSync({"z": -STEP_SIZE})

        ts = datetime.now().strftime("%H:%M:%S")
        pos = focus.position.value["z"]
        measurements.append((ts, pos))
        logging.info("Iteration %d/%d: z = %.9f m", i + 1, N_ITERATIONS, pos)

    return measurements


def write_results(
    filepath: Path,
    measurements: List[Tuple[str, float]],
) -> None:
    """
    Write the precision test results to a tab-separated file.

    The file contains a header row, one data row per measurement, and a footer
    row with the standard deviation of the normalised values.

    :param filepath: Destination file path.
    :param measurements: List of (timestamp_str, position_m) tuples.
    """
    if not measurements:
        logging.warning("No measurements to write.")
        return

    first_pos_nm = measurements[0][1] * 1e9
    normalized_nm = [pos * 1e9 - first_pos_nm for _, pos in measurements]
    stddev = statistics.stdev(normalized_nm) if len(normalized_nm) > 1 else 0.0

    with open(filepath, "w", encoding="utf-8") as f:
        f.write("Time stamp\tFocus stage position (m)\tFocus stage position (nm)\tNormalized (nm)\n")
        for (ts, pos), norm in zip(measurements, normalized_nm):
            pos_nm = pos * 1e9
            f.write(f"{ts}\t{pos:.12f}\t{pos_nm:.3f}\t{norm:.3f}\n")
        f.write(f"standard deviation (nm)\t{stddev:.3f}\n")

    logging.info("Results saved to: %s", filepath)

    if stddev > 30:
        logging.warning(
            "Standard deviation %.3f nm exceeds 30 nm threshold — focus stage precision is poor.",
            stddev,
        )


def main() -> None:
    """
    Main entry point: connect to the focus stage, run the precision test, and
    write the TSV report file.

    Always waits for a key press before returning so that the terminal window
    (which may be closed automatically on exit) stays open long enough for the
    user to read any messages or errors.
    """
    test_nohw = os.environ.get("TEST_NOHW", "0") == "1"
    if test_nohw:
        logging.warning("TEST_NOHW=1: running without hardware.")

    try:
        if test_nohw:
            focus = None
        else:
            focus = model.getComponent(role="focus")
    except CommunicationError:
        logging.error("Could not connect to Odemis. Is the backend running?")
        return
    except LookupError:
        logging.error("No component with role 'focus' found.")
        return

    measurements = run_precision_test(focus)

    output_path = get_output_path()
    write_results(output_path, measurements)
    print(f"Report written to: {output_path}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logging.exception("Unexpected error during measurement.")
    finally:
        input("\nPress Enter to exit...")
