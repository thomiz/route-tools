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
from matplotlib.colors import to_rgb
import numpy as np
from PIL import Image, ImageDraw
from requests import Response


script = Path(__file__).resolve().parents[1] / "src" / "generate-wheel.py"
spec = importlib.util.spec_from_file_location("generate_wheel", script)
wheel = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wheel)


class WheelLayoutTests(unittest.TestCase):
    def test_daily_route_colors_match_elevations_and_mark_track_ends(self):
        days = [0, 0, 1, 2, 3, 4, 5, 6, 7]
        colors = ["red", "red", "orange", "yellow", "green", "blue", "indigo", "violet", "red"]
        tracks = []
        for index, day in enumerate(days):
            point = f"<trkpt lat='51.0' lon='{index / 100}'><time>2026-01-{day + 1:02d}T12:00:00Z</time></trkpt>"
            end = f"<trkpt lat='51.001' lon='{index / 100 + 0.001}'/>"
            track = f"<trk><trkseg>{point}{'' if index == 2 else end}</trkseg></trk>"
            if index == 0:
                track += "<trk><trkseg><trkpt lat='51.002' lon='0.002'/></trkseg></trk>"
            tracks.append(track)

        with io.BytesIO() as data:
            Image.new("RGB", (256, 256), "white").save(data, format="PNG")
            response = Response()
            response.status_code = 200
            response._content = data.getvalue()

        def verify_elevation_colors(figure, *args, **kwargs):
            polar = figure.axes[0]
            elevation_lines = [line for line in polar.lines if line.get_linewidth() == 1.8]
            self.assertEqual([line.get_color() for line in elevation_lines],
                             [color for index, color in enumerate(colors) if index != 2])
            self.assertEqual(len(polar.collections), len(elevation_lines))
            for collection, line in zip(polar.collections, elevation_lines):
                np.testing.assert_allclose(collection.get_facecolor()[0][:3], to_rgb(line.get_color()))
            map_pixels = figure.axes[1].images[0].get_array()
            for call in dots.call_args_list:
                left, top, right, bottom = call.args[1]
                x, y = (left + right + 1) // 2, (top + bottom + 1) // 2
                self.assertEqual(tuple(map_pixels[y, x]), (0, 0, 0))

        real_line = ImageDraw.ImageDraw.line
        real_ellipse = ImageDraw.ImageDraw.ellipse
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            for index, track in enumerate(tracks):
                (folder / f"{index:02}.gpx").write_text(
                    f"<gpx version='1.1' creator='test'>{track}</gpx>", encoding="utf-8"
                )
            with patch.object(wheel.requests, "get", return_value=response), \
                 patch.object(ImageDraw.ImageDraw, "line", autospec=True, side_effect=real_line) as lines, \
                 patch.object(ImageDraw.ImageDraw, "ellipse", autospec=True, side_effect=real_ellipse) as dots, \
                 patch.object(Figure, "savefig", verify_elevation_colors), \
                 contextlib.redirect_stdout(io.StringIO()):
                wheel.create_activity_wheel(folder)

        line_calls = lines.call_args_list
        self.assertEqual([call.kwargs["fill"] for call in line_calls],
                         [color for index, color in enumerate(colors) if index != 2])
        self.assertTrue(all(call.kwargs["width"] == 4 for call in line_calls))
        self.assertEqual(len(dots.call_args_list), len(days) + 1)
        first_line_points = line_calls[0].args[1]
        for call, point in zip(dots.call_args_list[:2], [first_line_points[1], first_line_points[-1]]):
            left, top, right, bottom = call.args[1]
            self.assertEqual(call.kwargs["fill"], "black")
            self.assertEqual((right - left + 1, bottom - top + 1), (6, 6))
            self.assertAlmostEqual((left + right) / 2, point[0], delta=1)
            self.assertAlmostEqual((top + bottom) / 2, point[1], delta=1)

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
