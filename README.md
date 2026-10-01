# PadelJaksel

Live market-share tracker for padel venues on [ayo.co.id](https://ayo.co.id), covering every padel venue AYO lists in Indonesia.
Pick any venue (or brand), pick a market area (within 5 km, within 10 km, same city, same province) and see:
its share of booked court-hours for the last 3 days, today and the next 3 days, and the top 3 competitors each day.
The default view is Air Padel (AIR Padel + AIR Padel Active) in Kota Jakarta Selatan.

## How it works

| Piece | What it does |
|---|---|
| `scripts/scrape.py` | Lists every padel venue on AYO, looks up each new venue's coordinates, city and province once, then reads each court's slots (`/venues-ajax/op-times-and-fields`). A slot marked unavailable counts as a booked court-hour. |
| `.github/workflows/scrape.yml` | Every 30 min: today's slots for all venues. Every ~3 h and after midnight: venue discovery plus the next 3 days. |
| `data` branch | Where the job saves everything, as one commit replaced on each run, so the repo doesn't grow. |
| `data/` on this branch | The seed the job starts from on its first run. |
| `index.html` | The dashboard. It reads `summary.json` from the `data` branch, so serving it with GitHub Pages (Settings → Pages → deploy from branch, root) gives a live page. |

Files on the `data` branch:

- `venues.json`: venue list with brand, city, province and coordinates.
- `slots/<date>.json`: last seen state of every slot (kept 10 days).
- `daily/<date>.json`: booked and open court-hours per venue (kept forever).
- `summary.json`: last 3 days to next 3 days, read by the dashboard.

Run locally: `python3 scripts/scrape.py && python3 -m http.server`, then open http://localhost:8000.
`MODE=today|full` forces a run mode; `DATA_DIR` changes where data is written.

## Caveats

- Hours a venue blocks itself (coaching, maintenance, members) look the same as bookings on AYO.
- Only AYO calendars are visible; bookings made through other channels are not.
- Past days exist only from when tracking started (1 Oct 2026, 18:00 WIB for Jakarta Selatan, 18:36 WIB nationwide). That first day is partial.
- Chains are grouped into brands by `BRAND_RULES` in `scripts/scrape.py`; add a line for any other chain. Padel simulators are excluded.
- Distances are straight lines between the coordinates AYO shows on each venue page.
