import csv
import json
from pathlib import Path
import time
from urllib.parse import quote_plus
from flask import jsonify, request, render_template, flash, send_from_directory
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from .config import *


HIDDEN_DETAILS = {
    "Claim this business",
    "Identifies as women-owned",
    "Send to your phone",
    "Bu iÅŸletmeyi sahiplenin",
    "Sahibi kadÄ±n olduÄŸunu belirtiyor",
    "Telefonunuza gÃ¶nderin",
}

SEARCH_HISTORY_FILE = Path(__file__).with_name("search_history.json")
EXPORTS_DIR = Path(__file__).with_name("exports")


def clean_business_details(details):
    cleaned_details = []
    phone = None

    for detail in details:
        if detail is None or detail in HIDDEN_DETAILS:
            continue

        if detail.startswith(("05", "(0", "08")):
            phone = detail
            continue

        cleaned_details.append(detail)

    return cleaned_details, phone


def normalize_search_key(keyword, city, district):
    return "|".join([
        keyword.strip().lower(),
        city.strip().lower(),
        district.strip().lower(),
    ])


def normalize_fingerprint(value):
    return " ".join((value or "").strip().lower().split())


def business_fingerprint(business_data):
    return "|".join([
        normalize_fingerprint(business_data.get("name")),
        normalize_fingerprint(business_data.get("address")),
    ])


def load_search_history():
    if not SEARCH_HISTORY_FILE.exists():
        return {}

    try:
        with SEARCH_HISTORY_FILE.open("r", encoding="utf-8") as history_file:
            return json.load(history_file)
    except (json.JSONDecodeError, OSError):
        return {}


def save_search_history(history):
    with SEARCH_HISTORY_FILE.open("w", encoding="utf-8") as history_file:
        json.dump(history, history_file, indent=2)


def save_results_csv(results, search_key):
    EXPORTS_DIR.mkdir(exist_ok=True)
    safe_search_key = "".join(
        char if char.isalnum() else "_"
        for char in search_key
    ).strip("_")
    filename = f"{safe_search_key}_{int(time.time())}.csv"
    filepath = EXPORTS_DIR / filename

    with filepath.open("w", newline="", encoding="utf-8-sig") as csv_file:
        writer = csv.DictWriter(
            csv_file,
            fieldnames=["Name", "Address", "Phone", "Website"],
        )
        writer.writeheader()

        for business in results:
            writer.writerow({
                "Name": business.get("name", ""),
                "Address": business.get("address", ""),
                "Phone": business.get("phone", ""),
                "Website": ", ".join(business.get("details", [])),
            })

    return filename


def get_search_history_entry(history, search_key):
    entry = history.get(search_key, {})

    if isinstance(entry, list):
        return {
            "urls": entry,
            "fingerprints": [],
        }

    return {
        "urls": entry.get("urls", []),
        "fingerprints": entry.get("fingerprints", []),
    }


def first_visible_text(page, selectors):
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if locator.count() > 0 and locator.is_visible(timeout=1000):
                text = locator.inner_text(timeout=2000).strip()
                if text:
                    return text
        except PlaywrightError:
            continue

    return None


def scrape_place(page, business_url, default_address):
    page.goto(
        business_url,
        wait_until="domcontentloaded",
        timeout=60000,
    )

    page.wait_for_selector("h1", timeout=30000)
    page.wait_for_timeout(500)

    name = first_visible_text(page, [
        "h1.DUwDvf",
        "h1",
    ])
    if not name:
        return None

    phone = first_visible_text(page, [
        'button[data-item-id^="phone:tel:"] .Io6YTe',
        'button[aria-label^="Phone:"]',
    ])

    website = first_visible_text(page, [
        'a[data-item-id="authority"] .Io6YTe',
        'a[aria-label^="Website:"]',
    ])

    details = []
    if website:
        details.append(website)

    business_data = {
        "name": name,
        "address": default_address,
        "details": details,
    }

    if phone:
        business_data["phone"] = phone

    return business_data


@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        with sync_playwright() as p:
            keyword = request.form.get("keyword")
            city = request.form.get("city")
            district = request.form.get("district")

            if keyword == "" or city == "" or district == "":
                flash("Please fill in all required fields.")
                return render_template("index.html")

            try:
                result_qtn = int(request.form.get("qtn"))
            except (TypeError, ValueError):
                flash("Please enter a valid number of results.")
                return render_template("index.html")

            search_key = normalize_search_key(keyword, city, district)
            search_history = load_search_history()
            history_entry = get_search_history_entry(search_history, search_key)
            seen_urls = set(history_entry["urls"])
            seen_fingerprints = set(history_entry["fingerprints"])
            target_url_count = result_qtn + len(seen_urls)
            default_address = ", ".join([
                part.strip()
                for part in [district, city]
                if part.strip()
            ])

            browser = p.chromium.launch(headless=True)
            page = browser.new_page(locale="en-US")

            try:
                query = quote_plus(f"{keyword} nearby {district}/{city}")
                page.goto(
                    f"https://www.google.com/maps/search/{query}?hl=en",
                    wait_until="domcontentloaded",
                    timeout=60000,
                )

                page.wait_for_selector('a[class="hfpxzc"]', timeout=60000)
            except PlaywrightTimeoutError:
                browser.close()
                return jsonify({
                    "error": "Google Maps did not load search results in time. Please try again, or complete any visible Google consent/CAPTCHA prompt in the opened browser window."
                }), 504

            businesses = page.query_selector_all('a[class="hfpxzc"]')
            if len(businesses) == 0:
                browser.close()
                return jsonify({
                    "error": "No Google Maps businesses were found for this search."
                }), 404

            businesses[0].click()

            previous_count = 0
            stalled_scrolls = 0

            while len(businesses) < target_url_count and stalled_scrolls < 5:
                page.keyboard.press("End")
                page.wait_for_timeout(2500)
                businesses = page.query_selector_all('a[class="hfpxzc"]')

                if len(businesses) == previous_count:
                    stalled_scrolls += 1
                else:
                    stalled_scrolls = 0

                previous_count = len(businesses)

            businesses = page.query_selector_all('a[class="hfpxzc"]')
            business_urls = []

            for business in businesses:
                href = business.get_attribute("href")
                if href and href not in seen_urls and href not in business_urls:
                    business_urls.append(href)
                if len(business_urls) == result_qtn:
                    break

            if len(business_urls) == 0:
                browser.close()
                return jsonify({
                    "message": "No new businesses found for this same search. Try increasing the result count, changing the area, or clearing search history."
                })

            data = []
            skipped = 0
            returned_urls = []
            returned_fingerprints = []

            for business_url in business_urls:
                try:
                    business_data = scrape_place(page, business_url, default_address)
                except (PlaywrightTimeoutError, PlaywrightError):
                    skipped += 1
                    continue

                if not business_data:
                    skipped += 1
                    continue

                fingerprint = business_fingerprint(business_data)
                if fingerprint in seen_fingerprints or fingerprint in returned_fingerprints:
                    skipped += 1
                    continue

                data = [
                    business for business in data
                    if business.get("name") != business_data["name"]
                ]
                data.append(business_data)
                returned_urls.append(business_url)
                returned_fingerprints.append(fingerprint)

            if len(data) == 0:
                browser.close()
                return jsonify({
                    "error": "Google Maps loaded places, but none of the place detail pages could be read. Complete any visible Google consent/CAPTCHA prompt, then try again."
                }), 502

            browser.close()
            search_history[search_key] = {
                "urls": sorted(seen_urls.union(returned_urls)),
                "fingerprints": sorted(seen_fingerprints.union(returned_fingerprints)),
            }
            save_search_history(search_history)
            csv_filename = save_results_csv(data, search_key)

            return render_template(
                "results.html",
                results=data,
                skipped=skipped,
                csv_filename=csv_filename,
            )
    else:
        return render_template("index.html")


@app.route("/clear-history", methods=["POST"])
def clear_history():
    save_search_history({})
    return jsonify({
        "message": "Search history cleared. Repeated results can appear again."
    })


@app.route("/download-csv/<filename>")
def download_csv(filename):
    return send_from_directory(
        EXPORTS_DIR,
        filename,
        as_attachment=True,
        download_name="google_maps_results.csv",
    )
