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
import gzip
import io
import json
import math
import os
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "data", "prices-latest.json")
UA = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0 Safari/537.36 FuelGridEurope/0.17"),
    "Accept": "text/csv,application/json,*/*;q=0.8",
    "Accept-Language": "it-IT,it;q=0.9,es;q=0.8,en;q=0.6",
    "Accept-Encoding": "identity",
}

ES_URL = ("https://sedeaplicaciones.minetur.gob.es/ServiciosRESTCarburantes/"
          "PreciosCarburantes/EstacionesTerrestres/")
# Lighter diesel-only endpoint, used if the full feed keeps dropping (4 = Gasoleo A)
ES_URL_DIESEL = ES_URL.rstrip("/") + "/FiltroProducto/4"
ES_HOSTS = ["https://sedeaplicaciones.minetur.gob.es"]
FR_URL = "https://donnees.roulez-eco.fr/opendata/instantane"
DE_URL = "https://creativecommons.tankerkoenig.de/json/list.php"
# Italy moved domains before (mise -> mimit); we try both hosts.
IT_BASES = [
    "https://www.mimit.gov.it/images/exportCSV/",
    "https://www.mise.gov.it/images/exportCSV/",
]
IT_ANAG_FILE = "anagrafica_impianti_attivi.csv"
IT_PREZZI_FILE = "prezzo_alle_8.csv"
OCM_URL = "https://api.openchargemap.io/v3/poi"
EV_CCS = ["ES", "FR", "DE", "IT", "GB", "AT", "BE", "NL", "LU", "IE",
          "CZ", "SK", "HU", "SE"]          # all countries, EV via Open Charge Map
ECB_FX_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
EU_BULLETIN_URLS = [
    "https://ec.europa.eu/energy/observatory/reports/latest_prices_raw_data.xlsx",
    "https://energy.ec.europa.eu/system/files/latest_prices_raw_data.xlsx",
]
# countries we fill from the EC Weekly Oil Bulletin + their national currency
BULLETIN_CCS = {"BE": "EUR", "NL": "EUR", "LU": "EUR", "IE": "EUR",
                "CZ": "CZK", "SK": "EUR", "HU": "HUF", "SE": "SEK"}
AT_URL = "https://api.e-control.at/sprit/1.0/search/gas-stations/by-address"
MANUAL_DIR = os.path.join(HERE, "data", "manual")
UK_FEEDS = [
    ("Applegreen", "https://applegreenstores.com/fuel-prices/data.json"),
    ("Ascona", "https://fuelprices.asconagroup.co.uk/newfuel.json"),
    ("Asda", "https://storelocator.asda.com/fuel_prices_data.json"),
    ("BP", "https://www.bp.com/en_gb/united-kingdom/home/fuelprices/fuel_prices_data.json"),
    ("Esso", "https://fuelprices.esso.co.uk/latestdata.json"),
    ("Jet", "https://jetlocal.co.uk/fuel_prices_data.json"),
    ("Morrisons", "https://www.morrisons.com/fuel-prices/fuel.json"),
    ("Moto", "https://moto-way.com/fuel-price/fuel_prices.json"),
    ("MFG", "https://fuel.motorfuelgroup.com/fuel_prices_data.json"),
    ("Rontec", "https://www.rontec-servicestations.co.uk/fuel-prices/data/fuel_prices_data.json"),
    ("Sainsburys", "https://api.sainsburys.co.uk/v1/exports/latest/fuel_prices_data.json"),
    ("Shell", "https://www.shell.co.uk/fuel-prices-data.html"),
    ("Tesco", "https://www.tesco.com/fuel_prices/fuel_prices_data.json"),
]

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


def http_get(url, timeout=180, tries=3):
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
            if data[:2] == b"\x1f\x8b":  # gzip magic - some CDNs compress uninvited
                data = gzip.decompress(data)
            return data
        except Exception as exc:
            last = exc
            if attempt < tries - 1:
                wait = 4 * (attempt + 1)
                print(f"  retry {attempt + 1}/{tries - 1} for "
                      f"{url.split('?')[0]} in {wait}s ({exc})")
                time.sleep(wait)
    raise last


def to_f(x):
    """Parse '1,439' / '1.439' / '-3,70' -> float, else None."""
    if x is None:
        return None
    s = str(x).strip().replace(",", ".")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def to_price(x, lo=0.2, hi=5.0):
    """Price parser: like to_f but only accepts sane per-litre/kWh values."""
    v = to_f(x)
    return v if v is not None and lo < v < hi else None


def row(lat, lng, cc, brand, name, price, mwy):
    return [round(lat, 5), round(lng, 5), cc, brand[:30], name[:60],
            round(price, 3), 1 if mwy else 0]


# ----------------------------------------------------------------- Spain
def fetch_es():
    filtered = False
    data = last = None
    for host in ES_HOSTS:
        base = host + ("/ServiciosRESTCarburantes/PreciosCarburantes/"
                       "EstacionesTerrestres/")
        for url, filt in ((base, False), (base.rstrip("/") + "/FiltroProducto/4", True)):
            try:
                data = json.loads(http_get(url).decode("utf-8"))
                filtered = filt
                break
            except Exception as exc:
                last = exc
                print(f"ES: {host.split('//')[1]}"
                      f"{' diesel-only' if filt else ''} failed ({exc})")
        if data is not None:
            break
    if data is None:
        raise RuntimeError(f"all MITECO endpoints failed ({last})")
    diesel, hvo = [], []
    for e in data.get("ListaEESSPrecio", []):
        lat = to_f(e.get("Latitud"))
        lng = to_f(e.get("Longitud (WGS84)"))
        if lat is None or lng is None:
            continue
        if not (27.0 < lat < 44.5 and -19.0 < lng < 5.0):
            continue
        brand = (e.get("R\u00f3tulo") or "Estaci\u00f3n").strip().title() or "Estaci\u00f3n"
        town = (e.get("Municipio") or "").strip().title()
        name = f"{brand} \u00b7 {town}" if town else brand
        p = to_price(e.get("Precio Gasoleo A") or e.get("PrecioProducto"))
        if p:
            diesel.append(row(lat, lng, "ES", brand, name, p, 0))
        if filtered:
            continue  # diesel-only endpoint has no HVO columns
        for k in ES_HVO_KEYS:
            hp = to_price(e.get(k))
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
                p = to_price(prix.get("valeur"))
                break
        if p:
            name = f"Station \u00b7 {ville}" if ville else "Station"
            out.append(row(lat, lng, "FR", "Station", name, p, mwy))
    return out


# --------------------------------------------------------------- Germany
TK_DUMP_BASE = ("https://dev.azure.com/tankerkoenig/tankerkoenig-data/_apis/"
                "git/repositories/tankerkoenig-data/items")


_TK_VARIANTS = [
    "api-version=7.0&$format=octetStream&resolveLfs=true",
    "api-version=6.0&$format=octetStream&resolveLfs=true",
    "api-version=6.0&download=true&resolveLfs=true",
    "$format=text&api-version=7.0",
]
_tk_good = [None]        # remember the variant that works, reuse it


def _tk_dump(path, expect):
    """Fetch a raw CSV file from the Azure repo. Azure only returns raw
    bytes with the right query param, so try the variants (fast: 15s, no
    retries) and verify the response is really the CSV we asked for. Once
    one variant works, reuse it. Distinguish 'server answered but not CSV'
    (wrong URL scheme -> give up) from 'HTTP error' (file may not exist)."""
    order = ([_tk_good[0]] + [q for q in _TK_VARIANTS if q != _tk_good[0]]
             if _tk_good[0] else _TK_VARIANTS)
    non_csv = False
    last = ""
    for q in order:
        try:
            txt = http_get(f"{TK_DUMP_BASE}?path={path}&{q}", 15,
                           tries=1).decode("utf-8", errors="replace")
        except Exception as exc:
            last = f"HTTP {exc}"
            continue
        if expect in txt[:600].lower():
            _tk_good[0] = q
            return txt
        non_csv = True
        last = "non-CSV: " + txt[:80].replace("\n", " ").replace("\r", " ")
    err = RuntimeError(last or "no response")
    err.non_csv = non_csv      # signal: server responded but wrong content
    raise err


def _tk_stations(day):
    txt = _tk_dump(f"/stations/{day:%Y/%m}/{day:%Y-%m-%d}-stations.csv",
                   "latitude")
    st = {}
    for r in csv.DictReader(io.StringIO(txt)):
        uuid = (r.get("uuid") or "").strip()
        lat, lng = to_f(r.get("latitude")), to_f(r.get("longitude"))
        if not uuid or lat is None or lng is None:
            continue
        if not (47.0 < lat < 55.2 and 5.8 < lng < 15.1):
            continue
        brand = ((r.get("brand") or "").strip().title() or "Tankstelle")[:30]
        city = (r.get("city") or "").strip().title()
        st[uuid] = (round(lat, 5), round(lng, 5), brand, city)
    return st


def fetch_de_dump():
    """Primary German source: Tankerkoenig's official dated CSV dumps
    (the MTS-K dataset) from its Azure data repository - a static host
    that, unlike the live API, does not block cloud runners. Join the
    stations dump with the prices dump on the station UUID."""
    stations = None
    for back in range(3):                       # today + 2 days back only
        day = dt.date.today() - dt.timedelta(days=back)
        try:
            stations = _tk_stations(day)
            if stations:
                print(f"DE dump: {len(stations)} stations ({day})")
                break
        except Exception as exc:
            print(f"DE dump: stations {day} unavailable ({exc})")
            stations = None
            if getattr(exc, "non_csv", False):
                print("DE dump: Azure returned a page, not the CSV - dump URL "
                      "scheme not working, skipping dump this run")
                return []       # wrong scheme; don't grind other days
    if not stations:
        return []
    for back in range(3):
        day = dt.date.today() - dt.timedelta(days=back)
        try:
            txt = _tk_dump(f"/prices/{day:%Y/%m}/{day:%Y-%m-%d}-prices.csv",
                           "station_uuid")
        except Exception as exc:
            print(f"DE dump: prices {day} unavailable ({exc})")
            continue
        out = []
        for r in csv.DictReader(io.StringIO(txt)):
            st = stations.get((r.get("station_uuid") or "").strip())
            if not st:
                continue
            p = to_price(r.get("diesel"))
            if p is None:
                continue
            lat, lng, brand, city = st
            name = (f"{brand} \u00b7 {city}" if city else brand)[:60]
            out.append([lat, lng, "DE", brand, name, round(p, 3), 0])
        if out:
            print(f"DE dump: {len(out)} diesel prices ({day})")
            return out
        print(f"DE dump: prices {day} joined 0 stations - trying older")
    return []


def fetch_de():
    """Germany: pull the official daily CSV dump first (static host, no
    rate limit, ~15k stations in two files); if it yields nothing this
    run, fall back to the live API (which may be blocked/throttled)."""
    try:
        rows = fetch_de_dump()
    except Exception as exc:
        print(f"DE dump: failed - {exc}")
        rows = []
    if rows:
        print(f"DE: {len(rows)} stations from the daily dump")
        return rows
    print("DE: daily dump empty this run - trying the live API")
    return fetch_de_live()


def fetch_de_live():
    key = os.environ.get("TANKERKOENIG_API_KEY", "").strip()
    if not key:
        print("DE: TANKERKOENIG_API_KEY secret not set - skipping Germany "
              "(get a free key at creativecommons.tankerkoenig.de)")
        return []
    print(f"DE: API key detected (length {len(key)}, ends ...{key[-4:]})")
    probe = (f"{DE_URL}?lat=52.520&lng=13.405&rad=5&sort=dist"
             f"&type=diesel&apikey={key}")
    try:
        pj = json.loads(http_get(probe, 30, tries=1).decode("utf-8"))
        if pj.get("ok") is False:
            print(f"DE: live API rejected the probe ('{pj.get('message')}') - "
                  "skipping the sweep; using pinned average this run")
            return []
        print("DE: live API answered the probe - running the full sweep")
    except Exception as exc:
        print(f"DE: live API unreachable from the runner ({exc}) - skipping the "
              "sweep; using pinned average (Tankerkoenig blocks cloud IPs)")
        return []
    seen, out = set(), []
    errs, pause = {}, 0.8
    bad_streak, probed = 0, False
    lat, lat_step = 47.30, 0.30           # 25 km radius circles on ~33 km grid
    calls = 0
    while lat <= 55.10:
        lng_step = 0.30 / max(0.25, math.cos(math.radians(lat)))
        lng = 5.85
        while lng <= 15.05:
            url = (f"{DE_URL}?lat={lat:.3f}&lng={lng:.3f}&rad=25"
                   f"&sort=dist&type=diesel&apikey={key}")
            try:
                js = json.loads(http_get(url, 45, tries=1).decode("utf-8"))
                bad_streak = 0
                if js.get("ok") is False:
                    msg = str(js.get("message") or "unknown API error")[:90]
                    errs[msg] = errs.get(msg, 0) + 1
                    if "key" in msg.lower() and errs[msg] >= 3:
                        print(f"DE: Tankerkoenig rejects the API key ('{msg}') - aborting sweep")
                        return []
                    if any(w in msg.lower() for w in ("rate", "limit", "too many")):
                        pause = min(pause + 0.4, 2.4)
                for s in (js.get("stations", []) if js.get("ok") is not False else []):
                    sid = s.get("id")
                    p = to_price(s.get("diesel"))
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
                bad_streak += 1
                if bad_streak >= 8:
                    print(f"DE: {bad_streak} cells failed in a row - stopping the "
                          f"sweep, keeping {len(out)} stations (retried next run)")
                    return out
            calls += 1
            time.sleep(pause)  # polite spacing; slows down if the API asks
            lng += lng_step
        lat += lat_step
    print(f"DE: {calls} grid calls, {len(out)} unique stations")
    if not out and errs:
        for msg, n in sorted(errs.items(), key=lambda kv: -kv[1])[:2]:
            print(f"DE: {n} cells answered: {msg}")
        print("DE: open the repo secret TANKERKOENIG_API_KEY and re-paste the key "
              "(value must be the key only - no spaces or quotes)")
    return out


# ----------------------------------------------------------------- Italy
def _parse_it_csv(raw):
    """Parse a MIMIT CSV from raw bytes. Tolerates the date-banner line
    (present or not), a UTF-8 BOM, and stray whitespace in header names."""
    text = raw.decode("utf-8-sig", errors="replace").splitlines()
    start = 0
    for i, line in enumerate(text[:5]):
        if "idImpianto" in line:
            start = i
            break
    if not text:
        return []
    head = text[start]
    # MIMIT has shipped both ';' and '|' as separators - detect per file
    delim = "|" if head.count("|") >= head.count(";") else ";"
    rdr = csv.reader(text[start:], delimiter=delim)
    header = [h.strip().lstrip("\ufeff") for h in next(rdr, [])]
    return [dict(zip(header, row)) for row in rdr if row]


def fetch_it():
    anag, prezzi_rows, last_err = {}, [], None
    for base in IT_BASES:
        try:
            raw = http_get(base + IT_ANAG_FILE)
            a_rows = _parse_it_csv(raw)
            if not a_rows or "idImpianto" not in a_rows[0]:
                peek = " ".join(raw[:400].decode("utf-8", "replace").split())[:220]
                raise ValueError(f"unexpected format; server sent: {peek!r}")
            prezzi_rows = _parse_it_csv(http_get(base + IT_PREZZI_FILE))
            for r in a_rows:
                i = (r.get("idImpianto") or "").strip()
                if i:
                    anag[i] = r
            print(f"IT: source {base} ok "
                  f"({len(anag)} stations, {len(prezzi_rows)} price rows)")
            break
        except Exception as exc:
            last_err = exc
            print(f"IT: source {base} failed - {exc}")
    if not anag:
        raise RuntimeError(f"all Italian sources failed - last: {last_err}")
    best, hvo_best = {}, {}
    for r in prezzi_rows:
        i = (r.get("idImpianto") or "").strip()
        p = to_price(r.get("prezzo"))
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


def _parse_ev_cost(s):
    """Extract a per-kWh euro price from OCM's free-text UsageCost, if any."""
    if not s:
        return None
    m = re.search(r"(\d+[.,]\d+)\s*(?:\u20ac|eur)?\s*/?\s*kwh", str(s).lower())
    if not m:
        return None
    v = to_f(m.group(1))
    return round(v, 2) if v is not None and 0.05 < v < 2.0 else None


def fetch_ev():
    key = os.environ.get("OCM_API_KEY", "").strip()
    if not key:
        print("EV: OCM_API_KEY secret not set - keeping sample EV data "
              "(free key at openchargemap.org)")
        return []
    out = []
    for cc in EV_CCS:
        try:
            url = (f"{OCM_URL}?output=json&countrycode={cc}&maxresults=4000"
                   f"&compact=true&verbose=false&key={key}")
            pois = json.loads(http_get(url, 120).decode("utf-8"))
            n0 = len(out)
            for p in pois:
                ai = p.get("AddressInfo") or {}
                lat, lng = ai.get("Latitude"), ai.get("Longitude")
                if lat is None or lng is None:
                    continue
                conns = p.get("Connections") or []
                kw = 0.0
                for c in conns:
                    try:
                        kw = max(kw, float(c.get("PowerKW") or 0))
                    except (TypeError, ValueError):
                        pass
                op = ((p.get("OperatorInfo") or {}).get("Title")
                      or "Operator n/a").strip()[:30]
                town = (ai.get("Town") or "").strip().title()
                name = f"{op} \u00b7 {town}" if town else op
                ty = "HPC" if kw >= 100 else ("DC" if kw >= 43 else "AC")
                out.append([round(float(lat), 5), round(float(lng), 5), cc,
                            op, name[:60], _parse_ev_cost(p.get("UsageCost")),
                            0, int(kw) or None, max(len(conns), 1), ty])
            print(f"EV {cc}: {len(out) - n0} chargers")
            time.sleep(1.0)
        except Exception as exc:
            print(f"EV {cc}: failed - {exc}")
    from collections import Counter as _C
    print(f"EV: {len(out)} chargers total across "
          f"{len(_C(r[2] for r in out))} countries")
    return out


def ecb_rates():
    """Official ECB reference rates: units of currency per 1 EUR."""
    root = ET.fromstring(http_get(ECB_FX_URL, 60))
    out = {}
    for cube in root.iter():
        cur = cube.attrib.get("currency")
        if cur:
            out[cur] = float(cube.attrib["rate"])
    return out


def gbp_to_eur_rate():
    r = ecb_rates().get("GBP")
    if not r:
        raise RuntimeError("GBP not found in ECB feed")
    return r


def _col_idx(ref):
    n = 0
    for ch in ref:
        if ch.isalpha():
            n = n * 26 + ord(ch.upper()) - 64
        else:
            break
    return n - 1


def _xlsx_sheets(raw):
    """All worksheets of an XLSX as row-lists - stdlib only."""
    zf = zipfile.ZipFile(io.BytesIO(raw))
    shared = []
    if "xl/sharedStrings.xml" in zf.namelist():
        for si in ET.fromstring(zf.read("xl/sharedStrings.xml")):
            shared.append("".join(t.text or "" for t in si.iter()
                                  if t.tag.endswith("}t")))
    sheets = []
    for name in sorted(n for n in zf.namelist()
                       if n.startswith("xl/worksheets/sheet")):
        rows = []
        for row in ET.fromstring(zf.read(name)).iter():
            if not row.tag.endswith("}row"):
                continue
            cells = {}
            for c in row:
                if not c.tag.endswith("}c"):
                    continue
                v = None
                for ch in c:
                    if ch.tag.endswith("}v"):
                        v = ch.text
                    elif ch.tag.endswith("}is"):
                        v = "".join(t.text or "" for t in ch.iter()
                                    if t.tag.endswith("}t"))
                if c.attrib.get("t") == "s" and v is not None:
                    try:
                        v = shared[int(v)]
                    except (ValueError, IndexError):
                        pass
                cells[_col_idx(c.attrib.get("r", "A"))] = v
            width = max(cells) + 1 if cells else 0
            rows.append([cells.get(k) for k in range(width)])
        sheets.append(rows)
    return sheets


BULLETIN_PAGE = "https://energy.ec.europa.eu/data-and-analysis/weekly-oil-bulletin_en"
CC_NAMES = {"BELGIUM": "BE", "BELGIQUE": "BE", "NETHERLANDS": "NL",
            "LUXEMBOURG": "LU", "IRELAND": "IE", "CZECHIA": "CZ",
            "CZECH REPUBLIC": "CZ", "SLOVAKIA": "SK", "SLOVAK REPUBLIC": "SK",
            "HUNGARY": "HU", "SWEDEN": "SE"}


def _bulletin_candidates():
    urls = list(EU_BULLETIN_URLS)
    try:
        html = http_get(BULLETIN_PAGE, 90).decode("utf-8", errors="replace")
        found = 0
        for m in re.finditer(r'href="([^"]+\.xlsx[^"]*)"', html, re.I):
            u = m.group(1)
            if u.startswith("/"):
                u = "https://energy.ec.europa.eu" + u
            if u.startswith("http") and u not in urls:
                urls.append(u)
                found += 1
        print(f"EU bulletin: page scan found {found} workbook link(s)")
    except Exception as exc:
        print(f"EU bulletin: page scan failed - {exc}")

    def prio(u):
        low = u.lower()
        if "wo_tax" in low or "wo-tax" in low or "without" in low:
            return 2          # prices WITHOUT taxes - not what we publish
        if "tax" in low or "raw" in low:
            return 0
        return 1
    return sorted(urls, key=prio)[:8]


def _row_cc(cells):
    for cell in cells[:3]:
        t = str(cell or "").strip().upper()
        if t in BULLETIN_CCS:
            return t
        if t in CC_NAMES:
            return CC_NAMES[t]
    return None


def _row_datekey(cells, fallback):
    best = fallback
    for cell in cells:
        v = to_f(cell)
        if v is not None and 30000 < v < 70000:       # excel serial date
            best = max(best, int(v) + 10_000_000)
        m = re.search(r"(\d{1,2})[./-](\d{1,2})[./-](\d{4})", str(cell or ""))
        if m:
            d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            best = max(best, y * 10000 + mo * 100 + d + 100_000_000)
    return best


def _plausible(v):
    return 1.2 < v < 2.6      # diesel incl. taxes, EUR per litre


def _parse_bulletin_rows(rows, fx):
    gas_col = rate_col = None
    for r in rows[:60]:
        for k, cell in enumerate(r):
            t = str(cell or "").lower()
            if "gas oil" in t or "gasoil" in t:
                gas_col = k
            if "exchange" in t or "taux" in t:
                rate_col = k
        if gas_col is not None:
            break
    best = {}
    for idx, r in enumerate(rows):
        cc = _row_cc(r)
        if cc is None:
            continue
        val = None
        if gas_col is not None and gas_col < len(r):
            val = to_f(r[gas_col])
        if val is None or val < 400:
            big = [x for x in (to_f(c) for c in r) if x is not None and x > 400]
            val = big[1] if len(big) >= 2 else (big[0] if big else None)
        if val is None:
            continue
        # candidate denominations: the file's own rate, ECB national rate, already-EUR
        rates = []
        if rate_col is not None and rate_col < len(r):
            fr = to_f(r[rate_col])
            if fr and fr > 0:
                rates.append(fr)
        cur = BULLETIN_CCS[cc]
        if cur != "EUR" and fx.get(cur):
            rates.append(fx[cur])
        rates.append(1.0)
        eur_l = next((round(val / 1000.0 / rt, 3) for rt in rates
                      if _plausible(val / 1000.0 / rt)), None)
        if eur_l is None:
            continue
        key = _row_datekey(r, idx)
        if cc not in best or key >= best[cc][0]:
            best[cc] = (key, eur_l)
    return {cc: v for cc, (k, v) in best.items()}


def fetch_eu_bulletin():
    """EC Weekly Oil Bulletin -> official national diesel averages, EUR/L.
    Scans the bulletin page for workbooks, reads every sheet, detects the
    currency denomination, and only accepts plausible with-taxes sets."""
    try:
        fx = ecb_rates()
    except Exception:
        fx = {}
    result = {}
    for u in _bulletin_candidates():
        try:
            sheets = _xlsx_sheets(http_get(u, 120))
        except Exception as exc:
            print(f"EU bulletin: {u.split('/')[-1][:60]} - {exc}")
            continue
        fname = (u.split("/")[-1].split("?")[0] or "workbook")[:50]
        for si, rows in enumerate(sheets):
            got = _parse_bulletin_rows(rows, fx)
            if len(got) < 4:
                continue
            vals = sorted(got.values())
            med = vals[len(vals) // 2]
            if not (1.3 < med < 2.3):
                print(f"EU bulletin: {fname} sheet {si + 1} rejected "
                      f"(median \u20ac{med:.3f} implausible)")
                continue
            print(f"EU bulletin: accepted {fname} sheet {si + 1} "
                  f"({len(got)} countries)")
            if len(got) > len(result):
                result = got
        if len(result) >= 6:
            break
    for cc in sorted(result):
        print(f"EU bulletin {cc}: diesel \u2248 \u20ac{result[cc]}/L")
    missing = sorted(set(BULLETIN_CCS) - set(result))
    if missing:
        print(f"EU bulletin: no values for {','.join(missing)}")
    return {"diesel": result} if result else {}


def fetch_gb():
    try:
        rate = gbp_to_eur_rate()
        print(f"GB: ECB rate {rate:.4f} GBP/EUR")
    except Exception as exc:
        print(f"GB: ECB FX unavailable ({exc}) - skipping UK this run")
        return []
    out, seen, ok_feeds = [], set(), 0
    for tag, url in UK_FEEDS:
        before = len(out)
        try:
            js = json.loads(http_get(url, 90).decode("utf-8", errors="replace"))
            for s in js.get("stations") or []:
                loc = s.get("location") or {}
                try:
                    lat, lng = float(loc.get("latitude")), float(loc.get("longitude"))
                except (TypeError, ValueError):
                    continue
                if not (49.8 < lat < 61.5 and -8.7 < lng < 2.2):
                    continue
                sid = str(s.get("site_id") or f"{lat:.4f},{lng:.4f}")
                if sid in seen:
                    continue
                pr = s.get("prices") or {}
                p = pr.get("B7", pr.get("b7"))
                try:
                    p = float(str(p).replace(",", "."))
                except (TypeError, ValueError):
                    continue
                if p > 10:
                    p = p / 100.0
                eur = round(p / rate, 3)
                if not (0.8 < eur < 3.5):
                    continue
                seen.add(sid)
                brand = (s.get("brand") or tag).strip().title()[:30]
                pc = (s.get("postcode") or "").strip()
                name = f"{brand} \u00b7 {pc}" if pc else brand
                mwy = 1 if brand.upper() in ("MOTO", "WELCOME BREAK", "ROADCHEF") else 0
                out.append([round(lat, 5), round(lng, 5), "GB", brand, name[:60], eur, mwy])
            ok_feeds += 1
            print(f"GB feed {tag}: +{len(out) - before}")
        except Exception as exc:
            print(f"GB feed {tag}: failed - {exc}")
    print(f"GB: {len(out)} stations from {ok_feeds}/{len(UK_FEEDS)} feeds")
    return out


def fetch_at():
    stations, calls, fails = {}, 0, 0
    lat = 46.30
    while lat <= 49.10:
        lng = 9.40
        while lng <= 17.20:
            url = (f"{AT_URL}?latitude={lat:.3f}&longitude={lng:.3f}"
                   f"&fuelType=DIE&includeClosed=false")
            try:
                data = json.loads(http_get(url, 45, tries=1).decode("utf-8"))
                fails = 0
                for s in data:
                    sid = s.get("id")
                    loc = s.get("location") or {}
                    la, lo = loc.get("latitude"), loc.get("longitude")
                    if sid is None or la is None or lo is None:
                        continue
                    p = None
                    for pr in (s.get("prices") or []):
                        if pr.get("fuelType") == "DIE":
                            p = to_price(pr.get("amount"))
                    prev = stations.get(sid)
                    if prev is not None and not (prev[5] is None and p is not None):
                        continue
                    nm = (s.get("name") or "Tankstelle").strip().title()[:30]
                    city = (loc.get("city") or "").strip().title()
                    stations[sid] = [round(float(la), 5), round(float(lo), 5), "AT", nm,
                                     (f"{nm} \u00b7 {city}" if city else nm)[:60],
                                     round(p, 3) if p else None, 0]
            except Exception as exc:
                print(f"AT cell ({lat:.2f},{lng:.2f}) failed - {exc}")
                fails += 1
                if fails >= 12:
                    out = list(stations.values())
                    priced = sum(1 for r in out if r[5] is not None)
                    print(f"AT: {fails} cells failed in a row - stopping, keeping "
                          f"{len(out)} stations ({priced} priced) (retried next run)")
                    return out
            calls += 1
            time.sleep(0.3)
            lng += 0.5
        lat += 0.25
    out = list(stations.values())
    priced = sum(1 for r in out if r[5] is not None)
    print(f"AT: {calls} cells, {len(out)} stations ({priced} priced)")
    return out


def load_dkv():
    path = os.path.join(MANUAL_DIR, "dkv_stations.csv")
    res = {"diesel": [], "hvo": [], "ev": []}
    if not os.path.exists(path):
        return res
    with open(path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            cc = (r.get("country") or "").strip().upper()
            fuel = (r.get("fuel") or "diesel").strip().lower()
            lat, lng = to_f(r.get("lat")), to_f(r.get("lng"))
            if not cc or lat is None or lng is None or fuel not in res:
                continue
            name = (r.get("name") or "DKV station").strip()[:60]
            brand = (r.get("brand") or "DKV").strip()[:30]
            p = to_price(r.get("price"))
            if fuel == "ev":
                kw = to_f(r.get("kw"))
                res["ev"].append([round(lat, 5), round(lng, 5), cc, brand, name,
                                  round(p, 2) if p else None, 0,
                                  int(kw) if kw else None,
                                  int(to_f(r.get("bays")) or 1),
                                  "DC" if (kw or 0) >= 43 else "AC"])
            else:
                res[fuel].append([round(lat, 5), round(lng, 5), cc, brand,
                                  name, round(p, 3) if p else None, 0])
    for k, v in res.items():
        if v:
            print(f"DKV import: {len(v)} {k} points")
    return res


def load_manual_averages():
    path = os.path.join(MANUAL_DIR, "national_averages.csv")
    out = {}
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            cc = (r.get("country") or "").strip().upper()
            fuel = (r.get("fuel") or "").strip().lower()
            v = to_price(r.get("eur"))
            if cc and fuel and v:
                out.setdefault(fuel, {})[cc] = v
    if out:
        print(f"Manual averages: {sum(len(x) for x in out.values())} entries")
    return out


def load_previous():
    try:
        with open(OUT, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def carry_forward(prev, fuel_key, rows):
    """Top up this run's rows with last run's stations for any country
    that returned nothing today - a bad feed day should never blank a
    country on the map."""
    have = {r[2] for r in rows}
    old = (prev.get("fuels", {}).get(fuel_key, {}) or {}).get("stations") or []
    kept = [r for r in old if r[2] not in have]
    if kept:
        by = {}
        for r in kept:
            by[r[2]] = by.get(r[2], 0) + 1
        print(f"{fuel_key}: carrying forward previous data for {by}")
    return rows + kept


# ------------------------------------------------------------------ main
# Official EU Weekly Oil Bulletin diesel averages (EUR/L, incl. taxes) for
# countries with no station-level feed. Indicative baseline - refresh from
# the public bulletin any time (it opens in a browser as an Excel file):
#   https://energy.ec.europa.eu/data-and-analysis/weekly-oil-bulletin_en
# Override any value at runtime via data/manual/national_averages.csv.
DEFAULT_DIESEL_AVG = {
    "DE": 1.66,
    "BE": 1.79, "NL": 1.86, "LU": 1.45, "IE": 1.77,
    "CZ": 1.47, "SK": 1.51, "HU": 1.63, "SE": 1.72,
}


def merged_averages():
    """No-feed national diesel averages: a pinned official baseline,
    overridden by data/manual/national_averages.csv. The Commission's
    spreadsheet changes format/URL too often to parse reliably from an
    automated run, so the figures are pinned rather than scraped."""
    out = {"diesel": dict(DEFAULT_DIESEL_AVG)}
    for fuel, m in load_manual_averages().items():
        out.setdefault(fuel, {}).update(m)
    return out


def main():
    diesel, hvo = [], []
    jobs = [
        ("ES", fetch_es, True),
        ("FR", fetch_fr, False),
        ("DE", fetch_de, False),
        ("IT", fetch_it, True),
        ("GB", fetch_gb, False),
        ("AT", fetch_at, False),
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

    by_cc = {}
    for r in diesel:
        by_cc[r[2]] = by_cc.get(r[2], 0) + 1
    print("Diesel stations by country:", by_cc or "none")
    for cc in ("ES", "FR", "DE", "IT", "GB", "AT"):
        if by_cc.get(cc, 0) == 0:
            print(f"WARNING: no fresh diesel data for {cc} this run")

    ev = []
    try:
        ev = fetch_ev()
    except Exception as exc:
        print(f"EV: FAILED - {exc}", file=sys.stderr)

    dkv = load_dkv()
    diesel += dkv["diesel"]
    hvo += dkv["hvo"]
    ev += dkv["ev"]

    prev = load_previous()
    diesel = carry_forward(prev, "diesel", diesel)
    hvo = carry_forward(prev, "hvo", hvo)
    ev = carry_forward(prev, "ev", ev)

    if not diesel:
        sys.exit("No diesel data fetched from any source - keeping previous file.")

    fuels = {
        "diesel": {"live": True, "stations": diesel},
        # only flip HVO to live if we actually got a meaningful set
        "hvo": {"live": len(hvo) >= 25, "stations": hvo},
        "ev": {"live": len(ev) >= 50, "stations": ev},
    }
    now = dt.datetime.now(dt.timezone.utc)
    payload = {
        "generated": now.isoformat(timespec="seconds"),
        "snapshot_label": now.strftime("%a %-d %b %Y"),
        "fuels": fuels,
        "manual_avg": merged_averages(),
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"), ensure_ascii=False)
    size_mb = os.path.getsize(OUT) / 1e6
    print(f"Wrote {OUT}  ({size_mb:.1f} MB) - "
          f"{len(diesel)} diesel / {len(hvo)} HVO / {len(ev)} EV stations")


if __name__ == "__main__":
    main()
