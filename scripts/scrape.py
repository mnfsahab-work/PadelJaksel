#!/usr/bin/env python3
"""Snapshot padel court bookings across Indonesia from ayo.co.id.

AYO only exposes slots that have not started yet: past dates return nothing and
today's elapsed hours drop off. So every run records the latest state of each
upcoming slot and keeps the last state it saw once the slot disappears.

Two run modes keep the load on AYO modest:
  today   (every run, ~30 min): today's slots for every venue. This is the
          capture that makes past days complete.
  full    (every ~3 h and after midnight): venue discovery, location lookup for
          new venues, and today + the next 3 days.

Outputs, under $DATA_DIR (default ./data):
  venues.json           every padel venue on AYO, with city, province and coordinates
  slots/<date>.json     last seen state of every slot for that play date (kept 10 days)
  daily/<date>.json     booked / open court-hours per venue for that date (kept forever)
  summary.json          last 3 days, today and next 3 days, read by the dashboard
  state.json            when the last full run happened
"""
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date as Date, datetime, timedelta, timezone
from pathlib import Path

BASE = "https://ayo.co.id"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
PADEL_SPORT_ID = 12
WIB = timezone(timedelta(hours=7))
DAYS_AHEAD = 3        # scrape today + next 3 days
DAYS_BACK = 3         # summary covers the last 3 days too
FULL_EVERY_MIN = 170  # a full run roughly every 3 hours
KEEP_SLOT_DAYS = 10
WORKERS = int(os.environ.get("WORKERS", "8"))
# Stop issuing requests after this many minutes so a slow run still saves what it
# collected (jobs are ordered today first) instead of hitting the CI timeout.
DEADLINE = time.time() + 60 * float(os.environ.get("TIME_BUDGET_MIN", "40"))

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("DATA_DIR", ROOT / "data")).resolve()
SLOTS = DATA / "slots"
DAILY = DATA / "daily"


def get(url, params=None, ajax=False, retries=4):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {"User-Agent": UA, "Accept-Language": "id-ID,id;q=0.9,en;q=0.8"}
    if ajax:
        headers["X-Requested-With"] = "XMLHttpRequest"
        headers["Accept"] = "application/json"
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8")
        except Exception as e:  # noqa: BLE001 - retry any network error
            if attempt == retries - 1:
                raise
            print(f"retry {url}: {e}", file=sys.stderr)
            time.sleep(2 ** (attempt + 1))


def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path, obj, pretty=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(obj, ensure_ascii=False, indent=1 if pretty else None,
                      separators=None if pretty else (",", ":"), sort_keys=not pretty)
    path.write_text(text)


# ---------------------------------------------------------------- venues

CARD_RE = re.compile(
    r"id='venue-(\d+)'>\s*<a[^>]*href='https://ayo\.co\.id/v/([^']+)'.*?"
    r"<h5 class='text-left s20-500 turncate'>(.*?)</h5>.*?"
    r"·\s*([^<]+?)\s*</h5>",
    re.S,
)


def listing_page(page):
    # sortby=1 (price, low to high) keeps page order stable; the default popularity
    # sort reshuffles between requests and drops ~10% of venues across pages.
    h = get(f"{BASE}/venues", {"cabor": PADEL_SPORT_ID, "sortby": 1, "page": page})
    return h, CARD_RE.findall(h)


def discover_venues():
    """Every venue AYO lists under padel, across Indonesia."""
    first, cards = listing_page(1)
    pages = max([int(p) for p in re.findall(r"[?&](?:amp;)?page=(\d+)", first)] or [1])
    found = {1: cards}
    with ThreadPoolExecutor(max_workers=4) as pool:
        for page, (_, c) in zip(range(2, pages + 1), pool.map(listing_page, range(2, pages + 1))):
            found[page] = c
    venues = {}
    for cards in found.values():
        for vid, slug, name, city in cards:
            venues[vid] = {"id": int(vid), "slug": slug,
                           "name": html.unescape(name).strip(), "city": html.unescape(city).strip()}
    print(f"discovered {len(venues)} padel venues on {pages} listing pages")
    return venues


def venue_location(slug):
    h = get(f"{BASE}/v/{slug}")
    loc = {}
    m = re.search(r"open_map\(\s*(-?[0-9.]+)\s*,\s*(-?[0-9.]+)\s*\)", h)
    if m:
        lat, lng = float(m.group(1)), float(m.group(2))
        if lat or lng:
            loc["lat"], loc["lng"] = round(lat, 6), round(lng, 6)
    m = re.search(r'itemprop="addressLocality">([^<]*)<', h)
    if m and m.group(1).strip():
        loc["city"] = html.unescape(m.group(1)).strip()
    m = re.search(r'itemprop="addressRegion">([^<]*)<', h)
    if m and m.group(1).strip():
        loc["province"] = html.unescape(m.group(1)).strip()
    return loc


# Chains with several venues are ranked as one brand. Add a line per chain.
BRAND_RULES = [
    (r"^\s*air\s*padel(\s+active)?\s*$", "Air Padel"),  # not "Air Padel Court Samarinda"
    (r"^\s*republic\s*(padel|premier)\b", "Republic Padel"),
    (r"^\s*metropolar\s*padel\b", "Metropolar Padel"),
    (r"^\s*padel\s*parc\b", "Padel Parc"),
]


def brand_of(name):
    for pattern, brand in BRAND_RULES:
        if re.search(pattern, name, re.I):
            return brand
    return name


def is_simulator(name):
    # Padel simulators are listed under padel but are not courts. Keep venues
    # that also have a real court (e.g. "Lifestyle Capital Club: Padel Court, ...").
    return bool(re.search(r"simulator", name, re.I)) and not re.search(r"padel court", name, re.I)


def refresh_venues(stamp):
    known = load_json(DATA / "venues.json", {"venues": {}})["venues"]
    try:
        current = discover_venues()
    except Exception as e:  # noqa: BLE001 - keep tracking known venues if listing fails
        print(f"venue discovery failed, using cached list: {e}", file=sys.stderr)
        current = {}
    if len(current) < 0.8 * len([v for v in known.values() if v.get("listed")]):
        print("listing looks incomplete; keeping previous venue list", file=sys.stderr)
        current = {}
    for vid, v in known.items():
        if current:
            v["listed"] = vid in current
    for vid, v in current.items():
        old = known.get(vid, {})
        known[vid] = {**old, **{k: v[k] for k in ("id", "slug", "name")},
                      "city": old.get("city") or v["city"], "listed": True,
                      "first_seen": old.get("first_seen", stamp)}

    missing = [vid for vid, v in known.items() if v.get("listed") and "located" not in v]
    if missing:
        print(f"looking up location for {len(missing)} venues")

        def locate(vid):
            try:
                return vid, venue_location(known[vid]["slug"])
            except Exception as e:  # noqa: BLE001 - retried on the next full run
                print(f"location failed {vid}: {e}", file=sys.stderr)
                return vid, None
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            for vid, loc in pool.map(locate, missing):
                if loc is not None:
                    known[vid].update(loc)
                    known[vid]["located"] = stamp
    for v in known.values():
        v["brand"] = brand_of(v["name"])
        v["simulator"] = is_simulator(v["name"])
    write_json(DATA / "venues.json", {"updated": stamp, "venues": known}, pretty=True)
    return known


# ---------------------------------------------------------------- slots

def encode_field(slots):
    """{"HH:MM": [avail, minutes]} -> compact form. Regular grids become one string."""
    keys = sorted(slots)
    durs = {slots[k][1] for k in keys}
    if keys and len(durs) == 1:
        d = durs.pop()
        start = int(keys[0][:2]) * 60 + int(keys[0][3:])
        if all(int(k[:2]) * 60 + int(k[3:]) == start + i * d for i, k in enumerate(keys)):
            return {"t": keys[0], "d": d, "a": "".join(str(slots[k][0]) for k in keys)}
    return {"x": {k: slots[k] for k in keys}}


def decode_field(f):
    if "slots" in f:  # first-version files: {"name", "slots": {"HH:MM": [avail, minutes, last_seen]}}
        return {k: [v[0], v[1]] for k, v in f["slots"].items()}
    if "x" in f:
        return {k: list(v) for k, v in f["x"].items()}
    h, m = int(f["t"][:2]), int(f["t"][3:])
    out = {}
    for i, a in enumerate(f["a"]):
        mins = h * 60 + m + i * f["d"]
        out[f"{mins // 60:02d}:{mins % 60:02d}"] = [int(a), f["d"]]
    return out


def fetch_day(job):
    vid, d = job
    if time.time() > DEADLINE:
        return job, "skipped"
    try:
        body = get(f"{BASE}/venues-ajax/op-times-and-fields",
                   {"venue_id": vid, "date": d}, ajax=True)
        return job, json.loads(body)
    except Exception as e:  # noqa: BLE001 - one bad venue must not sink the run
        print(f"fail venue {vid} {d}: {e}", file=sys.stderr)
        return job, None


def scrape(venues, dates, stamp):
    targets = [vid for vid, v in venues.items() if v.get("listed") and not v.get("simulator")]
    jobs = [(vid, d) for d in dates for vid in targets]
    print(f"scraping {len(targets)} venues x {len(dates)} days")
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = list(pool.map(fetch_day, jobs))
    docs = {d: load_json(SLOTS / f"{d}.json", None) or {"date": d, "first_run": stamp, "venues": {}}
            for d in dates}
    errors = skipped = 0
    for (vid, d), res in results:
        if res == "skipped":
            skipped += 1
            continue
        if res is None:
            errors += 1
            continue
        vdoc = docs[d]["venues"].setdefault(vid, {})
        for f in res.get("fields", []):
            if f.get("sport_id") != PADEL_SPORT_ID or not f.get("slots"):
                continue
            fid = str(f["field_id"])
            merged = decode_field(vdoc[fid]) if fid in vdoc else {}
            for s in f["slots"]:
                merged[s["start_time"][:5]] = [int(s["is_available"]), int(s["duration_per_session"] or 60)]
            vdoc[fid] = encode_field(merged)
    for d, doc in docs.items():
        doc["last_run"] = stamp
        write_json(SLOTS / f"{d}.json", doc)
        write_daily(doc)
    print(f"scraped {len(jobs) - errors - skipped}/{len(jobs)} venue-days in {time.time() - t0:.0f}s"
          f" ({errors} failed, {skipped} skipped for time)", flush=True)
    return errors, len(jobs) - skipped


def write_daily(doc):
    """Per-venue totals: [booked_hours, open_slot_hours, courts]."""
    out = {}
    for vid, fields in doc["venues"].items():
        booked = total = 0.0
        for f in fields.values():
            for avail, mins in decode_field(f).values():
                total += mins / 60
                if not avail:
                    booked += mins / 60
        if total:
            out[vid] = [round(booked, 2), round(total, 2), len(fields)]
    write_json(DAILY / f"{doc['date']}.json",
               {"date": doc["date"], "first_run": doc["first_run"], "last_run": doc["last_run"], "venues": out})


def prune(today):
    for p in SLOTS.glob("*.json"):
        try:
            if (today - Date.fromisoformat(p.stem)).days > KEEP_SLOT_DAYS:
                p.unlink()
        except ValueError:
            pass


# ---------------------------------------------------------------- summary

def build_summary(now, venues):
    today = now.date()
    days, used = [], set()
    for i in range(-DAYS_BACK, DAYS_AHEAD + 1):
        d = (today + timedelta(days=i)).isoformat()
        doc = load_json(DAILY / f"{d}.json", None)
        day = {"date": d, "offset": i, "tracked": doc is not None, "v": {}}
        if doc:
            day["first_run"], day["last_run"] = doc["first_run"], doc["last_run"]
            for vid, row in doc["venues"].items():
                if vid in venues and not venues[vid].get("simulator"):
                    day["v"][vid] = row
                    used.add(vid)
        days.append(day)
    meta = {}
    for vid in sorted(used, key=int):
        v = venues[vid]
        meta[vid] = {"n": v["name"], "b": v["brand"], "s": v["slug"], "c": v.get("city"),
                     "p": v.get("province"), "lat": v.get("lat"), "lng": v.get("lng")}
    write_json(DATA / "summary.json", {
        "generated": now.strftime("%Y-%m-%dT%H:%M:%S+07:00"),
        "source": "ayo.co.id",
        "columns": ["booked_hours", "open_slot_hours", "courts"],
        "venues": meta,
        "days": days,
    })


# ---------------------------------------------------------------- main

def main():
    now = datetime.now(WIB)
    stamp = now.strftime("%Y-%m-%dT%H:%M")
    today = now.date()
    state = load_json(DATA / "state.json", {})
    last_full = state.get("last_full")
    full = (os.environ.get("MODE") == "full" or not last_full
            or last_full[:10] != today.isoformat()
            or (now.replace(tzinfo=None) - datetime.fromisoformat(last_full)).total_seconds() / 60 >= FULL_EVERY_MIN)
    if os.environ.get("MODE") == "today":
        full = False
    print(f"mode: {'full' if full else 'today'} at {stamp} WIB")

    if full:
        venues = refresh_venues(stamp)
    else:
        venues = load_json(DATA / "venues.json", {"venues": {}})["venues"]
        if not venues:
            venues, full = refresh_venues(stamp), True
    days = DAYS_AHEAD if full else 0
    dates = [(today + timedelta(days=i)).isoformat() for i in range(days + 1)]
    errors, total = scrape(venues, dates, stamp)
    if full:
        state["last_full"] = stamp
    state["last_run"] = stamp
    write_json(DATA / "state.json", state, pretty=True)
    prune(today)
    build_summary(now, venues)
    if total and errors > total / 2:
        sys.exit("more than half of the requests failed")


if __name__ == "__main__":
    main()
