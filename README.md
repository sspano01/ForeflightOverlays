# ForeflightOverlays

A Python tool that scrapes all **Diners, Drive-Ins and Dives** restaurant locations from the [Food Network DDD A–Z listing](https://www.foodnetwork.com/restaurants/shows/diners-drive-ins-and-dives/a-z), geocodes their addresses, and exports them as a **KML overlay** ready to import into [ForeFlight](https://foreflight.com/) — the popular aviation app for iPad.

Pilots can use the generated KML file to see every DDD restaurant location on their ForeFlight map while flight planning.

## Quick Start — Get the KML into ForeFlight

The ready-to-use KML file is located at **`output/DDD_Restaurants.kml`** in this repository.

### How to upload it as a Custom Map Overlay in ForeFlight

1. **Get the file to your iPad** — email it to yourself, use AirDrop, or sync it via a cloud service (iCloud, Dropbox, etc.).
2. **Open the file on your iPad** — tap the `.kml` attachment/file and choose **"Open in ForeFlight"** from the share sheet, or use ForeFlight's built-in file importer: **More → Files → Import**.
3. **Enable the overlay on the map** — go to the **Maps** view, tap the **Map Elements** (layer) button, scroll to **Custom Map Overlays**, and toggle on **Diners, Drive-Ins and Dives**.
4. **Explore** — red pins will appear at each DDD restaurant location. Tap any pin to see the restaurant name, address, phone number, and description.

---

## Features

- Scrapes 1,000+ restaurants from the Food Network DDD listing (with pagination)
- Extracts restaurant name, address, phone number, and description for each location
- Geocodes addresses to GPS coordinates using [Nominatim](https://nominatim.openstreetmap.org/) (OpenStreetMap)
- Exports a ForeFlight-compatible KML file with red map pins and rich popup details
- Exports a human-readable Markdown table as a secondary reference
- Saves an intermediate `restaurants.json` so scraping doesn't need to be re-run on every export

---

## Requirements

- Python 3.8+
- [Playwright](https://playwright.dev/python/) for headless browser scraping
- [GeoPy](https://geopy.readthedocs.io/) for geocoding

Install dependencies:

```bash
pip install -r requirements.txt
playwright install chromium
```

---

## Usage

Run the scraper:

```bash
python scrape_ddd.py
```

The script will:

1. **Scrape** all restaurant listings from the Food Network website
2. **Extract** address, phone, and description from each restaurant's detail page
3. **Geocode** each address to obtain GPS coordinates
4. **Export** the results to the `output/` directory:
   - `output/DDD_Restaurants.kml` — KML overlay for ForeFlight
   - `output/DDD_Restaurants.md` — Markdown table
   - `output/restaurants.json` — Intermediate JSON data

> **Tip:** For a quick test run, set `MAX_PAGES = 2` near the top of `scrape_ddd.py` to limit scraping to the first two pages.

---

## Importing into ForeFlight

1. Transfer `DDD_Restaurants.kml` to your iPad via email, AirDrop, or cloud storage.
2. Open **ForeFlight** → **More** → **Files** and import the KML file.
3. On the map, tap **Map Elements** and enable the DDD overlay.
4. Red pins will appear at each restaurant location. Tap a pin to see the name, address, phone, and a brief description.

---

## Output

### KML (ForeFlight Overlay)

Each restaurant is represented as a placemark with:
- **Red circular pin** icon
- **Name** as the placemark title
- **Description** including address, phone number, and a short summary

### Markdown Table

| Name | Address | Description | Phone | Latitude | Longitude |
|------|---------|-------------|-------|----------|-----------|
| Graze | 1888 Eastland Ave, Nashville 37206 | Graze is a family-owned, locally-sourced vegan restaurant. | (615) 686-1060 | 36.1824279 | -86.7355654 |
| … | … | … | … | … | … |

---

## Project Structure

```
ForeflightOverlays/
├── scrape_ddd.py        # Main scraper, geocoder, and exporter
├── requirements.txt     # Python dependencies
└── output/
    ├── DDD_Restaurants.kml   # ForeFlight KML overlay
    ├── DDD_Restaurants.md    # Markdown reference table
    └── restaurants.json      # Intermediate geocoded data
```

---

## Notes

- Geocoding is rate-limited to ~1 request per second to comply with Nominatim's [usage policy](https://operations.osmfoundation.org/policies/nominatim/).
- Only restaurants with a valid address, phone number, and geocoded coordinates are included in the final export.
- The scraper uses a real browser (Chromium via Playwright) to handle JavaScript-rendered content and cookie consent dialogs on the Food Network website.
