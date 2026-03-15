"""
Scrape restaurant listings and export as ForeFlight-compatible KMZ overlays.

Supported sources:
  --source ddd        Diners, Drive-Ins and Dives (Food Network)
  --source michelin   Michelin Guide US (guide.michelin.com)
  --source atlas      Atlas Obscura US places (atlasobscura.com)

Usage:
  python scrape.py --source ddd
  python scrape.py --source michelin
  python scrape.py --source michelin --max-pages 5
  python scrape.py --source michelin --resume
  python scrape.py --source atlas
  python scrape.py --source atlas --max-pages 10
"""

import argparse
import asyncio
import io
import json
import random
import re
import xml.etree.ElementTree as ET
import zipfile
from html import unescape
from pathlib import Path
from urllib.parse import urljoin

# --- third-party imports (installed via requirements.txt) ---
from playwright.async_api import async_playwright
from geopy.geocoders import Nominatim
from geopy.extra.rate_limiter import RateLimiter


# ---------------------------------------------------------------------------
# 1a. DDD (Diners, Drive-Ins and Dives) Scraper
# ---------------------------------------------------------------------------

DDD_BASE_URL = "https://www.foodnetwork.com/restaurants/shows/diners-drive-ins-and-dives/a-z"

SKIPPED_WORDS = ["Browse", "Next", "Previous", "Reviews", "Restaurants by Show",
                 "City Guides", "States' Plates", "Diners, Drive-Ins and Dives"]


async def dismiss_consent(page):
    """Try to dismiss the cookie / privacy consent dialog if it appears."""
    for selector in [
        "button:has-text('Agree')",
        "button:has-text('Accept')",
        "button:has-text('I Accept')",
        "button:has-text('OK')",
        "button:has-text('Got it')",
        "[class*='consent'] button",
    ]:
        try:
            btn = page.locator(selector).first
            if await btn.is_visible(timeout=3000):
                await btn.click()
                await page.wait_for_timeout(1000)
                return
        except Exception:
            continue


async def ddd_scrape_current_page(page):
    """Extract restaurant cards from the current DDD page."""
    restaurants = []

    # Wait for restaurant listing to load
    await page.wait_for_timeout(2000)

    # Food Network restaurant listings use various selectors – try common ones
    card_selectors = [
        "div.o-CatalogListing a.o-CatalogListing__a-Item",
        "ul.o-CatalogListing a",
        "section.o-Catalog a",
        ".m-MediaBlock",
        "article",
        ".o-ResultCard",
        "li.o-ResultCard",
        "div[class*='restaurant']",
        "div[class*='listing'] a",
        "a[class*='Item']",
    ]

    # Attempt to find cards from page content
    # First, let's get all the links that point to /restaurants/ paths
    restaurant_links = await page.query_selector_all("a[href*='/restaurants/']")
    
    seen_names = set()
    
    for link in restaurant_links:
        try:
            name_el = await link.query_selector("span.m-MediaBlock__a-HeadlineText, h3, .headline, [class*='Headline'], [class*='title']")
            if not name_el:
                # Try using the link text itself
                text = (await link.inner_text()).strip()
                if not text or len(text) < 3 or len(text) > 150:
                    continue
                name = text.split("\n")[0].strip()
            else:
                name = (await name_el.inner_text()).strip()

            if not name or name in seen_names:
                continue
            # Filter out navigation / garbage entries
            if any(word.lower() in name.lower() for word in SKIPPED_WORDS):
                continue
            seen_names.add(name)

            # Try to get description / address from parent or sibling
            parent = await link.evaluate_handle("el => el.closest('li') || el.closest('div') || el.parentElement")
            parent_text = (await parent.inner_text() if parent else "").strip() if parent else ""

            href = await link.get_attribute("href") or ""

            restaurants.append({
                "name": name,
                "raw_text": parent_text,
                "url": urljoin("https://www.foodnetwork.com", href),
            })
        except Exception:
            continue

    return restaurants


async def ddd_scrape_detail_page(page, url):
    """Visit a single DDD detail page and extract address, description, and phone."""
    info = {"address": "", "description": "", "phone": ""}
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(1500)
        await dismiss_consent(page)

        # --- address ---
        for sel in [
            "[class*='Address']",
            "[class*='address']",
            "span[itemprop='streetAddress']",
            ".o-RestaurantInfo__a-Address",
            ".restaurant-address",
        ]:
            el = page.locator(sel).first
            try:
                if await el.is_visible(timeout=1000):
                    info["address"] = (await el.inner_text()).strip()
                    break
            except Exception:
                continue

        # Clean up address: remove trailing "| Get Directions" and similar
        if info["address"]:
            info["address"] = re.split(r"\s*\|\s*", info["address"])[0].strip()

        # If we still don't have an address, try scraping text that looks like one
        if not info["address"]:
            body_text = await page.inner_text("body")
            # Look for typical US address pattern
            match = re.search(
                r"\d{1,5}\s[\w\s.]+(?:Street|St|Avenue|Ave|Boulevard|Blvd|Road|Rd|Drive|Dr|Lane|Ln|Way|Hwy|Highway|Pike|Place|Pl)[.,]?\s*[\w\s]*,\s*[A-Z]{2}\s*\d{5}",
                body_text,
            )
            if match:
                info["address"] = match.group(0).strip()

        # --- description ---
        for sel in [
            ".o-RestaurantInfo__a-Description",
            "[class*='Description']",
            "[class*='description']",
            "meta[name='description']",
            ".article-body p:first-of-type",
            ".o-AssetDescription",
            "p",
        ]:
            el = page.locator(sel).first
            try:
                if sel.startswith("meta"):
                    val = await el.get_attribute("content")
                    if val:
                        info["description"] = val.strip()
                        break
                elif await el.is_visible(timeout=1000):
                    info["description"] = (await el.inner_text()).strip()
                    break
            except Exception:
                continue

        # --- phone ---
        for sel in [
            "[class*='Phone']",
            "[class*='phone']",
            "a[href^='tel:']",
            "span[itemprop='telephone']",
            ".o-RestaurantInfo__a-Phone",
        ]:
            el = page.locator(sel).first
            try:
                if await el.is_visible(timeout=1000):
                    info["phone"] = (await el.inner_text()).strip()
                    break
            except Exception:
                continue

        # Fallback: search body text for phone pattern
        if not info["phone"]:
            body_text = await page.inner_text("body")
            match = re.search(r"\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}", body_text)
            if match:
                info["phone"] = match.group(0).strip()

        # Clean up phone: strip leading "Phone:" prefix if present
        if info["phone"]:
            info["phone"] = re.sub(r"^Phone:\s*", "", info["phone"], flags=re.IGNORECASE).strip()

    except Exception as exc:
        print(f"  ⚠ Could not load detail page {url}: {exc}")

    return info


async def ddd_click_next_button(page):
    """Click the DDD 'Next' / pagination button. Returns True if successful."""
    next_selectors = [
        "a:has-text('Next')",
        "button:has-text('Next')",
        "a[aria-label='Next']",
        "a.o-Pagination__a-NextButton",
        ".o-Pagination__a-Button--next",
        "a[class*='next']",
        "a[class*='Next']",
        "li.next a",
        "[rel='next']",
        ".pagination .next a",
        "a[title='Next']",
    ]
    for sel in next_selectors:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=2000):
                await btn.click()
                await page.wait_for_timeout(2500)
                return True
        except Exception:
            continue
    return False


async def scrape_all_ddd(max_pages=None):
    """Main DDD scraping loop – paginates through every listing page."""
    all_restaurants = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page = await context.new_page()

        print(f"[*] Loading listing page: {DDD_BASE_URL}")
        await page.goto(DDD_BASE_URL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(3000)
        await dismiss_consent(page)

        page_num = 1
        while True:
            print(f"\n--- Page {page_num} ---")
            restaurants = await ddd_scrape_current_page(page)
            if not restaurants:
                print("  No restaurants found on this page – trying alternate parse…")
                # Fallback: grab all text and parse
                body = await page.inner_text("body")
                print(f"  (body length: {len(body)} chars)")
                # If truly empty, break
                if page_num > 1:
                    break
            else:
                print(f"  Found {len(restaurants)} restaurant(s)")
                all_restaurants.extend(restaurants)

            if max_pages and page_num >= max_pages:
                print(f"  Reached page limit ({max_pages}) – stopping.")
                break

            if not await ddd_click_next_button(page):
                print("  No more pages (Next button not found).")
                break

            page_num += 1

            # Safety cap
            if page_num > 200:
                print("  Reached page cap – stopping.")
                break

        # ---- Now visit each detail page to get address + description ----
        print(f"\n[*] Visiting {len(all_restaurants)} detail pages for addresses & descriptions …")
        detail_page = await context.new_page()

        for i, rest in enumerate(all_restaurants):
            pct = f"[{i + 1}/{len(all_restaurants)}]"
            if rest.get("url"):
                print(f"  {pct} {rest['name']}")
                info = await ddd_scrape_detail_page(detail_page, rest["url"])
                rest["address"] = info["address"]
                rest["description"] = info["description"]
                rest["phone"] = info["phone"]
            else:
                rest.setdefault("address", "")
                rest.setdefault("description", "")
                rest.setdefault("phone", "")

        await browser.close()

    return all_restaurants


def summarize(description: str, max_words: int = 20) -> str:
    """Trim a description to a short sentence."""
    if not description:
        return ""
    # Take the first sentence
    first_sentence = re.split(r"(?<=[.!?])\s+", description)[0]
    words = first_sentence.split()
    if len(words) <= max_words:
        return first_sentence
    return " ".join(words[:max_words]) + "…"


def geocode_restaurants(restaurants: list[dict]) -> list[dict]:
    """Add lat/lon to each restaurant using its address (skips if already set)."""
    geolocator = Nominatim(user_agent="foreflight_overlay_scraper/1.0")
    geocode = RateLimiter(geolocator.geocode, min_delay_seconds=1.1)

    for i, rest in enumerate(restaurants):
        if rest.get("lat") is not None and rest.get("lon") is not None:
            continue
        query = rest.get("address") or rest.get("location") or rest.get("name", "")
        if not query:
            rest["lat"] = None
            rest["lon"] = None
            continue

        print(f"  Geocoding [{i + 1}/{len(restaurants)}]: {query[:80]}")
        try:
            location = geocode(query, timeout=10)
            if location:
                rest["lat"] = location.latitude
                rest["lon"] = location.longitude
            else:
                # Fallback: try just name + state if address failed
                rest["lat"] = None
                rest["lon"] = None
        except Exception as exc:
            print(f"    ⚠ Geocoding failed: {exc}")
            rest["lat"] = None
            rest["lon"] = None

    return restaurants


def build_kml(restaurants: list[dict], output_path: str,
              title: str = "Diners, Drive-Ins and Dives",
              subtitle: str = "Restaurant locations from Diners, Drive-Ins and Dives \u2013 scraped from foodnetwork.com",
              pin_color: str = "ff0000ff",
              include_phone: bool = True):
    """
    Write a KMZ file that ForeFlight can import as a custom map overlay.
    ForeFlight supports KML/KMZ for custom content (map layers).
    """
    kml_ns = "http://www.opengis.net/kml/2.2"
    ET.register_namespace("", kml_ns)

    kml = ET.Element("kml", xmlns=kml_ns)
    doc = ET.SubElement(kml, "Document")
    ET.SubElement(doc, "name").text = title
    ET.SubElement(doc, "description").text = subtitle

    # Define a shared style for the placemarks
    style = ET.SubElement(doc, "Style", id="restaurant-pin")
    icon_style = ET.SubElement(style, "IconStyle")
    ET.SubElement(icon_style, "color").text = pin_color
    ET.SubElement(icon_style, "scale").text = "1.2"
    icon = ET.SubElement(icon_style, "Icon")
    ET.SubElement(icon, "href").text = (
        "http://maps.google.com/mapfiles/kml/paddle/red-circle.png"
    )

    placed = 0
    skipped = 0

    for rest in restaurants:
        lat = rest.get("lat")
        lon = rest.get("lon")
        if lat is None or lon is None:
            skipped += 1
            continue

        pm = ET.SubElement(doc, "Placemark")
        ET.SubElement(pm, "name").text = rest.get("name", "Unknown")
        ET.SubElement(pm, "styleUrl").text = "#restaurant-pin"

        desc_parts = []
        if rest.get("address"):
            desc_parts.append(rest['address'])
        if include_phone and rest.get("phone"):
            desc_parts.append(rest['phone'])
        if rest.get("summary"):
            desc_parts.append(rest['summary'])
        ET.SubElement(pm, "description").text = "\n".join(desc_parts)

        point = ET.SubElement(pm, "Point")
        ET.SubElement(point, "coordinates").text = f"{lon},{lat},0"
        placed += 1

    tree = ET.ElementTree(kml)
    ET.indent(tree, space="  ")
    kml_bytes = io.BytesIO()
    tree.write(kml_bytes, xml_declaration=True, encoding="UTF-8")
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("doc.kml", kml_bytes.getvalue())
    print(f"\n[✓] KMZ written to {output_path}")
    print(f"    Placemarks: {placed}  |  Skipped (no coords): {skipped}")


def write_markdown(restaurants: list[dict], output_path: str,
                   title: str = "Diners, Drive-Ins and Dives Restaurants",
                   include_phone: bool = True):
    """Write a Markdown file with a table of name, address, description, and GPS coordinates."""
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(f"# {title}\n\n")
        if include_phone:
            f.write("| Name | Address | Description | Phone | Latitude | Longitude |\n")
            f.write("|------|---------|-------------|-------|----------|-----------|\n")
        else:
            f.write("| Name | Address | Description | Latitude | Longitude |\n")
            f.write("|------|---------|-------------|----------|-----------|\n")
        for rest in restaurants:
            lat = rest.get("lat", "")
            lon = rest.get("lon", "")
            if not lat or not lon:
                continue
            name = (rest.get("name", "") or "").replace("|", "/")
            addr = (rest.get("address", "") or "").replace("|", "/")
            desc = (rest.get("summary", "") or "").replace("|", "/")
            if include_phone:
                phone = (rest.get("phone", "") or "").replace("|", "/")
                f.write(f"| {name} | {addr} | {desc} | {phone} | {lat} | {lon} |\n")
            else:
                f.write(f"| {name} | {addr} | {desc} | {lat} | {lon} |\n")
    print(f"[\u2713] Markdown written to {output_path}")


# ---------------------------------------------------------------------------
# 1b. Atlas Obscura Scraper
# ---------------------------------------------------------------------------

ATLAS_LIST_URL = "https://www.atlasobscura.com/things-to-do/united-states/places"


def _atlas_parse_page(html: str) -> list[dict]:
    """Parse Atlas Obscura listing page HTML and return place dicts."""
    places = []
    # Each card: <a class="Card --content-card-v2 ..." data-lat="x" data-lng="y" ... href="/places/slug">
    card_re = re.compile(
        r'<a\s+class="Card\s+--content-card-v2[^"]*"([^>]+)>(.*?)</a>',
        re.DOTALL,
    )
    for m in card_re.finditer(html):
        attrs_str, inner = m.group(1), m.group(2)
        # Extract data attributes
        def attr(name):
            am = re.search(rf'{name}="([^"]*)"', attrs_str)
            return am.group(1) if am else ""

        href = attr("href")
        lat_s = attr("data-lat")
        lng_s = attr("data-lng")
        city = attr("data-city")
        state = attr("data-state")

        # Title: <h3 class="Card__heading ..."><span>Name</span></h3>
        title_m = re.search(r'<span>([^<]+)</span>', inner)
        name = unescape(title_m.group(1).strip()) if title_m else ""

        # Description: <div class="Card__content js-subtitle-content">text</div>
        desc_m = re.search(
            r'class="Card__content\s+js-subtitle-content">\s*(.+?)\s*</div>', inner, re.DOTALL
        )
        if not desc_m:
            # Fallback: try any <p> inside the card
            desc_m = re.search(r'<p[^>]*>\s*(.+?)\s*</p>', inner, re.DOTALL)
        description = unescape(re.sub(r'<[^>]+>', '', desc_m.group(1)).strip()) if desc_m else ""

        if not name or not lat_s or not lng_s:
            continue

        location = f"{city}, {state}" if city and state else city or state or ""

        places.append({
            "name": name,
            "url": f"https://www.atlasobscura.com{href}" if href else "",
            "lat": float(lat_s),
            "lon": float(lng_s),
            "address": location,
            "description": description,
        })
    return places


def scrape_all_atlas(max_pages: int | None = None,
                     checkpoint_path: str | None = None) -> list[dict]:
    """Scrape all Atlas Obscura US places using cloudscraper (no browser needed)."""
    import cloudscraper
    import time as _time

    # Resume from checkpoint if available
    all_places: list[dict] = []
    seen_urls: set[str] = set()
    start_page = 1
    if checkpoint_path and Path(checkpoint_path).exists():
        with open(checkpoint_path, "r", encoding="utf-8") as f:
            ckpt = json.load(f)
        all_places = ckpt.get("places", [])
        start_page = ckpt.get("next_page", 1)
        seen_urls = {p["url"] for p in all_places}
        print(f"  [checkpoint] Resuming from page {start_page} "
              f"with {len(all_places)} places already collected")

    scraper = cloudscraper.create_scraper()
    page_num = start_page
    cap = max_pages or 99999
    empties = 0
    base_delay = 3.0          # seconds between requests
    max_retries = 6           # retries per page on 429

    while page_num <= cap:
        url = ATLAS_LIST_URL if page_num == 1 else f"{ATLAS_LIST_URL}?page={page_num}"
        print(f"  Page {page_num} …", end=" ", flush=True)

        resp = None
        for attempt in range(max_retries):
            try:
                resp = scraper.get(url, timeout=30)
            except Exception as exc:
                print(f"(request failed: {exc})")
                resp = None
                break

            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", 0))
                wait = max(retry_after, base_delay * 2 ** attempt) + random.uniform(1, 5)
                print(f"(429 – waiting {wait:.0f}s)", end=" ", flush=True)
                _time.sleep(wait)
                # Rotate session after 2 consecutive 429s to get fresh cookies
                if attempt >= 2:
                    scraper = cloudscraper.create_scraper()
                continue
            break  # got a non-429 response

        if resp is None:
            empties += 1
            if empties >= 3:
                break
            page_num += 1
            continue

        if resp.status_code != 200:
            print(f"(HTTP {resp.status_code})")
            empties += 1
            if empties >= 3:
                break
            page_num += 1
            continue

        batch = _atlas_parse_page(resp.text)
        if not batch:
            empties += 1
            print("(empty)")
            if empties >= 3:
                break
        else:
            empties = 0
            new = 0
            for p in batch:
                if p["url"] not in seen_urls:
                    seen_urls.add(p["url"])
                    all_places.append(p)
                    new += 1
            print(f"({new} new → {len(all_places)} total)")

        # Save checkpoint every 25 pages
        if checkpoint_path and page_num % 25 == 0:
            with open(checkpoint_path, "w", encoding="utf-8") as f:
                json.dump({"places": all_places, "next_page": page_num + 1},
                          f, ensure_ascii=False)
            print(f"    [checkpoint saved – {len(all_places)} places]")

        page_num += 1
        # Polite delay between pages to avoid 429s
        _time.sleep(base_delay + random.uniform(1, 3))

    # Final checkpoint
    if checkpoint_path:
        with open(checkpoint_path, "w", encoding="utf-8") as f:
            json.dump({"places": all_places, "next_page": page_num},
                      f, ensure_ascii=False)

    return all_places


# ---------------------------------------------------------------------------
# 1c. Michelin Guide Scraper
# ---------------------------------------------------------------------------

MICHELIN_BASE = "https://guide.michelin.com"
MICHELIN_LIST_URL = MICHELIN_BASE + "/us/en/restaurants/page/{page}"


async def michelin_scrape_listing_page(page, page_num):
    """Scrape restaurant cards from one Michelin Guide listing page."""
    url = MICHELIN_LIST_URL.format(page=page_num)
    try:
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        if resp and resp.status >= 400:
            return []
    except Exception:
        return []
    await page.wait_for_timeout(2000)

    # Collect all unique restaurant hrefs and the best name/card text for each
    GENERIC_NAMES = {"reserve a table", "book a table", "see the menu",
                     "view menu", "get directions", "read more"}
    href_data: dict[str, dict] = {}  # full_url -> {name, card_text}
    links = await page.query_selector_all("a[href*='/restaurant/']")

    for link in links:
        try:
            href = await link.get_attribute("href") or ""
            if "/restaurants/" in href or "/restaurant/" not in href:
                continue
            full_url = urljoin(MICHELIN_BASE, href)

            text = (await link.inner_text()).strip()
            name = text.split("\n")[0].strip() if text else ""
            name = re.sub(r"^Open\s+", "", name).strip()
            # Reject names that are HTML tags, URLs, or generic action phrases
            if name.startswith("<") or name.startswith("http"):
                name = ""
            if name.lower() in GENERIC_NAMES:
                name = ""

            # Try to read card context from parent container
            card_text = ""
            try:
                parent = await link.evaluate_handle(
                    "el => el.closest('[class*=\"card\"]') || "
                    "el.closest('li') || el.parentElement"
                )
                if parent:
                    card_text = (await parent.inner_text()).strip()
            except Exception:
                pass

            # Keep the version with the longest name (image links have empty text)
            existing = href_data.get(full_url)
            if not existing or len(name) > len(existing.get("name", "")):
                href_data[full_url] = {"name": name, "card_text": card_text}
        except Exception:
            continue

    # Build restaurant list from deduplicated data
    restaurants = []
    for full_url, data in href_data.items():
        name = data["name"]
        # Derive name from URL slug as fallback
        if not name or len(name) < 2:
            slug = full_url.rstrip("/").rsplit("/", 1)[-1]
            name = slug.replace("-", " ").title()
        if not name or len(name) > 200:
            continue

        card_text = data["card_text"]
        location = ""
        cuisine = ""
        price = ""
        distinction = ""
        for cline in card_text.split("\n"):
            cline = cline.strip()
            if re.search(r",\s*\w{2,},?\s*USA", cline) and not location:
                location = cline
            m = re.match(r"(\$+)\s*\u00b7\s*(.+)", cline)
            if m:
                price = m.group(1)
                cuisine = m.group(2).strip()
            for tag in ["MICHELIN Star", "Bib Gourmand", "Green Star",
                        "Two Stars", "Three Stars", "One Star"]:
                if tag.lower() in cline.lower():
                    distinction = cline.strip()

        restaurants.append({
            "name": name,
            "url": full_url,
            "location": location,
            "cuisine": cuisine,
            "price": price,
            "distinction": distinction,
        })

    return restaurants


async def michelin_scrape_detail(page, url):
    """Extract address, phone, description, and coords from a Michelin detail page."""
    info = {"address": "", "description": "", "phone": "", "lat": None, "lon": None}
    try:
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        if resp and resp.status >= 400:
            return info
        await page.wait_for_timeout(800 + random.randint(0, 1200))

        # --- JSON-LD structured data (most reliable for lat/lon) ---
        scripts = await page.query_selector_all("script[type='application/ld+json']")
        for script in scripts:
            try:
                raw = await script.inner_text()
                data = json.loads(raw)
                if isinstance(data, list):
                    data = data[0]
                if "Restaurant" in str(data.get("@type", "")):
                    addr = data.get("address", {})
                    if isinstance(addr, dict):
                        parts = [
                            addr.get("streetAddress", ""),
                            addr.get("addressLocality", ""),
                            addr.get("addressRegion", ""),
                            addr.get("postalCode", ""),
                        ]
                        info["address"] = ", ".join(p for p in parts if p)
                    geo = data.get("geo", {})
                    if isinstance(geo, dict):
                        lat = geo.get("latitude")
                        lon = geo.get("longitude")
                        if lat and lon:
                            flat, flon = float(lat), float(lon)
                            if abs(flat) > 0.1 and abs(flon) > 0.1:
                                info["lat"] = flat
                                info["lon"] = flon
                    info["phone"] = data.get("telephone", "") or ""
                    info["description"] = data.get("description", "") or ""
                    break
            except Exception:
                continue

        # --- Fallback CSS selectors ---
        if not info["address"]:
            for sel in [
                "[class*='restaurant-details__heading--address'] span",
                "[class*='address']", "[itemprop='streetAddress']",
            ]:
                try:
                    el = page.locator(sel).first
                    if await el.is_visible(timeout=800):
                        info["address"] = (await el.inner_text()).strip()
                        break
                except Exception:
                    continue

        if not info["description"]:
            try:
                el = page.locator("meta[name='description']").first
                val = await el.get_attribute("content")
                if val:
                    info["description"] = val.strip()
            except Exception:
                pass

        if not info["phone"]:
            try:
                el = page.locator("a[href^='tel:']").first
                if await el.is_visible(timeout=800):
                    info["phone"] = (await el.inner_text()).strip()
            except Exception:
                pass

    except Exception as exc:
        print(f"  \u26a0 Detail page failed {url}: {exc}")

    return info


async def _michelin_detail_worker(context, queue, results, progress):
    """Worker coroutine: pulls restaurants off queue, scrapes detail pages."""
    page = await context.new_page()
    while True:
        try:
            rest = queue.get_nowait()
        except asyncio.QueueEmpty:
            break

        progress["done"] += 1
        n, total = progress["done"], progress["total"]
        if n % 50 == 0 or n == 1 or n == total:
            print(f"  Detail pages: {n}/{total} ({100 * n // total}%)")

        info = await michelin_scrape_detail(page, rest["url"])
        rest.update(info)
        results.append(rest)

        # Periodic checkpoint
        if n % 500 == 0:
            cp = progress.get("checkpoint")
            if cp:
                with open(cp, "w", encoding="utf-8") as f:
                    json.dump(results, f, ensure_ascii=False)
                print(f"    [checkpoint] {len(results)} restaurants saved")

    await page.close()


async def scrape_all_michelin(max_pages=None, concurrency=5, checkpoint_path=None):
    """Scrape all Michelin Guide US restaurants (listing pages then detail pages)."""
    already = {}
    if checkpoint_path and Path(checkpoint_path).exists():
        with open(checkpoint_path, "r", encoding="utf-8") as f:
            already = {r["url"]: r for r in json.load(f) if r.get("url")}
        print(f"[*] Checkpoint loaded: {len(already)} restaurants already scraped")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )

        # Phase 1: listing pages
        print("[*] Phase 1 \u2013 collecting restaurant URLs from listing pages \u2026")
        lpage = await ctx.new_page()
        all_rests = []
        seen_urls = set()
        page_num = 1
        cap = max_pages or 99999
        empties = 0

        while page_num <= cap:
            print(f"  Page {page_num} \u2026", end=" ", flush=True)
            batch = await michelin_scrape_listing_page(lpage, page_num)
            if not batch:
                empties += 1
                print("(empty)")
                if empties >= 3:
                    break
            else:
                empties = 0
                new = 0
                for r in batch:
                    if r["url"] not in seen_urls:
                        seen_urls.add(r["url"])
                        all_rests.append(r)
                        new += 1
                print(f"({new} new \u2192 {len(all_rests)} total)")
            page_num += 1

        await lpage.close()
        print(f"\n[*] Collected {len(all_rests)} unique restaurant URLs")

        # Phase 2: detail pages
        to_scrape = [r for r in all_rests if r["url"] not in already]
        results = list(already.values())
        print(
            f"[*] Phase 2 \u2013 scraping {len(to_scrape)} detail pages "
            f"({len(results)} from checkpoint) \u2026"
        )

        if to_scrape:
            q = asyncio.Queue()
            for r in to_scrape:
                q.put_nowait(r)
            prog = {"done": 0, "total": len(to_scrape), "checkpoint": checkpoint_path}
            workers = [
                _michelin_detail_worker(ctx, q, results, prog)
                for _ in range(min(concurrency, len(to_scrape)))
            ]
            await asyncio.gather(*workers)

        # Final checkpoint
        if checkpoint_path:
            with open(checkpoint_path, "w", encoding="utf-8") as f:
                json.dump(results, f, ensure_ascii=False)

        await browser.close()

    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    parser = argparse.ArgumentParser(
        description="Scrape restaurant listings and generate ForeFlight KMZ overlay."
    )
    parser.add_argument(
        "--source", choices=["ddd", "michelin", "atlas"], default="ddd",
        help="ddd = Diners Drive-Ins & Dives, michelin = Michelin Guide US, atlas = Atlas Obscura US",
    )
    parser.add_argument("--max-pages", type=int, default=None,
                        help="Max listing pages to scrape (default: all)")
    parser.add_argument("--concurrency", type=int, default=5,
                        help="Concurrent detail scrapers – Michelin only (default: 5)")
    parser.add_argument("--resume", action="store_true",
                        help="Resume from checkpoint (Michelin and Atlas)")
    args = parser.parse_args()

    out_dir = Path(__file__).parent / "output"
    out_dir.mkdir(exist_ok=True)

    if args.source == "atlas":
        atlas_ckpt = str(out_dir / "atlas_checkpoint.json") if args.resume else None
        print("=" * 60)
        print("STEP 1: Scraping Atlas Obscura US places …")
        print("=" * 60)
        places = scrape_all_atlas(max_pages=args.max_pages,
                                  checkpoint_path=atlas_ckpt)
        print(f"\nTotal places collected: {len(places)}")

        if not places:
            print("No places found. The site structure may have changed.")
            return

        print("\n" + "=" * 60)
        print("STEP 2: Summarizing descriptions …")
        print("=" * 60)
        for p in places:
            p["summary"] = summarize(p.get("description", ""))

        # All Atlas cards already include lat/lon, so no geocoding needed
        coded = sum(1 for p in places if p.get("lat") is not None)
        print(f"  Coordinates available: {coded}/{len(places)}")

        json_path = out_dir / "AtlasObscura_StrangeSites.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(places, f, indent=2, ensure_ascii=False)
        print(f"[\u2713] JSON saved to {json_path}")

        print("\n" + "=" * 60)
        print("STEP 3: Generating ForeFlight KMZ overlay …")
        print("=" * 60)
        build_kml(
            places, str(out_dir / "AtlasObscura_StrangeSites.kmz"),
            title="Atlas Obscura Strange Sites",
            subtitle="Atlas Obscura unusual places in the United States \u2013 atlasobscura.com",
            pin_color="ff86b8ff",  # orange in KML AABBGGRR
            include_phone=False,
        )
        write_markdown(
            places, str(out_dir / "AtlasObscura_StrangeSites.md"),
            title="Atlas Obscura Strange Sites",
            include_phone=False,
        )

        print("\n" + "=" * 60)
        print("DONE! Import AtlasObscura_StrangeSites.kmz into ForeFlight.")
        print("=" * 60)

    elif args.source == "michelin":
        checkpoint = str(out_dir / "michelin_checkpoint.json")

        print("=" * 60)
        print("STEP 1: Scraping Michelin Guide restaurant listings …")
        print("=" * 60)
        restaurants = await scrape_all_michelin(
            max_pages=args.max_pages,
            concurrency=args.concurrency,
            checkpoint_path=checkpoint,
        )
        print(f"\nTotal restaurants collected: {len(restaurants)}")

        if not restaurants:
            print("No restaurants found. The site structure may have changed.")
            return

        print("\n" + "=" * 60)
        print("STEP 2: Summarizing descriptions …")
        print("=" * 60)
        for rest in restaurants:
            # Use listing-page location as fallback address
            if not rest.get("address") and rest.get("location"):
                rest["address"] = rest["location"]
            parts = []
            if rest.get("distinction"):
                parts.append(rest["distinction"])
            if rest.get("cuisine"):
                parts.append(rest["cuisine"])
            if rest.get("price"):
                parts.append(rest["price"])
            desc = rest.get("description", "")
            if desc:
                parts.append(summarize(desc))
            rest["summary"] = " | ".join(parts) if parts else ""

        print("\n" + "=" * 60)
        print("STEP 3: Geocoding restaurants without coordinates …")
        print("=" * 60)
        need_geo = [r for r in restaurants
                    if r.get("lat") is None or r.get("lon") is None]
        if need_geo:
            print(f"  {len(need_geo)} restaurants need geocoding …")
            geocode_restaurants(need_geo)
        else:
            print("  All restaurants already have coordinates.")

        coded = sum(1 for r in restaurants if r.get("lat") is not None)
        print(f"  Coordinates available: {coded}/{len(restaurants)}")

        json_path = out_dir / "michelin_restaurants.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(restaurants, f, indent=2, ensure_ascii=False)
        print(f"[✓] JSON saved to {json_path}")

        print("\n" + "=" * 60)
        print("STEP 4: Generating ForeFlight KMZ overlay …")
        print("=" * 60)
        build_kml(
            restaurants, str(out_dir / "Michelin_Restaurants.kmz"),
            title="Michelin Guide Restaurants",
            subtitle="Michelin Guide restaurant locations – guide.michelin.com",
            pin_color="ff00d7ff",  # gold in KML AABBGGRR
        )
        write_markdown(
            restaurants, str(out_dir / "Michelin_Restaurants.md"),
            title="Michelin Guide Restaurants",
        )

        print("\n" + "=" * 60)
        print("DONE! Import Michelin_Restaurants.kmz into ForeFlight.")
        print("=" * 60)

    else:
        # ---- DDD (original flow) ----
        print("=" * 60)
        print("STEP 1: Scraping DDD restaurant listings (all pages) …")
        print("=" * 60)
        restaurants = await scrape_all_ddd(max_pages=args.max_pages)
        print(f"\nTotal restaurants scraped: {len(restaurants)}")

        if not restaurants:
            print("No restaurants found. The page structure may have changed.")
            return

        print("\n" + "=" * 60)
        print("STEP 2: Summarizing descriptions …")
        print("=" * 60)
        for rest in restaurants:
            rest["summary"] = summarize(rest.get("description", ""))

        print("\n" + "=" * 60)
        print("STEP 3: Geocoding addresses …")
        print("=" * 60)
        geocode_restaurants(restaurants)

        coded = sum(1 for r in restaurants if r.get("lat") is not None)
        print(f"  Geocoded: {coded}/{len(restaurants)}")

        before = len(restaurants)
        restaurants = [
            r for r in restaurants
            if r.get("lat") and r.get("lon") and r.get("phone")
        ]
        print(f"  Kept {len(restaurants)}/{before}")

        json_path = out_dir / "DDD_Restaurants.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(restaurants, f, indent=2, ensure_ascii=False)
        print(f"[✓] JSON saved to {json_path}")

        print("\n" + "=" * 60)
        print("STEP 4: Generating ForeFlight KMZ overlay …")
        print("=" * 60)
        build_kml(restaurants, str(out_dir / "DDD_Restaurants.kmz"))
        write_markdown(restaurants, str(out_dir / "DDD_Restaurants.md"))

        print("\n" + "=" * 60)
        print("DONE! Import DDD_Restaurants.kmz into ForeFlight:")
        print("  1. Transfer the .kmz file to your iPad")
        print("  2. Open ForeFlight → More → Files")
        print("  3. Import the KMZ file")
        print("  4. It appears as a custom map layer under Map → Map Elements")
        print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
