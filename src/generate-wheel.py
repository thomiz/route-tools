import io
import json
import math
import requests
from PIL import Image, ImageChops, ImageDraw
import argparse
import glob
import os
from pathlib import Path
import gpxpy
import gpxpy.geo
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.font_manager import FontProperties
from matplotlib.patches import Circle
from matplotlib.textpath import TextToPath
from matplotlib.transforms import Bbox

DAY_COLORS = ("red", "orange", "yellow", "green", "blue", "indigo", "violet")

# Radial layout of the wheel, in polar-axis units
R_ELE_BASE = 1.06    # Inner ring where elevation starts
R_ELE_MAX = 1.45     # Outer limit for highest elevation peak
R_LABEL = 1.54       # Radius of the inner label line
R_MAX = 1.85         # Outer edge of the wheel
SUMMARY_SPAN = math.radians(46)

# Upper bound (exclusive), color and legend label of each gradient band
GRADIENT_BANDS = (
    (-6, "#1d4e89", "below -6 %"),
    (-2, "#6fa8dc", "-6 to -2 %"),
    (2, "#4caf50", "-2 to 2 %"),
    (5, "#f2c94c", "2 to 5 %"),
    (8, "#f2994a", "5 to 8 %"),
    (12, "#d64545", "8 to 12 %"),
    (math.inf, "#7b2d8b", "above 12 %"),
)
PROFILE_MAX_SAMPLES = 2000
PROFILE_SMOOTHING_M = 80
GRADIENT_WINDOW_M = 100
# Recording pauses at least this long and far are drawn as train or ferry rides
TRANSIT_GAP_S = 600
TRANSIT_MIN_M = 1000
TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
DOCS_DIR = Path(__file__).resolve().parents[1] / "docs"

class WheelSaveError(OSError):
    pass

def is_transit_gap(prev_pt, pt, gap_s, min_m):
    """A recording pause long in both time and distance is treated as a train or ferry ride."""
    if prev_pt.time is None or pt.time is None:
        return False
    return (pt.time - prev_pt.time).total_seconds() >= gap_s and pt.distance_2d(prev_pt) >= min_m

def parse_gpx_folder(folder_path, transit_gap_s=TRANSIT_GAP_S, transit_min_m=TRANSIT_MIN_M,
                     transit_between_files=False):
    activities = []
    all_lats, all_lons = [], []

    for filepath in sorted(glob.glob(os.path.join(folder_path, "*.gpx"))):
        with open(filepath, "r", encoding="utf-8") as f:
            gpx = gpxpy.parse(f)

        coords = []
        track_end_indices = []
        transit_break_indices = []
        transit_legs = []
        transit_dist = 0.0
        transit_time = 0.0
        elevations = []
        point_elevations = []
        distances = [0.0]
        total_dist = 0.0
        elev_gain = 0.0
        time_bounds = gpx.get_time_bounds()
        start_time = time_bounds.start_time
        track_name = next(
            (track.name.strip() for track in gpx.tracks
             if track.name and track.name.strip() and any(segment.points for segment in track.segments)),
            Path(filepath).stem,
        )

        prev_pt = None
        for track in gpx.tracks:
            track_start = len(coords)
            for segment in track.segments:
                for point_index, pt in enumerate(segment.points):
                    coords.append((pt.longitude, pt.latitude))
                    all_lats.append(pt.latitude)
                    all_lons.append(pt.longitude)
                    
                    ele = pt.elevation if pt.elevation is not None else 0.0
                    elevations.append(ele)
                    point_elevations.append(pt.elevation)

                    if prev_pt is not None:
                        if is_transit_gap(prev_pt, pt, transit_gap_s, transit_min_m):
                            transit_break_indices.append(len(coords) - 1)
                            transit_legs.append([(prev_pt.longitude, prev_pt.latitude), (pt.longitude, pt.latitude)])
                            transit_dist += pt.distance_2d(prev_pt)
                            # gpxpy only counts time inside a segment, so only such gaps are subtracted.
                            if point_index > 0:
                                transit_time += (pt.time - prev_pt.time).total_seconds()
                        else:
                            total_dist += pt.distance_2d(prev_pt)
                            prev_ele = prev_pt.elevation if prev_pt.elevation is not None else 0.0
                            if ele > prev_ele:
                                elev_gain += (ele - prev_ele)
                        distances.append(total_dist)
                    prev_pt = pt
            if len(coords) > track_start:
                track_end_indices.append(len(coords) - 1)

        if not coords:
            continue

        # Resample elevation to 150 evenly spaced points per ride for smooth plotting
        target_dists = np.linspace(0, total_dist, 150)
        resampled_ele = np.interp(target_dists, distances, elevations)
        duration = gpx.get_duration() if start_time is not None else None

        activities.append({
            "date": start_time.strftime("%Y-%m-%d") if start_time else os.path.basename(filepath)[:10],
            "name": track_name,
            "sort_key": start_time.timestamp() if start_time else 0,
            "coords": coords,
            "track_end_indices": track_end_indices,
            "transit_break_indices": transit_break_indices,
            "transit_legs": transit_legs,
            "transit_km": transit_dist / 1000.0,
            "distances_m": distances,
            "point_elevations": point_elevations,
            "elevations": resampled_ele,
            "distance_km": total_dist / 1000.0,
            "elev_gain_m": elev_gain,
            "duration_s": duration - transit_time if duration is not None else None,
            "start_time": start_time,
            "end_time": time_bounds.end_time,
        })

    activities.sort(key=lambda x: x["sort_key"])
    if transit_between_files:
        add_transit_between_files(activities, transit_gap_s, transit_min_m)
    return activities, all_lats, all_lons

def add_transit_between_files(activities, gap_s, min_m):
    """Add a transit leg before a ride that starts far from where the previous ride that day ended."""
    for previous, act in zip(activities, activities[1:]):
        if previous["date"] != act["date"] or previous["end_time"] is None or act["start_time"] is None:
            continue
        (lon1, lat1), (lon2, lat2) = previous["coords"][-1], act["coords"][0]
        distance = gpxpy.geo.distance(lat1, lon1, None, lat2, lon2, None)
        if (act["start_time"] - previous["end_time"]).total_seconds() >= gap_s and distance >= min_m:
            act["transit_legs"].insert(0, [(lon1, lat1), (lon2, lat2)])
            act["transit_km"] += distance / 1000.0

def validate_theme(background, transparent_theme):
    if background not in ("dark", "white", "transparent"):
        raise ValueError(f"Unknown background: {background}")
    if transparent_theme is not None and transparent_theme not in ("light", "dark"):
        raise ValueError(f"Unknown transparent theme: {transparent_theme}")
    if transparent_theme is not None and background != "transparent":
        raise ValueError("Transparent theme requires a transparent background.")

def theme_colors(background, transparent_theme=None):
    dark = background == "dark" or (background == "transparent" and transparent_theme == "dark")
    border = "#aab4bd" if background == "transparent" and dark else ("#3a404a" if dark else "#6c757d")
    return {
        "dark": dark,
        "background": "#121417" if background == "dark" else ("white" if background == "white" else "none"),
        "text": "#e0e6ed" if dark else "#121417",
        "border": border,
        "summary": "#25313a" if dark else "#e1e9e9",
        "divider": "#2b303a" if background == "dark" else border,
    }

def assign_day_colors(activities):
    day_colors = {}
    for act in activities:
        if act["date"] not in day_colors:
            day_colors[act["date"]] = DAY_COLORS[len(day_colors) % len(DAY_COLORS)]
        act["color"] = day_colors[act["date"]]

def summarize(activities):
    durations = [act["duration_s"] for act in activities if act["duration_s"] is not None]
    if durations:
        hours, remainder = divmod(round(sum(durations)), 3600)
        minutes, seconds = divmod(remainder, 60)
        elapsed = f"{hours}h {minutes:02d}m {seconds:02d}s"
        if len(durations) < len(activities):
            elapsed += " (partial)"
    else:
        elapsed = "unavailable"
    return {
        "distance_km": sum(act["distance_km"] for act in activities),
        "transit_km": sum(act.get("transit_km", 0.0) for act in activities),
        "elev_gain_m": sum(act["elev_gain_m"] for act in activities),
        "elapsed": elapsed,
    }

def summary_lines(totals):
    """Totals cover cycling only; train and ferry distance is listed separately."""
    lines = [
        "ALL RIDES",
        f"{totals['distance_km']:,.1f} km | +{totals['elev_gain_m']:,.0f} m",
        f"Time: {totals['elapsed']}",
    ]
    if totals["transit_km"] > 0:
        lines.append(f"Train/ferry: \N{ALMOST EQUAL TO} {totals['transit_km']:,.0f} km")
    return lines

def wheel_sectors(activities):
    """Return the gap between sectors and (activity, start, end) angles for rides with distance."""
    rides = [act for act in activities if act["distance_km"] > 0]
    total_distance = sum(act["distance_km"] for act in activities)
    gap_rad = min(math.radians(1.5), (2 * math.pi - SUMMARY_SPAN) / (4 * (len(rides) + 1)))
    available_span = 2 * math.pi - SUMMARY_SPAN - (len(rides) + 1) * gap_rad
    theta_start = SUMMARY_SPAN / 2 + gap_rad
    sectors = []
    for act in rides:
        theta_end = theta_start + available_span * act["distance_km"] / total_distance
        sectors.append((act, theta_start, theta_end))
        theta_start = theta_end + gap_rad
    return gap_rad, sectors

def gradient_band(gradient):
    return next(index for index, (upper, _, _) in enumerate(GRADIENT_BANDS) if gradient < upper)

def route_pieces(act):
    """Inclusive index ranges of continuous riding, split at GPX track ends and train or ferry gaps."""
    breaks = sorted(act.get("transit_break_indices", ()))
    pieces = []
    start = 0
    for end in act["track_end_indices"]:
        for index in breaks:
            if start < index <= end:
                pieces.append((start, index - 1))
                start = index
        pieces.append((start, end))
        start = end + 1
    return pieces

def _smoothed_gradient(samples, elevations, step):
    window = max(1, round(PROFILE_SMOOTHING_M / step))
    padded = np.pad(elevations, (window // 2, window - 1 - window // 2), mode="edge")
    smoothed = np.convolve(padded, np.ones(window) / window, mode="valid")
    half = max(1, round(GRADIENT_WINDOW_M / 2 / step))
    index = np.arange(len(samples))
    low = np.clip(index - half, 0, len(samples) - 1)
    high = np.clip(index + half, 0, len(samples) - 1)
    run = samples[high] - samples[low]
    return np.divide((smoothed[high] - smoothed[low]) * 100, run, out=np.zeros_like(run), where=run > 0)

def _interp_unique(samples, distances, values):
    unique_distances, first = np.unique(distances, return_index=True)
    return np.interp(samples, unique_distances, values[first])

def ride_profile(act):
    """Resample a ride by distance with interpolated elevation and smoothed gradient.

    Train and ferry gaps split the ride into pieces that are resampled separately, so the
    elevation change across a gap is never treated as a climb.
    """
    distances = np.asarray(act["distances_m"], dtype=float)
    total = distances[-1]
    if total <= 0:
        return None
    step = max(10.0, total / PROFILE_MAX_SAMPLES)
    coords = np.asarray(act["coords"], dtype=float)
    raw = np.array([np.nan if ele is None else ele for ele in act["point_elevations"]], dtype=float)
    valid = ~np.isnan(raw)
    has_elevation = bool(valid.any())
    breaks = sorted(act.get("transit_break_indices", ()))

    parts = {key: [] for key in ("distance_m", "elevation_m", "gradient_pct", "lat", "lon")}
    for start, stop in zip([0, *breaks], [*breaks, len(distances)]):
        first_m, last_m = distances[start], distances[stop - 1]
        if last_m <= first_m:
            continue
        piece = slice(start, stop)
        samples = np.append(np.arange(first_m, last_m, step), last_m)
        piece_valid = valid[piece]
        if piece_valid.any():
            elevations = _interp_unique(samples, distances[piece][piece_valid], raw[piece][piece_valid])
        elif has_elevation:
            elevations = _interp_unique(samples, distances[valid], raw[valid])
        else:
            elevations = np.zeros_like(samples)
        parts["distance_m"].append(samples)
        parts["elevation_m"].append(elevations)
        parts["gradient_pct"].append(_smoothed_gradient(samples, elevations, step))
        parts["lon"].append(_interp_unique(samples, distances[piece], coords[piece, 0]))
        parts["lat"].append(_interp_unique(samples, distances[piece], coords[piece, 1]))

    profile = {key: np.concatenate(values) for key, values in parts.items()}
    profile["band"] = [gradient_band(gradient) for gradient in profile["gradient_pct"]]
    profile["has_elevation"] = has_elevation
    profile["transit_breaks_m"] = [float(distances[index]) for index in breaks]
    return profile

def split_tracks(act, min_spacing_m):
    """Split a ride into continuous [lat, lon] polylines, keeping points at least min_spacing_m apart."""
    tracks = []
    for start, end in route_pieces(act):
        points = []
        last = None
        for index in range(start, end + 1):
            distance = act["distances_m"][index]
            if last is None or index == end or distance - last >= min_spacing_m:
                lon, lat = act["coords"][index]
                points.append([round(lat, 6), round(lon, 6)])
                last = distance
        tracks.append(points)
    return tracks

def _latlon(point):
    lon, lat = point
    return [round(lat, 6), round(lon, 6)]

def track_ends(act):
    return [_latlon(act["coords"][index]) for index in act["track_end_indices"]]

def transit_lines(act):
    return [[_latlon(start), _latlon(end)] for start, end in act.get("transit_legs", ())]

def group_days(activities):
    days = {}
    for act in activities:
        days.setdefault(act["date"], {"date": act["date"], "color": act["color"], "rides": []})["rides"].append(act)
    return list(days.values())

def _rounded(values, digits):
    return [round(float(value), digits) for value in values]

def day_payload(day):
    profile = {key: [] for key in ("distance_km", "elevation_m", "gradient_pct", "band", "lat", "lon")}
    boundaries_km = []
    transit_breaks_km = []
    rides = []
    has_elevation = False
    offset = 0.0
    for act in day["rides"]:
        rides.append({
            "name": act["name"],
            "distance_km": round(act["distance_km"], 3),
            "elev_gain_m": round(act["elev_gain_m"], 1),
            "tracks": split_tracks(act, min_spacing_m=5),
            "ends": track_ends(act),
            "transit": transit_lines(act),
        })
        ride = ride_profile(act)
        if ride is not None:
            if profile["distance_km"]:
                boundaries_km.append(round(offset / 1000, 3))
            transit_breaks_km += [round((offset + distance) / 1000, 3) for distance in ride["transit_breaks_m"]]
            profile["distance_km"] += _rounded((ride["distance_m"] + offset) / 1000, 3)
            profile["elevation_m"] += _rounded(ride["elevation_m"], 1)
            profile["gradient_pct"] += _rounded(ride["gradient_pct"], 1)
            profile["band"] += ride["band"]
            profile["lat"] += _rounded(ride["lat"], 6)
            profile["lon"] += _rounded(ride["lon"], 6)
            has_elevation = has_elevation or ride["has_elevation"]
            offset += ride["distance_m"][-1]
    totals = summarize(day["rides"])
    return {
        "date": day["date"],
        "color": day["color"],
        "distance_km": round(totals["distance_km"], 3),
        "transit_km": round(totals["transit_km"], 3),
        "elev_gain_m": round(totals["elev_gain_m"], 1),
        "elapsed": totals["elapsed"],
        "rides": rides,
        "profile": profile,
        "ride_boundaries_km": boundaries_km,
        "transit_breaks_km": transit_breaks_km,
        "has_elevation": has_elevation,
    }

def wheel_payload(activities, colors, day_page):
    all_eles = np.concatenate([act["elevations"] for act in activities])
    min_ele = np.min(all_eles)
    ele_span = max(np.max(all_eles) - min_ele, 1.0)
    gap_rad, sectors = wheel_sectors(activities)
    totals = summarize(activities)
    ride_ids = {id(act): index for index, act in enumerate(activities)}
    return {
        "theme": colors,
        "day_page": day_page,
        "radii": {"base": R_ELE_BASE, "peak": R_ELE_MAX, "label": R_LABEL, "max": R_MAX},
        "summary_span": SUMMARY_SPAN,
        "gap": gap_rad,
        "summary": summary_lines(totals),
        "sectors": [{
            "ride_id": ride_ids[id(act)],
            "date": act["date"],
            "color": act["color"],
            "start": start,
            "end": end,
            "label": f"{act['date']} | {act['name']}",
            "stats": f"{act['distance_km']:.1f} km | +{act['elev_gain_m']:.0f} m",
            "profile": _rounded((act["elevations"] - min_ele) / ele_span, 4),
        } for act, start, end in sectors],
        "rides": [{
            "id": index,
            "date": act["date"],
            "name": act["name"],
            "color": act["color"],
            "tracks": split_tracks(act, min_spacing_m=50),
            "ends": track_ends(act),
            "transit": transit_lines(act),
        } for index, act in enumerate(activities)],
    }

def render_page(template_name, payload):
    template = (TEMPLATE_DIR / template_name).read_text(encoding="utf-8")
    # Escape characters that could close the script element when GPX names are embedded.
    data = (json.dumps(payload, allow_nan=False, separators=(",", ":"))
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))
    return template.replace("__WHEEL_DATA__", data)

def create_interactive_wheel(gpx_folder, output_file=None, background="dark", transparent_theme=None,
                             **transit_options):
    validate_theme(background, transparent_theme)
    wheel_path = Path(output_file if output_file is not None else DOCS_DIR / "activity_wheel.html").absolute()
    if wheel_path.suffix.lower() not in (".html", ".htm"):
        raise ValueError("Interactive output requires an HTML file.")
    day_path = wheel_path.with_name(f"{wheel_path.stem}_day{wheel_path.suffix}")
    activities, _, _ = parse_gpx_folder(gpx_folder, **transit_options)
    if not activities:
        print("No valid GPX tracks found.")
        return
    assign_day_colors(activities)
    colors = theme_colors(background, transparent_theme)
    if colors["background"] == "none":
        colors["background"] = "transparent"
    wheel_html = render_page("wheel.html", wheel_payload(activities, colors, day_path.name))
    day_html = render_page("day.html", {
        "theme": colors,
        "wheel_page": wheel_path.name,
        "bands": [{"color": color, "label": label} for _, color, label in GRADIENT_BANDS],
        "days": [day_payload(day) for day in group_days(activities)],
    })
    for path, html in ((wheel_path, wheel_html), (day_path, day_html)):
        try:
            if output_file is None:
                wheel_path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(html, encoding="utf-8")
        except OSError as exc:
            raise WheelSaveError(
                f'Could not save the interactive activity wheel to "{path}": {exc}. '
                "Use --output to select a different filename in a writable folder."
            ) from exc
    print(f"Saved interactive activity wheel to {wheel_path}")
    print(f"Saved interactive day view to {day_path}")

def fetch_center_map(activities, size_px=800, output_px=None, *, map_scale=100):
    if not 100 <= map_scale <= 400:
        raise ValueError("Map scale must be between 100 and 400 percent.")
    all_lons = [pt[0] for act in activities for pt in act["coords"]]
    all_lats = [pt[1] for act in activities for pt in act["coords"]]
    
    min_lon, max_lon = min(all_lons), max(all_lons)
    min_lat, max_lat = min(all_lats), max(all_lats)
    center_lon = (min_lon + max_lon) / 2
    center_lat = (min_lat + max_lat) / 2

    def deg2num(lat_deg, lon_deg, zoom):
        lat_rad = math.radians(lat_deg)
        n = 2.0 ** zoom
        xtile = (lon_deg + 180.0) / 360.0 * n
        ytile = (1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n
        return xtile, ytile

    # Pick zoom level so all tracks fit comfortably inside size_px
    zoom = 13
    for z in range(15, 4, -1):
        x1, y1 = deg2num(max_lat, min_lon, z)
        x2, y2 = deg2num(min_lat, max_lon, z)
        if max(abs(x2 - x1), abs(y2 - y1)) * 256 < size_px * 0.72:
            zoom = z
            break

    output_px = size_px if output_px is None else output_px
    zoom_increase = min(max(0, math.ceil(math.log2(output_px / size_px))), 19 - zoom)
    zoom += zoom_increase
    source_size = size_px * 2 ** zoom_increase
    cx, cy = deg2num(center_lat, center_lon, zoom)
    zoom_reduction = min(zoom, round(math.log2(map_scale / 100)))
    source_scale = 2 ** zoom_reduction
    # Anchor the lower-detail crop to the native crop so route positions stay identical.
    left = math.floor(cx * 256 - source_size / 2) / source_scale
    top = math.floor(cy * 256 - source_size / 2) / source_scale
    source_size /= source_scale
    zoom -= zoom_reduction
    first_x, first_y = math.floor(left / 256), math.floor(top / 256)
    last_x = math.ceil((left + source_size) / 256) - 1
    last_y = math.ceil((top + source_size) / 256) - 1
    canvas = Image.new("RGB", ((last_x - first_x + 1) * 256, (last_y - first_y + 1) * 256))
    headers = {"User-Agent": "GPXActivityWheel/1.0 (local-script)"}

    for tx in range(first_x, last_x + 1):
        for ty in range(first_y, last_y + 1):
            url = f"https://tile.openstreetmap.org/{zoom}/{tx}/{ty}.png"
            resp = requests.get(url, headers=headers, timeout=10)
            resp.raise_for_status()
            tile = Image.open(io.BytesIO(resp.content)).convert("RGB")
            canvas.paste(tile, ((tx - first_x) * 256, (ty - first_y) * 256))

    crop_left, crop_top = left - first_x * 256, top - first_y * 256
    crop_box = (crop_left, crop_top, crop_left + source_size, crop_top + source_size)
    if zoom_reduction:
        cropped = canvas.resize((output_px, output_px), Image.Resampling.LANCZOS, box=crop_box)
    else:
        cropped = canvas.crop(crop_box)
        if source_size != output_px:
            cropped = cropped.resize((output_px, output_px), Image.Resampling.LANCZOS)

    # Draw GPX tracks onto the cropped map
    draw = ImageDraw.Draw(cropped)
    pixel_scale = output_px / source_size
    route_width = max(1, round(4 * output_px / size_px))
    dot_size = max(route_width + 1, round(6 * output_px / size_px))
    endpoints = []
    transit_dot = max(2, round(route_width * 0.75))
    transit_spacing = 2.5 * transit_dot
    for act in activities:
        pts = []
        for lon, lat in act["coords"]:
            tx, ty = deg2num(lat, lon, zoom)
            px = (tx * 256 - left) * pixel_scale
            py = (ty * 256 - top) * pixel_scale
            pts.append((px, py))
        for start, end in route_pieces(act):
            if end > start:
                draw.line(pts[start:end + 1], fill=act["color"], width=route_width)
        for leg in act.get("transit_legs", ()):
            (x1, y1), (x2, y2) = [
                ((tx * 256 - left) * pixel_scale, (ty * 256 - top) * pixel_scale)
                for tx, ty in (deg2num(lat, lon, zoom) for lon, lat in leg)
            ]
            length = math.hypot(x2 - x1, y2 - y1)
            for offset in np.arange(0, length + 1e-9, transit_spacing):
                fraction = offset / length if length else 0
                cx, cy = x1 + (x2 - x1) * fraction, y1 + (y2 - y1) * fraction
                radius = transit_dot / 2
                draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=act["color"])
        endpoints.extend(pts[index] for index in act["track_end_indices"])

    for px, py in endpoints:
        x = round(px - (dot_size - 1) / 2)
        y = round(py - (dot_size - 1) / 2)
        draw.ellipse((x, y, x + dot_size - 1, y + dot_size - 1), fill="black")

    return np.array(cropped)

def draw_arc_label(ax, text, theta, radius, span, reverse, color="#e0e6ed"):
    font = FontProperties(family="sans-serif", size=8.5)
    metrics = TextToPath()
    widths = [metrics.get_text_width_height_descent(char, font, False)[0] for char in text]
    center = ax.transData.transform((theta, 0))
    position = ax.transData.transform((theta, radius))
    radius_pt = np.linalg.norm(position - center) * 72 / ax.figure.dpi
    scale = min(1.0, 0.9 * span * radius_pt / sum(widths))
    if font.get_size_in_points() * scale < 1:
        return False
    font.set_size(font.get_size_in_points() * scale)

    # The lower half reads counterclockwise so its letters stay upright.
    direction = -1 if reverse else 1
    offset = -sum(widths) / 2
    for char, width in zip(text, widths):
        angle = theta + direction * (offset + width / 2) * scale / radius_pt
        if not char.isspace():
            ax.text(
                angle, radius, char, fontproperties=font, color=color,
                ha="center", va="center_baseline", rotation_mode="anchor",
                rotation=-math.degrees(angle) + (180 if reverse else 0),
            )
        offset += width
    return True

def create_activity_wheel(gpx_folder, output_file=None, background="dark", transparent_theme=None,
                          *, map_scale=100, **transit_options):
    validate_theme(background, transparent_theme)
    if not 100 <= map_scale <= 400:
        raise ValueError("Map scale must be between 100 and 400 percent.")
    output_path = Path(output_file if output_file is not None else DOCS_DIR / "activity_wheel.png").absolute()
    if background == "transparent" and output_path.suffix.lower() != ".png":
        raise ValueError("Transparent background requires a PNG output file.")
    activities, _, _ = parse_gpx_folder(gpx_folder, **transit_options)
    if not activities:
        print("No valid GPX tracks found.")
        return

    assign_day_colors(activities)

    # 1. Setup figure and polar axis
    colors = theme_colors(background, transparent_theme)
    background_color = colors["background"]
    text_color = colors["text"]
    border_color = colors["border"]
    dpi = 300
    fig = plt.figure(figsize=(12, 12), dpi=dpi, facecolor=background_color)
    ax = fig.add_subplot(111, projection="polar", facecolor=background_color)
    ax.set_theta_zero_location("N")  # Start at 12 o'clock
    ax.set_theta_direction(-1)       # Go clockwise
    ax.set_ylim(0, R_MAX)
    ax.axis("off")
    ax.apply_aspect()

    R_MAP = R_ELE_BASE - 6 * ax.get_rmax() / (ax.bbox.height / 2)

    # 2. Embed circular map in the center using a standard Cartesian overlay axis
    polar_bounds = ax.get_position()
    map_fraction = R_MAP / ax.get_rmax()
    map_width = polar_bounds.width * map_fraction
    map_height = polar_bounds.height * map_fraction
    ax_map = fig.add_axes([
        polar_bounds.x0 + (polar_bounds.width - map_width) / 2,
        polar_bounds.y0 + (polar_bounds.height - map_height) / 2,
        map_width, map_height,
    ], zorder=10)
    ax_map.axis("off")
    map_pixels = math.ceil(ax_map.bbox.width)
    map_img = None
    try:
        map_img = fetch_center_map(activities, size_px=900, output_px=map_pixels, map_scale=map_scale)
    finally:
        if map_img is None:
            plt.close(fig)
    im = ax_map.imshow(map_img, interpolation="nearest")
    
    # Clip the square map image into a circle
    patch = Circle((0.5, 0.5), radius=0.5, transform=ax_map.transAxes)
    im.set_clip_path(patch)
    # Add a clean border ring around the map
    border_theta = np.linspace(0, 2 * np.pi, 300)
    ax.plot(border_theta, np.full_like(border_theta, R_MAP), color=border_color, lw=2)

    # 3. Normalize elevations globally across all rides
    all_eles = np.concatenate([a["elevations"] for a in activities])
    min_ele, max_ele = np.min(all_eles), np.max(all_eles)
    ele_span = max(max_ele - min_ele, 1.0)

    total_distance = sum(act["distance_km"] for act in activities)
    totals = summarize(activities)

    ax.bar(
        0, R_ELE_MAX - R_ELE_BASE, width=SUMMARY_SPAN, bottom=R_ELE_BASE,
        color=colors["summary"], edgecolor=border_color,
    )
    ax.text(
        0, (R_ELE_BASE + R_ELE_MAX) / 2,
        "\n".join(summary_lines(totals)),
        ha="center", va="center", fontsize=8.5, color=text_color,
    )

    if total_distance == 0:
        print("No positive ride distances; showing totals without ride sectors.")
    gap_rad, sectors = wheel_sectors(activities)

    for act, theta_start, theta_end in sectors:
        sector_span = theta_end - theta_start
        thetas = np.linspace(theta_start, theta_end, len(act["elevations"]))

        # Scale elevation to radial height
        norm_ele = (act["elevations"] - min_ele) / ele_span
        radii = R_ELE_BASE + norm_ele * (R_ELE_MAX - R_ELE_BASE)

        # Fill and stroke the elevation profile
        ax.fill_between(thetas, R_ELE_BASE, radii, color=act["color"], alpha=0.45)
        ax.plot(thetas, radii, color=act["color"], lw=1.8)

        # Add radial divider line
        ax.plot([theta_start - gap_rad/2]*2, [R_ELE_BASE, R_ELE_MAX], color=colors["divider"], lw=0.8, ls="--")

        # Keep the date above the stats on both halves of the wheel.
        mid_theta = (theta_start + theta_end) / 2
        reverse = math.cos(mid_theta) < 0
        date_radius, stats_radius = (R_LABEL, R_LABEL + 0.08) if reverse else (R_LABEL + 0.08, R_LABEL)
        ride_label = f"{act['date']} | {act['name']}"
        date_drawn = draw_arc_label(ax, ride_label, mid_theta, date_radius, sector_span, reverse, color=text_color)
        stats = f"{act['distance_km']:.1f} km | +{act['elev_gain_m']:.0f} m"
        stats_drawn = draw_arc_label(ax, stats, mid_theta, stats_radius, sector_span, reverse, color=text_color)
        if not (date_drawn and stats_drawn):
            print(f"Ride sector too narrow for the full rim label: {ride_label} ({stats})")

    canvas = FigureCanvasAgg(fig)
    canvas.draw()
    rendered = Image.frombuffer("RGBA", canvas.get_width_height(), canvas.buffer_rgba(), "raw", "RGBA", 0, 1)
    if background == "transparent":
        content = rendered.getchannel("A")
    else:
        rgb = rendered.convert("RGB")
        content = ImageChops.difference(rgb, Image.new("RGB", rgb.size, rgb.getpixel((0, 0))))
    bounds = content.getbbox()
    if bounds is None:
        plt.close(fig)
        raise ValueError("Cannot determine the bounds of an empty infographic.")
    left, top, right, bottom = bounds
    width, height = rendered.size
    fig.text(
        right / width, (height - bottom) / height, "\N{COPYRIGHT SIGN} OpenStreetMap contributors",
        ha="right", va="bottom", fontsize=7, color=text_color,
    )
    padding = 10
    crop = Bbox.from_extents(
        (left - padding) / dpi, (height - bottom - padding) / dpi,
        (right + padding) / dpi, (height - top + padding) / dpi,
    )
    try:
        if output_file is None:
            output_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(output_path, dpi=dpi, bbox_inches=crop, facecolor=background_color)
    except OSError as exc:
        raise WheelSaveError(
            f'Could not save the activity wheel to "{output_path}": {exc}. '
            "Use --output to select a different filename in a writable folder."
        ) from exc
    finally:
        plt.close(fig)
    print(f"Saved activity wheel to {output_path}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate an activity wheel from GPX files.")
    parser.add_argument(
        "gpx_folder",
        nargs="?",
        default="./gpx_tracks",
        help="directory containing GPX files (default: ./gpx_tracks)",
    )
    parser.add_argument(
        "-o", "--output",
        help="output path (default: activity_wheel.png, or activity_wheel.html with --interactive, "
             "in the repository's docs folder; explicit paths override this)",
    )
    parser.add_argument(
        "--background", choices=("dark", "white", "transparent"), default="dark",
        help="image background (default: dark)",
    )
    parser.add_argument(
        "--transparent-theme", choices=("light", "dark"),
        help="intended background for a transparent PNG (default: light)",
    )
    parser.add_argument(
        "--interactive", action="store_true",
        help="write an interactive HTML activity wheel and a linked day view instead of a PNG",
    )
    parser.add_argument(
        "--map-scale", type=float, metavar="PERCENT",
        help="basemap label scale, 100-400 percent; larger means less detail "
             "(default: 100; PNG only; GPS tracks stay at output resolution)",
    )
    parser.add_argument(
        "--transit-gap-min", type=float, default=TRANSIT_GAP_S / 60, metavar="MINUTES",
        help="minimum recording pause treated as a train or ferry ride (default: 10)",
    )
    parser.add_argument(
        "--transit-min-km", type=float, default=TRANSIT_MIN_M / 1000, metavar="KM",
        help="minimum straight-line distance across a pause for a train or ferry ride (default: 1)",
    )
    parser.add_argument(
        "--transit-between-files", action="store_true",
        help="also draw a train or ferry ride between consecutive GPX files on the same date",
    )
    args = parser.parse_args()
    if not os.path.isdir(args.gpx_folder):
        parser.error(f"GPX directory does not exist or is not a directory: {args.gpx_folder}")
    if not args.transit_gap_min > 0:
        parser.error("--transit-gap-min must be greater than 0.")
    if not args.transit_min_km >= 0:
        parser.error("--transit-min-km must not be negative.")
    if args.map_scale is not None and not 100 <= args.map_scale <= 400:
        parser.error("--map-scale must be between 100 and 400 percent.")
    if args.interactive and args.map_scale is not None:
        parser.error("--map-scale is only available for PNG image generation.")
    output_suffix = Path(args.output).suffix.lower() if args.output else None
    if args.interactive and args.output and output_suffix not in (".html", ".htm"):
        parser.error("Interactive output requires an HTML file.")
    if not args.interactive and args.background == "transparent" and args.output and output_suffix != ".png":
        parser.error("Transparent background requires a PNG output file.")
    if args.transparent_theme is not None and args.background != "transparent":
        parser.error("--transparent-theme requires --background transparent.")
    options = {
        "transit_gap_s": args.transit_gap_min * 60,
        "transit_min_m": args.transit_min_km * 1000,
        "transit_between_files": args.transit_between_files,
    }
    if not args.interactive:
        options["map_scale"] = args.map_scale if args.map_scale is not None else 100
    create = create_interactive_wheel if args.interactive else create_activity_wheel
    try:
        create(
            args.gpx_folder, args.output, args.background, args.transparent_theme,
            **options,
        )
    except WheelSaveError as exc:
        parser.exit(1, f"{parser.prog}: {exc}\n")
