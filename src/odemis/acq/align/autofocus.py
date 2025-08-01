# -*- coding: utf-8 -*-
"""
Created on 11 Apr 2014

@author: Kimon Tsitsikas

Copyright © 2013-2016 Kimon Tsitsikas and Éric Piel, Delmic

This file is part of Odemis.

Odemis is free software: you can redistribute it and/or modify it under the
terms  of the GNU General Public License version 2 as published by the Free
Software  Foundation.

Odemis is distributed in the hope that it will be useful, but WITHOUT ANY
WARRANTY;  without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
PARTICULAR  PURPOSE. See the GNU General Public License for more details.

You should have received a copy of the GNU General Public License along with
Odemis. If not, see http://www.gnu.org/licenses/.
"""

import logging
import threading
import time
from collections.abc import Iterable
from concurrent.futures import TimeoutError, CancelledError
from concurrent.futures._base import CANCELLED, FINISHED, RUNNING
from typing import Any, Dict, List, Optional, Tuple, Union, Callable

import numpy

from odemis import model
from odemis.acq.align import light
from odemis.model import InstantaneousFuture
from odemis.util import executeAsyncTask, almost_equal
from odemis.util.driver import guessActuatorMoveDuration
from odemis.util.focus import MeasureSEMFocus, Measure1d, MeasureSpotsFocus, AssessFocus
from odemis.util.img import Subtract

MTD_BINARY = 0
MTD_EXHAUSTIVE = 1

MAX_STEPS_NUMBER = 100  # Max steps to perform autofocus
MAX_BS_NUMBER = 1  # Maximum number of applying binary search with a smaller max_step


def getNextImage(det: model.Detector, timeout: Optional[float] = None) -> model.DataArray:
    """
    Acquire one image from the given detector
    det: detector from which to acquire an image
    timeout: maximum time to wait (>0)
    returns:
        Image (with subtracted background if requested)
    raise:
        IOError: if it timed out
    """
    # Code based on Dataflow.get(), to support timeout
    min_time = time.time()  # asap=False
    is_received = threading.Event()
    data_shared = [None]  # in python2 we need to create a new container object

    def receive_one_image(df, data):
        if data.metadata.get(model.MD_ACQ_DATE, float("inf")) >= min_time:
            df.unsubscribe(receive_one_image)
            data_shared[0] = data
            is_received.set()

    det.data.subscribe(receive_one_image)
    if not is_received.wait(timeout):
        det.data.unsubscribe(receive_one_image)
        raise IOError("No data received after %g s" % (timeout,))
    return data_shared[0]


def AcquireNoBackground(
        det: model.Detector,
        dfbkg: Optional[model.DataFlow] = None,
        timeout: Optional[float] = None
) -> model.DataArray:
    """
    Performs optical acquisition with background subtraction if possible.
    Particularly used in order to eliminate the e-beam source background in the
    Delphi.
    det: detector from which to acquire an image
    dfbkg: dataflow of se- or bs- detector to
    start/stop the source. If None, a standard acquisition is performed (without
    background subtraction)
    timeout: maximum time to wait (>0)
    returns:
        Image (with subtracted background if requested)
    raise:
        IOError: if it timed out
    """
    if dfbkg is not None:
        # acquire background
        bg_image = getNextImage(det, timeout)

        # acquire with signal
        dfbkg.subscribe(_discard_data)
        try:
            data = getNextImage(det, timeout)
        finally:
            dfbkg.unsubscribe(_discard_data)

        return Subtract(data, bg_image)
    else:
        return getNextImage(det, timeout)


def _discard_data(df: Any, data: Any) -> None:
    """
    Does nothing, just discard the SEM data received (for spot mode)
    """
    pass


def _DoBinaryFocus(
        future: model.ProgressiveFuture,
        detector: model.Detector,
        emt: Optional[model.Emitter],
        focus: model.Actuator,
        dfbkg: Optional[model.DataFlow],
        good_focus: Optional[float],
        rng_focus: Optional[Tuple[float, float]],
        measure_func: Optional[Callable] = None
) -> Tuple[float, float, float]:
    """
    Iteratively acquires an optical image, measures its focus level and adjusts
    the optical focus with respect to the focus level.
    future: Progressive future provided by the wrapper
    detector: Detector on which to improve the focus quality
    emt: In case of a SED this is the scanner used
    focus: The focus actuator (with a "z" axis)
    dfbkg: dataflow of se- or bs- detector
    good_focus: if provided, an already known good focus position to be
      taken into consideration while autofocusing
    rng_focus: if provided, the search of the best focus position is limited
      within this range
    measure_func: function to measure the focus level on the image,
      for instance MeasureSEMFocus. If None the focus metric used is based
      on the detector.
    returns:
        (float): Focus position (m)
        (float): Focus level
        (float): Focus confidence (0<=f<=1, 0 is not in focus and 1 is the best possible focus)
    raises:
            CancelledError if cancelled
            IOError if procedure failed
    """
    # TODO: dfbkg is mis-named, as it's the dataflow to use to _activate_ the
    # emitter. It's necessary to acquire the background, as otherwise we assume
    # the emitter is always active, but during background acquisition, that
    # emitter is explicitly _disabled_.
    # => change emt to "scanner", and "dfbkg" to "emitter". Or pass a stream?
    # Note: the emt is almost not used, only to estimate completion time,
    # and read the depthOfField.

    # It does a dichotomy search on the focus level. In practice, it means it
    # will start going into the direction that increase the focus with big steps
    # until the focus decreases again. Then it'll bounce back and forth with
    # smaller and smaller steps.
    # The tricky parts are:
    # * it's hard to estimate the focus level (on an arbitrary image)
    # * two acquisitions at the same focus position can have (slightly) different
    #   focus levels (due to noise and sample degradation)
    # * if the focus actuator is not precise (eg, open loop), it's hard to
    #   even go back to the same focus position when wanted
    logging.debug("Starting binary autofocus on detector %s...", detector.name)

    try:
        # Big timeout, most important being that it's shorter than eternity
        timeout = 3 + 2 * estimateAcquisitionTime(detector, emt)

        # use the .depthOfField on detector or emitter as maximum stepsize
        avail_depths = (detector, emt)
        if model.hasVA(emt, "dwellTime"):
            # Hack in case of using the e-beam with a DigitalCamera detector.
            # All the digital cameras have a depthOfField, which is updated based
            # on the optical lens properties... but the depthOfField in this
            # case depends on the e-beam lens.
            # TODO: or better rely on which component the focuser affects? If it
            # affects (also) the emitter, use this one first? (but in the
            # current models the focusers affects nothing)
            avail_depths = (emt, detector)
        for c in avail_depths:
            if model.hasVA(c, "depthOfField"):
                dof = c.depthOfField.value
                break
        else:
            logging.debug("No depth of field info found")
            dof = 1e-6  # m, not too bad value
        logging.debug("Depth of field is %.7g", dof)
        min_step = dof / 2

        # adjust to rng_focus if provided
        rng = focus.axes["z"].range
        if rng_focus:
            rng = (max(rng[0], rng_focus[0]), min(rng[1], rng_focus[1]))

        max_step = (rng[1] - rng[0]) / 2
        if max_step <= 0:
            raise ValueError("Unexpected focus range %s" % (rng,))

        rough_search = True  # False once we've passed the maximum level (ie, start bouncing)
        # It's used to cache the focus level, to avoid reacquiring at the same
        # position. We do it only for the 'rough' max search because for the fine
        # search, the actuator and acquisition delta are likely to play a role
        focus_levels = {}  # focus pos (float) -> focus level (float)

        best_pos = focus.position.value['z']
        best_fm = 0
        last_pos = None

        # Pick measurement method based on the heuristics that SEM detectors
        # are typically just a point (ie, shape == data depth).
        # TODO: is this working as expected? Alternatively, we could check
        # MD_DET_TYPE.
        if measure_func is None:
            if len(detector.shape) > 1:
                if detector.role == 'diagnostic-ccd':
                    logging.debug("Using Spot method to estimate focus")
                    Measure = MeasureSpotsFocus
                elif detector.resolution.value[1] == 1:
                    logging.debug("Using 1d method to estimate focus")
                    Measure = Measure1d
                else:
                    logging.debug("Using Spot method to estimate focus")
                    Measure = MeasureSpotsFocus
            else:
                logging.debug("Using SEM method to estimate focus")
                Measure = MeasureSEMFocus
        else:
            logging.debug(f"Using measure function {measure_func.__name__} to estimate focus")
            Measure = measure_func

        step_factor = 2 ** 7
        if good_focus is not None:
            current_pos = focus.position.value['z']
            image = AcquireNoBackground(detector, dfbkg, timeout)
            fm_current = Measure(image)
            logging.debug("Focus level at %.7g is %.7g", current_pos, fm_current)
            focus_levels[current_pos] = fm_current

            focus.moveAbsSync({"z": good_focus})
            good_focus = focus.position.value["z"]
            image = AcquireNoBackground(detector, dfbkg, timeout)
            fm_good = Measure(image)
            logging.debug("Good Focus level known at %.7g is %.7g", good_focus, fm_good)
            focus_levels[good_focus] = fm_good
            last_pos = good_focus

            if fm_good < fm_current:
                # Move back to current position if good_pos is not that good
                # after all
                focus.moveAbsSync({"z": current_pos})
                # it also means we are pretty close
            step_factor = 2 ** 4

        if step_factor * min_step > max_step:
            # Large steps would be too big. We can reduce step_factor and/or
            # min_step. => let's take our time, and maybe find finer focus
            min_step = max_step / step_factor
            logging.debug("Reducing min step to %g", min_step)

        # TODO: to go a bit faster, we could use synchronised acquisition on
        # the detector (if it supports it)
        # TODO: we could estimate the quality of the autofocus by looking at the
        # standard deviation of the the focus levels (and the standard deviation
        # of the focus levels measured for the same focus position)
        logging.debug("Step factor used for autofocus: %g", step_factor)
        step_cntr = 1
        while step_factor >= 1 and step_cntr <= MAX_STEPS_NUMBER:
            # TODO: update the estimated time (based on how long it takes to
            # move + acquire, and how many steps are approximately left)

            # Start at the current focus position
            center = focus.position.value['z']
            # Don't redo the acquisition either if we've just done it, or if it
            # was already done and we are still doing a rough search
            if (rough_search or last_pos == center) and center in focus_levels:
                fm_center = focus_levels[center]
            else:
                image = AcquireNoBackground(detector, dfbkg, timeout)
                fm_center = Measure(image)
                logging.debug("Focus level (center) at %.7g is %.7g", center, fm_center)
                focus_levels[center] = fm_center

            last_pos = center

            # Move to right position
            right = center + step_factor * min_step
            right = max(rng[0], min(right, rng[1]))  # clip
            if rough_search and right in focus_levels:
                fm_right = focus_levels[right]
            else:
                focus.moveAbsSync({"z": right})
                right = focus.position.value["z"]
                last_pos = right
                image = AcquireNoBackground(detector, dfbkg, timeout)
                fm_right = Measure(image)
                logging.debug("Focus level (right) at %.7g is %.7g", right, fm_right)
                focus_levels[right] = fm_right

            # Move to left position
            left = center - step_factor * min_step
            left = max(rng[0], min(left, rng[1]))  # clip
            if rough_search and left in focus_levels:
                fm_left = focus_levels[left]
            else:
                focus.moveAbsSync({"z": left})
                left = focus.position.value["z"]
                last_pos = left
                image = AcquireNoBackground(detector, dfbkg, timeout)
                fm_left = Measure(image)
                logging.debug("Focus level (left) at %.7g is %.7g", left, fm_left)
                focus_levels[left] = fm_left

            fm_range = (fm_left, fm_center, fm_right)
            if all(almost_equal(fm_left, fm, rtol=1e-6) for fm in fm_range[1:]):
                logging.debug("All focus levels identical, picking the middle one")
                # Most probably the images are all noise, or they are not affected
                # by the focus. In any case, the best is to not move the focus,
                # so let's "center" on it. That's better than the default behaviour
                # which would tend to pick "left" because that's the first one.
                i_max = 1
                best_pos, best_fm = center, fm_center
            else:
                pos_range = (left, center, right)
                best_fm = max(fm_range)
                i_max = fm_range.index(best_fm)
                best_pos = pos_range[i_max]

            if future._autofocus_state == CANCELLED:
                raise CancelledError()

            # if best focus was found at the center
            if i_max == 1:
                step_factor /= 2
                if rough_search:
                    logging.debug("Now zooming in on improved focus")
                rough_search = False
            elif (rng[0] > best_pos - step_factor * min_step or
                  rng[1] < best_pos + step_factor * min_step):
                step_factor /= 1.5
                logging.debug("Reducing step factor to %g because the focus (%g) is near range limit %s",
                              step_factor, best_pos, rng)
                if step_factor <= 8:
                    rough_search = False  # Force re-checking data

            if last_pos != best_pos:
                # Clip best_pos in case the hardware reports a position outside of the range.
                best_pos = max(rng[0], min(best_pos, rng[1]))
                focus.moveAbsSync({"z": best_pos})

            if left == right:
                # Do this after moving to the best position, because clipping left and right can cause
                # left and right to be equal, even when both are unequal to the best position.
                logging.info("Seems to have reached minimum step size (at %g m)", 2 * step_factor * min_step)
                break

            step_cntr += 1

        worst_fm = min(focus_levels.values())
        if step_cntr == MAX_STEPS_NUMBER:
            logging.info("Auto focus gave up after %d steps @ %g m", step_cntr, best_pos)
            confidence = 0.1
        elif (best_fm - worst_fm) < best_fm * 0.5:
            # We can be confident of the data if there is a "big" (50%) difference
            # between the focus levels.
            logging.info("Auto focus indecisive but picking level %g @ %g m (lowest = %g)",
                         best_fm, best_pos, worst_fm)
            confidence = 0.2
        else:
            logging.info("Auto focus found best level %g @ %g m", best_fm, best_pos)
            confidence = 0.8

        return best_pos, best_fm, confidence

    except CancelledError:
        # Go to the best position known so far
        focus.moveAbsSync({"z": best_pos})
    finally:
        with future._autofocus_lock:
            if future._autofocus_state == CANCELLED:
                raise CancelledError()
            future._autofocus_state = FINISHED


def _DoExhaustiveFocus(
        future: model.ProgressiveFuture,
        detector: model.Detector,
        emt: Optional[model.Emitter],
        focus: model.Actuator,
        dfbkg: Optional[model.DataFlow],
        good_focus: Optional[float],
        rng_focus: Optional[Tuple[float, float]],
        measure_func: Optional[Callable] = None
) -> Tuple[float, float, float]:
    """
    Moves the optical focus through the whole given range, measures the focus
    level on each position and ends up where the best focus level was found. In
    case a significant deviation was found while going through the range, it
    stops and limits the search within a smaller range around this position.
    future: Progressive future provided by the wrapper
    detector: Detector on which to improve the focus quality
    emt: In case of a SED this is the scanner used
    focus: The optical focus
    dfbkg: dataflow of se- or bs- detector
    good_focus: if provided, an already known good focus position to be
      taken into consideration while autofocusing
    rng_focus: if provided, the search of the best focus position is limited
      within this range
    measure_func: function to measure the focus level on the image,
      for instance MeasureSEMFocus. If None the focus metric used is based
      on the detector.
    returns:
        (float): Focus position (m)
        (float): Focus level
        (float): Focus confidence (0<=f<=1, 0 is not in focus and 1 is the best possible focus)
    raises:
            CancelledError if cancelled
            IOError if procedure failed
    """
    logging.debug("Starting exhaustive autofocus on detector %s...", detector.name)

    try:
        # Big timeout, most important being that it's shorter than eternity
        timeout = 3 + 2 * estimateAcquisitionTime(detector, emt)

        # use the .depthOfField on detector or emitter as maximum stepsize
        avail_depths = (detector, emt)
        if model.hasVA(emt, "dwellTime"):
            # Hack in case of using the e-beam with a DigitalCamera detector.
            # All the digital cameras have a depthOfField, which is updated based
            # on the optical lens properties... but the depthOfField in this
            # case depends on the e-beam lens.
            avail_depths = (emt, detector)
        for c in avail_depths:
            if model.hasVA(c, "depthOfField"):
                dof = c.depthOfField.value
                break
        else:
            logging.debug("No depth of field info found")
            dof = 1e-6  # m, not too bad value
        logging.debug("Depth of field is %.7g", dof)

        # Pick measurement method based on the heuristics that SEM detectors
        # are typically just a point (ie, shape == data depth).
        # TODO: is this working as expected? Alternatively, we could check
        # MD_DET_TYPE.
        if measure_func is None:
            if len(detector.shape) > 1:
                if detector.role == 'diagnostic-ccd':
                    logging.debug("Using Spot method to estimate focus")
                    Measure = MeasureSpotsFocus
                elif detector.resolution.value[1] == 1:
                    logging.debug("Using 1d method to estimate focus")
                    Measure = Measure1d
                else:
                    logging.debug("Using Spot method to estimate focus")
                    Measure = MeasureSpotsFocus
            else:
                logging.debug("Using SEM method to estimate focus")
                Measure = MeasureSEMFocus
        else:
            logging.debug(f"Using measure function {measure_func.__name__} to estimate focus")
            Measure = measure_func

        # adjust to rng_focus if provided
        rng = focus.axes["z"].range
        if rng_focus:
            rng = (max(rng[0], rng_focus[0]), min(rng[1], rng_focus[1]))

        if good_focus:
            logging.debug(f"moving to good focus level at z:{good_focus}")
            focus.moveAbsSync({"z": good_focus})

        focus_levels = []  # list with focus levels measured so far
        best_pos = orig_pos = focus.position.value['z']
        best_fm = 0

        if future._autofocus_state == CANCELLED:
            raise CancelledError()

        # Based on our measurements on spot detection, a spot is visible within
        # a margin of ~30microns around its best focus position. Such a step
        # (i.e. ~ 6microns) ensures that we will eventually be able to notice a
        # difference compared to the focus levels measured so far.
        step = 8 * dof
        lower_bound, upper_bound = rng
        # Ensure we take at least 10 steps
        if (upper_bound - lower_bound) < 10 * step:
            logging.debug("Focus range < 10 steps, adjusting step size to %g m", (upper_bound - lower_bound) / 10)
            step = (upper_bound - lower_bound) / 10
        # start moving upwards until we reach the upper bound or we find some
        # significant deviation in focus level
        # The number of steps is the distance to the upper bound divided by the step size.
        for next_pos in numpy.linspace(orig_pos, upper_bound, int((upper_bound - orig_pos) / step)):
            focus.moveAbsSync({"z": next_pos})
            image = AcquireNoBackground(detector, dfbkg, timeout)
            new_fm = Measure(image)
            focus_levels.append(new_fm)
            logging.debug("Focus level at %.7g is %.7g", next_pos, new_fm)
            if new_fm >= best_fm:
                best_fm = new_fm
                best_pos = next_pos
            if len(focus_levels) >= 10 and AssessFocus(focus_levels):
                # trigger binary search on if significant deviation was
                # found in current position
                return _DoBinaryFocus(future, detector, emt, focus, dfbkg, best_pos,
                                      (best_pos - 2 * step, best_pos + 2 * step), measure_func=measure_func)

        if future._autofocus_state == CANCELLED:
            raise CancelledError()

        # if nothing was found go downwards, starting one step below the original position
        num = max(int((orig_pos - lower_bound) / step), 0)  # Take 0 steps if orig_pos is too close to lower_bound
        for next_pos in numpy.linspace(orig_pos - step, lower_bound, num):
            focus.moveAbsSync({"z": next_pos})
            image = AcquireNoBackground(detector, dfbkg, timeout)
            new_fm = Measure(image)
            focus_levels.append(new_fm)
            logging.debug("Focus level at %.7g is %.7g", next_pos, new_fm)
            if new_fm >= best_fm:
                best_fm = new_fm
                best_pos = next_pos
            if len(focus_levels) >= 10 and AssessFocus(focus_levels):
                # trigger binary search on if significant deviation was
                # found in current position
                return _DoBinaryFocus(future, detector, emt, focus, dfbkg, best_pos,
                                      (best_pos - 2 * step, best_pos + 2 * step), measure_func=measure_func)

        if future._autofocus_state == CANCELLED:
            raise CancelledError()

        logging.debug("No significant focus level was found so far, thus we just move to the best position found %.7g",
                      best_pos)
        focus.moveAbsSync({"z": best_pos})
        return _DoBinaryFocus(future, detector, emt, focus, dfbkg, best_pos, (best_pos - 2 * step, best_pos + 2 * step),
                              measure_func=measure_func)

    except CancelledError:
        # Go to the best position known so far
        focus.moveAbsSync({"z": best_pos})
    finally:
        # Only used if for some reason the binary focus is not called (e.g. cancellation)
        with future._autofocus_lock:
            if future._autofocus_state == CANCELLED:
                raise CancelledError()
            future._autofocus_state = FINISHED


def _CancelAutoFocus(future: model.ProgressiveFuture) -> bool:
    """
    Canceller of AutoFocus task.
    """
    logging.debug("Cancelling autofocus...")

    with future._autofocus_lock:
        if future._autofocus_state == FINISHED:
            return False
        future._autofocus_state = CANCELLED
        logging.debug("Autofocus cancellation requested.")

    return True


def estimateAcquisitionTime(
        detector: model.Detector,
        scanner: Optional[model.Emitter] = None
) -> float:
    """
    Estimate how long one acquisition will take
    detector: Detector on which to improve the focus quality
    scanner: In case of a SED this is the scanner used
    return (0<float): time in s
    """
    # Check if there is a scanner (focusing = SEM)
    if model.hasVA(scanner, "dwellTime"):
        et = scanner.dwellTime.value * numpy.prod(scanner.resolution.value)
    elif model.hasVA(detector, "exposureTime"):
        et = detector.exposureTime.value
        # TODO: also add readoutRate * resolution if present
    else:
        # Completely random... but we are in a case where probably that's the last
        # thing the caller will care about.
        et = 1

    return et


def estimateAutoFocusTime(
        detector: model.Detector,
        emt: Optional[model.Emitter],
        focus: model.Actuator,
        dfbkg: Optional[model.DataFlow] = None,
        good_focus: Optional[float] = None,
        rng_focus: Optional[Tuple[float, float]] = None,
        method: int = MTD_BINARY
) -> float:
    """
    Estimates autofocus procedure duration.
    For the input parameters, see AutoFocus function docstring
    :return: time in seconds
    """
    # adjust to rng_focus if provided
    rng = focus.axes["z"].range
    if rng_focus:
        rng = (max(rng[0], rng_focus[0]), min(rng[1], rng_focus[1]))
    distance = rng[1] - rng[0]
    # Optimally, the focus starts from middle to minimum, then maximum. Then it goes back to the middle.
    # optimistic guess
    move_time = guessActuatorMoveDuration(focus, "z", distance) + 2 * guessActuatorMoveDuration(focus, "z",
                                                                                                distance / 2)
    # pessimistic guess
    acquisition_time = MAX_STEPS_NUMBER * estimateAcquisitionTime(detector, emt)
    return move_time + acquisition_time


def Sparc2AutoFocus(
        align_mode: str,
        opm: 'OpticalPathManager',
        streams: Optional[List['Stream']] = None,
        start_autofocus: bool = True
) -> model.ProgressiveFuture:
    """
    It provides the ability to check the progress of the complete Sparc2 autofocus
    procedure in a Future or even cancel it.
        Pick the hardware components
        Turn on the light and wait for it to be complete
        Change the optical path (closing the slit)
        Run AutoFocusSpectrometer
        Acquire one last image
        Turn off the light
    align_mode: The optical path mode for which the spectrograph focus should be optimized.
    This automatically defines the spectrograph to use and the detectors.
    Possible values are: "spec-focus", "spec-focus-ext", "streak-focus", "streak-focus-ext", and
    "spec-fiber-focus".
    opm: the optical path manager to move the actuators to the correct positions.
    streams: list of streams. The first stream is used for displaying the last
       image with the slit closed.
    start_autofocus: if True, the autofocus procedure will be executed
    return (ProgressiveFuture -> dict((grating, detector)->focus position)): a progressive future
          which will eventually return a map of grating/detector -> focus position, the same as AutoFocusSpectrometer
    raises:
            CancelledError if cancelled
            LookupError if procedure failed
    """
    if streams is None:
        streams = []

    focuser = _findSparc2Focuser(align_mode)

    for s in streams:
        if s.focuser is None:
            logging.debug("Stream %s has no focuser, will assume it's fine", s)
        elif s.focuser != focuser:
            logging.warning("Stream %s has focuser %s, while expected %s", s, s.focuser, focuser)

    # Get all the detectors, spectrograph and selectors affected by the focuser
    try:
        spgr, dets, selector, bl = _getSpectrometerFocusingComponents(focuser)
    except LookupError as ex:
        # TODO: just run the standard autofocus procedure instead?
        raise LookupError("Failed to focus in mode %s: %s" % (align_mode, ex))

    for s in streams:
        if s.detector.role not in (d.role for d in dets):
            logging.warning("The detector of the stream is not found to be one of the picked detectors %s")

    # Create ProgressiveFuture and update its state to RUNNING
    est_start = time.time() + 0.1

    # Rough approximation of the times of each action:
    # * 5 s to turn on the light
    # * 5 s to close the slit
    # * af_time s for the AutoFocusSpectrometer procedure to be completed
    # * 0.2 s to acquire one last image
    # * 0.1 s to turn off the light
    if start_autofocus:
        # calculate the time needed for the AutoFocusSpectrometer procedure to be completed
        af_time = _totalAutoFocusTime(spgr, focuser, dets, selector, streams)
        autofocus_loading_times = (5, 5, af_time, 0.2, 5)  # a list with the time that each action needs
    else:
        autofocus_loading_times = (5, 5)

    f = model.ProgressiveFuture(start=est_start, end=est_start + sum(autofocus_loading_times))
    f._autofocus_state = RUNNING
    # Time for each action left
    f._actions_time = list(autofocus_loading_times)
    f.task_canceller = _CancelSparc2AutoFocus
    f._autofocus_lock = threading.Lock()
    f._running_subf = model.InstantaneousFuture()

    # Run in separate thread
    executeAsyncTask(f, _DoSparc2AutoFocus,
                     args=(f, streams, align_mode, opm, dets, spgr, selector, bl, focuser, start_autofocus))
    return f


def _cancelSparc2ManualFocus(future: model.ProgressiveFuture) -> bool:
    """
    Canceller of _DoSparc2ManualFocus task.
    """
    logging.debug("Cancelling manual focus...")
    if future._state == FINISHED:
        return False
    future._state = CANCELLED
    return True


def Sparc2ManualFocus(
        opm: 'OpticalPathManager',
        align_mode: str,
        toggled: bool = True
) -> model.ProgressiveFuture:
    """
    Provides the ability to check the progress of the Sparc2 manual focus
    procedure in a Future or even cancel it.
    :param opm: OpticalPathManager object
    :param align_mode: OPM mode, spec-focus or spec-fiber-focus, streak-focus, spec-focus-ext
    :param mf_toggled: Toggle the manual focus button on/off
    :return (ProgressiveFuture -> for the _DoSparc2ManualFocus function)
    """
    focuser = _findSparc2Focuser(align_mode)

    # Find all the hardware affected by the focuser... although we only care about the brightlight
    try:
        spgr, _, _, bl = _getSpectrometerFocusingComponents(focuser)
    except LookupError as ex:
        logging.warning("Failed to find all the components for focusing mode %s, will just use the brightlight: %s",
                        align_mode, ex)
        # It's correct most of the time
        bl = model.getComponent(role="brightlight")

    est_start = time.time() + 0.1
    manual_focus_loading_time = 10  # Rough estimation of the slit movement
    f = model.ProgressiveFuture(start=est_start, end=est_start + manual_focus_loading_time)
    # The only goal for using a canceller is to make the progress bar stop
    # as soon as it's cancelled.
    f.task_canceller = _cancelSparc2ManualFocus
    executeAsyncTask(f, _DoSparc2ManualFocus, args=(opm, spgr, bl, align_mode, toggled))
    return f


def _DoSparc2ManualFocus(
        opm: 'OpticalPathManager',
        spgr: model.Actuator,
        bl: model.Emitter,
        align_mode: str,
        toggled: bool = True
) -> None:
    """
    The actual implementation of the manual focus procedure, run asynchronously
    When the manual focus button is toggled:
            - Turn on the light
            - Change the optical path (closing the slit)
    :param opm: OpticalPathManager object
    :param spgr: spectrograph
    :param bl: brightlight object
    :param align_mode: OPM mode, spec-focus or spec-fiber-focus, streak-focus, spec-focus-ext
    :param toggled (bool): Toggle the manual focus button on/off
    """
    if toggled:
        # Go to the special focus mode (=> close the slit)
        f = opm.setPath(align_mode)
        bl.power.value = bl.power.range[1]
        f.result()
    else:
        # Don't change the optical path (to the previous position), this it's up to the caller
        bl.power.value = bl.power.range[0]


def GetSpectrometerFocusingDetectors(
        focuser: model.Actuator
) -> List[model.Detector]:
    """
    Public wrapper around _getSpectrometerFocusingComponents to return detectors only
    :param focuser: the focuser that will be used to change focus
    :return: detectors: the detectors attached on the
          spectrograph, which can be used for focusing
    """
    dets = []
    for n in focuser.affects.value:
        try:
            d = model.getComponent(name=n)
        except LookupError:
            logging.info("Focuser affects non-existing component %s", n)
            continue
        if d.role.startswith("ccd") or d.role.startswith("sp-ccd"):  # catches ccd*, sp-ccd*
            dets.append(d)
    return dets


def _findSparc2Focuser(align_mode: str) -> model.Actuator:
    """
    Find the correct focus actuator for the given alignment mode, based on the microscope model.
    :param align_mode: see Sparc2AutoFocus
    :return: the focuser
    :raise: LookupError if the focuser cannot be found
    """
    if align_mode == "spec-focus":
        focuser = model.getComponent(role='focus')
    elif align_mode == "spec-focus-ext":
        focuser = model.getComponent(role='spec-ded-focus')
    elif align_mode == "streak-focus":
        focuser = model.getComponent(role='focus')
    elif align_mode == "streak-focus-ext":
        focuser = model.getComponent(role='spec-ded-focus')
    elif align_mode == "spec-fiber-focus":
        # The "right" focuser is the one which affects the same detectors as the fiber-aligner
        aligner = model.getComponent(role='fiber-aligner')
        aligner_affected = aligner.affects.value  # List of component names
        focuser = _findSameAffects(("spec-ded-focus", "focus"), aligner_affected)
    else:
        raise ValueError("Unknown align_mode %s" % (align_mode,))

    if focuser is None:
        raise LookupError("Failed to find the focuser for align mode %s" % (align_mode,))

    return focuser


def _getSpectrometerFocusingComponents(focuser: model.Actuator) -> Tuple[
    model.Actuator, List[model.Detector], Optional[model.Actuator],
    model.Emitter
]:
    """
    Finds the different components needed to run auto-focusing with the
    given focuser.
    :param focuser: the focuser that will be used to change focus
    return:
        * spectrograph: component to move the grating and wavelength
        * detectors: the detectors attached on the
          spectrograph, which can be used for focusing
        * selector: the component to switch detectors
        * brightlight: the light source to be turned on during focusing
    raise LookupError: if not all the components could be found
    """
    dets = GetSpectrometerFocusingDetectors(focuser)
    if not dets:
        raise LookupError("Failed to find any detector for the spectrometer focusing")

    # The order doesn't matter for SpectrometerAutofocus, but the first detector
    # is used for detecting the light is on. In addition it's nice to be reproducible.
    # => Use alphabetical order of the roles
    dets.sort(key=lambda c: c.role)

    det_names = [d.name for d in dets]

    # Get the spectrograph and selector based on the fact they affect the
    # same detectors.
    spgr = _findSameAffects(("spectrograph", "spectrograph-dedicated"), det_names)

    # Only need the selector if there are several detectors
    if len(dets) <= 1:
        selector = None  # we can keep it simple
    else:
        selector = _findSameAffects(("spec-det-selector", "spec-ded-det-selector"), det_names)

    bl = _findSameAffects(("brightlight", "brightlight-ext"), [det_names[0]])

    return spgr, dets, selector, bl


def _findSameAffects(roles: List[str], affected: List[str]) -> model.Component:
    """
    Find a component that affects *all* the given components
    :param roles: list of component's roles in which to look for the "affecter". The first one that
    matches will be returned. It's allowed to pass a role which is not present in the model.
    :param affected: the name of the affected components
    :return: the first component that affects all the affected
    :raise: LookupError, if no component found
    """
    affected = frozenset(affected)
    for r in roles:
        try:
            c = model.getComponent(role=r)
        except LookupError:
            logging.debug("No component with role %s found", r)
            continue
        if affected <= set(c.affects.value):
            return c
    else:
        raise LookupError(f"Failed to find a component within {roles} that affects all {affected}")


def _DoSparc2AutoFocus(
        future: model.ProgressiveFuture,
        streams: List['Stream'],
        align_mode: str,
        opm: 'OpticalPathManager',
        dets: List[model.Detector],
        spgr: model.Actuator,
        selector: Optional[model.Actuator],
        bl: model.Emitter,
        focuser: model.Actuator,
        start_autofocus: bool = True
) -> Optional[Dict[Any, Any]]:
    """
        cf Sparc2AutoFocus
        return dict((grating, detector) -> focus pos)
    """

    def updateProgress(subf, start, end):
        """
        Updates the time progress when the current subfuture updates its progress
        """
        # if the future is complete, the standard progress update will work better
        if not subf.done():
            future.set_progress(end=end + sum(future._actions_time))

    try:
        if future._autofocus_state == CANCELLED:
            logging.info("Autofocus procedure cancelled before the light is on")
            raise CancelledError()

        logging.debug("Turning on the light")

        # Make sure it's in 0th order (ie, show the image as-is). This also ensures the spectrograph
        # is done with any previous actions.
        spgr.moveAbsSync({"wavelength": 0})

        _playStream(dets[0], streams)
        future._running_subf = light.turnOnLight(bl, dets[0])
        try:
            future._running_subf.result(timeout=60)
        except TimeoutError:
            future._running_subf.cancel()
            logging.warning("Light doesn't appear to have turned on after 60s, will try focusing anyway")
        if future._autofocus_state == CANCELLED:
            logging.info("Autofocus procedure cancelled after turning on the light")
            raise CancelledError()
        future._actions_time.pop(0)
        future.set_progress(end=time.time() + sum(future._actions_time))

        # Configure the optical path to the specific focus mode, for the detector
        # (so that the path manager knows which component matters). In case of
        # multiple detectors, any of them should be fine, as the only difference
        # should be the selector, which AutoFocusSpectrometer() takes care of.
        logging.debug("Adjusting the optical path to %s", align_mode)
        future._running_subf = opm.setPath(align_mode, detector=dets[0])
        future._running_subf.result()
        if future._autofocus_state == CANCELLED:
            logging.info("Autofocus procedure cancelled after closing the slit")
            raise CancelledError()
        future._actions_time.pop(0)
        future.set_progress(end=time.time() + sum(future._actions_time))

        # In case autofocus is manual return
        if not start_autofocus:
            return None

        # Configure each detector with good settings
        for d in dets:
            # The stream takes care of configuring its detector, so no need
            # In case there is no streams for the detector, take the binning and exposureTime values as far as they
            # exist
            if not any(s.detector.role == d.role for s in streams):
                binning = 1, 1
                if model.hasVA(d, "binning"):
                    d.binning.value = d.binning.clip((2, 2))
                    binning = d.binning.value
                if model.hasVA(d, "exposureTime"):
                    # 0.2 s tends to be good for most cameras, but need to compensate
                    # if binning is smaller
                    exp = 0.2 * ((2 * 2) / numpy.prod(binning))
                    d.exposureTime.value = d.exposureTime.clip(exp)
        ret = {}
        logging.debug("Running AutoFocusSpectrometer on %s, using %s, for the detectors %s, and using selector %s",
                      spgr, focuser, dets, selector)

        try:
            future._running_subf = AutoFocusSpectrometer(spgr, focuser, dets, selector, streams)
            et = future._actions_time.pop(0)
            future._running_subf.add_update_callback(updateProgress)
            ret = future._running_subf.result(timeout=3 * et + 10)
        except TimeoutError:
            future._running_subf.cancel()
            logging.error("Timeout for autofocus spectrometer after %g s", et)
        except IOError:
            if future._autofocus_state == CANCELLED:
                raise CancelledError()
            raise
        if future._autofocus_state == CANCELLED:
            logging.info("Autofocus procedure cancelled after the completion of the autofocus")
            raise CancelledError()
        future.set_progress(end=time.time() + sum(future._actions_time))

        logging.debug("Acquiring the last image")
        if streams:
            _playStream(streams[0].detector, streams)
            # Ensure the latest image shows the slit focused
            streams[0].detector.data.get(asap=False)
            # pause the streams
            streams[0].is_active.value = False
        if future._autofocus_state == CANCELLED:
            logging.info("Autofocus procedure cancelled after acquiring the last image")
            raise CancelledError()
        future._actions_time.pop(0)
        future.set_progress(end=time.time() + sum(future._actions_time))

        logging.debug("Turning off the light")
        bl.power.value = bl.power.range[0]
        if future._autofocus_state == CANCELLED:
            logging.warning("Autofocus procedure is cancelled after turning off the light")
            raise CancelledError()
        future._actions_time.pop(0)
        future.set_progress(end=time.time() + sum(future._actions_time))

        return ret

    except CancelledError:
        logging.debug("DoSparc2AutoFocus cancelled")
    finally:
        # Make sure the light is always turned off, even if cancelled/error half-way
        if start_autofocus:
            try:
                bl.power.value = bl.power.range[0]
            except:
                logging.exception("Failed to turn off the light")

        with future._autofocus_lock:
            if future._autofocus_state == CANCELLED:
                raise CancelledError()
            future._autofocus_state = FINISHED


def _CancelSparc2AutoFocus(future: model.ProgressiveFuture) -> bool:
    """
    Canceller of _DoSparc2AutoFocus task.
    """
    logging.debug("Cancelling autofocus...")

    with future._autofocus_lock:
        if future._autofocus_state == FINISHED:
            return False
        future._autofocus_state = CANCELLED
        future._running_subf.cancel()
        logging.debug("Sparc2AutoFocus cancellation requested.")

    return True


def AutoFocus(
        detector: model.Detector,
        emt: Optional[model.Emitter],
        focus: model.Actuator,
        dfbkg: Optional[model.DataFlow] = None,
        good_focus: Optional[float] = None,
        rng_focus: Optional[Tuple[float, float]] = None,
        method: int = MTD_BINARY,
        measure_func: Optional[Callable] = None
) -> model.ProgressiveFuture:
    """
    Wrapper for DoAutoFocus. It provides the ability to check the progress of autofocus
    procedure or even cancel it.
    detector: Detector on which to
      improve the focus quality
    emt: In case of a SED this is the scanner used
    focus: The focus actuator
    dfbkg: If provided, will be used to start/stop
     the e-beam emission (it must be the dataflow of se- or bs-detector) in
     order to do background subtraction. If None, no background subtraction is
     performed.
    good_focus: if provided, an already known good focus position to be
      taken into consideration while autofocusing
    rng_focus: if provided, the search of the best focus position is limited
      within this range
    method (MTD_*): focusing method, if BINARY we follow a dichotomic method while in
      case of EXHAUSTIVE we iterate through the whole provided range
    measure_func: function to measure the focus level on the image,
      for instance MeasureSEMFocus. If None the focus metric used is based
      on the detector.
    returns:  Progress of DoAutoFocus, whose result() will return:
            Focus position (m)
            Focus level
    """
    # Create ProgressiveFuture and update its state to RUNNING
    est_start = time.time() + 0.1
    f = model.ProgressiveFuture(start=est_start,
                                end=est_start + estimateAutoFocusTime(detector, emt, focus, dfbkg, good_focus,
                                                                      rng_focus))
    f._autofocus_state = RUNNING
    f._autofocus_lock = threading.Lock()
    f.task_canceller = _CancelAutoFocus

    # Run in separate thread
    if method == MTD_EXHAUSTIVE:
        autofocus_fn = _DoExhaustiveFocus
    elif method == MTD_BINARY:
        autofocus_fn = _DoBinaryFocus
    else:
        raise ValueError("Unknown autofocus method")

    executeAsyncTask(f, autofocus_fn,
                     args=(f, detector, emt, focus, dfbkg, good_focus, rng_focus, measure_func))
    return f


def AutoFocusSpectrometer(
        spectrograph: model.Actuator,
        focuser: model.Actuator,
        detectors: Union[model.Detector, List[model.Detector]],
        selector: Optional[model.Actuator] = None,
        streams: Optional[List['Stream']] = None
) -> model.ProgressiveFuture:
    """
    Run autofocus for a spectrograph. It will actually run autofocus on each
    gratings, and for each detectors. The input slit should already be in a
    good position (typically, almost closed), and a light source should be
    active.
    Note: it's currently tailored to the Andor Shamrock SR-193i. It's recommended
    to put the detector on the "direct" output as first detector.
    spectrograph: should have grating and wavelength.
    focuser: should have a z axis
    detectors: all the detectors available on
      the spectrometer.
    selector: must have a rx axis with each position corresponding
     to one of the detectors. If there is only one detector, selector can be None.
    return (ProgressiveFuture -> dict((grating, detector)->focus position)): a progressive future
      which will eventually return a map of grating/detector -> focus position.
    """
    if not isinstance(detectors, Iterable):
        detectors = [detectors]
    if not detectors:
        raise ValueError("At least one detector must be provided")
    if len(detectors) > 1 and selector is None:
        raise ValueError("No selector provided, but multiple detectors")

    if streams is None:
        streams = []

    # Create ProgressiveFuture and update its state to RUNNING
    est_start = time.time() + 0.1
    # calculate the time for the AutoFocusSpectrometer procedure to be completed
    a_time = _totalAutoFocusTime(spectrograph, focuser, detectors, selector, streams)
    f = model.ProgressiveFuture(start=est_start, end=est_start + a_time)
    f.task_canceller = _CancelAutoFocusSpectrometer
    # Extra info for the canceller
    f._autofocus_state = RUNNING
    f._autofocus_lock = threading.Lock()
    f._subfuture = InstantaneousFuture()
    # Run in separate thread
    executeAsyncTask(f, _DoAutoFocusSpectrometer, args=(f, spectrograph, focuser, detectors, selector, streams))
    return f


# Rough time estimation for movements
MOVE_TIME_GRATING = 20  # s
MOVE_TIME_DETECTOR = 5  # , for the detector selector


def _totalAutoFocusTime(
        spectrograph: model.Actuator,
        focuser: model.Actuator,
        detectors: List[model.Detector],
        selector: Optional[model.Actuator],
        streams: List['Stream']
) -> float:
    ngs = len(spectrograph.axes["grating"].choices)
    nds = len(detectors)
    et = estimateAutoFocusTime(detectors[0], None, focuser)

    # 1 time for each grating/detector combination, with the gratings changing slowly
    move_et = ngs * MOVE_TIME_GRATING if ngs > 1 else 0
    move_et += (ngs * (nds - 1) + (1 if nds > 1 else 0)) * MOVE_TIME_DETECTOR

    return (ngs * nds) * et + move_et


def _updateAFSProgress(
        future: model.ProgressiveFuture,
        af_dur: float,
        grating_moves: int,
        detector_moves: int
) -> None:
    """
    Update the progress of the future based on duration of the previous autofocus
    future
    af_dur: total duration of the next autofocusing actions (> 0)
    grating_moves: number of grating moves left to do (>= 0)
    detector_moves: number of detector moves left to do (>= 0)
    """
    tleft = af_dur + grating_moves * MOVE_TIME_GRATING + detector_moves * MOVE_TIME_DETECTOR
    future.set_progress(end=time.time() + tleft)


def CLSpotsAutoFocus(
        detector: model.Detector,
        focus: model.Actuator,
        good_focus: Optional[float] = None,
        rng_focus: Optional[Tuple[float, float]] = None,
        method: int = MTD_EXHAUSTIVE
) -> model.ProgressiveFuture:
    """
    Wrapper for do auto focus for CL spots. It provides the ability to check the progress of the CL spots auto focus
    procedure in a Future or even cancel it.

    detector: Detector on which to improve the focus quality. Should have the
            role diagnostic-ccd.
    focus: The focus actuator.
    good_focus: if provided, an already known good focus position to be
            taken into consideration while autofocusing.
    rng_focus: if provided, the search of the best focus position is limited within this range.
    method: if provided, the search of the best focus position is limited within this range.
    returns: Progress of DoAutoFocus, whose result() will return:
        Focus position (m)
        Focus level
    """
    detector.exposureTime.value = 0.01
    return AutoFocus(detector, None, focus, good_focus=good_focus, rng_focus=rng_focus, method=method)


def _mapDetectorToSelector(
        selector: model.Actuator,
        detectors: List[model.Detector]
) -> Tuple[str, Dict[str, Any]]:
    """
    Maps detector to selector positions
    returns:
       axis: the selector axis to use
       position_map: detector name -> selector position
    """
    # We pick the right axis by assuming that it's the only one which has
    # choices, and the choices are a dict pos -> detector name.
    # TODO: handle every way of indicating affect position in acq.path? -> move to odemis.util
    det_2_sel = {}
    sel_axis = None
    for an, ad in selector.axes.items():
        if hasattr(ad, "choices") and isinstance(ad.choices, dict):
            sel_axis = an
            for pos, value in ad.choices.items():
                for d in detectors:
                    if d.name in value:
                        # set the position so it points to the target
                        det_2_sel[d] = pos

            if det_2_sel:
                # Found an axis with names of detectors, that should be the
                # right one!
                break

    if len(det_2_sel) < len(detectors):
        raise ValueError("Failed to find all detectors (%s) in positions of selector axes %s" %
                         (", ".join(d.name for d in detectors), list(selector.axes.keys())))

    return sel_axis, det_2_sel


def _playStream(
        detector: model.Detector,
        streams: List['Stream']
) -> None:
    """
    It first pauses the streams and then plays only the stream related to the corresponding detector
    detector: detector from which the image is acquired
    streams: list of streams
    """
    # First pause all the streams
    for s in streams:
        if s.detector.role != detector.role:
            s.is_active.value = False
            s.should_update.value = False

    # After all the streams are paused, play only the steam that is related to the detector
    for s in streams:
        if s.detector.role == detector.role:
            s.should_update.value = True
            s.is_active.value = True
            break


def _DoAutoFocusSpectrometer(
        future: model.ProgressiveFuture,
        spectrograph: model.Actuator,
        focuser: model.Actuator,
        detectors: List[model.Detector],
        selector: Optional[model.Actuator],
        streams: List['Stream']
) -> Optional[Dict[Any, Any]]:
    """
    cf AutoFocusSpectrometer
    return dict((grating, detector) -> focus pos)
    """
    ret = {}
    # Record the wavelength and grating position
    pos_orig = {k: v for k, v in spectrograph.position.value.items()
                if k in ("wavelength", "grating")}
    gratings = list(spectrograph.axes["grating"].choices.keys())
    if selector:
        sel_orig = selector.position.value
        sel_axis, det_2_sel = _mapDetectorToSelector(selector, detectors)

    def is_current_det(d):
        """
        return bool: True if the given detector is the current one selected by
          the selector.
        """
        if selector is None:
            return True
        return det_2_sel[d] == selector.position.value[sel_axis]

    # Note: this procedure works well with the SR-193i. In particular, it
    # records the focus position for each grating and detector.
    # It needs to be double checked if used with other spectrographs.
    if "Shamrock" not in spectrograph.hwVersion:
        logging.warning("Spectrometer autofocusing has not been tested on"
                        "this type of spectrograph (%s)", spectrograph.hwVersion)

    # In theory, it should be "safe" to only find the right focus once for each
    # grating (for a given detector), and once for each detector (for a given
    # grating). The focus for the other combinations grating/ detectors should
    # be grating + detector offset. However, currently the spectrograph API
    # doesn't allow to explicitly set these values. As in the worse case so far,
    # the spectrograph has only 2 gratings and 2 detectors, it's simpler to just
    # run the autofocus a 4th time.

    # For progress update
    ngs = len(gratings)
    nds = len(detectors)
    cnts = ngs * nds
    ngs_moves = ngs if ngs > 1 else 0
    nds_moves = (ngs * (nds - 1) + (1 if nds > 1 else 0))
    try:
        if future._autofocus_state == CANCELLED:
            raise CancelledError()

        # We "scan" in two dimensions: grating + detector. Grating is the "slow"
        # dimension, as it's typically the move that takes the most time (eg, 20s).

        # Start with the current grating, to save time
        gratings.sort(key=lambda g: 0 if g == pos_orig["grating"] else 1)
        for g in gratings:
            # Start with the current detector
            dets = sorted(detectors, key=is_current_det, reverse=True)
            for d in dets:
                logging.debug("Autofocusing on grating %s, detector %s", g, d.name)
                if selector:
                    if selector.position.value[sel_axis] != det_2_sel[d]:
                        nds_moves = max(0, nds_moves - 1)
                    selector.moveAbsSync({sel_axis: det_2_sel[d]})
                try:
                    if spectrograph.position.value["grating"] != g:
                        ngs_moves = max(0, ngs_moves - 1)
                    # 0th order is not absolutely necessary for focusing, but it
                    # typically gives the best results
                    spectrograph.moveAbsSync({"wavelength": 0, "grating": g})
                except Exception:
                    logging.exception("Failed to move to 0th order for grating %s", g)

                if future._autofocus_state == CANCELLED:
                    raise CancelledError()

                tstart = time.time()
                # Note: we could try to reuse the focus position from the previous
                # grating or detector, and pass it as good_focus, to save a bit
                # of time. However, if for some reason the previous value was
                # way off (eg, because it's a simulated detector, or there is
                # something wrong with the grating), it might prevent this run
                # from finding the correct value.
                _playStream(d, streams)
                future._subfuture = AutoFocus(d, None, focuser)
                fp, flvl, _ = future._subfuture.result()
                ret[(g, d)] = fp
                cnts -= 1
                _updateAFSProgress(future, (time.time() - tstart) * cnts, ngs_moves, nds_moves)

                if future._autofocus_state == CANCELLED:
                    raise CancelledError()

        return ret
    except CancelledError:
        logging.debug("AutofocusSpectrometer cancelled")
    finally:
        spectrograph.moveAbsSync(pos_orig)
        if selector:
            selector.moveAbsSync(sel_orig)
        with future._autofocus_lock:
            if future._autofocus_state == CANCELLED:
                raise CancelledError()
            future._autofocus_state = FINISHED


def _CancelAutoFocusSpectrometer(future: model.ProgressiveFuture) -> bool:
    """
    Canceller of _DoAutoFocus task.
    """
    logging.debug("Cancelling autofocus...")

    with future._autofocus_lock:
        if future._autofocus_state == FINISHED:
            return False
        future._autofocus_state = CANCELLED
        future._subfuture.cancel()
        logging.debug("AutofocusSpectrometer cancellation requested.")

    return True
