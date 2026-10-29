#!/usr/bin/env bash
# GeoJSON -> PMTiles with tippecanoe.  Run from the project root after build.py.
#   brew install tippecanoe
#   bash pipeline/tile.sh
#
# Produces tiles/<fuel>.pmtiles (clustered quarterly data for the heatmap, one
# per fuel category plus "all"), tiles/plants.pmtiles (monthly, unclustered,
# for circles at high zoom) and tiles/labels.pmtiles.
set -euo pipefail
cd "$(dirname "$0")/.."
OUT=data/out
TILES=tiles
mkdir -p "$TILES"

command -v tippecanoe >/dev/null || { echo "tippecanoe not found: brew install tippecanoe"; exit 1; }

# Sum every quarterly attribute when points are clustered.
ACC=$(python3 -c "import json; q=json.load(open('$OUT/fuel_totals.json'))['quarters']; print(json.dumps({f'netgen_{k}':'sum' for k in q}))")

# Heatmap tilesets, zoom 0-10. Keep every point (-r1) but merge points within a
# few pixels of each other (units: pixels of a 256px tile), summing the
# generation attributes so totals are preserved exactly. Low zooms use a wider
# merge distance so the whole-country tiles stay small; the two passes are
# merged with tile-join. (--cluster-densest-as-needed would do this adaptively
# but segfaults in tippecanoe 2.8x when ~100 attributes are accumulated.)
CLUSTER_LO=${CLUSTER_LO:-5}    # zooms 0-5
CLUSTER_HI=${CLUSTER_HI:-2}    # zooms 6-10
tile_fuel () {  # name, filter-expression
  echo "== $1"
  tippecanoe -o "$TILES/_lo.pmtiles" --force -l gen -Z0 -z5 -r1 --cluster-distance="$CLUSTER_LO" \
    --accumulate-attribute="$ACC" -j "$2" --no-tile-size-limit --quiet "$OUT/quarterly_generation.geojson"
  tippecanoe -o "$TILES/_hi.pmtiles" --force -l gen -Z6 -z10 -r1 --cluster-distance="$CLUSTER_HI" \
    --accumulate-attribute="$ACC" -j "$2" --no-tile-size-limit --quiet "$OUT/quarterly_generation.geojson"
  tile-join -o "$TILES/$1.pmtiles" --force --no-tile-size-limit "$TILES/_lo.pmtiles" "$TILES/_hi.pmtiles" 2>/dev/null
  rm -f "$TILES/_lo.pmtiles" "$TILES/_hi.pmtiles"
}
tile_fuel all        '{"*":["all",["!=","fuel_type",11],["!=","fuel_type",12]]}'   # all generation; batteries (11) and pumped hydro (12) excluded
for f in 0 1 2 3 4 5 6 7 8 9 11 12; do
  tile_fuel "fuel$f" "{\"*\":[\"==\",\"fuel_type\",$f]}"
done

# Individual plants (monthly data) at zoom 9-10 only, nothing dropped; MapLibre
# overzooms beyond 10 (the source is declared maxzoom 10 in index.html).
# Each extra zoom level would store every plant's 300+ monthly values again.
echo "== plants"
tippecanoe -o "$TILES/plants.pmtiles" --force -l plants -Z9 -z10 -r1 \
  --no-feature-limit --no-tile-size-limit --quiet "$OUT/plant_generation.geojson"
echo "== labels"
tippecanoe -o "$TILES/labels.pmtiles" --force -l labels -Z10 -z10 -r1 \
  --no-feature-limit --no-tile-size-limit --quiet "$OUT/plant_labels.geojson"

if [ -f "$OUT/planned.geojson" ]; then
  echo "== planned"
  tippecanoe -o "$TILES/planned.pmtiles" --force -l planned -Z5 -z10 -r1 \
    --no-feature-limit --no-tile-size-limit --quiet "$OUT/planned.geojson"
fi

ls -lh "$TILES"
