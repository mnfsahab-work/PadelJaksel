# PadelJaksel

Live market-share tracker for padel courts in Kota Jakarta Selatan, from [ayo.co.id](https://ayo.co.id).
It answers: what share of booked court-hours does **Air Padel** (AIR Padel + AIR Padel Active) hold,
for the last 3 days, today and the next 3 days, and who are the top 3 brands each day?

## How it works

| Piece | What it does |
|---|---|
| `scripts/scrape.py` | Lists every padel venue AYO shows in Jakarta Selatan, then reads each court's hourly slots for today + 3 days (`/venues-ajax/op-times-and-fields`). A slot marked unavailable counts as a booked court-hour. |
| `.github/workflows/scrape.yml` | Runs the scraper every 30 min and commits the results. |
| `data/slots/<date>.json` | Last seen state of every slot for that play date. AYO hides a slot once it starts, so this archive is what makes past days possible. |
| `data/summary.json` | Booked court-hours per venue for D-3 … D+3. The dashboard reads this. |
| `index.html` | The dashboard. Serve the repo with GitHub Pages (Settings → Pages → deploy from branch, root). |

Run locally: `python3 scripts/scrape.py && python3 -m http.server`, then open http://localhost:8000.

## Caveats

- Hours a venue blocks itself (coaching, maintenance, members) look the same as bookings on AYO.
- Only AYO calendars are visible; bookings made through other channels are not.
- Past days exist only from when tracking started (1 Oct 2026, 18:00 WIB). That first day is partial.
- Chains are ranked as one brand (`BRAND_RULES` in `scripts/scrape.py`). Padel simulators are excluded.
