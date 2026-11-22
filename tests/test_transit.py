import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image, ImageDraw
from requests import Response


script = Path(__file__).resolve().parents[1] / "src" / "generate-wheel.py"
spec = importlib.util.spec_from_file_location("generate_wheel", script)
wheel = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wheel)


def point(lat, seconds, ele, date="2026-05-01", lon=10.0):
    hours, rest = divmod(8 * 3600 + seconds, 3600)
    return (f'<trkpt lat="{lat:.6f}" lon="{lon}"><ele>{ele}</ele>'
            f"<time>{date}T{hours:02d}:{rest // 60:02d}:{rest % 60:02d}Z</time></trkpt>")


def write_gpx(path, segments):
    body = "".join(f"<trkseg>{''.join(segment)}</trkseg>" for segment in segments)
    path.write_text(f'<gpx version="1.1" creator="test"><trk><name>Trip</name>{body}</trk></gpx>',
                    encoding="utf-8")


def train_day(split_segment=False):
    """1 km ride, 15 min cafe stop, 30 min / ~21 km train to a station 50 m higher, then 1 km ride."""
    before = [point(60 + i * 0.0009, i * 60, 10) for i in range(11)]
    before.append(point(60.009, 1500, 10))
    after = [point(60.2 + i * 0.0009, 3300 + i * 60, 60) for i in range(11)]
    return [before, after] if split_segment else [before + after]


def white_tiles():
    with io.BytesIO() as data:
        Image.new("RGB", (256, 256), "white").save(data, format="PNG")
        response = Response()
        response.status_code = 200
        response._content = data.getvalue()
    return response


def embedded_data(html):
    start = html.index("const DATA = ") + len("const DATA = ")
    return json.loads(html[start:html.index(";</script>", start)])


class TransitTests(unittest.TestCase):
    def parse(self, segments, **options):
        with tempfile.TemporaryDirectory() as folder:
            write_gpx(Path(folder, "trip.gpx"), segments)
            activities, _, _ = wheel.parse_gpx_folder(folder, **options)
        return activities[0]

    def test_train_is_excluded_from_cycling_distance_time_and_climbing(self):
        for split_segment in (False, True):
            with self.subTest(split_segment=split_segment):
                act = self.parse(train_day(split_segment))
                self.assertAlmostEqual(act["distance_km"], 2.0, delta=0.05)
                self.assertAlmostEqual(act["transit_km"], 21.2, delta=0.3)
                # The cafe stop is kept; the 30 min train ride is removed.
                self.assertEqual(act["duration_s"], 2100)
                self.assertEqual(act["elev_gain_m"], 0)
                self.assertEqual(act["transit_break_indices"], [12])
                self.assertEqual(wheel.route_pieces(act), [(0, 11), (12, 22)])
                self.assertEqual(len(act["transit_legs"]), 1)
                totals = wheel.summarize([act])
                self.assertEqual(totals["elapsed"], "0h 35m 00s")
                self.assertTrue(wheel.summary_lines(totals)[-1].startswith("Train/ferry: \N{ALMOST EQUAL TO} 21"))

    def test_thresholds_are_configurable(self):
        act = self.parse(train_day(), transit_gap_s=3600)
        self.assertEqual(act["transit_km"], 0)
        self.assertAlmostEqual(act["distance_km"], 23.2, delta=0.3)
        act = self.parse(train_day(), transit_min_m=30_000)
        self.assertEqual(act["transit_break_indices"], [])

    def test_profile_has_no_false_climb_at_the_station(self):
        profile = wheel.ride_profile(self.parse(train_day()))
        self.assertEqual(set(profile["band"]), {wheel.gradient_band(0)})
        self.assertEqual(len(profile["transit_breaks_m"]), 1)
        self.assertAlmostEqual(profile["transit_breaks_m"][0], 1000, delta=25)

    def test_transit_between_files_is_opt_in(self):
        with tempfile.TemporaryDirectory() as folder:
            write_gpx(Path(folder, "a.gpx"), [[point(60 + i * 0.0009, i * 60, 10) for i in range(11)]])
            write_gpx(Path(folder, "b.gpx"), [[point(60.3 + i * 0.0009, 7200 + i * 60, 10) for i in range(11)]])
            write_gpx(Path(folder, "c.gpx"), [[point(61 + i * 0.0009, i * 60, 10, "2026-05-02") for i in range(11)]])
            default, _, _ = wheel.parse_gpx_folder(folder)
            enabled, _, _ = wheel.parse_gpx_folder(folder, transit_between_files=True)
        self.assertEqual([act["transit_km"] for act in default], [0, 0, 0])
        self.assertEqual([len(act["transit_legs"]) for act in enabled], [0, 1, 0])
        self.assertAlmostEqual(enabled[1]["transit_km"], 32.4, delta=0.3)
        self.assertAlmostEqual(wheel.summarize(enabled)["distance_km"], 3.0, delta=0.1)

    def test_png_map_draws_dotted_transit_without_end_dot(self):
        act = self.parse(train_day())
        act["color"] = "red"
        line, ellipse = ImageDraw.ImageDraw.line, ImageDraw.ImageDraw.ellipse
        with patch.object(wheel.requests, "get", return_value=white_tiles()), \
             patch.object(ImageDraw.ImageDraw, "line", autospec=True, side_effect=line) as lines, \
             patch.object(ImageDraw.ImageDraw, "ellipse", autospec=True, side_effect=ellipse) as dots:
            wheel.fetch_center_map([act], size_px=900)
        self.assertEqual([call.kwargs["width"] for call in lines.call_args_list], [4, 4])
        fills = [call.kwargs["fill"] for call in dots.call_args_list]
        self.assertEqual(fills.count("black"), 1)
        self.assertGreater(fills.count("red"), 10)
        left, top, right, bottom = next(call.args[1] for call in dots.call_args_list if call.kwargs["fill"] == "red")
        self.assertEqual((right - left, bottom - top), (3, 3))

    def test_interactive_pages_include_transit(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(wheel.requests, "get", side_effect=AssertionError("no tile requests")):
            write_gpx(Path(folder, "trip.gpx"), train_day())
            with contextlib.redirect_stdout(io.StringIO()):
                wheel.create_interactive_wheel(folder, Path(folder, "activity_wheel.html"))
            wheel_data = embedded_data(Path(folder, "activity_wheel.html").read_text(encoding="utf-8"))
            day_data = embedded_data(Path(folder, "activity_wheel_day.html").read_text(encoding="utf-8"))
        self.assertTrue(wheel_data["summary"][-1].startswith("Train/ferry"))
        overview = wheel_data["days"][0]
        self.assertEqual((len(overview["tracks"]), len(overview["ends"]), len(overview["transit"])), (2, 1, 1))
        day = day_data["days"][0]
        self.assertAlmostEqual(day["distance_km"], 2.0, delta=0.05)
        self.assertAlmostEqual(day["transit_km"], 21.2, delta=0.3)
        self.assertEqual(len(day["transit_breaks_km"]), 1)
        self.assertEqual(len(day["rides"][0]["transit"]), 1)
        self.assertEqual(len(day["rides"][0]["ends"]), 1)


if __name__ == "__main__":
    unittest.main()
