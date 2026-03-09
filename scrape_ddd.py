"""
Scrape Diners, Drive-Ins and Dives restaurant listings from Food Network,
geocode addresses, and export as a ForeFlight-compatible KML overlay.
"""

import asyncio
import csv
import json
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urljoin

# --- third-party imports (installed via requirements.txt) ---
from playwright.async_api import async_playwright
from geopy.geocoders import Nominatim
from geopy.extra.rate_limiter import RateLimiter


# ---------------------------------------------------------------------------
# 1.  Scraping
# ---------------------------------------------------------------------------

BASE_URL = "https://www.foodnetwork.com/restaurants/shows/diners-drive-ins-and-dives/a-z"

# Debug: limit number of pages to scrape (set to None for all pages)
# MAX_PAGES = 2
MAX_PAGES = None

# Skip any parsed entry whose name contains these words (navigation garbage)
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


async def scrape_current_page(page):
    """Extract restaurant cards from the current page."""
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


async def scrape_detail_page(page, url):
    """Visit a single restaurant detail page and extract address, description, and phone."""
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


async def click_next_button(page):
    """Click the 'Next' / pagination button. Returns True if successful."""
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


async def scrape_all_restaurants():
    """Main scraping loop – paginates through every listing page."""
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

        print(f"[*] Loading listing page: {BASE_URL}")
        await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(3000)
        await dismiss_consent(page)

        page_num = 1
        while True:
            print(f"\n--- Page {page_num} ---")
            restaurants = await scrape_current_page(page)
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

            # Debug page cap – stop before clicking next
            if MAX_PAGES and page_num >= MAX_PAGES:
                print(f"  Reached debug page limit ({MAX_PAGES}) – stopping.")
                break

            # Try to go to next page
            if not await click_next_button(page):
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
                info = await scrape_detail_page(detail_page, rest["url"])
                rest["address"] = info["address"]
                rest["description"] = info["description"]
                rest["phone"] = info["phone"]
            else:
                rest.setdefault("address", "")
                rest.setdefault("description", "")
                rest.setdefault("phone", "")

        await browser.close()

    return all_restaurants


# ---------------------------------------------------------------------------
# 2.  Summarize descriptions
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# 3.  Geocoding
# ---------------------------------------------------------------------------

def geocode_restaurants(restaurants: list[dict]) -> list[dict]:
    """Add lat/lon to each restaurant using its address."""
    geolocator = Nominatim(user_agent="foreflight_ddd_overlay/1.0")
    geocode = RateLimiter(geolocator.geocode, min_delay_seconds=1.1)

    for i, rest in enumerate(restaurants):
        query = rest.get("address") or rest.get("name", "")
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


# ---------------------------------------------------------------------------
# 4.  ForeFlight KML Export
# ---------------------------------------------------------------------------

def build_kml(restaurants: list[dict], output_path: str):
    """
    Write a KML file that ForeFlight can import as a custom map overlay.
    ForeFlight supports KML/KMZ for custom content (map layers).
    """
    kml_ns = "http://www.opengis.net/kml/2.2"
    ET.register_namespace("", kml_ns)

    kml = ET.Element("kml", xmlns=kml_ns)
    doc = ET.SubElement(kml, "Document")
    ET.SubElement(doc, "name").text = "Diners, Drive-Ins and Dives"
    ET.SubElement(doc, "description").text = (
        "Restaurant locations from Diners, Drive-Ins and Dives – scraped from foodnetwork.com"
    )

    # Define a shared style for the placemarks
    style = ET.SubElement(doc, "Style", id="restaurant-pin")
    icon_style = ET.SubElement(style, "IconStyle")
    ET.SubElement(icon_style, "color").text = "ff0000ff"  # red
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
        if rest.get("phone"):
            desc_parts.append(rest['phone'])
        if rest.get("summary"):
            desc_parts.append(rest['summary'])
        ET.SubElement(pm, "description").text = "\n".join(desc_parts)

        point = ET.SubElement(pm, "Point")
        ET.SubElement(point, "coordinates").text = f"{lon},{lat},0"
        placed += 1

    tree = ET.ElementTree(kml)
    ET.indent(tree, space="  ")
    tree.write(output_path, xml_declaration=True, encoding="UTF-8")
    print(f"\n[✓] KML written to {output_path}")
    print(f"    Placemarks: {placed}  |  Skipped (no coords): {skipped}")


# ---------------------------------------------------------------------------
# 5.  Markdown Export (readable table format)
# ---------------------------------------------------------------------------

def write_markdown(restaurants: list[dict], output_path: str):
    """Write a Markdown file with a table of name, address, description, phone, and GPS coordinates."""
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("# Diners, Drive-Ins and Dives Restaurants\n\n")
        f.write("| Name | Address | Description | Phone | Latitude | Longitude |\n")
        f.write("|------|---------|-------------|-------|----------|-----------|\n")
        for rest in restaurants:
            lat = rest.get("lat", "")
            lon = rest.get("lon", "")
            if not lat or not lon:
                continue
            name = (rest.get("name", "") or "").replace("|", "/")
            addr = (rest.get("address", "") or "").replace("|", "/")
            desc = (rest.get("summary", "") or "").replace("|", "/")
            phone = (rest.get("phone", "") or "").replace("|", "/")
            f.write(f"| {name} | {addr} | {desc} | {phone} | {lat} | {lon} |\n")
    print(f"[\u2713] Markdown written to {output_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    out_dir = Path(__file__).parent / "output"
    out_dir.mkdir(exist_ok=True)

    # Step 1 – Scrape
    print("=" * 60)
    print("STEP 1: Scraping restaurant listings (all pages)…")
    print("=" * 60)
    restaurants = await scrape_all_restaurants()
    print(f"\nTotal restaurants scraped: {len(restaurants)}")

    if not restaurants:
        print("No restaurants were found. The page structure may have changed.")
        print("Saving page source for debugging…")
        return

    # Step 2 – Summarize
    print("\n" + "=" * 60)
    print("STEP 2: Summarizing descriptions…")
    print("=" * 60)
    for rest in restaurants:
        rest["summary"] = summarize(rest.get("description", ""))

    # Step 3 – Geocode
    print("\n" + "=" * 60)
    print("STEP 3: Geocoding addresses…")
    print("=" * 60)
    geocode_restaurants(restaurants)

    coded = sum(1 for r in restaurants if r.get("lat") is not None)
    print(f"  Successfully geocoded: {coded}/{len(restaurants)}")

    # Filter out restaurants missing GPS coordinates or phone number
    before = len(restaurants)
    restaurants = [
        r for r in restaurants
        if r.get("lat") and r.get("lon") and r.get("phone")
    ]
    print(f"  Kept {len(restaurants)}/{before} (skipped {before - len(restaurants)} missing GPS or phone)")

    # Save intermediate JSON (useful for reruns)
    json_path = out_dir / "restaurants.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(restaurants, f, indent=2, ensure_ascii=False)
    print(f"[✓] JSON data saved to {json_path}")

    # Step 4 – Export
    print("\n" + "=" * 60)
    print("STEP 4: Generating ForeFlight KML overlay…")
    print("=" * 60)
    kml_path = str(out_dir / "DDD_Restaurants.kml")
    build_kml(restaurants, kml_path)

    csv_path = str(out_dir / "DDD_Restaurants.md")
    write_markdown(restaurants, csv_path)

    print("\n" + "=" * 60)
    print("DONE! Import DDD_Restaurants.kml into ForeFlight:")
    print("  1. Transfer the .kml file to your iPad")
    print("  2. Open ForeFlight → More → Files")
    print("  3. Import the KML file")
    print("  4. It will appear as a custom map layer under Map → Map Elements")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
