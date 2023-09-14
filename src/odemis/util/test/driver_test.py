# -*- coding: utf-8 -*-
'''
Created on 26 Apr 2013

@author: Éric Piel

Copyright © 2013 Éric Piel, Delmic

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
import logging
import os
import sys
import time
import unittest
from concurrent.futures import CancelledError

import odemis
from odemis import model
from odemis.util import testing
from odemis.util.driver import getSerialDriver, speedUpPyroConnect, readMemoryUsage, get_linux_version, ProgressiveMove


logging.getLogger().setLevel(logging.DEBUG)

CONFIG_PATH = os.path.dirname(odemis.__file__) + "/../../install/linux/usr/share/odemis/"
SECOM_CONFIG = CONFIG_PATH + "sim/secom-sim.odm.yaml"
FSLM_CONFIG = CONFIG_PATH + "sim/sparc2-fslm-sim.odm.yaml"


class TestDriver(unittest.TestCase):
    """
    Test the different functions of driver
    """
    def test_getSerialDriver(self):
        # very simple to fit any platform => just check it doesn't raise exception

        name = getSerialDriver("booo")
        self.assertEqual("Unknown", name)

    def test_speedUpPyroConnect(self):
        try:
            testing.start_backend(SECOM_CONFIG)
            need_stop = True
        except LookupError:
            logging.info("A running backend is already found, will not stop it")
            need_stop = False
        except IOError as exp:
            logging.error(str(exp))
            raise

        model._components._microscope = None # force reset of the microscope for next connection

        speedUpPyroConnect(model.getMicroscope())

        time.sleep(2)
        if need_stop:
            testing.stop_backend()

    def test_memoryUsage(self):
        m = readMemoryUsage()
        self.assertGreater(m, 1)

    def test_linux_version(self):

        if sys.platform.startswith('linux'):
            v = get_linux_version()
            self.assertGreaterEqual(v[0], 2)
            self.assertEqual(len(v), 3)
        else:
            with self.assertRaises(LookupError):
                v = get_linux_version()


class TestProgressiveMove(unittest.TestCase):
    """
    Test a move with the ProgressiveMove class
    - see if requested progress increases over time
    - see if the progressive move can be cancelled
    """
    @classmethod
    def setUpClass(cls) -> None:
        testing.start_backend(FSLM_CONFIG)
        cls.spec_switch = model.getComponent(role="spec-switch")
        cls.spec_sw_md = cls.spec_switch.getMetadata()

    def test_progressive_move(self):
        # first move the axis we want to use to the 0.0 position
        f = self.spec_switch.moveAbs({"x": 0.0})
        f.result()

        old_pos = self.spec_switch.position.value
        # take the active position from the yaml file as new pos
        new_pos = self.spec_sw_md[model.MD_FAV_POS_ACTIVE]

        # with a progressive move, move to the engage position (FAV_POS_ACTIVE)
        prog_move = ProgressiveMove(self.spec_switch, new_pos)

        # request the progress and calculate the elapsed time
        prog_1_start, prog_1_end = prog_move.get_progress()
        now = time.time()
        elapsed_time_1 = now - prog_1_start

        time.sleep(2)
        # after waiting a few seconds request the progress and calculate the elapsed time again
        prog_2_start, prog_2_end = prog_move.get_progress()
        now = time.time()
        elapsed_time_2 = now - prog_2_start

        # check if the elapsed time of the second check is greater than the first check
        self.assertGreater(elapsed_time_2, elapsed_time_1)

        # check if the axis moved
        testing.assert_pos_not_almost_equal(old_pos, new_pos)

        # wait for the move to end
        prog_move.result(timeout=10)

        # check if the end position is the same as the FAV_POS_ACTIVE position
        testing.assert_pos_almost_equal(self.spec_switch.position.value, new_pos)

        prog_3_start, prog_3_end = prog_move.get_progress()
        # check if the elapsed end time is lesser than the actual time
        self.assertLess(prog_3_end, time.time())

    def test_progressive_move_cancel(self):
        # first move the axis we want to use to the 0.0 position
        f = self.spec_switch.moveAbs({"x": 0.0})
        f.result()

        # start moving to the retract position (FAV_POS_DEACTIVE) and cancel the progression
        old_pos = self.spec_switch.position.value
        new_pos = self.spec_sw_md[model.MD_FAV_POS_DEACTIVE]

        # with a progressive move, move to engage position (POS_ACTIVE)
        prog_move = ProgressiveMove(self.spec_switch, new_pos)
        time.sleep(0.5)
        prog_move.cancel()

        with self.assertRaises(CancelledError):
            prog_move.result()

        # see if the axis stopped somewhere in between 0.0 (old_pos) and the retract position (new_pos)
        self.assertNotEqual(old_pos, self.spec_switch.position.value)
        self.assertNotEqual(new_pos, self.spec_switch.position.value)


if __name__ == "__main__":
    #import sys;sys.argv = ['', 'Test.testName']
    unittest.main()
