import io
import math
import requests
from PIL import Image, ImageDraw
import argparse
import glob
import os
from pathlib import Path
import gpxpy
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.font_manager import FontProperties
from matplotlib.patches import Circle
from matplotlib.textpath import TextToPath

class WheelSaveError(OSError):
    pass

def parse_gpx_folder(folder_path):
    activities = []
    all_lats, all_lons = [], []

    for filepath in sorted(glob.glob(os.path.join(folder_path, "*.gpx"))):
        with open(filepath, "r", encoding="utf-8") as f:
            gpx = gpxpy.parse(f)

        coords = []
        elevations = []
        distances = [0.0]
        total_dist = 0.0
        elev_gain = 0.0
        start_time = gpx.get_time_bounds().start_time
        track_name = next(
            (track.name.strip() for track in gpx.tracks
             if track.name and track.name.strip() and any(segment.points for segment in track.segments)),
            Path(filepath).stem,
        )

        prev_pt = None
        for track in gpx.tracks:
            for segment in track.segments:
                for pt in segment.points:
                    coords.append((pt.longitude, pt.latitude))
                    all_lats.append(pt.latitude)
                    all_lons.append(pt.longitude)
                    
                    ele = pt.elevation if pt.elevation is not None else 0.0
                    elevations.append(ele)

                    if prev_pt is not None:
                        d = pt.distance_2d(prev_pt)
                        total_dist += d
                        distances.append(total_dist)
                        
                        prev_ele = prev_pt.elevation if prev_pt.elevation is not None else 0.0
                        if ele > prev_ele:
                            elev_gain += (ele - prev_ele)
                    prev_pt = pt

        if not coords:
            continue

        # Resample elevation to 150 evenly spaced points per ride for smooth plotting
        target_dists = np.linspace(0, total_dist, 150)
        resampled_ele = np.interp(target_dists, distances, elevations)

        activities.append({
            "date": start_time.strftime("%Y-%m-%d") if start_time else os.path.basename(filepath)[:10],
            "name": track_name,
            "sort_key": start_time.timestamp() if start_time else 0,
            "coords": coords,
            "elevations": resampled_ele,
            "distance_km": total_dist / 1000.0,
            "elev_gain_m": elev_gain,
            "duration_s": gpx.get_duration() if start_time is not None else None,
        })

    activities.sort(key=lambda x: x["sort_key"])
    return activities, all_lats, all_lons

def fetch_center_map(activities, size_px=800):
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

    cx, cy = deg2num(center_lat, center_lon, zoom)
    tiles_radius = math.ceil(size_px / 512) + 1
    cx_int, cy_int = int(cx), int(cy)

    canvas_tiles = (tiles_radius * 2 + 1) * 256
    canvas = Image.new("RGB", (canvas_tiles, canvas_tiles), (24, 27, 32))
    headers = {"User-Agent": "GPXActivityWheel/1.0 (local-script)"}

    for dx in range(-tiles_radius, tiles_radius + 1):
        for dy in range(-tiles_radius, tiles_radius + 1):
            tx, ty = cx_int + dx, cy_int + dy
            url = f"https://tile.openstreetmap.org/{zoom}/{tx}/{ty}.png"
            resp = requests.get(url, headers=headers, timeout=10)
            resp.raise_for_status()
            tile = Image.open(io.BytesIO(resp.content)).convert("RGB")
            canvas.paste(tile, ((dx + tiles_radius) * 256, (dy + tiles_radius) * 256))

    # Crop centered on the bounding box midpoint
    center_px_x = (tiles_radius + (cx - cx_int)) * 256
    center_px_y = (tiles_radius + (cy - cy_int)) * 256
    left = int(center_px_x - size_px / 2)
    top = int(center_px_y - size_px / 2)
    cropped = canvas.crop((left, top, left + size_px, top + size_px))

    # Draw GPX tracks onto the cropped map
    draw = ImageDraw.Draw(cropped)
    for act in activities:
        pts = []
        for lon, lat in act["coords"]:
            tx, ty = deg2num(lat, lon, zoom)
            px = (tiles_radius + (tx - cx_int)) * 256 - left
            py = (tiles_radius + (ty - cy_int)) * 256 - top
            pts.append((px, py))
        if len(pts) > 1:
            draw.line(pts, fill="#e63946", width=4)

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

def create_activity_wheel(gpx_folder, output_file=None, background="dark"):
    if background not in ("dark", "white", "transparent"):
        raise ValueError(f"Unknown background: {background}")
    output_path = Path(output_file if output_file is not None else Path(gpx_folder) / "activity_wheel.png").absolute()
    if background == "transparent" and output_path.suffix.lower() != ".png":
        raise ValueError("Transparent background requires a PNG output file.")
    activities, _, _ = parse_gpx_folder(gpx_folder)
    if not activities:
        print("No valid GPX tracks found.")
        return

    # 1. Fetch OpenStreetMap image with routes overlaid
    map_img = fetch_center_map(activities, size_px=900)

    # 2. Setup figure and polar axis
    dark = background == "dark"
    background_color = "#121417" if dark else ("white" if background == "white" else "none")
    text_color = "#e0e6ed" if dark else "#121417"
    border_color = "#3a404a" if dark else "#6c757d"
    fig = plt.figure(figsize=(12, 12), facecolor=background_color)
    ax = fig.add_subplot(111, projection="polar", facecolor=background_color)
    ax.set_theta_zero_location("N")  # Start at 12 o'clock
    ax.set_theta_direction(-1)       # Go clockwise
    ax.set_ylim(0, 1.85)
    ax.axis("off")
    ax.apply_aspect()

    # Radial layout dimensions
    R_MAP = 1.0          # Radius of the inner map circle
    R_ELE_BASE = 1.06    # Inner ring where elevation starts
    R_ELE_MAX = 1.45     # Outer limit for highest elevation peak
    R_LABEL = 1.54       # Radius of the inner label line

    # 3. Embed circular map in the center using a standard Cartesian overlay axis
    ax_map = fig.add_axes([0.325, 0.325, 0.35, 0.35], zorder=10)
    ax_map.axis("off")
    im = ax_map.imshow(map_img)
    
    # Clip the square map image into a circle
    h, w, _ = map_img.shape
    patch = Circle((w / 2, h / 2), radius=min(w, h) / 2, transform=ax_map.transData)
    im.set_clip_path(patch)
    ax_map.text(
        0.5, 0.1, "\N{COPYRIGHT SIGN} OpenStreetMap contributors",
        transform=ax_map.transAxes, ha="center", va="center", fontsize=7,
        color="white", bbox={"facecolor": "#121417", "edgecolor": "none", "alpha": 0.8},
    )

    # Add a clean border ring around the map
    border_theta = np.linspace(0, 2 * np.pi, 300)
    ax.plot(border_theta, np.full_like(border_theta, R_MAP + 0.02), color=border_color, lw=2)

    # 4. Normalize elevations globally across all rides
    all_eles = np.concatenate([a["elevations"] for a in activities])
    min_ele, max_ele = np.min(all_eles), np.max(all_eles)
    ele_span = max(max_ele - min_ele, 1.0)

    total_distance = sum(act["distance_km"] for act in activities)
    total_gain = sum(act["elev_gain_m"] for act in activities)
    durations = [act["duration_s"] for act in activities if act["duration_s"] is not None]
    if durations:
        hours, remainder = divmod(round(sum(durations)), 3600)
        minutes, seconds = divmod(remainder, 60)
        elapsed = f"{hours}h {minutes:02d}m {seconds:02d}s"
        if len(durations) < len(activities):
            elapsed += " (partial)"
    else:
        elapsed = "unavailable"

    summary_span = math.radians(46)
    ax.bar(
        0, R_ELE_MAX - R_ELE_BASE, width=summary_span, bottom=R_ELE_BASE,
        color="#25313a" if dark else "#e1e9e9", edgecolor=border_color,
    )
    ax.text(
        0, (R_ELE_BASE + R_ELE_MAX) / 2,
        f"ALL RIDES\n{total_distance:,.1f} km | +{total_gain:,.0f} m\nTime: {elapsed}",
        ha="center", va="center", fontsize=8.5, color=text_color,
    )

    rides = [act for act in activities if act["distance_km"] > 0]
    if total_distance == 0:
        print("No positive ride distances; showing totals without ride sectors.")
    gap_rad = min(math.radians(1.5), (2 * math.pi - summary_span) / (4 * (len(rides) + 1)))
    available_span = 2 * math.pi - summary_span - (len(rides) + 1) * gap_rad
    theta_start = summary_span / 2 + gap_rad

    for act in rides:
        sector_span = available_span * act["distance_km"] / total_distance
        theta_end = theta_start + sector_span
        thetas = np.linspace(theta_start, theta_end, len(act["elevations"]))

        # Scale elevation to radial height
        norm_ele = (act["elevations"] - min_ele) / ele_span
        radii = R_ELE_BASE + norm_ele * (R_ELE_MAX - R_ELE_BASE)

        # Fill and stroke the elevation profile
        ax.fill_between(thetas, R_ELE_BASE, radii, color="#2a9d8f", alpha=0.45)
        ax.plot(thetas, radii, color="#4cc9f0" if dark else "#087d8d", lw=1.8)

        # Add radial divider line
        ax.plot([theta_start - gap_rad/2]*2, [R_ELE_BASE, R_ELE_MAX], color="#2b303a" if dark else border_color, lw=0.8, ls="--")

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
        theta_start = theta_end + gap_rad

    try:
        fig.savefig(output_path, dpi=300, bbox_inches="tight", facecolor=background_color)
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
        help="output image path (default: activity_wheel.png in the GPX directory)",
    )
    parser.add_argument(
        "--background", choices=("dark", "white", "transparent"), default="dark",
        help="image background (default: dark; transparent works best on light backgrounds)",
    )
    args = parser.parse_args()
    if not os.path.isdir(args.gpx_folder):
        parser.error(f"GPX directory does not exist or is not a directory: {args.gpx_folder}")
    if args.background == "transparent" and args.output and Path(args.output).suffix.lower() != ".png":
        parser.error("Transparent background requires a PNG output file.")
    try:
        create_activity_wheel(args.gpx_folder, args.output, args.background)
    except WheelSaveError as exc:
        parser.exit(1, f"{parser.prog}: {exc}\n")
