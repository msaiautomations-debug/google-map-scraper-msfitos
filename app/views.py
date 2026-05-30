import csv
import io
import json
from pathlib import Path
import re
from difflib import SequenceMatcher
import time
from urllib.error import URLError
from urllib.parse import quote_plus, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from flask import Response, jsonify, request, render_template, flash, redirect, send_from_directory, url_for
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
CITY_WIDE_AREA = "all areas"
RESULT_LINK_SELECTOR = 'a[class="hfpxzc"]'
RESULT_FEED_SELECTORS = [
    'div[role="feed"]',
    'div[aria-label*="Results for"]',
]
DEFAULT_SCRAPE_CAP = 300
NICHE_KEYWORD_EXPANSIONS = {
    "clothing": [
        "clothing store",
        "mens clothing store",
        "womens clothing store",
        "fashion store",
        "garment shop",
        "apparel store",
        "boutique",
        "ethnic wear store",
        "kids clothing store",
        "saree shop",
        "jeans store",
        "western wear store",
    ],
    "salon": [
        "salon",
        "saloon",
        "beauty salon",
        "hair salon",
        "mens salon",
        "unisex salon",
        "beauty parlour",
        "makeup studio",
        "spa salon",
    ],
    "saloon": [
        "salon",
        "saloon",
        "beauty salon",
        "hair salon",
        "mens salon",
        "unisex salon",
        "beauty parlour",
        "makeup studio",
        "spa salon",
    ],
    "gym": [
        "gym",
        "gyms",
        "fitness centre",
        "fitness club",
        "training center",
        "health club",
        "crossfit gym",
        "personal trainer",
        "yoga studio",
    ],
}
GRID_STEP_DEGREES = 0.035
GRID_DEPTHS = {
    "fast": [-1, 0, 1],
    "deep": [-2, -1, 0, 1, 2],
}
MAX_EXPANDED_KEYWORDS = 12
AUTOMATIC_CATEGORY_PATTERNS = [
    "{keyword}",
    "best {keyword}",
    "{keyword} services",
]
BLOCKED_SUGGESTION_WORDS = {
    "admission",
    "career",
    "careers",
    "course",
    "courses",
    "job",
    "jobs",
    "salary",
    "training",
    "vacancy",
    "vacancies",
}
GENERIC_NAME_WORDS = {
    "a",
    "academy",
    "agra",
    "area",
    "baluganj",
    "and",
    "best",
    "bhadoriya",
    "branch",
    "by",
    "center",
    "centre",
    "city",
    "club",
    "dayalbagh",
    "dwara",
    "for",
    "gadhi",
    "guru",
    "gym",
    "gyms",
    "health",
    "hub",
    "in",
    "india",
    "ii",
    "ladies",
    "luxury",
    "kamla",
    "khandari",
    "nagar",
    "near",
    "of",
    "paschimpuri",
    "premium",
    "shastripuram",
    "sikandra",
    "studio",
    "the",
    "training",
    "unit",
    "unisex",
    "vijay",
    "workout",
    "yoga",
    "zumba",
}
MIN_SIMILARITY = 0.88


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
        (district or CITY_WIDE_AREA).strip().lower(),
    ])


def normalize_url(value):
    if not value:
        return ""

    parsed_url = urlsplit(value.strip())
    return urlunsplit((
        parsed_url.scheme,
        parsed_url.netloc,
        parsed_url.path.rstrip("/"),
        "",
        "",
    ))


def parse_keywords(value):
    keywords = []
    seen_keywords = set()

    for keyword in (value or "").split(","):
        cleaned_keyword = " ".join(keyword.strip().split())
        keyword_key = cleaned_keyword.lower()
        if cleaned_keyword and keyword_key not in seen_keywords:
            keywords.append(cleaned_keyword)
            seen_keywords.add(keyword_key)

    return keywords


def clean_suggested_keyword(suggestion, city):
    cleaned = re.sub(r"\bnear me\b", "", suggestion or "", flags=re.IGNORECASE)
    cleaned = re.sub(r"\bbest\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(rf"\bin\s+{re.escape(city)}\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(rf"\bnear\s+{re.escape(city)}\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,-")
    return cleaned


def google_suggest_keywords(keyword, city, limit=8):
    query = quote_plus(f"{keyword} in {city}")
    request = Request(
        f"https://suggestqueries.google.com/complete/search?client=firefox&q={query}",
        headers={"User-Agent": "Mozilla/5.0"},
    )

    try:
        with urlopen(request, timeout=5) as response:
            suggestions = json.loads(response.read().decode("utf-8"))[1]
    except (OSError, URLError, ValueError, json.JSONDecodeError, IndexError):
        return []

    cleaned_suggestions = []
    seen_suggestions = set()

    for suggestion in suggestions:
        cleaned = clean_suggested_keyword(suggestion, city)
        cleaned_key = cleaned.lower()
        cleaned_words = set(normalized_name_words(cleaned))
        if (
            cleaned
            and cleaned_key not in seen_suggestions
            and not cleaned_words.intersection(BLOCKED_SUGGESTION_WORDS)
        ):
            cleaned_suggestions.append(cleaned)
            seen_suggestions.add(cleaned_key)

        if len(cleaned_suggestions) >= limit:
            break

    return cleaned_suggestions


def generic_category_keywords(keyword):
    if len(keyword.split()) > 2:
        return [keyword]

    return [
        pattern.format(keyword=keyword)
        for pattern in AUTOMATIC_CATEGORY_PATTERNS
    ]


def expand_niche_keywords(keywords, city):
    expanded_keywords = []
    seen_keywords = set()

    for keyword in keywords:
        candidates = [keyword]
        keyword_key = keyword.lower()

        for trigger, expansions in NICHE_KEYWORD_EXPANSIONS.items():
            if trigger in keyword_key or any(trigger in word for word in keyword_key.split()):
                candidates.extend(expansions)

        candidates.extend(google_suggest_keywords(keyword, city))
        candidates.extend(generic_category_keywords(keyword))

        for candidate in candidates:
            cleaned_candidate = " ".join(candidate.strip().split())
            cleaned_key = cleaned_candidate.lower()
            if cleaned_candidate and cleaned_key not in seen_keywords:
                expanded_keywords.append(cleaned_candidate)
                seen_keywords.add(cleaned_key)

            if len(expanded_keywords) >= MAX_EXPANDED_KEYWORDS:
                return expanded_keywords

    return expanded_keywords


def search_scope_key(city, district):
    return "|".join([
        city.strip().lower(),
        (district or CITY_WIDE_AREA).strip().lower(),
    ])


def history_urls_for_scope(history, city, district):
    scope_key = search_scope_key(city, district)
    urls = set()
    fingerprints = set()
    match_keys = set()
    name_keys = set()

    for search_key, _entry in history.items():
        key_parts = search_key.split("|")
        if len(key_parts) != 3 or "|".join(key_parts[1:]) != scope_key:
            continue

        entry = get_search_history_entry(history, search_key)
        urls.update(normalize_url(url) for url in entry["urls"] if normalize_url(url))
        fingerprints.update(entry["fingerprints"])
        match_keys.update(entry["match_keys"])
        name_keys.update(entry["name_keys"])

        for fingerprint in entry["fingerprints"]:
            fingerprint_name = (fingerprint or "").split("|", 1)[0]
            fingerprint_name_key = business_name_key(fingerprint_name, city)
            if fingerprint_name_key:
                name_keys.add(fingerprint_name_key)
                match_keys.add(f"name:{fingerprint_name_key}")

    return urls, fingerprints, match_keys, name_keys


def build_maps_query(keyword, city, district, search_point=None):
    if district:
        return f"{keyword} nearby {district}/{city}"

    if search_point:
        return f"{keyword} near {search_point['lat']},{search_point['lng']}"

    return f"{keyword} in {city}"


def maps_coordinates_from_url(url):
    match = re.search(r"@(-?\d+\.\d+),(-?\d+\.\d+)", url or "")
    if not match:
        return None

    return {
        "lat": float(match.group(1)),
        "lng": float(match.group(2)),
    }


def resolve_city_center(page, city):
    query = quote_plus(city)
    page.goto(
        f"https://www.google.com/maps/search/{query}?hl=en",
        wait_until="domcontentloaded",
        timeout=60000,
    )
    page.wait_for_timeout(3000)
    return maps_coordinates_from_url(page.url)


def build_city_grid(center, depth):
    offsets = GRID_DEPTHS.get(depth, GRID_DEPTHS["fast"])
    search_points = []

    for lat_offset in offsets:
        for lng_offset in offsets:
            search_points.append({
                "label": f"grid {lat_offset},{lng_offset}",
                "lat": round(center["lat"] + (lat_offset * GRID_STEP_DEGREES), 6),
                "lng": round(center["lng"] + (lng_offset * GRID_STEP_DEGREES), 6),
            })

    return search_points


def build_search_points(page, city, district, scrape_full_city, coverage_depth):
    if district or not scrape_full_city:
        return [None]

    try:
        center = resolve_city_center(page, city)
    except (PlaywrightTimeoutError, PlaywrightError):
        return [None]

    if not center:
        return [None]

    return build_city_grid(center, coverage_depth)


def normalize_fingerprint(value):
    return " ".join((value or "").strip().lower().split())


def normalize_phone(value):
    return re.sub(r"\D+", "", value or "")


def normalize_website(value):
    website = (value or "").strip().lower()
    if not website:
        return ""

    if "://" not in website:
        website = f"https://{website}"

    parsed_url = urlsplit(website)
    host = parsed_url.netloc or parsed_url.path.split("/")[0]
    return host.removeprefix("www.")


def normalized_name_words(value):
    normalized = (value or "").lower()
    normalized = normalized.replace("'s", "s")
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    return [word for word in normalized.split() if word]


def business_name_key(value, city=""):
    city_words = set(normalized_name_words(city))
    words = [
        word
        for word in normalized_name_words(value)
        if word not in GENERIC_NAME_WORDS and word not in city_words
    ]

    if not words:
        words = normalized_name_words(value)

    return " ".join(words[:3])


def business_match_keys(business_data, city=""):
    details = business_data.get("details", [])
    website = details[0] if details else ""
    phone = normalize_phone(business_data.get("phone"))

    keys = {
        "name_key": business_name_key(business_data.get("name"), city),
        "phone_key": f"phone:{phone}" if len(phone) >= 8 else "",
        "website_key": f"site:{normalize_website(website)}" if normalize_website(website) else "",
    }
    keys["match_keys"] = [
        key
        for key in [keys["phone_key"], keys["website_key"], f"name:{keys['name_key']}"]
        if key and not key.endswith(":")
    ]
    return keys


def is_similar_name(name_key, seen_name_keys):
    if not name_key:
        return False

    for seen_name_key in seen_name_keys:
        if not seen_name_key:
            continue

        if name_key == seen_name_key:
            return True

        shorter_length = min(len(name_key), len(seen_name_key))
        if shorter_length >= 5 and (
            name_key.startswith(seen_name_key) or seen_name_key.startswith(name_key)
        ):
            return True

        if shorter_length >= 6 and SequenceMatcher(None, name_key, seen_name_key).ratio() >= MIN_SIMILARITY:
            return True

    return False


def is_duplicate_business(business_data, city, seen_match_keys, seen_name_keys):
    keys = business_match_keys(business_data, city)

    if any(key in seen_match_keys for key in keys["match_keys"]):
        return True, keys

    if is_similar_name(keys["name_key"], seen_name_keys):
        return True, keys

    return False, keys


def remember_business_keys(keys, seen_match_keys, seen_name_keys):
    seen_match_keys.update(keys["match_keys"])
    if keys["name_key"]:
        seen_name_keys.add(keys["name_key"])


def business_fingerprint(business_data):
    return "|".join([
        normalize_fingerprint(business_data.get("name")),
        normalize_fingerprint(business_data.get("address")),
    ])


def dedupe_results(results, city=""):
    deduped_results = []
    seen_match_keys = set()
    seen_name_keys = set()
    skipped = 0

    for business_data in results:
        is_duplicate, keys = is_duplicate_business(
            business_data,
            city,
            seen_match_keys,
            seen_name_keys,
        )
        if is_duplicate:
            skipped += 1
            continue

        remember_business_keys(keys, seen_match_keys, seen_name_keys)
        deduped_results.append(business_data)

    return deduped_results, skipped


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


def load_results_csv(filename):
    filepath = EXPORTS_DIR / Path(filename).name
    results = []

    with filepath.open("r", newline="", encoding="utf-8-sig") as csv_file:
        reader = csv.DictReader(csv_file)
        for row in reader:
            website = (row.get("Website") or "").strip()
            results.append({
                "name": row.get("Name", ""),
                "address": row.get("Address", ""),
                "phone": row.get("Phone", ""),
                "details": [detail.strip() for detail in website.split(",") if detail.strip()],
            })

    return results


def city_from_results(results):
    if not results:
        return ""

    return results[0].get("address", "")


def latest_csv_filename():
    if not EXPORTS_DIR.exists():
        return None

    csv_files = sorted(
        EXPORTS_DIR.glob("*.csv"),
        key=lambda filepath: filepath.stat().st_mtime,
        reverse=True,
    )

    if not csv_files:
        return None

    return csv_files[0].name


def get_search_history_entry(history, search_key):
    entry = history.get(search_key, {})

    if isinstance(entry, list):
        return {
            "urls": entry,
            "fingerprints": [],
            "match_keys": [],
            "name_keys": [],
            "businesses": [],
        }

    return {
        "urls": entry.get("urls", []),
        "fingerprints": entry.get("fingerprints", []),
        "match_keys": entry.get("match_keys", []),
        "name_keys": entry.get("name_keys", []),
        "businesses": entry.get("businesses", []),
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


def scroll_results(page):
    for selector in RESULT_FEED_SELECTORS:
        feed = page.locator(selector).first
        try:
            if feed.count() > 0 and feed.is_visible(timeout=1000):
                feed.evaluate("(element) => element.scrollTop = element.scrollHeight")
                return
        except PlaywrightError:
            continue

    page.keyboard.press("End")


def collect_business_urls(page, result_qtn, seen_urls):
    business_urls = []
    business_url_keys = set()
    previous_loaded_count = 0
    stalled_scrolls = 0
    max_scrolls = max(12, result_qtn * 2)
    scrolls = 0

    while len(business_urls) < result_qtn and stalled_scrolls < 10 and scrolls < max_scrolls:
        businesses = page.query_selector_all(RESULT_LINK_SELECTOR)

        for business in businesses:
            href = business.get_attribute("href")
            url_key = normalize_url(href)
            if href and url_key and url_key not in seen_urls and url_key not in business_url_keys:
                business_urls.append(href)
                business_url_keys.add(url_key)

            if len(business_urls) == result_qtn:
                break

        if len(business_urls) == result_qtn:
            break

        loaded_count = len(businesses)
        if loaded_count == previous_loaded_count:
            stalled_scrolls += 1
        else:
            stalled_scrolls = 0

        previous_loaded_count = loaded_count
        scroll_results(page)
        page.wait_for_timeout(2000)
        scrolls += 1

    return business_urls


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

    address = first_visible_text(page, [
        'button[data-item-id="address"] .Io6YTe',
        'button[aria-label^="Address:"]',
    ])

    details = []
    if website:
        details.append(website)

    business_data = {
        "name": name,
        "address": address or default_address,
        "details": details,
    }

    if phone:
        business_data["phone"] = phone

    return business_data


@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        with sync_playwright() as p:
            keyword_input = request.form.get("keyword")
            keywords = parse_keywords(keyword_input)
            city = (request.form.get("city") or "").strip()
            district = (request.form.get("district") or "").strip()
            scrape_full_city = request.form.get("scrape_full_city") == "on"
            scrape_all_available = request.form.get("scrape_all_available") == "on"
            coverage_depth = request.form.get("coverage_depth") or "fast"

            if scrape_full_city:
                district = ""
                keywords = expand_niche_keywords(keywords, city)

            if len(keywords) == 0 or city == "":
                flash("Please fill in keyword and city.")
                return render_template("index.html")

            try:
                result_qtn = int(request.form.get("qtn") or DEFAULT_SCRAPE_CAP)
            except (TypeError, ValueError):
                flash("Please enter a valid number of results.")
                return render_template("index.html")

            if result_qtn <= 0:
                flash("Please enter at least 1 result.")
                return render_template("index.html")

            search_history = load_search_history()
            (
                seen_urls,
                seen_fingerprints,
                seen_match_keys,
                seen_name_keys,
            ) = history_urls_for_scope(search_history, city, district)
            default_address = ", ".join([
                part.strip()
                for part in [district, city]
                if part.strip()
            ]) or city.strip()

            browser = p.chromium.launch(headless=True)
            page = browser.new_page(locale="en-US")
            search_points = build_search_points(
                page,
                city,
                district,
                scrape_full_city,
                coverage_depth,
            )

            data = []
            skipped = 0
            returned_by_search_key = {}

            for keyword in keywords:
                if not scrape_all_available and len(data) >= result_qtn:
                    break

                search_key = normalize_search_key(keyword, city, district)

                for search_point in search_points:
                    if len(data) >= result_qtn:
                        break

                    remaining_qtn = result_qtn - len(data)

                    try:
                        query = quote_plus(build_maps_query(keyword, city, district, search_point))
                        page.goto(
                            f"https://www.google.com/maps/search/{query}?hl=en",
                            wait_until="domcontentloaded",
                            timeout=60000,
                        )

                        page.wait_for_selector(RESULT_LINK_SELECTOR, timeout=60000)
                    except PlaywrightTimeoutError:
                        if len(data) == 0:
                            browser.close()
                            return jsonify({
                                "error": "Google Maps did not load search results in time. Please try again, or complete any visible Google consent/CAPTCHA prompt in the opened browser window."
                            }), 504
                        continue

                    businesses = page.query_selector_all(RESULT_LINK_SELECTOR)
                    if len(businesses) == 0:
                        continue

                    businesses[0].click()
                    page.wait_for_timeout(1000)
                    per_search_limit = max(10, min(60, remaining_qtn + 20))
                    if scrape_full_city:
                        per_search_limit = max(20, min(60, remaining_qtn + 20))
                    business_urls = collect_business_urls(page, per_search_limit, seen_urls)

                    for business_url in business_urls:
                        url_key = normalize_url(business_url)
                        if not url_key or url_key in seen_urls:
                            skipped += 1
                            continue

                        try:
                            business_data = scrape_place(page, business_url, default_address)
                        except (PlaywrightTimeoutError, PlaywrightError):
                            skipped += 1
                            continue

                        if not business_data:
                            skipped += 1
                            continue

                        returned_entry = returned_by_search_key.setdefault(search_key, {
                            "urls": [],
                            "fingerprints": [],
                            "match_keys": [],
                            "name_keys": [],
                            "businesses": [],
                        })
                        returned_entry["urls"].append(url_key)

                        fingerprint = business_fingerprint(business_data)
                        is_duplicate, match_keys = is_duplicate_business(
                            business_data,
                            city,
                            seen_match_keys,
                            seen_name_keys,
                        )
                        if fingerprint in seen_fingerprints or is_duplicate:
                            skipped += 1
                            seen_urls.add(url_key)
                            continue

                        data = [
                            business for business in data
                            if business.get("name") != business_data["name"]
                        ]
                        data.append(business_data)
                        seen_urls.add(url_key)
                        seen_fingerprints.add(fingerprint)
                        remember_business_keys(match_keys, seen_match_keys, seen_name_keys)

                        returned_entry["fingerprints"].append(fingerprint)
                        returned_entry["match_keys"].extend(match_keys["match_keys"])
                        if match_keys["name_key"]:
                            returned_entry["name_keys"].append(match_keys["name_key"])
                        returned_entry["businesses"].append({
                            "name": business_data.get("name", ""),
                            "address": business_data.get("address", ""),
                            "phone": business_data.get("phone", ""),
                            "website": ", ".join(business_data.get("details", [])),
                        })

                        if len(data) >= result_qtn:
                            break

            if len(data) == 0:
                browser.close()
                return jsonify({
                    "message": "No new businesses found for this city/area. Try increasing the result count, changing the category, changing the area, or clearing search history."
                }), 502

            browser.close()
            for search_key, returned_entry in returned_by_search_key.items():
                history_entry = get_search_history_entry(search_history, search_key)
                history_urls = set(
                    normalize_url(url)
                    for url in history_entry["urls"]
                    if normalize_url(url)
                )
                history_fingerprints = set(history_entry["fingerprints"])
                history_match_keys = set(history_entry["match_keys"])
                history_name_keys = set(history_entry["name_keys"])
                history_businesses = {
                    normalize_fingerprint(business.get("name")): business
                    for business in history_entry["businesses"]
                    if business.get("name")
                }
                for business in returned_entry["businesses"]:
                    business_name = normalize_fingerprint(business.get("name"))
                    if business_name:
                        history_businesses[business_name] = business

                search_history[search_key] = {
                    "urls": sorted(history_urls.union(returned_entry["urls"])),
                    "fingerprints": sorted(history_fingerprints.union(returned_entry["fingerprints"])),
                    "match_keys": sorted(history_match_keys.union(returned_entry["match_keys"])),
                    "name_keys": sorted(history_name_keys.union(returned_entry["name_keys"])),
                    "businesses": sorted(
                        history_businesses.values(),
                        key=lambda business: business.get("name", "").lower(),
                    ),
                }

            save_search_history(search_history)
            data, final_duplicate_skips = dedupe_results(data, city)
            skipped += final_duplicate_skips
            search_key = normalize_search_key("_".join(keywords), city, district)
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
    flash("Search history cleared. Repeated results can appear again.")
    return redirect(url_for("index"))


@app.route("/latest-results")
def latest_results():
    filename = latest_csv_filename()

    if not filename:
        flash("No exported results found yet.")
        return redirect(url_for("index"))

    results = load_results_csv(filename)
    results, _skipped = dedupe_results(results, city_from_results(results))

    return render_template(
        "results.html",
        results=results,
        skipped=0,
        csv_filename=filename,
    )


@app.route("/download-csv/<filename>")
def download_csv(filename):
    return send_from_directory(
        EXPORTS_DIR,
        Path(filename).name,
        as_attachment=True,
        download_name="google_maps_results.csv",
    )


@app.route("/download-deduped-csv/<filename>")
def download_deduped_csv(filename):
    filename = Path(filename).name
    results = load_results_csv(filename)
    results, _skipped = dedupe_results(results, city_from_results(results))

    csv_buffer = io.StringIO()
    csv_writer = csv.DictWriter(
        csv_buffer,
        fieldnames=["Name", "Address", "Phone", "Website"],
    )
    csv_writer.writeheader()

    for business in results:
        csv_writer.writerow({
            "Name": business.get("name", ""),
            "Address": business.get("address", ""),
            "Phone": business.get("phone", ""),
            "Website": ", ".join(business.get("details", [])),
        })

    return Response(
        csv_buffer.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=google_maps_results_deduped.csv"},
    )
