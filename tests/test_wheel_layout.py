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
    def test_map_fetch_error_closes_figure(self):
        activity = {
            "date": "2026-01-01", "name": "Ride", "coords": [(0, 0), (1, 1)],
            "elevations": np.array([0, 1]), "distance_km": 1,
            "elev_gain_m": 1, "duration_s": None,
        }
        open_figures = set(wheel.plt.get_fignums())
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(wheel, "parse_gpx_folder", return_value=([activity], [], [])), \
             patch.object(wheel, "fetch_center_map", side_effect=wheel.requests.Timeout("tile timeout")):
            with self.assertRaisesRegex(wheel.requests.Timeout, "tile timeout"):
                wheel.create_activity_wheel(folder, Path(folder, "activity_wheel.png"))
        self.assertEqual(set(wheel.plt.get_fignums()), open_figures)

    def test_map_pixels_match_final_rendered_size(self):
        savefig = Figure.savefig
        activity = {
            "date": "2026-01-01", "name": "Ride", "coords": [(0, 0), (1, 1)],
            "elevations": np.array([0, 1]), "distance_km": 1,
            "elev_gain_m": 1, "duration_s": None,
        }

        def fake_map(activities, size_px=800, output_px=None):
            size = output_px if output_px is not None else size_px
            return np.zeros((size, size, 3), dtype=np.uint8)

        def verify_map(figure, *args, **kwargs):
            figure.canvas.draw()
            axis = figure.axes[1]
            image = axis.images[0].get_array()
            bounds = axis.get_window_extent()
            self.assertAlmostEqual(image.shape[1], bounds.width, delta=1)
            self.assertAlmostEqual(image.shape[0], bounds.height, delta=1)
            kwargs["dpi"] = 60
            savefig(figure, *args, **kwargs)

        with tempfile.TemporaryDirectory() as folder, \
             patch.object(wheel, "DOCS_DIR", Path(folder) / "docs"), \
             patch.object(wheel, "parse_gpx_folder", return_value=([activity], [], [])), \
             patch.object(wheel, "fetch_center_map", side_effect=fake_map), \
             patch.object(Figure, "savefig", verify_map), \
             contextlib.redirect_stdout(io.StringIO()):
            wheel.create_activity_wheel(folder)
            self.assertTrue(Path(folder, "docs", "activity_wheel.png").is_file())
            self.assertFalse(Path(folder, "activity_wheel.png").exists())

    def test_higher_resolution_preserves_map_extent_and_route_positions(self):
        activity = {
            "coords": [(0.0, 51.0), (0.03, 51.01)],
            "track_end_indices": [1], "color": "red",
        }
        with io.BytesIO() as data:
            Image.new("RGB", (256, 256), "white").save(data, format="PNG")
            response = Response()
            response.status_code = 200
            response._content = data.getvalue()
        line = ImageDraw.ImageDraw.line
        with patch.object(wheel.requests, "get", return_value=response) as requests, \
             patch.object(ImageDraw.ImageDraw, "line", autospec=True, side_effect=line) as lines:
            base = wheel.fetch_center_map([activity], size_px=900)
            base_zoom = int(requests.call_args.args[0].split("/")[-3])
            base_points = np.array(lines.call_args.args[1])
            requests.reset_mock()
            high_res = wheel.fetch_center_map([activity], size_px=900, output_px=1576)
            high_zoom = int(requests.call_args.args[0].split("/")[-3])
            high_points = np.array(lines.call_args.args[1])

        self.assertEqual(base.shape, (900, 900, 3))
        self.assertEqual(high_res.shape, (1576, 1576, 3))
        self.assertEqual(high_zoom, base_zoom + 1)
        np.testing.assert_allclose(base_points / 900, high_points / 1576, atol=1 / 900)

    def test_daily_route_colors_match_elevations_and_mark_track_ends(self):
        days = [0, 0, 1, 2, 3, 4, 5, 6, 7]
        colors = ["red", "red", "orange", "yellow", "green", "blue", "indigo", "violet", "red"]
        map_sizes = []
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
            map_sizes.append(map_pixels.shape[0])
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
                wheel.create_activity_wheel(folder, Path(folder, "activity_wheel.png"))

        line_calls = lines.call_args_list
        self.assertEqual([call.kwargs["fill"] for call in line_calls],
                         [color for index, color in enumerate(colors) if index != 2])
        route_width = round(4 * map_sizes[0] / 900)
        dot_size = round(6 * map_sizes[0] / 900)
        self.assertTrue(all(call.kwargs["width"] == route_width for call in line_calls))
        self.assertEqual(len(dots.call_args_list), len(days) + 1)
        first_line_points = line_calls[0].args[1]
        # Separate GPX tracks are drawn as separate lines; the second dot marks the one-point second track.
        self.assertEqual(len(first_line_points), 2)
        for call, point in zip(dots.call_args_list[:1], [first_line_points[-1]]):
            left, top, right, bottom = call.args[1]
            self.assertEqual(call.kwargs["fill"], "black")
            self.assertEqual((right - left + 1, bottom - top + 1), (dot_size, dot_size))
            self.assertAlmostEqual((left + right) / 2, point[0], delta=1)
            self.assertAlmostEqual((top + bottom) / 2, point[1], delta=1)
        for call in dots.call_args_list:
            left, top, right, bottom = call.args[1]
            self.assertEqual((call.kwargs["fill"], right - left + 1), ("black", dot_size))

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
            wheel.create_activity_wheel(folder, Path(folder, "activity_wheel.png"))


if __name__ == "__main__":
    unittest.main()
