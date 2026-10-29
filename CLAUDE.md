# energy-viz-2026 — US electric generation & storage map

A rebuild of Chris Loer's 2019 "US Electric Generation" map
(https://chrisloer.github.io/energy-viz/, write-up:
https://medium.com/@chrisloer/how-i-built-it-us-electric-generation-map-ed8b2b682eed)
with EIA data through 2026 and a new **energy storage** layer.

Stack: Python/pandas pipeline → GeoJSON → tippecanoe → PMTiles → MapLibre GL JS
on GitHub Pages. No Mapbox account, no API keys. Basemap: OpenFreeMap "dark".
Satellite: Esri World Imagery.

## Layout

```
pipeline/download.py   fetch EIA-923 (2001–2026) + EIA-860 (2001–2025) zips → data/raw/  (--check / --list)
pipeline/build.py      zips → data/out/*.geojson + fuel_totals.json + annual_twh.csv  (cached per year in data/cache/)
pipeline/tile.sh       geojson → tiles/*.pmtiles incl. planned.pmtiles   (needs: brew install tippecanoe)
pipeline/inspect_raw.py  print members / sheets / header columns of a raw EIA file
pipeline/serve.py      local web server with HTTP Range support, for viewing the map
index.html             the map (MapLibre + pmtiles.js), reads tiles/ and data/out/fuel_totals.json
data/raw, data/cache, data/out/*.geojson   git-ignored (large)
tiles/, data/out/fuel_totals.json, data/out/annual_twh.csv   committed (served by GitHub Pages)
```

## Run

```
pip3 install -r requirements.txt
python3 pipeline/download.py      # ~1 GB, 10–20 min
python3 pipeline/build.py         # ~10 min first time, seconds after (cache)
bash   pipeline/tile.sh           # ~2–5 min
python3 pipeline/serve.py         # local test at http://localhost:8000 (plain http.server lacks Range support)
```

## Three sources, three roles (keep them separate — it is what prevents double counting)

- EIA-923 — the only source of MWh: monthly net generation, gross discharge for storage.
- EIA-860 (annual, 2001–2025) — attributes as of each year-end: coordinates (2012+ only),
  storage MW/MWh/technology, pumped-hydro MW, and each year's generator retirements
  (union of the yearly "Retired" tabs = full retirement history). Validated.
- EIA-860M (one file, newest month) — generator timeline since 2002: in-service and
  retirement month/year, status (OP/OS/SB/RE), coordinates, planned units, 2026 additions.
  Preliminary. Used for dates, status, coordinate gap-filling. Never for quantities.
- Rules: 923 wins on timing conflicts (output after a retirement date → QA list, not map);
  retirement is per generator, the map is per plant×fuel, so a plant-fuel feature is
  retired only when all its units are; newest file wins on coordinates.
- Implemented: `load_860m()` reads Operating/Retired/Planned, builds the per-(plant,fuel)
  timeline (`online`, `retired`, `status`, operating MW/MWh), fills coordinates for plants
  the annual files dropped (+1,133 plants), and writes `planned.geojson`. Annual 860 files
  2001–2011 are DBF/xls without coordinates and are skipped (kept for address geocoding
  if ever needed). Retirement history comes from 860M, not from the 25 inconsistent annual
  generator files, because 860M is the same record with later corrections applied.
- Label status line precedence: "retired M/YYYY" (860M, once reached and output has
  stopped) > "offline since M/YYYY" (923 output stopped) > "since YYYY" (860M in-service).
  Mohave shows both in turn: offline since 12/2005, retired 7/2012 — both are true.
- `qa_output_after_retirement.csv`: plant-fuels with 923 output after their 860M
  retirement month (74 in the sample run). The map follows the output.
- EIA-923 2026 is sample-only at plant level (3,497 plants vs 13,581 in 2025) but carries EIA's
  own "State-Fuel Level Increment" estimates (plant 99999) for everyone else: 379 TWh for
  Jan–Aug 2026 on top of 2,694 TWh from reporters (3,073 TWh vs 3,034 TWh for Jan–Aug 2025).
  `impute_year()` allocates each state×fuel estimate across that state's non-reporting plants in
  proportion to their prior-year output for the same month (EIA's own method when finalising),
  flags them `est=True` → feature property `est_from` (YYYYMM) → label "estimated (annual
  reporter)"; the slider reads "preliminary · small plants estimated". 9,909 plants received
  estimates, 0.6 TWh was unplaceable. Storage estimates are net-based and unusable, so the 456
  non-reporting storage units carry forward their 2025 same-month gross discharge (1.37 TWh;
  reporters carried 92% of 2025 battery discharge). Jan–Aug 2026 vs 2025: gas +3%, coal −8.5%,
  wind +8%, solar +21%, storage +30%. `EXCLUDE_PARTIAL_YEARS=1` drops the year instead.

## Data model (verified against the real files)

- EIA-923 "Page 1 Generation and Fuel Data": one row per (plant, prime mover, fuel).
  Header row position varies by year (row 6 in 2024, row 8 in 2003) and column names
  drift (`Netgen January` vs `NETGEN_JAN`) — `build.py` scans for the header row and
  regex-matches columns. Plant 99999 = EIA's non-response placeholder, dropped.
- EIA-923 "Page 1 Energy Storage" (2014+): same shape plus `Grossgen <month>` =
  energy **discharged**. Lists every unit at a plant that has storage, so filter by
  prime mover. **Storage net generation is negative** (round-trip losses); the map
  uses gross discharge. 2024: batteries 14.9 TWh gross vs −2.4 TWh net.
- Storage = prime mover `BA` battery, `PS` pumped hydro, `FW` flywheel, `CE` CAES,
  `ES` other. Fuel category **11**. `stype` property carries the sub-type.
- Fuel categories 0–9 are Chris's (0 gas, 1 coal, 2 nuclear, 3 hydro, 4 wind, 5 solar,
  6 biomass, 7 other, 8 geothermal, 9 petroleum). 7 is hidden in the UI.
- A plant reporting several categories becomes several features, id `<plant>-<cat>`.
  "Other" rows at a plant with a real fuel are folded into that fuel.
- EIA-860 `2___Plant_Y2025.xlsx` has coordinates for 99.4 % of 2024 plants but only
  84 % of 2003 plants (big retired coal plants are gone), hence the union with older
  860 plant files — newest wins, older fill gaps.
- EIA-860 `3_4_Energy_Storage_Y2025.xlsx`: nameplate MW / MWh, `Storage Technology 1`
  (LIB = lithium-ion), operating year. Pumped-storage capacity is in
  `3_1_Generator_Y2025.xlsx` (prime mover PS), not the storage file.
- Property naming follows Chris's page: monthly `netgen_<year>_<month0>`, quarterly
  `netgen_<year>_<quarter0>`; zeros are omitted (`to-number` of a missing property is 0).
- `fuel_totals.json`: `months`, `quarters` (slider axis), `reference_year` (last complete
  year) and per-fuel TWh used to normalise heatmap intensity between fuels.
- Each feature carries `first`/`last` = YYYYMM of its first and last month with output.
  The UI hides plants before `first` and shows a grey ring + "offline since" label
  after `last` (derived from EIA-923 output, not from EIA-860 retirement dates).
- 2025 is EIA early-release (full plant coverage, not fully validated). The 2026
  in-year monthly file only carries the ~3,200 monthly-sample plants individually;
  the ~10,000 annual filers (27 % of wind, 49 % of hydro, 23 % of solar output in
  2024) appear only as state-level 99999 estimates, which are dropped. `build.py`
  therefore excludes any year whose plant count falls below 60 % of the prior year
  (prints "EXCLUDING"); set `INCLUDE_PARTIAL_YEARS=1` to keep it anyway.

## Accuracy invariants (verified on the 2003/2024/860-2025 files)

- Storage rows are 1:1 between Page 1 and the Energy Storage sheet (786 = 786 in
  2024); `build.py` *replaces* the Page-1 storage rows with the storage-sheet rows,
  so each storage unit is counted once, with gross discharge.
- "All generation" excludes storage. EIA's own total is the sum of net generation
  over all units (4,299 TWh in 2024); ours is 4,307 (storage's −8 TWh net omitted).
  Adding gross discharge on top of other fuels would overcount by 38 TWh (0.9 %).
- Clustered tile sums equal the GeoJSON sums exactly at every zoom (re-verified on the
  full 2001–2025 build at z0/3/5/6/10 for 2007 and 2025) when counting features inside
  each tile's extent; the extra features in tile buffers are not drawn by MapLibre's
  circle/heatmap buckets.
- tile.sh clusters in two passes (z0–5 at 5 px, z6–10 at 2 px, merged with tile-join)
  because tippecanoe 2.8x segfaults on --cluster-densest-as-needed with ~100
  accumulated attributes. Largest tile ≈ 350 KB. Full build: 99 MB of tiles, biggest
  file plants.pmtiles 32 MB (GitHub Pages limit is 100 MB per file).
- Heatmap intensity is normalised per fuel (Chris's design): brightness is comparable
  across time within one fuel, NOT across fuels. The dropdown shows absolute TWh.

## Map (index.html)

Ported from Chris's page: heatmap (z0–10, quarterly tiles interpolated monthly via
`heatmap-weight`), per-fuel tileset swap with an intensity fade, circles (z9+) with
radius `sqrt(monthly MWh)/5` at z16, labels in a separate source (label layout is
only refreshed when zoom ≥ 9.5), satellite fading in z8–12, hash URL, 6-chapter tour.
Heatmap intensity per fuel = `0.5 × total TWh / fuel TWh` (reference year).
`const TOUR = false` at the top of the script turns the page into a plain
explorable map (no chapters or modals); the data layers are unaffected.

## Full-build results (Oct 28 2026, all 52 files)

308 months Jan 2001–Aug 2026 (2026 completed from EIA state estimates, see above). 16,429 plants, 97.7 % with
coordinates covering 99.96 % of generation. 19,032 plant×fuel features. Coal peaks 2,017 TWh
(2007), trough 773 (2020); gas 1,879 (2024); 2025: solar 298, wind 461, storage 51 TWh.
288 plant-fuels with output after their 860M retirement month (qa_output_after_retirement.csv).
Planned (860M): 1,799 plant×fuel units, 67 GW of storage.

## Sanity checks

`build.py` prints annual TWh by fuel. 2024 should read roughly: gas 1,877, nuclear
782, coal 651, wind 450, hydro 243, solar 219, storage 38. Coal should peak ~2005–2008
near 2,000 TWh. Batteries should be ~0 before 2017 and rise steeply from 2021.
