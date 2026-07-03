# FuelGrid Europe

Interactive fuel-price map for road transport operations: **Diesel B7, HVO100
and EV charging** across Spain, France, Germany and Italy. Traffic-light
colours adapt to zoom (regional averages zoomed out, station ranking zoomed
in), with a country filter and weekly automatic refresh.

## Repository layout (flat on purpose - easiest to upload)

- `index.html` - the whole app, one file
- `fetch_prices.py` - pulls official open data (ES/FR/DE/IT), writes
  `data/prices-latest.json` (the `data` folder is created automatically)
- `.github/workflows/refresh-prices.yml` - runs the fetcher every Monday
  06:00 UTC and commits the result

## Setup on GitHub (no tools needed, browser only)

1. Create a **public** repository, e.g. `fuelgrid-europe`.
2. On the empty repo page click **"uploading an existing file"** and drag in
   `index.html` and `fetch_prices.py` (and this README if you like).
   Commit changes.
3. **Add file -> Create new file**, name it exactly
   `.github/workflows/refresh-prices.yml`
   (each `/` creates a folder), paste the workflow contents, commit.
4. **Settings -> Pages** -> Deploy from a branch -> `main` / root -> Save.
   Your site: `https://YOUR-USERNAME.github.io/REPO-NAME/`
5. **Actions** tab -> *Refresh fuel prices* -> **Run workflow**. When it
   finishes green, reload the site: Diesel is now LIVE (ES + FR + IT).
6. Optional - Germany: get a free key at
   https://creativecommons.tankerkoenig.de then
   **Settings -> Secrets and variables -> Actions -> New repository secret**,
   name `TANKERKOENIG_API_KEY`, paste the key, re-run the workflow.

## What's live vs sample

| Fuel        | Status                                                       |
|-------------|--------------------------------------------------------------|
| Diesel B7   | Live: ES + FR + IT immediately, DE once the key is set       |
| HVO100      | Live where officially published (IT rows; ES probed). Falls back to clearly-labelled sample data otherwise. |
| EV charging | Sample for now - phase 3 (Open Charge Map + operator tariffs) |

The header always shows which fuels are live vs sample and the snapshot date.

## Troubleshooting

- Red X on an Action run: open the run, copy the error text, paste it to
  Claude - usually a one-line fix when an endpoint changes a field name.
- Site still shows sample after a green run: hard-refresh (Ctrl+Shift+R).
- The map preview inside the Claude chat never shows street tiles (sandbox
  restriction) - it uses the built-in simplified basemap there. Opened in a
  normal browser or via the GitHub Pages URL you get the full street map.

## Roadmap

- Phase 3: EV chargers via Open Charge Map + operator tariff table
- French station brands (official feed omits them)
- Route mode: corridor price profile between two cities
- Rename/brand: one string in `index.html`
