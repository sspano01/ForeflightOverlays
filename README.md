# ForeFlight Overlay Scraper

A Python tool that scrapes point-of-interest listings from multiple websites, geocodes their locations, and exports them as **KMZ overlays** ready to import into [ForeFlight](https://foreflight.com/) — the popular aviation app for iPad.

Pilots can use the generated KMZ files to see interesting destinations on their ForeFlight map while flight planning.

## Supported Sources

| Source | Flag | Description | Output Files |
|--------|------|-------------|--------------|
| **Diners, Drive-Ins and Dives** | `--source ddd` | 1,000+ restaurants from the Food Network TV show | `DDD_Restaurants.*` |
| **Michelin Guide US** | `--source michelin` | 18,000+ restaurants from the Michelin Guide | `Michelin_Restaurants.*` |
| **Atlas Obscura** | `--source atlas` | 12,000+ unusual and hidden places across the US | `AtlasObscura_StrangeSites.*` |

## Quick Start — Get a KMZ into ForeFlight

Pre-built KMZ files are located in the **`output/`** directory.

### How to upload as a Custom Map Overlay in ForeFlight

1. **Get the file to your iPad** — email it to yourself, use AirDrop, or sync via a cloud service (iCloud, Dropbox, etc.).
2. **Open the file on your iPad** — tap the `.kmz` attachment and choose **"Open in ForeFlight"**, or use **More → Files → Import** inside ForeFlight.
3. **Enable the overlay on the map** — go to the **Maps** view, tap **Map Elements** (layer button), scroll to **Custom Map Overlays**, and toggle on the overlay.
4. **Explore** — pins will appear at each location. Tap any pin to see the name, address, and a brief description.

---

## Requirements

- Python 3.10+
- [Playwright](https://playwright.dev/python/) — headless browser automation (DDD and Michelin scrapers)
- [GeoPy](https://geopy.readthedocs.io/) — geocoding via OpenStreetMap Nominatim
- [cloudscraper](https://github.com/VeNoMouS/cloudscraper) — HTTP client with Cloudflare bypass (Atlas Obscura scraper)

Install dependencies:

```bash
pip install -r requirements.txt
playwright install chromium
```

---

## Usage

```bash
# Diners, Drive-Ins and Dives (default)
python scrape.py --source ddd

# Michelin Guide US
python scrape.py --source michelin
python scrape.py --source michelin --max-pages 5      # limit pages
python scrape.py --source michelin --resume            # resume from checkpoint

# Atlas Obscura
python scrape.py --source atlas
python scrape.py --source atlas --max-pages 10        # limit pages
python scrape.py --source atlas --resume               # resume from checkpoint
```

### Command-Line Options

| Option | Description |
|--------|-------------|
| `--source {ddd,michelin,atlas}` | Which website to scrape (default: `ddd`) |
| `--max-pages N` | Limit scraping to the first N listing pages |
| `--concurrency N` | Number of concurrent detail-page scrapers — Michelin only (default: 5) |
| `--resume` | Resume a previous run from its checkpoint file (Michelin and Atlas) |

### What the script does

1. **Scrapes** all listing pages from the selected source website
2. **Extracts** name, address, description, and (where available) phone number from each entry
3. **Geocodes** addresses to GPS coordinates (skipped for sources that already provide coordinates)
4. **Exports** results to the `output/` directory as KMZ, Markdown, and JSON

---

## Output

Each source produces three files in `output/`:

| File | Purpose |
|------|---------|
| `*.kmz` | ForeFlight-compatible KMZ overlay with map pins |
| `*.md` | Human-readable Markdown table |
| `*.json` | Full scraped data in JSON format |

### KMZ Placemarks

Each location is a placemark with a colored pin icon, the place name, and a description containing the address and a short summary.

### Importing into ForeFlight

1. Transfer the `.kmz` file to your iPad via email, AirDrop, or cloud storage.
2. Open **ForeFlight** → **More** → **Files** and import the KMZ file.
3. On the map, tap **Map Elements** and enable the overlay.
4. Tap any pin to see details.

---

## Project Structure

```
ForeflightOverlays/
├── scrape.py              # Main scraper (all three sources)
├── requirements.txt       # Python dependencies
├── README.md
└── output/
    ├── DDD_Restaurants.kmz            # ForeFlight overlay — DDD
    ├── DDD_Restaurants.md
    ├── DDD_Restaurants.json
    ├── Michelin_Restaurants.kmz       # ForeFlight overlay — Michelin
    ├── Michelin_Restaurants.md
    ├── michelin_restaurants.json
    ├── AtlasObscura_StrangeSites.kmz  # ForeFlight overlay — Atlas Obscura
    ├── AtlasObscura_StrangeSites.md
    └── AtlasObscura_StrangeSites.json
```

---

## Data Sources & Credits

This tool scrapes publicly available data from the following websites. All content and trademarks belong to their respective owners.

- **[Food Network — Diners, Drive-Ins and Dives](https://www.foodnetwork.com/restaurants/shows/diners-drive-ins-and-dives/a-z)** — Restaurant listings from the Guy Fieri TV show. © Food Network / Warner Bros. Discovery.
- **[Michelin Guide US](https://guide.michelin.com/us/en/restaurants)** — Restaurant listings and distinctions (Stars, Bib Gourmand). © Michelin.
- **[Atlas Obscura](https://www.atlasobscura.com/things-to-do/united-states/places)** — Unusual and hidden places around the United States. © Atlas Obscura Inc.
- **[OpenStreetMap / Nominatim](https://nominatim.openstreetmap.org/)** — Geocoding service. © OpenStreetMap contributors, [ODbL](https://opendatacommons.org/licenses/odbl/).

This project is for personal, non-commercial use. The scraped data is not redistributed in bulk; generated KMZ files are intended as personal flight-planning aids.

---

## Notes

- Geocoding is rate-limited to ~1 request per second to comply with Nominatim's [usage policy](https://operations.osmfoundation.org/policies/nominatim/).
- The Atlas Obscura scraper includes polite delays and exponential backoff to respect rate limits.
- The Michelin and Atlas scrapers support checkpoint/resume (`--resume`) so long scraping runs can be interrupted and continued.
- The DDD and Michelin scrapers use a real browser (Chromium via Playwright) to handle JavaScript-rendered content and cookie consent dialogs.
- The Atlas Obscura scraper uses `cloudscraper` (no browser needed) since all data is available in the server-rendered HTML.
