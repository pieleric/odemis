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

Script to measure the excitation light power at the sample for each wavelength
and each power step (0-100% in 10% increments) on a METEOR system.

The light source is accessed via the Odemis model component with role "light".
The power is measured with a Thorlabs PM100D power meter connected via USB/VISA.

Usage:
    python3 scripts/meteor-fat-excitation-power.py

For testing without hardware (TEST_NOHW=1):
    TEST_NOHW=1 python3 scripts/meteor-fat-excitation-power.py

Prerequisites:
    sudo apt install python3-pyvisa
    sudo usermod -a -G dialout $USER
    sudo usermod -a -G plugdev $USER

Output file:
    $HOME/Documents/OdemisTestReports/<hostname>/excitation-power-at-sample-YYYYMMDD.csv
"""

import logging
import os
import random
import socket
import time
from datetime import date
from pathlib import Path
from typing import List, Optional, Tuple

import pyvisa

from odemis import model
from Pyro4.errors import CommunicationError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# Serial number of the Thorlabs PM100D power meter.
# Can be found with: lsusb -v -d 1313:8078 | grep iSerial
PM100D_SERIAL = "P0031850"

# Number of averages for the power meter (~1 reading per second at 1000).
PM_AVERAGING_COUNT = 1000

# Power steps as percentages of the maximum power.
POWER_STEPS_PCT = list(range(0, 101, 10))


def connect_power_meter(rm: pyvisa.ResourceManager, serial: str) -> "Resource":
    """
    Open a connection to the Thorlabs PM100D power meter via pyvisa.

    :param serial: Serial number of the PM100D instrument.
    :return: pyvisa resource instance ready for communication.
    :raises RuntimeError: If the instrument cannot be opened.
    """
    resource_name = f"USB0::0x1313::0x8078::{serial}::INSTR"
    inst = rm.open_resource(resource_name)
    logging.info("Connected to power meter: %s", inst.query("*IDN?").strip())
    inst.write(f"SENSE:AVERAGE:COUNT {PM_AVERAGING_COUNT}")
    logging.info("Power meter averaging count set to %d", PM_AVERAGING_COUNT)
    return inst


def set_meter_wavelength(inst: object, wavelength_nm: float) -> None:
    """
    Set the operating wavelength on the power meter for accurate QE compensation.

    :param inst: pyvisa resource instance for the PM100D.
    :param wavelength_nm: Wavelength in nanometres.
    """
    inst.write(f"SENSE:CORRECTION:WAVELENGTH {wavelength_nm:.0f}")
    actual = float(inst.query("SENSE:CORRECTION:WAVELENGTH?"))
    logging.debug("Power meter wavelength set to %.0f nm (reported: %.0f nm)",
                  wavelength_nm, actual)


def read_power_meter(inst: object) -> float:
    """
    Read the current power from the power meter.

    :param inst: pyvisa resource instance for the PM100D.
    :return: Measured power in watts.
    """
    power_w = float(inst.query("Measure:Scalar:POWer?"))
    return power_w


def get_output_path() -> Path:
    """
    Build the output file path based on hostname and current date.

    The directory is created if it does not exist.

    :return: Path to the output TSV file.
    """
    hostname = socket.gethostname()
    today = date.today().strftime("%Y%m%d")
    directory = Path.home() / "Documents" / "OdemisTestReports" / hostname
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"excitation-power-at-sample-{today}.csv"


def measure_wavelength(
    inst: Optional[object],
    light: object,
    channel_idx: int,
    center_wl_nm: float,
    test_nohw: bool,
) -> Tuple[List[float], List[float]]:
    """
    Perform the power sweep for one wavelength channel and return the results.

    Sets the power meter wavelength, then iterates over all power steps,
    setting the light source power and reading the measured power for each step.
    After the sweep, the light source is turned off for this channel.

    :param inst: pyvisa resource instance for the PM100D, or None in test mode.
    :param light: Odemis light component (role="light").
    :param channel_idx: Index of this wavelength channel in light.power / light.spectra.
    :param center_wl_nm: Centre wavelength of this channel in nanometres.
    :param test_nohw: If True, skip hardware communication and return random values.
    :return: Tuple (set_powers_mw, measured_powers_uw), both lists of length
             equal to POWER_STEPS_PCT.
    """
    max_power_w = light.power.range[1][channel_idx]
    n_channels = len(light.spectra.value)

    if not test_nohw:
        set_meter_wavelength(inst, center_wl_nm)

    set_powers_mw = []
    measured_powers_uw = []

    for step_pct in POWER_STEPS_PCT:
        # Build power list: only this channel is powered, others are off.
        power_values = [0.0] * n_channels
        target_w = max_power_w * step_pct / 100.0
        power_values[channel_idx] = target_w
        light.power.value = power_values

        set_powers_mw.append(target_w * 1e3)

        # Allow the light source to stabilise before reading.
        time.sleep(0.2)

        if test_nohw:
            measured_w = random.gauss(2000e-6, 50e-6)
        else:
            measured_w = read_power_meter(inst)

        measured_powers_uw.append(measured_w * 1e6)

        logging.info(
            "  wl=%.0f nm  step=%3d%%  set=%.3f mW  measured=%.1f µW",
            center_wl_nm, step_pct, target_w * 1e3, measured_w * 1e6,
        )

    # Turn off this channel after the sweep.
    light.power.value = [0.0] * n_channels

    return set_powers_mw, measured_powers_uw


def write_results(
    filepath: Path,
    results: List[Tuple[float, List[float], List[float]]],
) -> None:
    """
    Write measurement results to a tab-separated file.

    Each wavelength produces a block of 4 rows:
      - wavelength (nm)
      - power (%)
      - power (mW)
      - measure power (µW)

    Blocks are separated by a blank line.

    :param filepath: Destination file path.
    :param results: List of (center_wl_nm, set_powers_mw, measured_powers_uw) tuples.
    """
    with open(filepath, "w", encoding="utf-8") as f:
        for i, (wl_nm, set_mw, meas_uw) in enumerate(results):
            wl_int = int(round(wl_nm))
            steps_str = "\t".join(str(s) for s in POWER_STEPS_PCT)
            set_str = "\t".join(f"{v:.5f}" for v in set_mw)
            meas_str = "\t".join(f"{v:.5f}" for v in meas_uw)

            f.write(f"wavelength (nm)\t{wl_int}\n")
            f.write(f"power (%)\t{steps_str}\n")
            f.write(f"power (mW)\t{set_str}\n")
            f.write(f"measure power (µW)\t{meas_str}\n")

            if i < len(results) - 1:
                f.write("\n")

    logging.info("Results saved to: %s", filepath)


def main() -> None:
    """
    Main entry point: connect to hardware, sweep each wavelength channel,
    and write results to a TSV report file.

    Always waits for a key press before returning so that the terminal window
    (which may be closed automatically on exit) stays open long enough for the
    user to read any messages or errors.
    """
    test_nohw = os.environ.get("TEST_NOHW", "0") == "1"
    if test_nohw:
        logging.warning("TEST_NOHW=1: skipping all hardware communication with power meter.")

    # Connect to the light source via Odemis.
    try:
        light = model.getComponent(role="light")
    except CommunicationError:
        logging.error("Could not connect to Odemis. Is the backend running?")
        return

    spectra = light.spectra.value
    logging.info("Light source has %d wavelength channel(s).", len(spectra))

    # Connect to the power meter (skipped in test mode).
    inst = None
    rm = None
    if not test_nohw:
        # Initialize the VISA resource manager once, to be reused for all measurements.
        rm = pyvisa.ResourceManager('@py')
        inst = connect_power_meter(rm, PM100D_SERIAL)

    try:
        results = []
        for idx, band in enumerate(spectra):
            # The 5-tuple is (λ_-99%, λ_-25%, λ_center, λ_+25%, λ_+99%) in metres.
            center_wl_nm = band[2] * 1e9
            logging.info("Measuring channel %d: %.0f nm", idx, center_wl_nm)

            set_mw, meas_uw = measure_wavelength(inst, light, idx, center_wl_nm, test_nohw)
            results.append((center_wl_nm, set_mw, meas_uw))
    finally:
        if inst is not None:
            inst.close()
            rm.close()

    output_path = get_output_path()
    write_results(output_path, results)
    print(f"Report written to: {output_path}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logging.exception("Unexpected error during measurement.")
    finally:
        input("\nPress Enter to exit...")
