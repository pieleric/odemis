import math
import os
import unittest
import numpy as np
import numpy.testing
import odemis
from fs.wrap import read_only

from odemis.acq.scan import generate_scan_vector
from odemis import model
from odemis.util import testing


class MockScanner:
    def __init__(self, shape, settle_time):
        self.shape = shape
        self.settleTime = settle_time
        self.pixelSize = model.TupleContinuous((1e-6, 1e-6),
                                               range=((1e-9, 1e-9), (1e-3, 1e-3)),
                                               unit="m",
                                               readonly=True)

class TestGenerateScanVector(unittest.TestCase):
    def setUp(self):
        self.scanner = MockScanner(shape=(768, 512), settle_time=10e-6)

    def test_simple(self):
        """
        Test the generation of a scan vector which scan the whole FoV at the maximum resolution.
        """
        res = (768, 512)
        roi = (0, 0, 1.0, 1.0)  # Full area of the scanner
        rotation = 0.0  # rad
        dwell_time = 1e-6  # s

        scan_vector, margin, md_cor = generate_scan_vector(
            self.scanner, res, roi, rotation, dwell_time
        )

        # Check output shape: (N, 2), N = res[0] * res[1] + margin * res[1]
        expected_points = res[1] * (res[0] + margin)
        self.assertEqual(scan_vector.shape, (expected_points, 2))
        # position of the first and last pixels (which has to take into account the shift due to the center of the pixel)
        half_width = ((self.scanner.shape[0] - 1) / 2,
                      (self.scanner.shape[1] - 1) / 2)
        numpy.testing.assert_equal(scan_vector[0], [-half_width[0], -half_width[1]])  # Center of the first pixel

        self.assertEqual(margin, 10)  # 10µs settle time / 1µs dwell time = 10 pixels margin
        self.assertIn(model.MD_PIXEL_SIZE_COR, md_cor)
        self.assertIn(model.MD_POS_COR, md_cor)
        self.assertEqual(md_cor[model.MD_PIXEL_SIZE_COR], (1, 1))  # Max res, so no pixel size change
        self.assertEqual(md_cor[model.MD_POS_COR], (0, 0))  # Full FoV, so no translation
        self.assertEqual(md_cor[model.MD_ROTATION_COR], rotation)

    def test_rotation(self):
        res = (768, 512)
        roi = (0, 0, 1.0, 1.0)  # Full area of the scanner
        rotation = math.pi / 2  # rad, 90°
        dwell_time = 1e-6  # s

        scan_vector, margin, md_cor = generate_scan_vector(
            self.scanner, res, roi, rotation, dwell_time
        )
        self.assertEqual(md_cor[model.MD_ROTATION_COR], rotation)

        # Check output shape: (N, 2), N = res[0] * res[1] + margin * res[1]
        expected_points = res[1] * (res[0] + margin)
        self.assertEqual(scan_vector.shape, (expected_points, 2))
        # position of the first and last pixels (which has to take into account the shift due to the center of the pixel)
        # 90° rotation counter-clockwise means the first pixel is now at the bottom left corner
        half_width = ((self.scanner.shape[0] - 1) / 2,
                      (self.scanner.shape[1] - 1) / 2)
        numpy.testing.assert_almost_equal(scan_vector[0], [-half_width[1], half_width[0]])  # Center of the first pixel


    def test_margin_calculation(self):
        res = (768, 512)
        roi = (0, 0, 1.0, 1.0)  # Full area of the scanner
        rotation = 0

        # Large settleTime should increase margin
        self.scanner.settleTime = 100e-6
        dwell_time = 1e-6  # s
        scan_vector, margin, md_cor = generate_scan_vector(
            self.scanner, res, roi, rotation, dwell_time
        )
        self.assertEqual(margin, 100)  # 100µs settle time / 1µs dwell time = 100 pixels margin

        # only 1 pixel => no margin
        res = (1, 512)
        roi = (0, 0, 1/768, 1.0)
        scan_vector, margin, md_cor = generate_scan_vector(
            self.scanner, res, roi, rotation, dwell_time
        )
        self.assertEqual(margin, 0)

        # FIXME: is that correct? 2 px should be less than 1 percent, so still no margin?
        # > 1 pixel => > 1 pixel margin
        res = (2, 512)
        roi = (0, 0, 2 / 768, 1.0)
        scan_vector, margin, md_cor = generate_scan_vector(
            self.scanner, res, roi, rotation, dwell_time
        )
        self.assertEqual(margin, 1)

        # Half width => half the margin
        res = (384, 512)
        roi = (0, 0, 0.5, 1.0)  # Half area of the scanner in X
        scan_vector, margin, md_cor = generate_scan_vector(
            self.scanner, res, roi, rotation, dwell_time
        )
        self.assertEqual(margin, 50)

CONFIG_PATH = os.path.dirname(odemis.__file__) + "/../../install/linux/usr/share/odemis/"
SPARC2_HWSYNC_CONFIG = CONFIG_PATH + "sim/sparc2-nidaq-sim.odm.yaml"

class TestVectorAcquisition(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        testing.start_backend(SPARC2_HWSYNC_CONFIG)

        # Find CCD & SEM components
        cls.ebeam = model.getComponent(role="e-beam")
        cls.sed = model.getComponent(role="se-detector")
        cls.cl = model.getComponent(role="cl-detector")
        cls.spec = model.getComponent(role="spectrometer")

    def test_2d_img_as_vector(self):
        """
        Test basic acquisition of a full FoV image, using the vector scanning
        """
        res = (768, 512)
        roi = (0, 0, 1.0, 1.0)  # Full area of the scanner
        rotation = 0.0  # rad
        dwell_time = 1e-6  # s

        scan_vector, margin, md_cor = generate_scan_vector(
            self.scanner, res, roi, rotation, dwell_time
        )


        vector_data_to_img


if __name__ == "__main__":
    unittest.main()
