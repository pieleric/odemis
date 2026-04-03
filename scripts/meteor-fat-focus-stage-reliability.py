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

Script to measure the reliability/drift of the focus stage (z-axis) on a
METEOR system over time, using autofocus repeated every 5 minutes.

For each of 10 iterations:
  1. Run autofocus
  2. Record the focus z position
  3. Save the acquired image as OME-TIFF
  4. Wait 5 minutes

The final report contains: elapsed timestamps (s), raw positions (nm),
normalised deviations (nm), standard deviation, and slope (nm/min).

Usage:
    python3 scripts/meteor-fat-focus-stage-reliability.py

For testing without hardware (TEST_NOHW=1):
    TEST_NOHW=1 python3 scripts/meteor-fat-focus-stage-reliability.py

Output files:
    $HOME/Documents/OdemisTestReports/<hostname>/focus-stage-reliability-YYYYMMDD.csv
    $HOME/Documents/OdemisTestReports/<hostname>/YYYYMMDD-HHMMSS-focus.ome.tiff  (one per iteration)
"""

import logging
import os
import random
import socket
import statistics
import time
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional, Tuple

from odemis import model
from odemis.acq import acqmng, stream, align
from odemis.dataio import tiff
from odemis.util import fluo
from Pyro4.errors import CommunicationError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# Target wavelengths in metres
TARGET_EMISSION_WL = 515e-9   # 515 nm green emission
TARGET_EXCITATION_WL = 470e-9  # 470 nm excitation

# Acquisition settings
BINNING = (1, 1)
RESOLUTION = (512, 512)
EXPOSURE_TIME = 0.5  # s

# Number of autofocus iterations
N_ITERATIONS = 10

# Sleep between iterations (seconds)
SLEEP_BETWEEN_S = 5 * 60  # 5 minutes


def get_output_dir() -> Path:
    """
    Build the output directory path based on hostname.

    The directory is created if it does not exist.

    :return: Path to the output directory.
    """
    hostname = socket.gethostname()
    directory = Path.home() / "Documents" / "OdemisTestReports" / hostname
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def get_closest_band(choices, target_wl: float):
    """
    Return the band from choices that best fits the given target wavelength.

    Uses fluo.find_best_band_for_dye which picks the narrowest band centred
    around the wavelength for optimal signal-to-noise.

    :param choices: Iterable of wavelength band tuples (each a tuple of floats in metres).
    :param target_wl: Target wavelength in metres.
    :return: The best-fitting band tuple.
    """
    return fluo.find_best_band_for_dye(target_wl, choices)


def setup_fluo_stream(
    ccd: model.HwComponent,
    light: model.HwComponent,
    light_filter: model.HwComponent,
    focus: model.HwComponent,
) -> stream.FluoStream:
    """
    Create and configure a FluoStream for the METEOR system.

    Sets binning, resolution, emission/excitation wavelengths, power, and
    exposure time according to the test parameters defined at module level.

    :param ccd: CCD detector component (role="ccd").
    :param light: Light source component (role="light").
    :param light_filter: Emission filter component (role="filter").
    :param focus: Focus actuator component (role="focus").
    :return: Configured FluoStream ready for acquisition.
    """
    ccd.binning.value = BINNING
    ccd.resolution.value = RESOLUTION
    ccd.exposureTime.value = EXPOSURE_TIME

    fluo_stream = stream.FluoStream(
        "fluo-reliability",
        ccd,
        ccd.data,
        light,
        light_filter,
        focuser=focus,
    )

    # Select the emission band closest to 515 nm
    em_band = get_closest_band(fluo_stream.emission.choices, TARGET_EMISSION_WL)
    fluo_stream.emission.value = em_band
    logging.info(
        "Emission band selected: centre=%.1f nm",
        fluo.get_center(em_band) * 1e9,
    )

    # Select the excitation band closest to 470 nm
    exc_band = get_closest_band(fluo_stream.excitation.choices, TARGET_EXCITATION_WL)
    fluo_stream.excitation.value = exc_band
    logging.info(
        "Excitation band selected: centre=%.1f nm",
        fluo.get_center(exc_band) * 1e9,
    )

    # Set excitation power to maximum
    fluo_stream.power.value = fluo_stream.power.range[1]
    logging.info("Excitation power set to maximum: %.4f W", fluo_stream.power.value)

    return fluo_stream


def acquire_image_and_focus(
    fluo_stream: stream.FluoStream,
    focus: model.HwComponent,
    output_dir: Path,
) -> Tuple[float, float]:
    """
    Run autofocus, acquire one image and save it as OME-TIFF, then return timing and position.

    :param fluo_stream: Configured FluoStream.
    :param focus: Focus actuator component.
    :param output_dir: Directory where the TIFF file will be saved.
    :return: Tuple (elapsed_s, focus_z_m) where elapsed_s is wall-clock time
             since the epoch (used later to compute relative timestamps) and
             focus_z_m is the focus z position in metres.
    """
    logging.info("Running autofocus ...")
    f_focus = align.AutoFocus(fluo_stream.detector, fluo_stream.emitter, focus)
    foc_pos, fm_level, _ = f_focus.result()
    logging.info("Autofocus done: z=%.9f m, focus level=%.3f", foc_pos, fm_level)

    # Read actual position after autofocus
    z_pos = focus.position.value["z"]
    ts = time.time()

    # Acquire one image
    logging.info("Acquiring image ...")
    f_acq = acqmng.acquire([fluo_stream])
    das, acq_error = f_acq.result()
    if acq_error:
        logging.error("Acquisition error: %s", acq_error)

    # Save as OME-TIFF
    timestamp_str = datetime.now().strftime("%Y%m%d-%H%M%S")
    tiff_path = output_dir / f"{timestamp_str}-focus.ome.tiff"
    tiff.export(str(tiff_path), das)
    logging.info("Image saved to: %s", tiff_path)

    return ts, z_pos


def run_reliability_test(
    fluo_stream: Optional[stream.FluoStream],
    focus: Optional[model.HwComponent],
    output_dir: Path,
) -> List[Tuple[float, float]]:
    """
    Execute the focus stage reliability test.

    Runs autofocus N_ITERATIONS times, sleeping SLEEP_BETWEEN_S seconds
    between iterations.  After each autofocus the focus z position is
    recorded.  If either fluo_stream or focus is None, simulated
    positions are returned instead of real hardware measurements.

    :param fluo_stream: Configured FluoStream, or None to simulate.
    :param focus: Focus actuator component, or None to simulate.
    :param output_dir: Directory used to store per-iteration TIFF files.
    :return: List of (timestamp_s, focus_z_m) tuples, one per iteration.
    """
    measurements: List[Tuple[float, float]] = []

    if fluo_stream is None or focus is None:
        logging.warning("No hardware provided; returning simulated positions.")
        base_z = 5.055017e-3  # ~ 5 mm
        base_ts = time.time()
        for i in range(N_ITERATIONS):
            ts = base_ts + i * SLEEP_BETWEEN_S
            z = base_z + random.gauss(0, 300e-9)
            measurements.append((ts, z))
            logging.info(
                "Iteration %d/%d (simulated): z = %.9f m", i + 1, N_ITERATIONS, z
            )
        return measurements

    for i in range(N_ITERATIONS):
        logging.info("--- Iteration %d / %d ---", i + 1, N_ITERATIONS)
        ts, z_pos = acquire_image_and_focus(fluo_stream, focus, output_dir)
        measurements.append((ts, z_pos))
        logging.info(
            "Iteration %d/%d complete: z = %.9f m", i + 1, N_ITERATIONS, z_pos
        )

        if i < N_ITERATIONS - 1:
            logging.info("Waiting %.0f minutes ...", SLEEP_BETWEEN_S / 60)
            time.sleep(SLEEP_BETWEEN_S)

    return measurements


def compute_slope(timestamps_s: List[float], normalized_nm: List[float]) -> float:
    """
    Compute the linear slope (nm/min) of normalised focus position vs time.

    Uses ordinary least squares: slope = cov(t, y) / var(t).

    :param timestamps_s: Elapsed timestamps in seconds (starting from 0).
    :param normalized_nm: Normalised focus positions in nanometres.
    :return: Slope in nm/min.
    """
    n = len(timestamps_s)
    if n < 2:
        return 0.0

    t_min = [t / 60.0 for t in timestamps_s]  # convert to minutes
    t_mean = sum(t_min) / n
    y_mean = sum(normalized_nm) / n

    numerator = sum((t - t_mean) * (y - y_mean) for t, y in zip(t_min, normalized_nm))
    denominator = sum((t - t_mean) ** 2 for t in t_min)

    if denominator == 0:
        return 0.0

    return numerator / denominator


def write_results(
    filepath: Path,
    measurements: List[Tuple[float, float]],
) -> None:
    """
    Write the reliability test results to a tab-separated file.

    The file contains:
      - A header row
      - One data row per measurement (elapsed time in s, position in nm, normalised in nm)
      - A standard-deviation summary row
      - A slope summary row

    :param filepath: Destination file path.
    :param measurements: List of (timestamp_s, focus_z_m) tuples.
    """
    if not measurements:
        logging.warning("No measurements to write.")
        return

    t0 = measurements[0][0]
    first_pos_nm = measurements[0][1] * 1e9

    elapsed_s = [ts - t0 for ts, _ in measurements]
    positions_nm = [z * 1e9 for _, z in measurements]
    normalized_nm = [p - first_pos_nm for p in positions_nm]

    stddev = statistics.stdev(normalized_nm) if len(normalized_nm) > 1 else 0.0
    slope = compute_slope(elapsed_s, normalized_nm)

    with open(filepath, "w", encoding="utf-8") as f:
        f.write("Time stamp (s)\tFocus stage position (nm)\tNormalized (nm)\n")
        for elap, pos_nm, norm in zip(elapsed_s, positions_nm, normalized_nm):
            f.write(f"{elap:.0f}\t{pos_nm:.3f}\t{norm:.3f}\n")
        f.write(f"Standard deviation (nm):\t{stddev:.1f}\n")
        f.write(f"Slope (nm/min)\t{slope:.2f}\n")

    logging.info("Results saved to: %s", filepath)


def main() -> None:
    """
    Main entry point: connect to hardware, run the reliability test, and
    write the TSV report file.

    Always waits for a key press before returning so that the terminal window
    (which may be closed automatically on exit) stays open long enough for the
    user to read any messages or errors.
    """
    output_dir = get_output_dir()
    today = date.today().strftime("%Y%m%d")
    output_csv = output_dir / f"focus-stage-reliability-{today}.csv"

    try:
        ccd = model.getComponent(role="ccd")
        light = model.getComponent(role="light")
        light_filter = model.getComponent(role="filter")
        focus = model.getComponent(role="focus")
    except CommunicationError:
        logging.error("Could not connect to Odemis. Is the backend running?")
        return
    except LookupError as ex:
        logging.error("Could not find required component: %s", ex)
        return

    fluo_stream = setup_fluo_stream(ccd, light, light_filter, focus)
    measurements = run_reliability_test(fluo_stream, focus, output_dir)
    write_results(output_csv, measurements)
    print(f"\nReport written to: {output_csv}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logging.exception("Unexpected error during reliability test.")
    finally:
        input("\nPress Enter to exit...")
