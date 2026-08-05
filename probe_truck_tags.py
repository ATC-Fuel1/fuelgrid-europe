#!/usr/bin/env python3
"""
FuelGrid - OSM truck-tag COVERAGE PROBE (read-only, measures nothing else).

Purpose: before building a truck filter, find out how many fuel stations in
OpenStreetMap actually carry a real truck signal in each country the map
covers. If coverage is decent we build the filter on it; if it is tiny we
know OSM alone is not enough and we say so plainly.

This script ONLY reads from Overpass and prints a table. It writes nothing to
the repo and changes no app data, so it is completely safe to run.

Run it the same way you ran the bulletin fetcher:
    python probe_truck_tags.py

It takes a few minutes (it spaces requests out to respect Overpass fair-use).
"""
import json
import time
import urllib.parse
import urllib.request

# every country the map shows, with its current priced-station count for context
COUNTRIES = [
    ("IT", 21129), ("DE", 14317), ("ES", 11310), ("FR", 9529), ("PL", 8641),
    ("NL", 4102), ("CH", 3770), ("GB", 3706), ("SE", 3377), ("BE", 3014),
    ("RO", 2934), ("CZ", 2876), ("NO", 2280), ("DK", 2124), ("HU", 1817),
    ("IE", 1677), ("SK", 1089), ("HR", 956), ("SI", 588), ("AT", 569),
    ("LU", 245),
]

OVERPASS_HOSTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
]
UA = "FuelGrid-truck-probe/1.0"

# One query per country. It builds the full fuel set, then counts subsets:
#   total      = all amenity=fuel (nodes + ways)
#   hgv_yes    = hgv = yes / designated            (positively truck-allowed)
#   hgv_no     = hgv = no                          (positively truck-banned)
#   restricted = access = private / no / customers (not open to through trucks)
#   name_truck = name/operator looks truck-oriented (Autohof/LKW/Aire/Truck/Routier)
# Each "out count;" returns one count element, in this order.
QUERY = (
    '[out:json][timeout:180];'
    'area["ISO3166-1"="{cc}"]["admin_level"="2"]->.a;'
    '(node["amenity"="fuel"](area.a);way["amenity"="fuel"](area.a);)->.f;'
    '.f out count;'
    '(node.f["hgv"~"^(yes|designated)$"];way.f["hgv"~"^(yes|designated)$"];)->.hy;'
    '.hy out count;'
    '(node.f["hgv"="no"];way.f["hgv"="no"];)->.hn;'
    '.hn out count;'
    '(node.f["access"~"^(private|no|customers)$"];'
    'way.f["access"~"^(private|no|customers)$"];)->.rs;'
    '.rs out count;'
    '(node.f["name"~"Autohof|LKW|Aire|Truck|Routier",i];'
    'way.f["name"~"Autohof|LKW|Aire|Truck|Routier",i];'
    'node.f["operator"~"Autohof|LKW|Truck",i];'
    'way.f["operator"~"Autohof|LKW|Truck",i];)->.nm;'
    '.nm out count;'
)


def run_query(cc):
    """Return list of counts [total, hgv_yes, hgv_no, restricted, name_truck]
    for one country, or None if every mirror failed."""
    q = QUERY.format(cc=cc)
    last = ""
    for host in OVERPASS_HOSTS:
        try:
            url = host + "?data=" + urllib.parse.quote(q)
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            data = json.loads(urllib.request.urlopen(req, timeout=180).read().decode("utf-8"))
            counts = []
            for el in data.get("elements", []):
                if el.get("type") == "count":
                    counts.append(int(el.get("tags", {}).get("total", 0)))
            if len(counts) == 5:
                return counts
            last = f"expected 5 counts, got {len(counts)}"
        except Exception as exc:
            last = str(exc)
    print(f"  {cc}: query failed - {last}")
    return None


def main():
    print("OSM truck-tag coverage probe - read-only, writes nothing.\n")
    header = f"{'CC':<3} {'OSM fuel':>9} {'hgv=yes':>8} {'hgv=no':>7} {'restrict':>8} {'truck-name':>10} {'usable %':>9}"
    print(header)
    print("-" * len(header))
    totals = [0, 0, 0, 0, 0]
    for cc, _priced in COUNTRIES:
        c = run_query(cc)
        if c is None:
            time.sleep(8.0)
            continue
        total, hy, hn, rs, nm = c
        for i, v in enumerate(c):
            totals[i] += v
        # "usable" = stations we could positively call truck-OK from OSM today
        usable = hy
        pct = (100.0 * usable / total) if total else 0.0
        print(f"{cc:<3} {total:>9} {hy:>8} {hn:>7} {rs:>8} {nm:>10} {pct:>8.1f}%")
        time.sleep(8.0)                      # Overpass fair-use spacing
    print("-" * len(header))
    tot, hy, hn, rs, nm = totals
    pct = (100.0 * hy / tot) if tot else 0.0
    print(f"{'ALL':<3} {tot:>9} {hy:>8} {hn:>7} {rs:>8} {nm:>10} {pct:>8.1f}%")
    print("\nReading it:")
    print("  hgv=yes    = stations OSM positively marks truck-accessible (what a")
    print("               real 'Trucks' toggle could confidently show).")
    print("  hgv=no     = positively truck-banned (we'd hide these).")
    print("  restrict   = access private/customers only (context, often not through-trucks).")
    print("  truck-name = name looks truck-oriented (the weak heuristic, for comparison).")
    print("  Everything else is UNKNOWN and must be shown, not hidden.")
    print("\nIf 'usable %' is low across the board, OSM tags alone can't back an")
    print("honest filter yet - that's the signal to wait for the DKV network data.")


if __name__ == "__main__":
    main()
