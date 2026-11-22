import contextlib
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import matplotlib
matplotlib.use("Agg")
from matplotlib.figure import Figure
import numpy as np


script = Path(__file__).resolve().parents[1] / "src" / "generate-wheel.py"
spec = importlib.util.spec_from_file_location("generate_wheel", script)
wheel = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wheel)


class WheelLayoutTests(unittest.TestCase):
    def test_map_is_centered_within_ten_pixels_of_information_ring(self):
        def verify_layout(figure, *args, **kwargs):
            figure.canvas.draw()
            polar, map_axis = figure.axes
            bounds = map_axis.get_window_extent()
            center = polar.transData.transform((0, 0))
            ring_top = polar.transData.transform((0, 1.06))
            self.assertAlmostEqual((bounds.x0 + bounds.x1) / 2, center[0], delta=0.5)
            self.assertAlmostEqual((bounds.y0 + bounds.y1) / 2, center[1], delta=0.5)
            gap = ring_top[1] - bounds.y1
            self.assertGreaterEqual(gap, 0)
            self.assertLessEqual(gap, 10)

        activity = {
            "date": "2026-01-01", "name": "Ride", "coords": [(0, 0), (1, 1)],
            "elevations": np.array([0, 1]), "distance_km": 1,
            "elev_gain_m": 1, "duration_s": None,
        }
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(wheel, "parse_gpx_folder", return_value=([activity], [], [])), \
             patch.object(wheel, "fetch_center_map", return_value=np.zeros((32, 32, 3))), \
             patch.object(Figure, "savefig", verify_layout), \
             contextlib.redirect_stdout(io.StringIO()):
            wheel.create_activity_wheel(folder)


if __name__ == "__main__":
    unittest.main()
