#!/usr/bin/env python3
"""Prévision d'aurores boréales pour Tromsø et Grindavík.

Sources :
  - NOAA SWPC 3-day forecast (Kp par tranche de 3 h UT)  -> source de référence
  - GFZ Kp ensemble forecast (médiane, quartiles, min, max)
  - Open-Meteo (couverture nuageuse horaire)
  - Lune / soleil : calcul local (bibliothèque ephem), pas d'API

Sorties : <out>/index.html, <out>/data.json, et --alert-file (markdown) si Kp NOAA >= 5.
Tout est calculé en UTC ; seules les lignes d'information affichent des heures locales.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import ephem
from jinja2 import Environment, FileSystemLoader, select_autoescape

UTC = timezone.utc
ROOT = Path(__file__).resolve().parent

NOAA_URL = "https://services.swpc.noaa.gov/text/3-day-forecast.txt"
GFZ_URL = "https://spaceweather.gfz.de/fileadmin/Kp-Forecast/CSV/kp_product_file_FORECAST_PAGER_SWIFT_LAST.json"
OPEN_METEO_VARS = ["cloud_cover", "cloud_cover_low", "cloud_cover_mid", "cloud_cover_high",
                   "precipitation", "wind_speed_10m", "wind_gusts_10m", "temperature_2m"]
OPEN_METEO_URL = (
    "https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
    "&hourly=" + ",".join(OPEN_METEO_VARS) + "&forecast_days=5&timezone=GMT"
)
# Modèles complémentaires (nuages seulement) pour mesurer l'accord entre modèles.
# Identifiants confirmés dans la doc Open-Meteo : ecmwf_ifs, metno_seamless, dmi_seamless ; icon_seamless non confirmé.
OPEN_METEO_MODEL_URL = (
    "https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
    "&hourly=cloud_cover&forecast_days=5&timezone=GMT&models={model}"
)
SITE_MODELS = {
    "tromso": ["metno_seamless", "ecmwf_ifs", "icon_seamless"],
    "grindavik": ["dmi_seamless", "ecmwf_ifs", "icon_seamless"],
}
AGREEMENT = [(20, "bon"), (40, "moyen")]  # écart max entre modèles (points de %) -> libellé, sinon "faible"
USER_AGENT = "aurora-forecast/1.0 (+https://github.com/alexis-sk/aurora)"

SITES = {
    "tromso": {"name": "Tromsø", "lat": 69.6492, "lon": 18.9553, "tz": "Europe/Oslo",
               "note": "Aurores fréquentes dès Kp 2, belles dès Kp 3."},
    "grindavik": {"name": "Grindavík", "lat": 63.8424, "lon": -22.4338, "tz": "Atlantic/Reykjavik",
                  "note": "Kp 2 commence à suffire, Kp 4 = très bonnes chances."},
}
WEIGHTS = {"kp": 0.4, "clouds": 0.4, "moon": 0.2}
ALERT_KP = 5.0
NIGHT_SLOTS = [(0, 15), (0, 18), (0, 21), (1, 0), (1, 3), (1, 6)]  # (décalage jour, heure UTC de début)
SUN_DARK_ALT = -6.0          # soleil sous -6° = fin du crépuscule civil
NOAA_STALE_HOURS = 18
GFZ_STALE_HOURS = 48
JOURS = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]
JOURS_COURTS = ["lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim."]


# --------------------------------------------------------------------------- scoring
def kp_score(kp: float) -> float:
    """Kp -> score 0..1 (règle validée : 0-4 -> 0..0.40, 4-5 -> 0.40..0.85, 5-6 -> 0.85..1, >=6 -> 1)."""
    if kp <= 0:
        return 0.0
    if kp <= 4:
        return 0.4 * kp / 4
    if kp <= 5:
        return 0.4 + 0.45 * (kp - 4)
    if kp < 6:
        return 0.85 + 0.15 * (kp - 5)
    return 1.0


# --------------------------------------------------------------------------- fetch
def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8")


# --------------------------------------------------------------------------- parsers
MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


def parse_noaa(text: str) -> tuple[datetime, dict[datetime, float]]:
    """Retourne (date d'émission, {début de tranche UTC: Kp})."""
    m = re.search(r":Issued:\s+(\d{4}) (\w{3}) (\d{1,2}) (\d{2})(\d{2}) UTC", text)
    if not m:
        raise ValueError("NOAA : ligne :Issued: introuvable")
    issued = datetime(int(m[1]), MONTHS[m[2]], int(m[3]), int(m[4]), int(m[5]), tzinfo=UTC)

    lines = text.splitlines()
    idx = next((i for i, l in enumerate(lines) if l.startswith("NOAA Kp index breakdown")), None)
    if idx is None:
        raise ValueError("NOAA : tableau 'Kp index breakdown' introuvable")
    days = []
    for mon, day in re.findall(r"([A-Z][a-z]{2})\s+(\d{1,2})", lines[idx + 2]):
        d = date(issued.year, MONTHS[mon], int(day))
        if (d - issued.date()).days > 300:      # passage d'année (bulletin de fin décembre)
            d = d.replace(year=d.year - 1)
        elif (issued.date() - d).days > 300:
            d = d.replace(year=d.year + 1)
        days.append(d)
    if len(days) != 3:
        raise ValueError(f"NOAA : 3 dates attendues, trouvé {days}")

    kp: dict[datetime, float] = {}
    for line in lines[idx + 3: idx + 12]:
        row = re.match(r"\s*(\d{2})-(\d{2})UT\s+(.*)", line)
        if not row:
            continue
        hour = int(row[1])
        values = re.findall(r"\d+(?:\.\d+)?", re.sub(r"\(G\d\)", "", row[3]))
        for d, v in zip(days, values):
            kp[datetime(d.year, d.month, d.day, hour, tzinfo=UTC)] = float(v)
    if len(kp) != 24:
        raise ValueError(f"NOAA : 24 valeurs attendues, {len(kp)} lues")
    return issued, kp


def parse_gfz(raw: str) -> dict[datetime, dict[str, float]]:
    data = json.loads(raw)
    times = data["Time (UTC)"]
    cols = {"median": "median", "q25": "0.25-quantile", "q75": "0.75-quantile",
            "min": "minimum", "max": "maximum"}
    out = {}
    for k, t in times.items():
        dt = datetime.strptime(t, "%d-%m-%Y %H:%M").replace(tzinfo=UTC)
        out[dt] = {name: float(data[src][k]) for name, src in cols.items()}
    return out


def parse_open_meteo(raw: str) -> dict[str, dict[datetime, float]]:
    """Retourne {variable: {heure UTC: valeur}} pour les variables présentes."""
    data = json.loads(raw)
    if data.get("error"):
        raise ValueError(f"Open-Meteo : {data.get('reason')}")
    h = data["hourly"]
    times = [datetime.fromisoformat(x).replace(tzinfo=UTC) for x in h["time"]]
    return {var: {t: v for t, v in zip(times, vals) if v is not None}
            for var, vals in h.items() if var != "time" and isinstance(vals, list)}


def parse_model_clouds(raw: str) -> dict[datetime, float]:
    """Couverture nuageuse d'un modèle unique (clé `cloud_cover`, éventuellement suffixée du nom du modèle)."""
    series = parse_open_meteo(raw)
    key = next((k for k in series if k.startswith("cloud_cover")), None)
    if key is None or not series[key]:
        raise ValueError("pas de cloud_cover dans la réponse")
    return series[key]


def agreement_label(spread: float | None) -> str | None:
    if spread is None:
        return None
    return next((lbl for lim, lbl in AGREEMENT if spread <= lim), "faible")


# --------------------------------------------------------------------------- astro
def _observer(site: dict, when: datetime) -> ephem.Observer:
    o = ephem.Observer()
    o.lat, o.lon = str(site["lat"]), str(site["lon"])
    o.elevation = 0
    o.pressure = 0  # pas de réfraction : suffisant pour un critère "levée / couchée"
    o.date = ephem.Date(when.replace(tzinfo=None))
    return o


def body_alt(site: dict, when: datetime, body) -> float:
    b = body()
    b.compute(_observer(site, when))
    return float(b.alt) * 180 / 3.141592653589793


def moon_illum(when: datetime) -> float:
    m = ephem.Moon()
    m.compute(ephem.Date(when.replace(tzinfo=None)))
    return float(m.moon_phase) * 100


def moon_events(site: dict, start: datetime) -> dict:
    """Prochains lever / coucher de lune après `start`, en heure locale du site."""
    tz = ZoneInfo(site["tz"])
    o = _observer(site, start)
    res = {}
    for key, fn in (("rise", o.next_rising), ("set", o.next_setting)):
        try:
            dt = ephem.Date(fn(ephem.Moon())).datetime().replace(tzinfo=UTC).astimezone(tz)
            res[key] = f"{JOURS_COURTS[dt.weekday()]} {dt:%H:%M}"
        except ephem.AlwaysUpError:
            res[key] = "aucun (lune toujours levée)"
        except ephem.NeverUpError:
            res[key] = "aucun (lune sous l'horizon)"
    return res


MODEL_LABELS = {"metno_seamless": "MET Nordic", "dmi_seamless": "DMI", "ecmwf_ifs": "ECMWF", "icon_seamless": "ICON"}


def moon_night(site: dict, d: date) -> str:
    """Lever puis coucher de la lune pour la nuit commençant le jour d (heures UTC)."""
    start = datetime(d.year, d.month, d.day, 12, tzinfo=UTC)
    fmt = lambda x: f"{JOURS_COURTS[x.weekday()]} {x:%H:%M}"  # noqa: E731
    try:
        rise = ephem.Date(_observer(site, start).next_rising(ephem.Moon())).datetime().replace(tzinfo=UTC)
    except ephem.AlwaysUpError:
        return "levée toute la nuit"
    except ephem.NeverUpError:
        return "sous l'horizon toute la nuit"
    try:
        setting = ephem.Date(_observer(site, rise).next_setting(ephem.Moon())).datetime().replace(tzinfo=UTC)
        return f"lever {fmt(rise)} → coucher {fmt(setting)} UTC"
    except (ephem.AlwaysUpError, ephem.NeverUpError):
        return f"lever {fmt(rise)} UTC, puis levée toute la nuit"


# --------------------------------------------------------------------------- model
@dataclass
class SiteSlot:
    clouds_min: float | None = None
    clouds_max: float | None = None
    clouds_mean: float | None = None
    clouds_low: float | None = None
    clouds_mid: float | None = None
    clouds_high: float | None = None
    precip_mm: float | None = None
    wind_kmh: float | None = None
    gust_kmh: float | None = None
    temp_c: float | None = None
    models: dict = field(default_factory=dict)   # {modèle: nuages moyens %}
    model_spread: float | None = None
    agreement: str | None = None
    moon_alt: float = 0.0
    moon_up_frac: float = 0.0
    dark: bool = False
    score: float | None = None


@dataclass
class Slot:
    start: datetime
    kp: float | None
    gfz: dict | None
    illum: float
    sites: dict[str, SiteSlot] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return f"{self.start:%H}h-{(self.start + timedelta(hours=3)):%H}h"


def _vals(series: dict | None, start: datetime) -> list[float]:
    return [v for v in ((series or {}).get(start + timedelta(hours=h)) for h in range(3)) if v is not None]


def _mean(vals: list[float]) -> float | None:
    return round(sum(vals) / len(vals), 1) if vals else None


def build_slot(start: datetime, noaa: dict, gfz: dict | None, clouds: dict, wx: dict | None = None,
               models: dict | None = None) -> Slot:
    samples = [start + timedelta(minutes=m) for m in (30, 90, 150)]
    mid = samples[1]
    slot = Slot(start=start, kp=noaa.get(start), gfz=(gfz or {}).get(start), illum=moon_illum(mid))
    for key, site in SITES.items():
        s = SiteSlot()
        hours = [clouds.get(key, {}).get(start + timedelta(hours=h)) for h in range(3)]
        hours = [h for h in hours if h is not None]
        if hours:
            s.clouds_min, s.clouds_max = min(hours), max(hours)
            s.clouds_mean = sum(hours) / len(hours)
        site_wx = (wx or {}).get(key, {})
        s.clouds_low = _mean(_vals(site_wx.get("cloud_cover_low"), start))
        s.clouds_mid = _mean(_vals(site_wx.get("cloud_cover_mid"), start))
        s.clouds_high = _mean(_vals(site_wx.get("cloud_cover_high"), start))
        pr = _vals(site_wx.get("precipitation"), start)
        s.precip_mm = round(sum(pr), 1) if pr else None
        s.wind_kmh = _mean(_vals(site_wx.get("wind_speed_10m"), start))
        gu = _vals(site_wx.get("wind_gusts_10m"), start)
        s.gust_kmh = max(gu) if gu else None
        s.temp_c = _mean(_vals(site_wx.get("temperature_2m"), start))
        for model, series in (models or {}).get(key, {}).items():
            m = _mean(_vals(series, start))
            if m is not None:
                s.models[model] = m
        if len(s.models) >= 2:
            s.model_spread = round(max(s.models.values()) - min(s.models.values()), 1)
            s.agreement = agreement_label(s.model_spread)
        alts = [body_alt(site, t, ephem.Moon) for t in samples]
        s.moon_alt = alts[1]
        s.moon_up_frac = sum(a > 0 for a in alts) / len(alts)
        s.dark = body_alt(site, mid, ephem.Sun) < SUN_DARK_ALT
        if s.dark and slot.kp is not None and s.clouds_mean is not None:
            moon_score = 1 - (slot.illum / 100) * s.moon_up_frac
            s.score = (WEIGHTS["kp"] * kp_score(slot.kp)
                       + WEIGHTS["clouds"] * (1 - s.clouds_mean / 100)
                       + WEIGHTS["moon"] * moon_score)
        slot.sites[key] = s
    return slot


def build_night(d: date, noaa: dict, gfz: dict | None, clouds: dict, wx: dict | None = None,
                models: dict | None = None) -> dict:
    slots = [build_slot(datetime(d.year, d.month, d.day, tzinfo=UTC) + timedelta(days=off, hours=h),
                        noaa, gfz, clouds, wx, models) for off, h in NIGHT_SLOTS]
    with_kp = [s for s in slots if s.kp is not None]
    best = max(with_kp, key=lambda s: s.kp) if with_kp else None
    site_scores = {}
    for key in SITES:
        scored = [s.sites[key].score for s in slots if s.sites[key].score is not None]
        site_scores[key] = max(scored) if scored else None
    return {
        "date": d,
        "title": f"{d.isoformat()} - {JOURS[d.weekday()]}",
        "slots": slots,
        "kp_max": best.kp if best else None,
        "best_window": (best.start, best.start + timedelta(hours=3)) if best else None,
        "site_scores": site_scores,
        "has_noaa": bool(with_kp),
        "moon": {k: moon_night(s, d) for k, s in SITES.items()},
        "moon_illum": slots[2].illum,
        "noaa_slots": len(with_kp),
        "total_slots": len(slots),
    }


def next_saturday(today: date) -> date:
    return today + timedelta(days=(5 - today.weekday()) % 7)


def utc_offset_label(tz: str, now: datetime) -> str:
    off = now.astimezone(ZoneInfo(tz)).utcoffset()
    hours = off.total_seconds() / 3600
    return f"UTC{hours:+g}"


# --------------------------------------------------------------------------- alert
def alert_markdown(alerts: list[Slot], noaa_issued: datetime | None, page_url: str) -> str:
    """Corps de l'alerte (issue GitHub). La création / le commentaire de l'issue est fait par le workflow."""
    lines = [f"- {JOURS_COURTS[s.start.weekday()]} {s.start:%d/%m} {s.start:%H}h-{(s.start + timedelta(hours=3)):%H}h UTC : **Kp {s.kp:g}**"
             for s in alerts]
    issued = f"{noaa_issued:%d/%m/%Y %H:%M} UTC" if noaa_issued else "?"
    return (f"Kp NOAA ≥ {ALERT_KP:g} prévu (bulletin du {issued}) :\n\n" + "\n".join(lines)
            + (f"\n\nPage : {page_url}" if page_url else "") + "\n")


# --------------------------------------------------------------------------- réservation
CAR_PICKUP, CAR_RETURN = "15:00", "13:00"  # prise en charge samedi 15h, retour dimanche 13h (heure locale)
TRAVEL_SITES = {  # ordre d'affichage : Tromsø puis Keflavík
    "tromso": {"name": "Tromsø", "iata": "tos", "hertz": "TOST50", "booking_ss": "Aéroport de Tromsø Langnes",
               "booking_dest_id": "313", "carrental_is": None},
    "keflavik": {"name": "Keflavík (Reykjavík)", "iata": "kef", "hertz": "KEFT50",
                 "booking_ss": "Aéroport de Reykjavík - Keflavík, Reykjavik, Grand Reykjavik, Islande",
                 "booking_dest_id": "338", "carrental_is": "734"},
}
SIXT_VISA_URL = "https://www.sixt.fr/partners/visainfiniteweb-fr/"
BLUE_CAR_URL = "https://www.bluecarrental.is"
MRS_PARKING_URL = "https://www.marseille.aeroport.fr/parkings-et-acces/parkings/tarifs-plans"
# Windy : carte centrée sur chaque site, à 21h UTC le samedi suivant (format ?AAAA-MM-JJ-HH,lat,lon,zoom)
WINDY_SITES = [("Windy Tromsø", 69.642, 18.839), ("Windy Keflavík", 63.926, -22.491)]


def windy_links(sat: date, hour: int = 21) -> list[dict]:
    return [{"label": label, "url": f"https://www.windy.com/?{sat:%Y-%m-%d}-{hour:02d},{lat},{lon},11"}
            for label, lat, lon in WINDY_SITES]


# Carte Google My Maps des spots (lien en lecture seule « viewer », pas le lien d'édition)
SPOTS_MAP_URL = "https://www.google.com/maps/d/viewer?mid=1RMZHua_485EpJi3OGaLJ4qXm_fTGVc4"


def skyscanner_url(dep: str, arr: str, out_d: date, ret_d: date) -> str:
    return (f"https://www.skyscanner.fr/transport/vols/{dep}/{arr}/{out_d:%y%m%d}/{ret_d:%y%m%d}/"
            "?adultsv2=1&cabinclass=economy&childrenv2=&ref=home&rtn=1&preferdirects=false"
            "&outboundaltsenabled=false&inboundaltsenabled=false")


def booking_links(sat: date) -> dict:
    """Liens de réservation pour le week-end (aller samedi, retour dimanche)."""
    from urllib.parse import quote, urlencode
    sun = sat + timedelta(days=1)
    hp, mp = CAR_PICKUP.split(":")
    hr, mr = CAR_RETURN.split(":")
    out = {}
    for key, s in TRAVEL_SITES.items():
        code = s["iata"]
        flights = [
            {"label": f"Marseille → {s['name']}", "url": skyscanner_url("mrs", code, sat, sun)},
            {"label": "Marseille → Paris CDG", "url": skyscanner_url("mrs", "cdg", sat, sun)},
            {"label": f"Paris CDG → {s['name']}", "url": skyscanner_url("cdg", code, sat, sun)},
        ]
        hotel = "https://www.booking.com/searchresults.fr.html?" + urlencode({
            "ss": s["booking_ss"], "lang": "fr", "sb": 1, "src_elem": "sb", "src": "index",
            "dest_id": s["booking_dest_id"], "dest_type": "airport", "checkin": sat.isoformat(),
            "checkout": sun.isoformat(), "group_adults": 1, "no_rooms": 1, "group_children": 0})
        avis = ("https://secure.avis.fr/r%C3%A9sultats-recherche-v%C3%A9hicules?" + urlencode({
            "hire-location": code.upper(), "return-location": code.upper(),
            "date-from": f"{sat:%d/%m/%Y}", "time-from": f"{hp}{mp}",
            "date-to": f"{sun:%d/%m/%Y}", "time-to": f"{hr}{mr}"}))
        hertz = ("https://www.hertz.fr/fr/fr/book/vehicles?" + urlencode({
            "pid": s["hertz"], "did": s["hertz"], "pdate": f"{sat:%Y-%m-%d}T{CAR_PICKUP}:00",
            "ddate": f"{sun:%Y-%m-%d}T{CAR_RETURN}:00", "age": 25, "pCountryCode": "FR"}))
        cars = [{"label": "Hertz", "url": hertz}, {"label": "Avis", "url": avis},
                {"label": "Sixt", "url": SIXT_VISA_URL, "note": "recherche à saisir sur le site - visa"}]
        if s["carrental_is"]:
            cars.append({"label": "carrental.is",
                         "url": "https://carrental.is/results?" + urlencode({
                             "origin": s["carrental_is"], "destination": s["carrental_is"],
                             "pickupDate": sat.isoformat(), "pickupTime": f"T{CAR_PICKUP}:00.000Z",
                             "returnDate": sun.isoformat(), "returnTime": f"T{CAR_RETURN}:00.000Z"})})
            cars.append({"label": "Blue Car Rental", "url": BLUE_CAR_URL, "note": "recherche à saisir sur le site"})
        out[key] = {"name": s["name"], "flights": flights, "hotel": hotel, "cars": cars}
    return out


# --------------------------------------------------------------------------- main
def load_sources(fixtures: Path | None):
    """Retourne (noaa_text, gfz_raw, {site: open_meteo_raw}, {site: {modèle: raw}}, erreurs, erreurs_modèles)."""
    errors, model_errors = [], []

    def get(name, url, sink=errors, optional=False):
        try:
            if fixtures:
                f = fixtures / name
                if optional and not f.exists():
                    return None
                return f.read_text(encoding="utf-8")
            return fetch(url)
        except Exception as e:  # noqa: BLE001 - on veut continuer avec les autres sources
            sink.append(f"{name} : {str(e) or type(e).__name__}")  # certaines erreurs (timeout) ont un message vide
            return None

    noaa = get("noaa.txt", NOAA_URL)
    gfz = get("gfz.json", GFZ_URL)
    meteo = {k: get(f"open_meteo_{k}.json", OPEN_METEO_URL.format(**s)) for k, s in SITES.items()}
    models = {k: {m: get(f"open_meteo_{k}_{m}.json", OPEN_METEO_MODEL_URL.format(model=m, **s), model_errors, True)
                  for m in SITE_MODELS[k]} for k, s in SITES.items()}
    return noaa, gfz, meteo, models, errors, model_errors


def run(out: Path, now: datetime, fixtures: Path | None, page_url: str, alert_file: Path | None = None) -> int:
    noaa_raw, gfz_raw, meteo_raw, models_raw, errors, model_errors = load_sources(fixtures)
    warnings: list[str] = []

    noaa_issued, noaa = None, {}
    if noaa_raw:
        try:
            noaa_issued, noaa = parse_noaa(noaa_raw)
            if now - noaa_issued > timedelta(hours=NOAA_STALE_HOURS):
                warnings.append(f"Bulletin NOAA ancien (émis le {noaa_issued:%d/%m %H:%M} UTC).")
        except ValueError as e:
            errors.append(str(e))

    gfz, gfz_first = None, None
    if gfz_raw:
        try:
            parsed = parse_gfz(gfz_raw)
            gfz_first = min(parsed)
            if now - gfz_first > timedelta(hours=GFZ_STALE_HOURS):
                warnings.append(f"Fichier GFZ périmé (commence le {gfz_first:%d/%m/%Y}) : colonnes GFZ masquées.")
            else:
                gfz = parsed
        except (ValueError, KeyError) as e:
            errors.append(f"GFZ : {e}")

    clouds, wx = {}, {}
    for key, raw in meteo_raw.items():
        if raw:
            try:
                wx[key] = parse_open_meteo(raw)
                clouds[key] = wx[key]["cloud_cover"]
            except (ValueError, KeyError) as e:
                errors.append(f"Open-Meteo {SITES[key]['name']} : {e}")

    models: dict = {k: {} for k in SITES}
    for key, per_model in models_raw.items():
        for model, raw in per_model.items():
            if raw:
                try:
                    models[key][model] = parse_model_clouds(raw)
                except (ValueError, KeyError) as e:
                    model_errors.append(f"{SITES[key]['name']} / {model} : {e}")

    noaa_days = sorted({t.date() for t in noaa})
    nights = [build_night(d, noaa, gfz, clouds, wx, models) for d in noaa_days]

    sat_date = next_saturday(now.date())
    sat = build_night(sat_date, noaa, gfz, clouds, wx, models)
    reco = None
    if sat["has_noaa"]:
        scored = {k: v for k, v in sat["site_scores"].items() if v is not None}
        if scored:
            best = max(scored, key=scored.get)
            reco = {"key": best, "name": SITES[best]["name"], "score": scored[best]}

    alerts = [Slot(t, k, None, 0) for t, k in sorted(noaa.items()) if k >= ALERT_KP]
    if alert_file:
        alert_file.unlink(missing_ok=True)
        if alerts:
            alert_file.write_text(alert_markdown(alerts, noaa_issued, page_url), encoding="utf-8")

    paris = ZoneInfo("Europe/Paris")
    ctx = {
        "now_paris": now.astimezone(paris).strftime("%d/%m/%Y %H:%M:%S"),
        "now_utc": now.strftime("%d/%m/%Y %H:%M"),
        "weights": WEIGHTS,
        "sites": SITES,
        "sat": sat, "reco": reco,
        "nights": nights,
        "noaa_issued": noaa_issued,
        "gfz_ok": gfz is not None,
        "windy": windy_links(sat_date),
        "offsets": {"Paris": utc_offset_label("Europe/Paris", now),
                    **{s["name"]: utc_offset_label(s["tz"], now) for s in SITES.values()}},
        "alerts": alerts, "alert_kp": ALERT_KP,
        "errors": errors, "warnings": warnings,
        "model_errors": model_errors,
        "models_used": {k: sorted(v) for k, v in models.items()},
    }
    env = Environment(loader=FileSystemLoader(ROOT / "templates"), autoescape=select_autoescape(["html"]))
    env.filters["kp"] = lambda v: "—" if v is None else f"{v:g}"
    env.filters["num"] = lambda v: "—" if v is None else f"{v:.2f}".replace(".", ",")
    env.filters["model"] = lambda m: MODEL_LABELS.get(m, m)
    env.filters["jour"] = lambda d: JOURS_COURTS[d.weekday()]
    env.filters["pct"] = lambda v: "—" if v is None else f"{v * 100:.0f} %"

    out.mkdir(parents=True, exist_ok=True)
    for asset in (ROOT / "static").glob("*"):  # favicons et fichiers statiques
        shutil.copy2(asset, out / asset.name)
    (out / "index.html").write_text(env.get_template("index.html.j2").render(**ctx), encoding="utf-8")
    (out / "reservation.html").write_text(env.get_template("reservation.html.j2").render(
        sat=sat_date, sun=sat_date + timedelta(days=1), now_paris=ctx["now_paris"],
        sites=booking_links(sat_date), parking=MRS_PARKING_URL, spots_map=SPOTS_MAP_URL, car_pickup=CAR_PICKUP, car_return=CAR_RETURN),
        encoding="utf-8")
    (out / "data.json").write_text(json.dumps(to_json(ctx, now), indent=1, ensure_ascii=False), encoding="utf-8")

    reco_txt = f"{reco['name']} ({reco['score']:.0%})" if reco else "pas de données"
    summary = [f"NOAA émis : {noaa_issued}", f"Samedi {sat_date} : {reco_txt}",
               f"Alertes Kp>={ALERT_KP:g} : {len(alerts)} ", *[f"⚠ {w}" for w in warnings],
               *[f"✖ {e}" for e in errors]]
    print("\n".join(summary))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write("\n".join(f"- {l}" for l in summary) + "\n")
    return 1 if not noaa else 0


def to_json(ctx: dict, now: datetime) -> dict:
    def night(n):
        return {"date": n["date"].isoformat(), "kp_max": n["kp_max"], "site_scores": n["site_scores"],
                "noaa_slots": n["noaa_slots"],
                "slots": [{"start": s.start.isoformat(), "kp": s.kp, "gfz": s.gfz, "moon_illum": round(s.illum, 1),
                           "sites": {k: vars(v) for k, v in s.sites.items()}} for s in n["slots"]]}
    return {"generated_utc": now.isoformat(), "noaa_issued": ctx["noaa_issued"].isoformat() if ctx["noaa_issued"] else None,
            "saturday": night(ctx["sat"]), "recommendation": ctx["reco"] and {k: ctx["reco"][k] for k in ("key", "score")},
            "nights": [night(n) for n in ctx["nights"]], "alerts": [{"start": s.start.isoformat(), "kp": s.kp} for s in ctx["alerts"]],
            "warnings": ctx["warnings"], "errors": ctx["errors"],
            "models_used": ctx["models_used"], "model_errors": ctx["model_errors"],
            "agreement_thresholds": {"bon": "écart ≤ 20 pts", "moyen": "écart ≤ 40 pts", "faible": "écart > 40 pts"}}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", default="site", type=Path)
    p.add_argument("--fixtures", type=Path, help="lire les sources depuis un dossier local (tests)")
    p.add_argument("--now", help="date/heure UTC simulée, ex. 2026-10-16T17:15")
    p.add_argument("--page-url", default=os.environ.get("PAGE_URL", ""))
    p.add_argument("--alert-file", type=Path, help="écrit le corps de l'alerte (markdown) si Kp >= seuil")
    a = p.parse_args()
    now = datetime.fromisoformat(a.now).replace(tzinfo=UTC) if a.now else datetime.now(UTC)
    return run(a.out, now, a.fixtures, a.page_url, a.alert_file)


if __name__ == "__main__":
    sys.exit(main())
