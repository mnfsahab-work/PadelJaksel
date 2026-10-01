#!/usr/bin/env python3
"""Snapshot padel court bookings in Jakarta Selatan from ayo.co.id.

AYO only exposes slots that have not started yet: past dates return nothing and
today's elapsed hours drop off. So every run records the latest state of each
upcoming slot and keeps the last state it saw once the slot disappears. Run it
often (the workflow does every 30 min) and past days stay complete.

Outputs:
  data/venues.json          padel venues in Jakarta Selatan (refreshed each run)
  data/slots/<date>.json    per play-date slot state, merged across runs
  data/summary.json         per-day booked court-hours per venue, read by the dashboard
"""
import html
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = "https://ayo.co.id"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
PADEL_SPORT_ID = 12
REGION = {  # from /autocomplete-region?term=jakarta selatan
    "lokasi": "Kota Administrasi Jakarta Selatan",
    "region_id": "01M0RH2700PX8H9RSPFRTXXK80",
    "region_level": "2",
}
FOCUS_BRAND = "Air Padel"
WIB = timezone(timedelta(hours=7))
DAYS_AHEAD = 3   # scrape today + next 3 days
DAYS_BACK = 3    # summary covers last 3 days too

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SLOTS = DATA / "slots"


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


CARD_RE = re.compile(
    r"id='venue-(\d+)'>\s*<a[^>]*href='https://ayo\.co\.id/v/([^']+)'.*?"
    r"<h5 class='text-left s20-500 turncate'>(.*?)</h5>",
    re.S,
)


def discover_venues():
    venues, page = {}, 1
    while True:
        h = get(f"{BASE}/venues", {**REGION, "cabor": PADEL_SPORT_ID, "page": page})
        found = CARD_RE.findall(h)
        new = [(vid, slug, name) for vid, slug, name in found if vid not in venues]
        for vid, slug, name in new:
            venues[vid] = {"id": int(vid), "slug": slug, "name": html.unescape(name).strip()}
        if not new or f"page={page + 1}" not in h:
            break
        page += 1
    return venues


# Chains with several venues in Jakarta Selatan are ranked as one brand.
BRAND_RULES = [
    (r"^\s*air\s*padel\b", FOCUS_BRAND),
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


def fetch_day(venue_id, date):
    body = get(f"{BASE}/venues-ajax/op-times-and-fields",
               {"venue_id": venue_id, "date": date}, ajax=True)
    return json.loads(body)


def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def main():
    now = datetime.now(WIB)
    stamp = now.strftime("%Y-%m-%dT%H:%M")
    today = now.date()
    SLOTS.mkdir(parents=True, exist_ok=True)

    known = load_json(DATA / "venues.json", {"venues": {}})["venues"]
    try:
        current = discover_venues()
    except Exception as e:  # noqa: BLE001 - keep tracking known venues if listing fails
        print(f"venue discovery failed, using cached list: {e}", file=sys.stderr)
        current = {}
    for vid, v in current.items():
        v["brand"] = brand_of(v["name"])
        v["first_seen"] = known.get(vid, {}).get("first_seen", stamp)
        v["last_listed"] = stamp
        known[vid] = {**known.get(vid, {}), **v}
    (DATA / "venues.json").write_text(json.dumps(
        {"updated": stamp, "venues": known}, indent=1, ensure_ascii=False))
    active = {vid: v for vid, v in known.items() if v.get("last_listed") == stamp} or known
    active = {vid: v for vid, v in active.items() if not is_simulator(v["name"])}
    print(f"{len(active)} padel venues in Jakarta Selatan")

    dates = [(today + timedelta(days=i)).isoformat() for i in range(DAYS_AHEAD + 1)]
    jobs = [(vid, d) for vid in active for d in dates]
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda j: (j, safe_fetch(*j)), jobs))

    by_date = {d: load_json(SLOTS / f"{d}.json", {"date": d, "first_run": stamp, "venues": {}})
               for d in dates}
    errors = 0
    for (vid, d), res in results:
        if res is None:
            errors += 1
            continue
        doc = by_date[d]
        vdoc = doc["venues"].setdefault(vid, {})
        for f in res.get("fields", []):
            if f.get("sport_id") != PADEL_SPORT_ID:
                continue
            fdoc = vdoc.setdefault(str(f["field_id"]), {"name": f["field_name"], "slots": {}})
            for s in f.get("slots", []):
                # [is_available, duration_minutes, last_seen]
                fdoc["slots"][s["start_time"][:5]] = [s["is_available"], s["duration_per_session"], stamp]
    for d, doc in by_date.items():
        doc["last_run"] = stamp
        (SLOTS / f"{d}.json").write_text(json.dumps(doc, separators=(",", ":"), ensure_ascii=False))
    print(f"scraped {len(jobs) - errors}/{len(jobs)} venue-days")

    build_summary(now, known)
    if errors > len(jobs) / 2:
        sys.exit("more than half of the requests failed")


def safe_fetch(vid, d):
    try:
        return fetch_day(vid, d)
    except Exception as e:  # noqa: BLE001 - one bad venue must not sink the run
        print(f"fail venue {vid} {d}: {e}", file=sys.stderr)
        return None


def build_summary(now, venues):
    today = now.date()
    days = []
    for i in range(-DAYS_BACK, DAYS_AHEAD + 1):
        d = (today + timedelta(days=i)).isoformat()
        doc = load_json(SLOTS / f"{d}.json", None)
        day = {"date": d, "offset": i, "tracked": doc is not None, "venues": []}
        if doc:
            day["first_run"] = doc.get("first_run")
            day["last_run"] = doc.get("last_run")
            for vid, fields in doc["venues"].items():
                booked = slots = 0.0
                for f in fields.values():
                    for avail, dur, _ in f["slots"].values():
                        hrs = (dur or 60) / 60
                        slots += hrs
                        if not avail:
                            booked += hrs
                if slots == 0:
                    continue
                v = venues.get(vid, {})
                name = v.get("name", vid)
                if is_simulator(name):
                    continue
                day["venues"].append({
                    "id": int(vid), "name": name, "slug": v.get("slug"),
                    "brand": brand_of(name),
                    "courts": len(fields), "booked_hours": booked, "slot_hours": slots,
                })
        days.append(day)
    summary = {
        "generated": now.strftime("%Y-%m-%dT%H:%M:%S+07:00"),
        "focus_brand": FOCUS_BRAND,
        "region": "Kota Jakarta Selatan",
        "source": "ayo.co.id",
        "days": days,
    }
    (DATA / "summary.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
