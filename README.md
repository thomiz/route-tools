# Generate activity wheel

It reads all .gpx files in a folder (which you can export from Strava or any GPS device), places a cropped OpenStreetMap of your riding area in the center circle, wraps the continuous elevation profile of every ride around the perimeter, and labels each segment with the date, track name, distance, and elevation gain. It uses the first named track containing points, or the filename if no such name is available.

Ride labels curve along the rim, and each ride sector is proportional to its
share of the total distance. Zero-distance rides contribute to the totals but
have no sector. The top sector shows total distance, elevation gain, and elapsed
time (including pauses within recorded track segments). If only some rides
have usable timing data, the time is marked partial.

Prerequisites

Install the required libraries using pip:
~~~bash
pip install gpxpy matplotlib numpy requests pillow
~~~

Run the script with the directory containing your GPX files:
~~~powershell
python src\generate-wheel.py "C:\path\to\gpx_tracks"
~~~

The directory argument is optional and defaults to `./gpx_tracks`. The generated
`activity_wheel.png` is saved in the GPX directory. The script needs
internet access to download map tiles from `tile.openstreetmap.org`.

The default background is dark. Use `--background white` for a white image, or
`--background transparent` for a transparent PNG with dark labels (best on a
light background). The map and summary sector remain visible.
~~~powershell
python src\generate-wheel.py "C:\path\to\gpx_tracks" --background transparent
~~~

If the default output cannot be written, use `--output` to choose another
filename in an existing writable folder:
~~~powershell
python src\generate-wheel.py "C:\path\to\gpx_tracks" --output "$env:TEMP\activity_wheel_new.png"
~~~

## How to Use It with Strava Data

1. Export from Strava:
	• Single rides: Open any activity on Strava → click the ... (More) icon → Export GPX.
	• Bulk export: Go to Strava Settings → My Account → Download or Delete Your Account → Request Your Archive. Unzip the archive and copy the .gpx files from the activities folder into your chosen GPX directory. (Note: If your bulk archive contains .fit.gz files instead of .gpx, you can convert them using gpsbabel or fitparse.)

## Example output

![test](test/activity_wheel.png)
