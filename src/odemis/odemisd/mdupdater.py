# -*- coding: utf-8 -*-
'''
Created on 20 Aug 2012

@author: Éric Piel

Copyright © 2012-2014 Éric Piel, Delmic

This file is part of Odemis.

Odemis is free software: you can redistribute it and/or modify it under the terms
of the GNU General Public License version 2 as published by the Free Software
Foundation.

Odemis is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY;
without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR
PURPOSE. See the GNU General Public License for more details.

You should have received a copy of the GNU General Public License along with
Odemis. If not, see http://www.gnu.org/licenses/.
'''
import collections
import itertools
import logging
from typing import Optional, Tuple, Dict, Union, List

from odemis import model, util


class MetadataUpdater(model.Component):
    '''
    Takes care of updating the metadata of detectors, based on the physical
    attributes of other components in the system.
    This implementation is specific to microscopes.
    '''
    # This is kept in a separate module from the main backend because it has to
    # know the business semantic.

    def __init__(self, name, microscope, **kwargs):
        '''
        microscope (model.Microscope): the microscope to observe and update
        '''
        model.Component.__init__(self, name, **kwargs)

        # Warning: for efficiency, we want to run in the same container as the back-end
        # but this means the back-end is not running yet when we are created
        # so we cannot access the back-end.
        self._mic = microscope

        # keep list of already accessed components, to avoid creating new proxys
        # every time the mode changes
        self._known_comps = dict()  # str (name) -> component

        # list of 2-tuples (function, *arg): to be called on terminate
        self._onTerminate = []
        # All the components already observed
        # str -> set of str: name of affecting component -> names of affected
        self._observed = collections.defaultdict(set)

        # To handle monochromator (aka detectors), which are affected both by the spectrograph and the filter
        self._det_to_spectrograph : Dict[str, model.HwComponent] = {}  # Detector name -> Spectrograph that affects that detector
        self._det_to_filter : Dict[str, model.HwComponent] = {}  # Detector name -> Filter that affects that detector

        microscope.alive.subscribe(self._onAlive, init=True)

    def _getComponent(self, name):
        """
        same as model.getComponent, but optimised by caching the result
        return Component
        raise LookupError: if no component found
        """
        try:
            comp = self._known_comps[name]
        except LookupError:
            comp = model.getComponent(name=name)
            self._known_comps[name] = comp

        return comp

    def _onAlive(self, components):
        """
        Called when alive is changed => some component started or died
        """
        # For each component
        # For each component it affects
        # Subscribe to the changes of the attributes that matter
        for a in components:  # component in components of microscope
            for dn in a.affects.value:
                # TODO: if component not alive yet, wait for it
                try:
                    d = self._getComponent(dn)  # get components affected when changing the value of a
                except LookupError:
                    # TODO: stop subscriptions if the component was there (=> just died)
                    self._observed[a.name].discard(dn)
                    continue
                else:
                    if dn in self._observed[a.name]:
                        # already subscribed
                        continue

                if a.role == "stage":
                    # update the image position
                    observed = self.observeStage(a, d)
                elif a.role == "lens":
                    # update the pixel size, mag, and pole position
                    observed = self.observeLens(a, d)
                elif a.role == "light":
                    # update the emitted light wavelength
                    observed = self.observeLight(a, d)
                elif a.role and a.role.startswith("spectrograph"):  # spectrograph-XXX too
                    self._det_to_spectrograph[dn] = a
                    # update the output wavelength range
                    observed = self.observeSpectrograph(a, d)
                elif a.role in ("cl-filter", "filter"):
                    self._det_to_filter[dn] = a
                    # update the output wavelength range
                    observed = self.observeFilter(a, d)
                elif a.role == "quarter-wave-plate":
                    # update the position of the qwp in the polarization analyzer
                    observed = self.observeQWP(a, d)
                elif a.role == "lin-pol":
                    # update the position of the linear polarizer in the polarization analyzer
                    observed = self.observeLinPol(a, d)
                elif a.role == "pol-analyzer":
                    # update the position of the polarization analyzer
                    observed = self.observePolAnalyzer(a, d)
                elif a.role == "streak-lens":
                    # update the magnification of the streak lens
                    observed = self.observeStreakLens(a, d)
                elif a.role == "streak-delay":
                    # update the trigger related settings
                    observed = self.observeStreakDelay(a, d)
                elif a.role == "e-beam":
                    observed = self.observeEbeam(a, d)
                else:
                    observed = False

                if observed:
                    logging.info("Observing affect %s -> %s", a.name, dn)
                else:
                    logging.info("Not observing unhandled affect %s (%s) -> %s (%s)",
                                 a.name, a.role, dn, d.role)

                self._observed[a.name].add(dn)

        # TODO: drop subscriptions to dead components

# Note: The scope of variables is redefined in the nested/local function
# to ensure that the correct variables are used and were not overwritten
# before calling the function. That's why all the local functions are written
# with extra arguments such as "a=a" (for more info on this issue see:
# https://eev.ee/blog/2011/04/24/gotcha-python-scoping-closures/)

    def observeStage(self, stage, comp_affected):
        """
        return bool: True if will actually update the affected component,
                     False if the affect is not supported (here)
        """

        # we need to keep the information on the detector to update
        def updateStagePos(pos, comp_affected=comp_affected):
            # We need axes X and Y
            if "x" not in pos or "y" not in pos:
                logging.warning("Stage position doesn't contain X/Y axes")
            # if unknown, just assume a fixed position
            x = pos.get("x", 0)
            y = pos.get("y", 0)
            md = {model.MD_POS: (x, y)}
            logging.debug("Updating position for component %s, to %f, %f",
                          comp_affected.name, x, y)
            comp_affected.updateMetadata(md)

        stage.position.subscribe(updateStagePos, init=True)
        self._onTerminate.append((stage.position.unsubscribe, (updateStagePos,)))

        return True

    def observeLens(self, lens, comp_affected):
        # Only update components with roles of ccd*, sp-ccd*, or laser-mirror*
        if not any(comp_affected.role.startswith(r) for r in ("ccd", "sp-ccd", "laser-mirror", "diagnostic-ccd")):
            return False

        # update static information
        md = {model.MD_LENS_NAME: lens.hwVersion}
        comp_affected.updateMetadata(md)

        # List of direct VA -> MD mapping
        md_va_list = {"numericalAperture": model.MD_LENS_NA,
                      "refractiveIndex": model.MD_LENS_RI,
                      "xMax": model.MD_AR_XMAX,
                      "holeDiameter": model.MD_AR_HOLE_DIAMETER,
                      "focusDistance": model.MD_AR_FOCUS_DISTANCE,
                      "parabolaF": model.MD_AR_PARABOLA_F,
                      "rotation": model.MD_ROTATION,
                     }

        # If it's a scanner (ie, it has a "scale"), the component will take care
        # by itself of updating .pixelSize and MD_PIXEL_SIZE depending on the
        # MD_LENS_MAG.
        # For DigitalCamera, the .pixelSize is the SENSOR_PIXEL_SIZE, and we
        # compute PIXEL_SIZE every time the LENS_MAG *or* binning change.
        if model.hasVA(comp_affected, "scale"):
            md_va_list["magnification"] = model.MD_LENS_MAG
        else:
            # TODO: instead of updating PIXEL_SIZE everytime the CCD changes binning,
            # just let the CCD component compute the value based on its sensor
            # pixel size + MAG, like for the scanners.
            if model.hasVA(comp_affected, "binning"):
                binva = comp_affected.binning
            else:
                logging.debug("No binning")
                binva = None

            # Depends on the actual size of the ccd's density (should be constant)
            captor_mpp = comp_affected.pixelSize.value  # m, m
            md = {model.MD_SENSOR_PIXEL_SIZE: captor_mpp}
            comp_affected.updateMetadata(md)

            # we need to keep the information on the detector to update
            def updatePixelDensity(unused, lens=lens, comp_affected=comp_affected, binva=binva):
                # unused: because it might be magnification or binning

                # the formula is very simple: actual MpP = CCD MpP * binning / Mag
                if binva is None:
                    binning = 1, 1
                else:
                    binning = binva.value
                mag = lens.magnification.value
                mpp = (captor_mpp[0] * binning[0] / mag, captor_mpp[1] * binning[1] / mag)
                md = {model.MD_PIXEL_SIZE: mpp,
                      model.MD_LENS_MAG: mag,
                      model.MD_BINNING: binning,}
                comp_affected.updateMetadata(md)

            lens.magnification.subscribe(updatePixelDensity, init=True)
            self._onTerminate.append((lens.magnification.unsubscribe, (updatePixelDensity,)))
            binva.subscribe(updatePixelDensity)
            self._onTerminate.append((binva.unsubscribe, (updatePixelDensity,)))

        # update metadata for VAs which can be directly copied
        for va_name, md_key in md_va_list.items():
            if model.hasVA(lens, va_name):

                # Create a different function for each metadata & component
                def updateMDFromVABin(val, md_key=md_key, comp_affected=comp_affected):
                    md = {md_key: val}
                    comp_affected.updateMetadata(md)

                logging.debug("Listening to VA %s.%s -> MD %s", lens.name, va_name, md_key)
                va = getattr(lens, va_name)
                va.subscribe(updateMDFromVABin, init=True)
                self._onTerminate.append((va.unsubscribe, (updateMDFromVABin,)))

        # List of VA -> MD mapping when the VA values have to be divided by the binning
        # VA name -> (MD name, binning index)
        md_va_binning_list = {
            "polePosition": (model.MD_AR_POLE, (0, 1)),
            "mirrorPositionTop": (model.MD_AR_MIRROR_TOP, (1, 1)),  # Use Y for both a & b values
            "mirrorPositionBottom": (model.MD_AR_MIRROR_BOTTOM, (1, 1)),  # Use Y for both a & b values
        }

        # update metadata for VAs which has to be divided by binning
        for va_name, (md_key, bin_idx) in md_va_binning_list.items():
            if model.hasVA(lens, va_name):
                va = getattr(lens, va_name)

                def updateMDFromVABin(_, va=va, md_key=md_key, bin_idx=bin_idx, comp_affected=comp_affected):
                    # the formula is: Pole = Pole_no_binning / binning
                    try:
                        binning = comp_affected.binning.value
                    except AttributeError:  # No binning VA => it means it's "1x1"
                        binning = 1, 1
                    val = va.value
                    val_bin = tuple(v / binning[bi] for v, bi in zip(val, bin_idx))
                    md = {md_key: val_bin}
                    comp_affected.updateMetadata(md)

                logging.debug("Listening to VA %s.%s -> MD %s", lens.name, va_name, md_key)
                va.subscribe(updateMDFromVABin, init=True)
                self._onTerminate.append((va.unsubscribe, (updateMDFromVABin,)))
                try:
                    comp_affected.binning.subscribe(updateMDFromVABin)
                    self._onTerminate.append((comp_affected.binning.unsubscribe, (updateMDFromVABin,)))
                except AttributeError:  # No binning VA => just don't subscribe to it
                    pass

        return True

    def observeLight(self, light, comp_affected):
        def updateLightPower(power, light=light, comp_affected=comp_affected):
            # MD_IN_WL expects just min/max => if multiple sources, we need to combine
            spectra = light.spectra.value
            wls = []
            for i, intens in enumerate(power):
                if intens > 0:
                    wls.append((spectra[i][0], spectra[i][-1]))

            if wls:
                wl_range = (min(w[0] for w in wls),
                            max(w[1] for w in wls))
            else:
                wl_range = (0, 0)

            md = {model.MD_IN_WL: wl_range, model.MD_LIGHT_POWER: sum(power)}
            comp_affected.updateMetadata(md)

        light.power.subscribe(updateLightPower, init=True)
        self._onTerminate.append((light.power.unsubscribe, (updateLightPower,)))

        return True

    def getMonochromatorBandwidth(self, spectrograph: Optional[model.HwComponent]) -> Optional[Tuple[float, float]]:
        """
        Get the bandwidth of the monochromator based on the spectrograph settings.
        :param spectrograph: a spectrograph component
        :return: the bandwidth of the monochromator (min, max) in m, or None if not available
        """
        if spectrograph is None:
            return None

        # For monochomators, we need to know the minimum and maximum wavelength
        # detected based on the spectrograph settings. => Update MD_OUT_WL
        pos = spectrograph.position.value
        if 'slit-monochromator' not in pos:
            logging.info("No 'slit-monochromator' axis was found, will not be able to compute monochromator bandwidth.")
            wl = pos["wavelength"]
            if wl < 10e-9:  # 0 (or very small value) indicates it's in 0th order mode => all light is passed
                return None
            return (wl, wl)
        else:
            width = pos['slit-monochromator']
            bandwidth = spectrograph.getOpeningToWavelength(width)  # always a tuple
            return bandwidth

    def get_filter_pos(self, filter: Optional[model.HwComponent]
                       ) -> Union[Tuple[float, float], List[Tuple[float, float]], str, None]:
        """
        Get the position of the filter based on the filter settings.
        :param filter: a (light) "filter" (wheel) component (should have a "band" axis)
        :return: the bandwidth of the filter (min, max) in m,
                 or multiple bandwidths in the filter (series of min/max) in m,
                 or a string if it's defined just as a name,
                 or None if there is no filter.
        """
        if filter is None:
            return None

        pos = filter.position.value
        return filter.axes["band"].choices[pos["band"]]

    def convert_filter_to_bandwidth(self, pos: Union[Tuple[float, float], List[Tuple[float, float]], str, None]
                                    ) -> Optional[Tuple[float, float]]:
        """
        Convert the position of a filter into a bandwidth.
        :param pos: the position of the filter
        :return: the bandwidth of the filter (min, max) in m,
                 or None if the position indicate there is no filter or is not interpretable
        """
        if pos is None or pos == model.BAND_PASS_THROUGH:  # "pass-through" == no filter
            return None
        if isinstance(pos, str):  # Sometimes the filter is just a name like "red", for now we don't handle that
            logging.info("Filter %s is not a range, assuming it let all light pass for computation",
                         pos)
            return None

        # Most commonly, it's a list of 2 floats (min, max), but it can also be more specific and
        # define more precisely the filter range, with more than 2 values.
        # It can also be multi-band filter, in which case, we simplify it to a range from the smallest
        # band to the largest band.
        if isinstance(pos, (tuple, list)):
            try:
                if all(isinstance(v, (float, int)) for v in pos):
                    bandwidth = pos[0], pos[-1]
                else:  # Assume it's a series of bands
                    # Bands are not always in order, so just take smallest and largest value
                    all_wavelengths = list(itertools.chain.from_iterable(pos))
                    bandwidth = min(all_wavelengths), max(all_wavelengths)
            except Exception as ex:
                logging.warning("Invalid filter value: %s: %s", pos, ex)
                return None

            return bandwidth

        logging.warning("Invalid filter value: %s", pos)
        return None

    def updateOutWavelength(self, comp_affected: model.HwComponent,
                            filter: Optional[model.HwComponent],
                            spectrograph: Optional[model.HwComponent]) -> None:
        """
        Computes MD_OUT_WL as intersection of the filter and the spectrograph configuration,
        and updates the metadata of the affected component.
        :param comp_affected: Detector component affected by the filter and the spectrograph.
        Its metadata will be updated.
        :param filter: a (light) filter-(wheel) component (should have a "band" axis)
        :param spectrograph: a spectrograph component (should have a "wavelength" axis)
        Only used if the detector has the role "monochromator".
        """
        filter_pos = self.get_filter_pos(filter)

        # We only need to care about the spectrograph in the case of the monochromator, because for
        # the other types of components (eg, spectrometer), MD_OUT_WL is used exclusively for the
        # filter info, and the MD_WL_LIST is used to store the wavelength info (handled separately).
        if comp_affected.role == "monochromator":
            spec_bandwidth = self.getMonochromatorBandwidth(spectrograph)  # None if wavelength == 0
        else:
            spec_bandwidth = None

        if spec_bandwidth is None:  # Standard case, for everything except monochromators
            bandwidth = filter_pos  # None, str or tuple
        else:
            filter_bandwidth = self.convert_filter_to_bandwidth(filter_pos)
            if filter_bandwidth is None:
                bandwidth = spec_bandwidth
            else:  # Both are a wavelength range => take the intersection
                bandwidth = (max(spec_bandwidth[0], filter_bandwidth[0]),
                             min(spec_bandwidth[1], filter_bandwidth[1]))
                if bandwidth[0] > bandwidth[1]:  # Empty intersection
                    # In theory, that means the detector receives no light. Maybe that's true,
                    # but it's not helpful to store as metadata. It might also be that the filter
                    # info is not correct. So let's just use the spectrograph bandwidth.
                    logging.warning("No intersection between filter %s and spectrograph %s, will use spectrograph bandwidth",
                                    filter_bandwidth, spec_bandwidth)
                    bandwidth = spec_bandwidth

                logging.debug("Updating %s with intersection of filter %s and spectrograph %s -> %s",
                              comp_affected.name, filter_bandwidth, spec_bandwidth, bandwidth)

        comp_affected.updateMetadata({model.MD_OUT_WL: bandwidth})

    def observeSpectrograph(self, spectrograph, comp_affected):

        if comp_affected.role == "monochromator":
            def updateOutWLRange(pos, sp=spectrograph, comp_affected=comp_affected):
                self.updateOutWavelength(comp_affected,
                                         self._det_to_filter.get(comp_affected.name),
                                         sp)

            spectrograph.position.subscribe(updateOutWLRange, init=True)
            self._onTerminate.append((spectrograph.position.unsubscribe, (updateOutWLRange,)))

        elif any(comp_affected.role.startswith(r) for r in ("ccd", "sp-ccd", "spectrometer")):
            # Is the affected component CCD or Spectrometer? => needs to get the
            # wavelength list from the spectrograph.
            # Every time any of these changes, we need to recompute MD_WL_LIST:
            # * ccd.resolution
            # * ccd.binning
            # * spectrograph.position (wavelength or grating)
            #
            # We update the wavelength list in background because the call to getPixelToWavelength()
            # can be slow, which would cause two issues if running in the main thread:
            # * other observers that depend on the same VAs (eg, MD_PIXEL_SIZE computed based on the
            #   binning) would be blocked until the wavelength list is computed
            # * change of multiple VAs at once (very typical) would end-up calling the function
            #   multiple times, uselessly.
            background_wl_updater = util.BackgroundWorker(discard_old=True)
            self._onTerminate.append((background_wl_updater.terminate, ()))

            # Use default arguments to store the content of spectrograph and
            # comp_affected as they are *right now*.
            def updateWavelengthList(sp=spectrograph, det=comp_affected):
                npixels = det.resolution.value[0]
                pxs = det.pixelSize.value[0] * det.binning.value[0]
                wll = sp.getPixelToWavelength(npixels, pxs)
                md = {model.MD_WL_LIST: wll}
                if "slit-in" in sp.position.value:
                    md[model.MD_INPUT_SLIT_WIDTH] = sp.position.value["slit-in"]
                det.updateMetadata(md)

            # Schedule metadata update whenever a VA changes
            def on_va_change(_):
                background_wl_updater.schedule_work(updateWavelengthList)

            comp_affected.resolution.subscribe(on_va_change)
            self._onTerminate.append((comp_affected.resolution.unsubscribe, (on_va_change,)))
            comp_affected.binning.subscribe(on_va_change)
            self._onTerminate.append((comp_affected.binning.unsubscribe, (on_va_change,)))
            spectrograph.position.subscribe(on_va_change, init=True)
            self._onTerminate.append((spectrograph.position.unsubscribe, (on_va_change,)))
        else:
            return False

        return True

    def observeFilter(self, filter, comp_affected):
        # update any affected component
        def updateOutWLRange(pos, fl=filter, comp_affected=comp_affected):
            spec = self._det_to_spectrograph.get(comp_affected.name)  # can be None
            self.updateOutWavelength(comp_affected, fl, spec)

            # apply lateral chromatic correction to align with the reference channel
            apply_transform = fl.getMetadata().get(model.MD_CHROMATIC_COR, None)
            if apply_transform:
                try:
                    metadata_cor = apply_transform[fl.position.value["band"]]
                    assert isinstance(metadata_cor, dict), "Expected a dictionary format"
                    assert all(
                        isinstance(key, str) for key in metadata_cor), "All keys should be strings"
                    comp_affected.updateMetadata(metadata_cor)
                except (AssertionError, KeyError) as exp:
                    # Check if CHROMATIC_COR is a dictionary with filter band positions as keys and correction metadata
                    # dictionary as values. For e.g. correction metadata dictionary for a given band position has
                    # the following format:
                    # {"Pixel size cor": [1, 1], "Centre position cor": [0, 0] , "Rotation cor": 0 , "Shear cor": 1}
                    logging.error("Chromatic correction metadata was not updated due to %s", exp)

        filter.position.subscribe(updateOutWLRange, init=True)
        self._onTerminate.append((filter.position.unsubscribe, (updateOutWLRange,)))

        return True

    def observeQWP(self, qwp, comp_affected):

        if model.hasVA(qwp, "position"):
            def updatePosition(pos, comp_affected=comp_affected):
                md = {model.MD_POL_POS_QWP: pos["rz"]}
                comp_affected.updateMetadata(md)

            qwp.position.subscribe(updatePosition, init=True)
            self._onTerminate.append((qwp.position.unsubscribe, (updatePosition,)))

        return True

    def observeLinPol(self, linpol, comp_affected):

        if model.hasVA(linpol, "position"):
            def updatePosition(pos, comp_affected=comp_affected):
                md = {model.MD_POL_POS_LINPOL: pos["rz"]}
                comp_affected.updateMetadata(md)

            linpol.position.subscribe(updatePosition, init=True)
            self._onTerminate.append((linpol.position.unsubscribe, (updatePosition,)))

        return True

    def observePolAnalyzer(self, analyzer, comp_affected):

        if model.hasVA(analyzer, "position"):
            def updatePosition(pos, comp_affected=comp_affected):
                md = {model.MD_POL_MODE: pos["pol"]}
                comp_affected.updateMetadata(md)

            analyzer.position.subscribe(updatePosition, init=True)
            self._onTerminate.append((analyzer.position.unsubscribe, (updatePosition,)))

        return True

    def observeStreakLens(self, streak_lens, comp_affected):
        """Update the magnification of the streak lens affecting the
        streak readout camera."""

        if not comp_affected.role.endswith("ccd"):
            return False

        def updateMagnification(mag, comp_affected=comp_affected):
            md = {model.MD_LENS_MAG: mag}
            comp_affected.updateMetadata(md)

        streak_lens.magnification.subscribe(updateMagnification, init=True)
        self._onTerminate.append((streak_lens.magnification.unsubscribe, (updateMagnification,)))

        return True

    def observeStreakDelay(self, streak_delay, comp_affected):
        """Update the trigger related settings of the streak delay affecting the
        streak readout camera."""

        if not comp_affected.role.endswith("ccd"):
            return False

        # Typically, the streak-delay always has a triggerDelay
        if model.hasVA(streak_delay, "triggerDelay"):
            def updateTriggerDelay(delay, comp_affected=comp_affected):
                md = {model.MD_TRIGGER_DELAY: delay}
                comp_affected.updateMetadata(md)

            streak_delay.triggerDelay.subscribe(updateTriggerDelay, init=True)
            self._onTerminate.append((streak_delay.triggerDelay.unsubscribe, (updateTriggerDelay,)))

        # It may also have a triggerRate, which is used to detect/control the trigger frequency
        if model.hasVA(streak_delay, "triggerRate"):
            def updateTriggerRate(rate, comp_affected=comp_affected):
                md = {model.MD_TRIGGER_RATE: rate}
                comp_affected.updateMetadata(md)

            streak_delay.triggerRate.subscribe(updateTriggerRate, init=True)
            self._onTerminate.append((streak_delay.triggerRate.unsubscribe, (updateTriggerRate,)))

        return True

    def observeEbeam(self, ebeam, comp_affected):
        """Add ebeam rotation to multibeam metadata to make sure that the thumbnails
        are displayed correctly."""

        if comp_affected.role != "multibeam":
            return False

        def updateRotation(rot, comp_affected=comp_affected):
            md = {model.MD_ROTATION: rot}
            comp_affected.updateMetadata(md)

        ebeam.rotation.subscribe(updateRotation, init=True)
        self._onTerminate.append((ebeam.rotation.unsubscribe, (updateRotation,)))

        return True

    def terminate(self):
        self._mic.alive.unsubscribe(self._onAlive)

        # call all the unsubscribes
        for fun, args in self._onTerminate:
            try:
                fun(*args)
            except Exception as ex:
                logging.warning("Failed to unsubscribe metadata properly: %s", ex)

        model.Component.terminate(self)
