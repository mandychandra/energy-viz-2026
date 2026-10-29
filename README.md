# US electric generation & storage map, 2001–2026

Every US power plant and grid battery, month by month, on a map. A rebuild of
[Chris Loer's 2019 map](https://chrisloer.github.io/energy-viz/) with current
EIA data and a new energy-storage layer. See `CLAUDE.md` for the design notes.

```
pip3 install -r requirements.txt
brew install tippecanoe
python3 pipeline/download.py && python3 pipeline/build.py && bash pipeline/tile.sh
python3 pipeline/serve.py     # then open http://localhost:8000
```

Data: US EIA [Form 923](https://www.eia.gov/electricity/data/eia923/) and
[Form 860](https://www.eia.gov/electricity/data/eia860/).
