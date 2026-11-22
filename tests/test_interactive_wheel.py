import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np


script = Path(__file__).resolve().parents[1] / "src" / "generate-wheel.py"
spec = importlib.util.spec_from_file_location("generate_wheel", script)
wheel = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wheel)


def gpx_file(name, start, points):
    rows = "".join(
        f'<trkpt lat="{lat}" lon="{lon}">'
        + (f"<ele>{ele}</ele>" if ele is not None else "")
        + f"<time>{start}T08:{index // 60:02d}:{index % 60:02d}Z</time></trkpt>"
        for index, (lat, lon, ele) in enumerate(points)
    )
    return (
        '<?xml version="1.0"?><gpx version="1.1" creator="test" xmlns="http://www.topografix.com/GPX/1/1">'
        f"<trk><name>{name}</name><trkseg>{rows}</trkseg></trk></gpx>"
    )


def embedded_data(html):
    start = html.index("const DATA = ") + len("const DATA = ")
    return json.loads(html[start:html.index(";</script>", start)])


class RideProfileTests(unittest.TestCase):
    def test_gradient_follows_climb_and_ignores_missing_elevation(self):
        rng = np.random.default_rng(1)
        distances = np.arange(0, 2001, 10.0)
        elevations = np.where(distances < 1000, 100.0, 100 + 0.04 * (distances - 1000))
        elevations += rng.normal(0, 0.3, len(distances))
        point_elevations = [float(e) for e in elevations]
        for index in (20, 21, 150):
            point_elevations[index] = None
        activity = {
            "coords": [(0.0, d / 111_195) for d in distances],
            "distances_m": list(distances),
            "point_elevations": point_elevations,
        }

        profile = wheel.ride_profile(activity)

        flat = profile["distance_m"] < 850
        climb = (profile["distance_m"] > 1150) & (profile["distance_m"] < 1900)
        self.assertLess(np.max(np.abs(profile["gradient_pct"][flat])), 1.0)
        self.assertTrue(np.allclose(profile["gradient_pct"][climb], 4.0, atol=0.8))
        self.assertEqual({profile["band"][i] for i in np.flatnonzero(flat)}, {2})
        self.assertEqual({profile["band"][i] for i in np.flatnonzero(climb)}, {3})
        self.assertGreater(np.min(profile["elevation_m"]), 98)
        self.assertTrue(profile["has_elevation"])


class InteractiveWheelTests(unittest.TestCase):
    def write_rides(self, folder):
        climb = [(59.9 + i * 0.001, 10.7, 100 + i * 4) for i in range(20)]
        flat = [(59.95, 10.7 + i * 0.002, 50) for i in range(15)]
        hidden = [(59.96 + i * 0.001, 10.8, None) for i in range(10)]
        Path(folder, "a.gpx").write_text(gpx_file("Morning", "2026-05-01", climb), encoding="utf-8")
        Path(folder, "b.gpx").write_text(gpx_file("Evening", "2026-05-01", flat), encoding="utf-8")
        Path(folder, "c.gpx").write_text(
            gpx_file("&lt;/script&gt;&lt;b&gt;x", "2026-05-02", hidden), encoding="utf-8")

    def test_writes_linked_pages_with_safe_embedded_data(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(wheel.requests, "get", side_effect=AssertionError("no tile requests")):
            self.write_rides(folder)
            with contextlib.redirect_stdout(io.StringIO()):
                wheel.create_interactive_wheel(folder)
            wheel_html = Path(folder, "activity_wheel.html").read_text(encoding="utf-8")
            day_html = Path(folder, "activity_wheel_day.html").read_text(encoding="utf-8")

        self.assertNotIn("</script><b>", wheel_html + day_html)
        wheel_data = embedded_data(wheel_html)
        day_data = embedded_data(day_html)
        self.assertEqual(wheel_data["day_page"], "activity_wheel_day.html")
        self.assertEqual(day_data["wheel_page"], "activity_wheel.html")
        self.assertEqual([s["date"] for s in wheel_data["sectors"]], ["2026-05-01", "2026-05-01", "2026-05-02"])
        self.assertEqual(wheel_data["sectors"][2]["label"], "2026-05-02 | </script><b>x")

        first, second = day_data["days"]
        self.assertEqual([ride["name"] for ride in first["rides"]], ["Morning", "Evening"])
        self.assertEqual(first["color"], wheel.DAY_COLORS[0])
        self.assertEqual(second["color"], wheel.DAY_COLORS[1])
        self.assertEqual(len(first["ride_boundaries_km"]), 1)
        self.assertAlmostEqual(first["ride_boundaries_km"][0], first["rides"][0]["distance_km"], places=2)
        self.assertTrue(first["has_elevation"])
        self.assertFalse(second["has_elevation"])
        self.assertEqual(len(day_data["bands"]), len(wheel.GRADIENT_BANDS))

    def test_cli_rejects_non_html_interactive_output(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(sys, "argv", ["generate-wheel.py", folder, "--interactive", "--output", "x.png"]), \
             contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                runpy_main()
        self.assertEqual(raised.exception.code, 2)


def runpy_main():
    import runpy
    runpy.run_path(str(script), run_name="__main__")


if __name__ == "__main__":
    unittest.main()
