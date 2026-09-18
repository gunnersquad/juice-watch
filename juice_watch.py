#!/usr/bin/env python3
"""Juice Watch bot: finds price errors and really good prices on popular designer and niche fragrances and alerts your phone.

Run modes:
  python juice_watch.py            normal check
  python juice_watch.py --dry-run  print alerts instead of sending them
  python juice_watch.py --test     send one test notification and exit
(JUICE_MODE=test or JUICE_MODE=dry-run environment variables do the same.)
"""
import json
import os
import random
import re
import statistics
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from urllib import robotparser
from urllib.parse import urlparse

import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ROOT, "watchlist.json")
POPULAR_PATH = os.path.join(ROOT, "popular.json")
STATE_DIR = os.path.join(ROOT, "state")
STATE_PATH = os.path.join(STATE_DIR, "state.json")
PRUNE_AFTER_SECONDS = 30 * 24 * 3600

SAMPLE_WORDS = re.compile(r"\b(decants?|samples?|vials?|travel|minis?|miniature|atomizer)\b", re.I)
TESTER_WORDS = re.compile(r"\b(tester|unboxed|no box|damaged box)\b", re.I)
DUPE_WORDS = re.compile(r"\b(inspired|dupe|alternative|similar to|our version|impression)\b", re.I)
SIZE_ML = re.compile(r"(\d+(?:\.\d+)?)\s*ml\b", re.I)
SIZE_OZ = re.compile(r"(\d+(?:\.\d+)?)\s*(?:fl\.?\s*)?oz\b", re.I)


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def to_float(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(",", "").replace("$", "").strip())
    except ValueError:
        return None


def pct_off(price, reference):
    return round((1 - price / reference) * 100) if reference else 0


def is_sample(title):
    if SAMPLE_WORDS.search(title):
        return True
    m = SIZE_ML.search(title)
    if m and float(m.group(1)) <= 15:
        return True
    m = SIZE_OZ.search(title)
    if m and float(m.group(1)) <= 0.5:
        return True
    return False


# ---------------------------------------------------------------- polite HTTP

class PoliteClient:
    """Throttles per store, obeys robots.txt, and backs off when a store pushes back."""

    def __init__(self, cfg):
        self.delay = cfg.get("delay_seconds", 5)
        self.jitter = cfg.get("jitter_seconds", 3)
        self.timeout = cfg.get("timeout_seconds", 20)
        self.user_agent = cfg.get("user_agent", "JuiceWatchBot/1.0 (personal price alerts)")
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": self.user_agent,
            "Accept": "application/json, text/html;q=0.9, */*;q=0.5",
        })
        self.robots = {}
        self.last_request = {}
        self.blocked = set()
        self.last_status = None

    def _wait_turn(self, host):
        ready_at = self.last_request.get(host, 0) + self.delay + random.uniform(0, self.jitter)
        pause = ready_at - time.time()
        if pause > 0:
            time.sleep(pause)
        self.last_request[host] = time.time()

    def _allowed(self, url):
        parts = urlparse(url)
        host = parts.netloc
        if host not in self.robots:
            rp = robotparser.RobotFileParser()
            self._wait_turn(host)
            try:
                r = self.session.get(f"{parts.scheme}://{host}/robots.txt", timeout=self.timeout)
                if r.status_code in (401, 403):
                    rp.disallow_all = True
                    rp.modified()
                else:
                    rp.parse(r.text.splitlines() if r.status_code == 200 else [])
            except requests.RequestException:
                rp.parse([])
            self.robots[host] = rp
        return self.robots[host].can_fetch(self.user_agent, url)

    def get(self, url):
        host = urlparse(url).netloc
        if host in self.blocked:
            return None
        if not self._allowed(url):
            log(f"  robots.txt asks bots not to fetch {url}; skipping")
            return None
        for attempt in range(2):
            self._wait_turn(host)
            self.last_status = None
            try:
                r = self.session.get(url, timeout=self.timeout)
            except requests.RequestException as err:
                log(f"  network error on {url}: {err}")
                return None
            self.last_status = r.status_code
            if r.status_code in (429, 503):
                retry = to_float(r.headers.get("Retry-After")) or 60
                if attempt == 0 and retry <= 120:
                    log(f"  {host} asked us to slow down; waiting {int(retry)}s")
                    time.sleep(retry)
                    continue
                log(f"  {host} is still rate limiting; skipping it until the next run")
                self.blocked.add(host)
                return None
            if r.status_code in (401, 403):
                log(f"  {host} refused access (HTTP {r.status_code}); skipping it this run")
                self.blocked.add(host)
                return None
            if r.status_code == 404 and url.endswith("products.json?limit=1"):
                return None
            if r.status_code >= 400:
                log(f"  HTTP {r.status_code} for {url}")
                return None
            return r
        return None


# ---------------------------------------------------------------- store readers

def offers_from_product(store, product, cents=False):
    base = store["base_url"].rstrip("/")
    handle = product.get("handle", "")
    offers = []
    for v in product.get("variants", []):
        price = to_float(v.get("price"))
        compare = to_float(v.get("compare_at_price"))
        if price is None:
            continue
        if cents:
            price /= 100
            compare = compare / 100 if compare else None
        variant_title = v.get("title") or ""
        title = product.get("title", "Unknown product")
        if variant_title and variant_title != "Default Title":
            title = f"{title} - {variant_title}"
        offers.append({
            "id": str(v.get("id")),
            "key": f"{store['name']}:{v.get('id')}",
            "store": store["name"],
            "vendor": product.get("vendor") or "",
            "title": title,
            "price": price,
            "compare_at": compare,
            "available": v.get("available", True),
            "url": f"{base}/products/{handle}?variant={v.get('id')}",
        })
    return offers


def shopify_catalog(client, store, max_pages):
    base = store["base_url"].rstrip("/")
    for page in range(1, max_pages + 1):
        r = client.get(f"{base}/products.json?limit=250&page={page}")
        if r is None:
            return
        try:
            products = r.json().get("products", [])
        except ValueError:
            log(f"  {store['name']} did not return product data; it may not be a Shopify store")
            return
        if not products:
            return
        for product in products:
            yield product
        if len(products) < 250:
            return
    log(f"  {store['name']}: stopped at the {max_pages}-page limit")


def find_ld_price(node, in_offer=False):
    if isinstance(node, list):
        for child in node:
            found = find_ld_price(child, in_offer)
            if found:
                return found
    elif isinstance(node, dict):
        offer_here = in_offer or str(node.get("@type", "")).lower() in ("offer", "aggregateoffer")
        if offer_here:
            for field in ("price", "lowPrice"):
                found = to_float(node.get(field))
                if found:
                    return found
        for field, child in node.items():
            found = find_ld_price(child, offer_here or field == "offers")
            if found:
                return found
    return None


def price_from_html(html):
    for block in re.findall(r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", html, re.S | re.I):
        try:
            found = find_ld_price(json.loads(block.strip()))
        except ValueError:
            continue
        if found:
            return found
    patterns = [
        r"property=[\"']product:price:amount[\"'][^>]*content=[\"']([\d.,]+)",
        r"itemprop=[\"']price[\"'][^>]*content=[\"']([\d.,]+)",
    ]
    for pattern in patterns:
        m = re.search(pattern, html, re.I)
        if m:
            return to_float(m.group(1))
    return None


def fetch_watch_item(client, store, item):
    url = item["url"].split("#")[0]
    if store.get("platform") == "shopify":
        product_url = url.split("?")[0].rstrip("/")
        r = client.get(product_url + ".js")
        if r is None:
            return []
        try:
            offers = offers_from_product(store, r.json(), cents=True)
        except ValueError:
            log(f"  could not read product data for {item['name']}")
            return []
        wanted = (item.get("variant") or "").lower()
        if wanted:
            offers = [o for o in offers if wanted in o["title"].lower()]
        return offers
    r = client.get(url)
    if r is None:
        return []
    price = price_from_html(r.text)
    if price is None:
        log(f"  no price found on the page for {item['name']}")
        return []
    return [{
        "id": url, "key": f"{store['name']}:{url}", "store": store["name"],
        "title": item["name"], "price": price, "compare_at": None,
        "available": True, "url": url,
    }]


def resolve_platform(client, store, state, now):
    """Return "shopify" or "html". Stores set to "auto" are tested once a week."""
    platform = store.get("platform", "auto")
    if platform != "auto":
        return platform
    cached = state["platforms"].get(store["name"])
    if cached and now - cached[1] < 7 * 24 * 3600:
        return cached[0]
    r = client.get(store["base_url"].rstrip("/") + "/products.json?limit=1")
    if r is None:
        if client.last_status != 404:
            log(f"  {store['name']}: couldn't reach the store to check its type; will retry next run")
            return "html"
        found = "html"
    else:
        try:
            found = "shopify" if isinstance(r.json().get("products"), list) else "html"
        except (ValueError, AttributeError):
            found = "html"
    state["platforms"][store["name"]] = [found, now]
    log(f"  {store['name']} detected as {'Shopify (whole-store scans on)' if found == 'shopify' else 'non-Shopify (watchlist only)'}")
    return found


# ---------------------------------------------------------------- recognition

def normalize(text):
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    text = text.replace("&", " and ")
    return " " + re.sub(r"[^a-z0-9]+", " ", text).strip() + " "


class PopularMatcher:
    """Recognizes popular brands (and their best-known scents) from a product's brand and title."""

    def __init__(self, data):
        self.enabled = set(data.get("tiers_enabled", ["designer", "niche"]))
        self.brands = []
        for tier, brands in data.get("brands", {}).items():
            for name, info in brands.items():
                aliases = {normalize(a) for a in [name] + info.get("aliases", [])}
                famous = [(f, normalize(f)) for f in info.get("famous", [])]
                tokens = set(" ".join(aliases).split())
                self.brands.append({"name": name, "tier": tier, "aliases": aliases,
                                    "famous": famous, "tokens": tokens})

    def _find(self, text):
        best = None
        for brand in self.brands:
            for alias in brand["aliases"]:
                if alias in text and (best is None or len(alias) > best[0]):
                    best = (len(alias), brand)
        return best[1] if best else None

    def match(self, vendor, title):
        brand = self._find(normalize(vendor))
        if brand is None:
            if DUPE_WORDS.search(title):
                return None
            brand = self._find(normalize(title))
        if brand is None or brand["tier"] not in self.enabled:
            return None
        text = normalize(title)
        famous = next((f for f, n in brand["famous"] if n in text), None)
        return {"brand": brand["name"], "tier": brand["tier"], "famous": famous,
                "brand_tokens": brand["tokens"]}


# ---------------------------------------------------------------- product identity

CONCENTRATIONS = [
    ("extrait", re.compile(r" extrait ")),
    ("le parfum", re.compile(r" le parfum ")),
    ("edp", re.compile(r" (edp|eau de parfum) ")),
    ("edt", re.compile(r" (edt|eau de toilette) ")),
    ("edc", re.compile(r" (edc|eau de cologne|cologne) ")),
    ("parfum", re.compile(r" (parfum|pure perfume) ")),
]
NOT_FRAGRANCE = re.compile(r" (deodorant|deo|antiperspirant|candle|candles|lotion|shower|gel|balm|soap|"
                           r"cream|wash|shampoo|hair|body|bath|diffuser|reed|aftershave|after shave|"
                           r"powder|stick|lip|mascara|foundation|serum|moisturizer|cleanser) ")
NOT_A_BOTTLE = re.compile(r" (set|sets|gift|kit|coffret|bundle|travel|mist|mini|duo|trio|pack|discovery) ")
MEN_WORDS = {"men", "man", "mens", "homme", "uomo", "him", "m"}
WOMEN_WORDS = {"women", "woman", "womens", "femme", "donna", "her", "lady", "ladies", "w"}
UNISEX_WORDS = {"unisex", "everyone", "u"}
FILLER_WORDS = {
    "for", "by", "eau", "de", "du", "des", "la", "le", "les", "l", "d", "the", "and", "a", "an", "of",
    "in", "with", "new", "spray", "vaporisateur", "vapo", "natural", "regular", "box", "boxed",
    "unboxed", "packaging", "same", "liquid", "plainer", "fragrance", "perfume", "perfumes",
    "authentic", "original", "size", "full", "bottle", "oz", "fl", "ml", "sp", "tester", "tstr",
    "refillable", "pour", "edp", "edt", "edc", "toilette", "parfum", "parfums", "cologne",
    "extrait", "pure", "no", "version", "paris",
}
COMMON_ML = [5, 7.5, 10, 15, 20, 30, 35, 40, 45, 50, 60, 65, 70, 75, 80, 90, 100, 110,
             120, 125, 150, 180, 200, 250]


def size_ml(title):
    m = SIZE_ML.search(title)
    if m:
        return round(float(m.group(1)))
    m = SIZE_OZ.search(title)
    if m:
        ml = float(m.group(1)) * 29.5735
        nearest = min(COMMON_ML, key=lambda c: abs(c - ml))
        if abs(nearest - ml) / nearest <= 0.08:
            return round(nearest)
    return None


def product_identity(title, match):
    """Strict key for comparing the exact same bottle across stores (famous scents only)."""
    if not match or not match["famous"]:
        return None
    text = normalize(title)
    if NOT_A_BOTTLE.search(text):
        return None
    size = size_ml(title)
    if size is None:
        return None
    conc = next((name for name, pattern in CONCENTRATIONS if pattern.search(text)), None)
    if conc is None:
        return None
    words = text.split()
    genders = set()
    for w in words:
        if w in MEN_WORDS:
            genders.add("men")
        elif w in WOMEN_WORDS:
            genders.add("women")
        elif w in UNISEX_WORDS:
            genders.add("unisex")
    gender = genders.pop() if len(genders) == 1 else ("unisex" if genders else "unspecified")
    famous_tokens = set(normalize(match["famous"]).split())
    ignore = FILLER_WORDS | MEN_WORDS | WOMEN_WORDS | UNISEX_WORDS | match["brand_tokens"]
    name = {w for w in words if w not in ignore and not re.fullmatch(r"[0-9]+(ml|oz|floz)?", w)} | famous_tokens
    tester = "tester" if TESTER_WORDS.search(title) else "retail"
    return "|".join([match["brand"], gender, conc, str(size), tester, "+".join(sorted(name))])


# ---------------------------------------------------------------- detection

def store_check(offer, ref, record_low, rules):
    """Checks against this listing's own history. Returns (level, heading, reasons)."""
    price = offer["price"]
    level, heading, reasons = None, None, []
    drop = pct_off(price, ref) if ref and ref > price else 0
    if drop >= rules["error_drop_percent"]:
        level, heading = "error", "Possible price error"
        reasons.append(f"down {drop}% from its recent price of ${ref:.2f}")
    elif drop >= rules["big_drop_percent"]:
        level, heading = "deal", "Big price drop"
        reasons.append(f"down {drop}% from its recent price of ${ref:.2f}")
    if record_low:
        if level is None:
            level, heading = "deal", "Lowest price in a while"
            reasons.append(f"down {drop}% from its recent price of ${ref:.2f}")
        reasons.append(f"lowest price seen in {rules['record_low_days']} days")
    compare = offer["compare_at"]
    limit = rules.get("compare_at_percent")
    if limit and compare and compare > price and pct_off(price, compare) >= limit:
        level, heading = "error", "Possible price error"
        reasons.append(f"{pct_off(price, compare)}% below the store's own list price of ${compare:.2f}")
    if price <= rules["absurd_price"]:
        level, heading = "error", "Possible price error"
        reasons.append(f"listed at only ${price:.2f}")
    return level, heading, reasons


def market_check(offer, others, rules):
    """Checks against the same bottle at other stores. others is {store: price}."""
    if len(others) < rules["market_min_other_stores"]:
        return None, None, [], 0
    typical = statistics.median(others.values())
    below = pct_off(offer["price"], typical) if typical > offer["price"] else 0
    cheapest = min(others, key=others.get)
    reason = (f"{below}% below the typical price of ${typical:.2f} at {len(others)} other stores "
              f"(next best: ${others[cheapest]:.2f} at {cheapest})")
    if below >= rules["error_vs_market_percent"]:
        return "error", "Possible price error", [reason], below
    if below >= rules["good_vs_market_percent"]:
        return "deal", "Great price", [reason], below
    return None, None, [], below


def scan_store(store, history, polite, rules, matcher, now, window):
    """Reads one store's full catalog and updates its price history. Runs in its own thread."""
    client = PoliteClient(polite)
    day = 24 * 3600
    low_window = rules.get("record_low_days", 90) * day
    min_history = rules.get("record_low_min_history_days", 14) * day
    first_run = not history
    candidates, market = [], {}
    count = recognized = 0
    log(f"Scanning {store['name']}")
    for product in shopify_catalog(client, store, polite.get("max_pages_per_store", 30)):
        for offer in offers_from_product(store, product):
            count += 1
            price = offer["price"]
            old = history.get(offer["id"])
            record_low = False
            if old:
                ref, ref_time = (old[2], old[3]) if len(old) >= 4 else (old[0], old[1])
                low, low_time, first_seen = (old[4], old[5], old[6]) if len(old) >= 7 else (old[0], old[1], old[1])
                if price >= ref or now - ref_time > window:
                    ref, ref_time = price, now
                if now - low_time > low_window:
                    low, low_time = price, now
                record_low = (
                    price < low - 0.009
                    and now - first_seen >= min_history
                    and pct_off(price, ref) >= rules.get("record_low_min_drop_percent", 15)
                )
                if price <= low:
                    low, low_time = price, now
            else:
                ref = low = price
                ref_time = low_time = first_seen = now
            history[offer["id"]] = [price, now, ref, ref_time, low, low_time, first_seen]

            if not offer["available"] or price <= 0:
                continue
            if rules.get("ignore_samples", True) and is_sample(offer["title"]):
                continue
            if NOT_FRAGRANCE.search(normalize(offer["title"])):
                continue
            match = matcher.match(offer["vendor"], offer["title"])
            if match is None and rules.get("popular_only", True):
                continue
            if match and rules.get("famous_only", False) and not match["famous"]:
                continue
            recognized += 1
            identity = product_identity(offer["title"], match)
            if identity:
                market[identity] = min(price, market.get(identity, price))
            if not first_run:
                candidates.append((offer, match, identity, ref, record_low))
    for vid in [v for v, entry in history.items() if now - entry[1] > PRUNE_AFTER_SECONDS]:
        del history[vid]
    return candidates, market, count, recognized, first_run


# ---------------------------------------------------------------- alerts

def notify(title, body, url=None, dry_run=False, priority="high"):
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    discord = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if dry_run or not (topic or discord):
        print(f"\nALERT: {title}\n{body}\n{url or ''}\n", flush=True)
        return
    if topic:
        server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
        headers = {
            "Title": title.encode("ascii", "ignore").decode(),
            "Priority": priority,
            "Tags": "rotating_light",
        }
        if url:
            headers["Click"] = url
        try:
            requests.post(f"{server}/{topic}", data=body.encode("utf-8"), headers=headers, timeout=15)
        except requests.RequestException as err:
            log(f"ntfy send failed: {err}")
    if discord:
        content = f"**{title}**\n{body}\n{url or ''}"[:1900]
        try:
            requests.post(discord, json={"content": content}, timeout=15)
        except requests.RequestException as err:
            log(f"Discord send failed: {err}")


# ---------------------------------------------------------------- main

def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return default


def save_state(state):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, separators=(",", ":"))


def main():
    mode = os.environ.get("JUICE_MODE", "").strip().lower()
    dry_run = "--dry-run" in sys.argv or mode == "dry-run"
    if "--test" in sys.argv or mode == "test":
        notify("Juice Watch test", "Notifications are working.", None, dry_run)
        log("Test notification sent.")
        return

    cfg = load_json(CONFIG_PATH, None)
    if cfg is None:
        sys.exit("watchlist.json is missing or has a formatting error.")
    rules = cfg["rules"]
    polite = cfg["polite"]
    popular_data = load_json(POPULAR_PATH, None)
    if popular_data is None and rules.get("popular_only", True):
        sys.exit("popular.json is missing or has a formatting error.")
    matcher = PopularMatcher(popular_data or {})
    window = rules.get("reference_days", 14) * 24 * 3600

    state = load_json(STATE_PATH, {})
    for section in ("prices", "alerted", "platforms"):
        state.setdefault(section, {})
    client = PoliteClient(polite)
    now = int(time.time())

    watchlist = [i for i in cfg.get("watchlist", []) if "REPLACE-ME" not in i.get("url", "")]
    watched = {item.get("store") for item in watchlist}
    stores = {}
    for store in cfg["stores"]:
        store = dict(store)
        if store.get("scan_catalog") or store["name"] in watched:
            store["platform"] = resolve_platform(client, store, state, now)
        stores[store["name"]] = store

    flagged = {}
    checked = {}

    try:
        # Optional watchlist: specific products with a usual price you set.
        for item in watchlist:
            store = stores.get(item.get("store"))
            if not store:
                log(f"Watchlist item '{item.get('name')}' names an unknown store; skipping")
                continue
            log(f"Watchlist: {item['name']} at {store['name']}")
            usual = float(item["usual_price"])
            limit = usual * (1 - rules["watchlist_percent"] / 100)
            for offer in fetch_watch_item(client, store, item):
                key = "watch:" + offer["key"]
                checked[key] = offer["price"]
                if offer["available"] and 0 < offer["price"] <= limit:
                    note = " (tester)" if TESTER_WORDS.search(offer["title"]) else ""
                    flagged[key] = {
                        "price": offer["price"], "level": "error", "famous": True, "drop": pct_off(offer["price"], usual),
                        "title": f"Watchlist hit at {offer['store']}",
                        "body": f"{offer['title']}{note}\n${offer['price']:.2f} vs your usual "
                                f"${usual:.2f} ({pct_off(offer['price'], usual)}% off)",
                        "url": offer["url"],
                    }

        # Pass 1: read every product on stores with a full product feed, several stores at once.
        scan_list = [st for st in stores.values()
                     if st.get("scan_catalog") and st.get("platform") == "shopify"]
        for st in scan_list:
            state["prices"].setdefault(st["name"], {})
        had_history = {st["name"] for st in scan_list if state["prices"][st["name"]]}
        workers = max(1, min(len(scan_list), rules.get("parallel_stores", 6)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(
                lambda st: scan_store(st, state["prices"][st["name"]], polite, rules, matcher, now, window),
                scan_list))
        candidates = []
        market = {}
        for st, (store_candidates, store_market, count, recognized, first_run) in zip(scan_list, results):
            if first_run and count:
                log(f"{st['name']}: {count} listings saved (first run: learning prices, alerts start next run)")
            else:
                log(f"{st['name']}: {count} listings checked, {recognized} from popular brands")
            candidates.extend(store_candidates)
            for identity, price in store_market.items():
                market.setdefault(identity, {})[st["name"]] = price

        # Stores joining cross-store comparisons for the first time don't trigger a flood of old deals.
        contributing = {name for group in market.values() for name in group}
        if "market_members" in state:
            members = set(state["market_members"])
        else:
            members = had_history
        new_members = contributing - members
        if new_members:
            log(f"New to cross-store comparisons: {', '.join(sorted(new_members))} "
                "(existing price gaps with these stores are saved quietly this run)")
        state["market_members"] = sorted(members | contributing)

        matched = sum(1 for v in market.values() if len(v) > 1)
        log(f"Cross-store comparison: {matched} famous fragrances found at 2+ stores")

        # Pass 2: judge each listing against its own history and against other stores.
        for offer, match, identity, ref, record_low in candidates:
            key = "cat:" + offer["key"]
            checked[key] = offer["price"]
            level, heading, reasons = store_check(offer, ref, record_low, rules)
            store_level = level
            quiet = False
            below = 0
            if identity:
                others = {st: p for st, p in market[identity].items() if st != offer["store"]}
                m_level, m_heading, m_reasons, below = market_check(offer, others, rules)
                reasons += m_reasons
                if m_level == "deal" and store_level is None and new_members & set(market[identity]):
                    quiet = True
                if m_level == "error" or (m_level and level is None):
                    level, heading = m_level, m_heading
                elif m_level == "deal" and level == "deal":
                    heading = "Great price"
            if not level:
                continue
            tags = []
            if match:
                tags.append(f"{match['brand']} ({match['tier'].replace('_', ' ')})")
                if match["famous"]:
                    tags.append(f"popular scent: {match['famous']}")
            if TESTER_WORDS.search(offer["title"]):
                tags.append("tester")
            drop = pct_off(offer["price"], ref) if ref and ref > offer["price"] else 0
            flagged[key] = {
                "price": offer["price"], "level": level, "quiet": quiet,
                "famous": bool(match and match["famous"]),
                "drop": max(drop, below),
                "title": f"{heading} at {offer['store']}",
                "body": f"{offer['title']}\n${offer['price']:.2f}: " + "; ".join(reasons)
                        + (f"\n{' | '.join(tags)}" if tags else ""),
                "url": offer["url"],
            }

        # Send only new or lower-priced alerts; errors and famous scents first.
        alerted = state["alerted"]
        to_send = []
        for key, alert in flagged.items():
            previous = alerted.get(key)
            if (previous is None or alert["price"] < previous - 0.009) and not alert.get("quiet"):
                to_send.append(alert)
            alerted[key] = min(alert["price"], previous) if previous else alert["price"]
        for key in list(alerted):
            if key in checked and checked[key] > alerted[key] + 0.009:
                del alerted[key]
        to_send.sort(key=lambda a: (a["level"] != "error", not a["famous"], -a["drop"]))
        if candidates and not state.get("deal_baseline_v2"):
            quiet = [a for a in to_send if a["level"] != "error"]
            to_send = [a for a in to_send if a["level"] == "error"]
            state["deal_baseline_v2"] = True
            log(f"One-time catch-up: saved {len(quiet)} existing deals without alerting; "
                "only new deals will be sent from now on")

        cap = rules.get("max_alerts_per_run", 10)
        for alert in to_send[:cap]:
            priority = "high" if alert["level"] == "error" else "default"
            notify(alert["title"], alert["body"], alert["url"], dry_run, priority)
        if len(to_send) > cap:
            notify("Juice Watch", f"{len(to_send) - cap} more deals this run. "
                   "Check the Actions log for the full list.", None, dry_run, "default")
            for alert in to_send[cap:]:
                log(f"  extra: {alert['body']} {alert['url']}")
        log(f"Done. {len(flagged)} flagged, {len(to_send)} new alerts sent.")
    finally:
        state["updated"] = now
        save_state(state)


if __name__ == "__main__":
    main()
