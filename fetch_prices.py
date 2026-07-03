#!/usr/bin/env python3
"""
FuelGrid Europe - weekly price fetcher
======================================
Pulls official open data and writes data/prices-latest.json for the frontend.

Sources
  ES  Geoportal Gasolineras (MITECO)      - JSON, no key, all stations
  FR  prix-carburants.gouv.fr instantane  - zipped XML, no key, all stations
  DE  Tankerkoenig / MTS-K                - JSON API, FREE key required
                                            https://creativecommons.tankerkoenig.de
  IT  Osservaprezzi Carburanti (MIMIT)    - daily CSVs, no key, all stations
  HVO taken from the ES and IT feeds when those columns/rows exist
  EV  not wired yet (phase 3) - frontend keeps sample data for it

Only standard library is used except nothing at all - zero pip installs needed.
Runs in ~1 min without the German key, ~4-5 min with it (grid of API calls).

Any single country failing does NOT kill the run; you get whatever succeeded.
Exit code is non-zero only if NO diesel data was fetched at all.
"""

import csv
import datetime as dt
import io
import json
import math
import os
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "data", "prices-latest.json")
UA = {"User-Agent": "FuelGridEurope/0.2 (weekly open-data snapshot; contact via repo)"}

ES_URL = ("https://sedeaplicaciones.minetur.gob.es/ServiciosRESTCarburantes/"
          "PreciosCarburantes/EstacionesTerrestres/")
FR_URL = "https://donnees.roulez-eco.fr/opendata/instantane"
DE_URL = "https://creativecommons.tankerkoenig.de/json/list.php"
IT_ANAG = "https://www.mimit.gov.it/images/exportCSV/anagrafica_impianti_attivi.csv"
IT_PREZZI = "https://www.mimit.gov.it/images/exportCSV/prezzo_alle_8.csv"

# Spain's feed may expose renewable diesel under different column names
# depending on rollout stage - we try each. If none exist, HVO simply
# stays in sample mode for Spain.
ES_HVO_KEYS = [
    "Precio Di\u00e9sel Renovable",
    "Precio Gas\u00f3leo Renovable",
    "Precio Diesel Renovable",
    "Precio Gasoleo Renovable",
    "Precio HVO",
]


def http_get(url, timeout=180):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def to_f(x):
    """Parse '1,439' / '1.439' -> float, else None. Rejects <=0."""
    if x is None:
        return None
    s = str(x).strip().replace(",", ".")
    if not s:
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    return v if v > 0 else None


def row(lat, lng, cc, brand, name, price, mwy):
    return [round(lat, 5), round(lng, 5), cc, brand[:30], name[:60],
            round(price, 3), 1 if mwy else 0]


# ----------------------------------------------------------------- Spain
def fetch_es():
    data = json.loads(http_get(ES_URL).decode("utf-8"))
    diesel, hvo = [], []
    for e in data.get("ListaEESSPrecio", []):
        lat = to_f(e.get("Latitud"))
        lng = to_f(e.get("Longitud (WGS84)"))
        if lat is None or lng is None:
            continue
        brand = (e.get("R\u00f3tulo") or "Estaci\u00f3n").strip().title() or "Estaci\u00f3n"
        town = (e.get("Municipio") or "").strip().title()
        name = f"{brand} \u00b7 {town}" if town else brand
        p = to_f(e.get("Precio Gasoleo A"))
        if p:
            diesel.append(row(lat, lng, "ES", brand, name, p, 0))
        for k in ES_HVO_KEYS:
            hp = to_f(e.get(k))
            if hp:
                hvo.append(row(lat, lng, "ES", brand, name + " (HVO100)", hp, 0))
                break
    return diesel, hvo


# ---------------------------------------------------------------- France
def fetch_fr():
    raw = http_get(FR_URL)
    zf = zipfile.ZipFile(io.BytesIO(raw))
    xml_bytes = zf.read(zf.namelist()[0])
    root = ET.fromstring(xml_bytes)  # file declares its own encoding
    out = []
    for pdv in root.iter("pdv"):
        try:
            lat = float(pdv.get("latitude")) / 100000.0
            lng = float(pdv.get("longitude")) / 100000.0
        except (TypeError, ValueError):
            continue
        if not (41.0 < lat < 51.5) or not (-5.5 < lng < 10.0):
            continue  # metropolitan France only
        ville = (pdv.findtext("ville") or "").strip().title()
        mwy = (pdv.get("pop") == "A")  # A = autoroute, R = route
        p = None
        for prix in pdv.iter("prix"):
            if prix.get("nom") == "Gazole":
                p = to_f(prix.get("valeur"))
                break
        if p:
            name = f"Station \u00b7 {ville}" if ville else "Station"
            out.append(row(lat, lng, "FR", "Station", name, p, mwy))
    return out


# --------------------------------------------------------------- Germany
def fetch_de():
    key = os.environ.get("TANKERKOENIG_API_KEY", "").strip()
    if not key:
        print("DE: TANKERKOENIG_API_KEY secret not set - skipping Germany "
              "(get a free key at creativecommons.tankerkoenig.de)")
        return []
    seen, out = set(), []
    lat, lat_step = 47.30, 0.30           # 25 km radius circles on ~33 km grid
    calls = 0
    while lat <= 55.10:
        lng_step = 0.30 / max(0.25, math.cos(math.radians(lat)))
        lng = 5.85
        while lng <= 15.05:
            url = (f"{DE_URL}?lat={lat:.3f}&lng={lng:.3f}&rad=25"
                   f"&sort=dist&type=diesel&apikey={key}")
            try:
                js = json.loads(http_get(url, 60).decode("utf-8"))
                for s in js.get("stations", []):
                    sid = s.get("id")
                    p = to_f(s.get("diesel"))
                    slat, slng = s.get("lat"), s.get("lng")
                    if not sid or sid in seen or not p or slat is None:
                        continue
                    seen.add(sid)
                    brand = (s.get("brand") or "Freie Tankstelle").strip().title() \
                            or "Freie Tankstelle"
                    place = (s.get("place") or "").strip().title()
                    name = f"{brand} \u00b7 {place}" if place else brand
                    out.append(row(float(slat), float(slng), "DE",
                                   brand, name, p, 0))
            except Exception as exc:  # one bad cell must not kill the sweep
                print(f"DE grid cell ({lat:.2f},{lng:.2f}) failed: {exc}")
            calls += 1
            time.sleep(0.4)  # be polite to the free API
            lng += lng_step
        lat += lat_step
    print(f"DE: {calls} grid calls, {len(out)} unique stations")
    return out


# ----------------------------------------------------------------- Italy
def _it_rows(url):
    text = http_get(url).decode("utf-8", errors="replace").splitlines()
    if text and ";" not in text[0]:
        text = text[1:]  # first line is an extraction-date banner
    return list(csv.DictReader(text, delimiter=";"))


def fetch_it():
    anag = {}
    for r in _it_rows(IT_ANAG):
        i = (r.get("idImpianto") or "").strip()
        if i:
            anag[i] = r
    best, hvo_best = {}, {}
    for r in _it_rows(IT_PREZZI):
        i = (r.get("idImpianto") or "").strip()
        p = to_f(r.get("prezzo"))
        if not i or not p:
            continue
        desc = (r.get("descCarburante") or "").strip().lower()
        is_self = str(r.get("isSelf")).strip() in ("1", "true", "True")
        if "hvo" in desc:
            cur = hvo_best.get(i)
            if cur is None or (is_self and not cur[1]):
                hvo_best[i] = (p, is_self)
        elif desc == "gasolio":
            cur = best.get(i)
            if cur is None or (is_self and not cur[1]) \
               or (is_self == cur[1] and p < cur[0]):
                best[i] = (p, is_self)

    def station_row(i, price, hvo=False):
        a = anag.get(i)
        if not a:
            return None
        lat = to_f(a.get("Latitudine"))
        lng = to_f(a.get("Longitudine"))
        if lat is None or lng is None or not (35.0 < lat < 47.6) \
           or not (6.0 < lng < 19.0):
            return None
        brand = (a.get("Bandiera") or "Pompa Bianca").strip().title() \
                or "Pompa Bianca"
        town = (a.get("Comune") or "").strip().title()
        name = f"{brand} \u00b7 {town}" if town else brand
        if hvo:
            name += " (HVO100)"
        mwy = "autostrad" in (a.get("Tipo Impianto") or "").lower()
        return row(lat, lng, "IT", brand, name, price, mwy)

    diesel = [x for x in (station_row(i, p) for i, (p, _s) in best.items()) if x]
    hvo = [x for x in (station_row(i, p, True)
                       for i, (p, _s) in hvo_best.items()) if x]
    return diesel, hvo


# ------------------------------------------------------------------ main
def main():
    diesel, hvo = [], []
    jobs = [
        ("ES", fetch_es, True),
        ("FR", fetch_fr, False),
        ("DE", fetch_de, False),
        ("IT", fetch_it, True),
    ]
    for label, fn, returns_pair in jobs:
        try:
            res = fn()
            if returns_pair:
                d, h = res
                diesel += d
                hvo += h
                print(f"{label}: {len(d)} diesel, {len(h)} HVO")
            else:
                diesel += res
                print(f"{label}: {len(res)} diesel")
        except Exception as exc:
            print(f"{label}: FAILED - {exc}", file=sys.stderr)

    if not diesel:
        sys.exit("No diesel data fetched from any source - keeping previous file.")

    fuels = {
        "diesel": {"live": True, "stations": diesel},
        # only flip HVO to live if we actually got a meaningful set
        "hvo": ({"live": True, "stations": hvo} if len(hvo) >= 25
                else {"live": False}),
        "ev": {"live": False},  # phase 3
    }
    now = dt.datetime.now(dt.timezone.utc)
    payload = {
        "generated": now.isoformat(timespec="seconds"),
        "snapshot_label": now.strftime("%a %-d %b %Y"),
        "fuels": fuels,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"), ensure_ascii=False)
    size_mb = os.path.getsize(OUT) / 1e6
    print(f"Wrote {OUT}  ({size_mb:.1f} MB) - "
          f"{len(diesel)} diesel / {len(hvo)} HVO stations")


if __name__ == "__main__":
    main()
