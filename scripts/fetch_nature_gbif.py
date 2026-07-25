#!/usr/bin/env python3
"""Précalcule un résumé biodiversité par commune à partir de l'API publique GBIF.

Produit stats_v2/nature/{code}.json :
    {"code":"19136","total":12345,"kingdom":{"6":800,...},"class":{"212":210,...},"mode":"polygon"}

Le site (aumitan.com) lit ce fichier en priorité ; il n'y a donc plus de dépendance
à GBIF au chargement des pages. À relancer périodiquement dans le pipeline mitan_data.

Requêtes NON ABUSIVES par construction :
- GBIF : une requête facette par commune avec limit=0 (comptages seuls, aucun enregistrement
  rapatrié) — le volume ne dépend pas de la taille de la commune ;
- contour communal via geo.api.gouv.fr (mis en cache localement) ;
- délai poli entre requêtes ; reprise possible (on saute les communes déjà calculées).

Respect de la loi et des droits :
- on ne stocke que des COMPTAGES agrégés (faits) : ni coordonnées, ni espèces sensibles,
  ni données personnelles (nom d'observateur jamais requêté) ;
- attribution GBIF assurée côté site (page Détails).

Usage :
    python3 scripts/fetch_nature_gbif.py            # toutes les communes de stats_v2
    python3 scripts/fetch_nature_gbif.py --limit 50 # test rapide
    python3 scripts/fetch_nature_gbif.py --delay 1.0
"""

import argparse
import glob
import json
import os
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATS_DIR = os.path.join(ROOT, "stats_v2")
NATURE_DIR = os.path.join(STATS_DIR, "nature")
CONTOUR_CACHE = os.path.join(ROOT, "scripts", ".contour_cache")

GEO_API = "https://geo.api.gouv.fr/communes"
GBIF_API = "https://api.gbif.org/v1/occurrence/search"

# Groupes conservés (mêmes clés que le site) ; on stocke aussi tous les comptages de facette.
KINGDOM_KEYS = {"1", "5", "6"}          # Animalia, Fungi, Plantae
CLASS_KEYS = {"212", "216", "359", "131"}  # Oiseaux, Insectes, Mammifères, Amphibiens


def http_json(url: str, timeout: int = 60):
    req = urllib.request.Request(url, headers={"User-Agent": "mitan-data/1.0 (aumitan.com)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def ring_area(ring):
    a = 0.0
    for i in range(len(ring) - 1):
        a += ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1]
    return a / 2.0


def decimate(ring, max_pts=200):
    if len(ring) <= max_pts:
        return ring
    step = -(-len(ring) // max_pts)  # ceil
    return ring[::step]


def largest_ring(geometry):
    polys = geometry["coordinates"] if geometry["type"] == "MultiPolygon" else [geometry["coordinates"]]
    best, best_size = None, -1.0
    for poly in polys:
        ring = poly[0]
        if not ring or len(ring) < 4:
            continue
        size = abs(ring_area(ring))
        if size > best_size:
            best_size, best = size, ring
    return best


def contour_wkt(ring):
    r = decimate([[p[0], p[1]] for p in ring])
    if r[0] != r[-1]:
        r.append([r[0][0], r[0][1]])
    if ring_area(r) < 0:
        r = r[::-1]
    return "POLYGON((" + ",".join(f"{p[0]} {p[1]}" for p in r) + "))"


def bbox_wkt(ring):
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    a, b, c, d = min(xs), min(ys), max(xs), max(ys)
    return f"POLYGON(({a} {b},{c} {b},{c} {d},{a} {d},{a} {b}))"


def get_contour(code):
    os.makedirs(CONTOUR_CACHE, exist_ok=True)
    cache = os.path.join(CONTOUR_CACHE, f"{code}.json")
    if os.path.exists(cache):
        with open(cache) as f:
            return json.load(f)
    url = f"{GEO_API}/{code}?fields=contour&format=geojson"
    feat = http_json(url)
    with open(cache, "w") as f:
        json.dump(feat, f)
    return feat


def query_gbif(wkt):
    params = {
        "limit": "0",
        "hasCoordinate": "true",
        "hasGeospatialIssue": "false",
        "geometry": wkt,
    }
    url = GBIF_API + "?" + urllib.parse.urlencode(params) + "&facet=kingdomKey&facet=classKey&facetLimit=60"
    g = http_json(url)

    def facet(name):
        out = {}
        for f in g.get("facets", []):
            if f.get("field", "").upper() == name:
                for c in f.get("counts", []):
                    out[c["name"]] = c["count"]
        return out

    return g.get("count", 0), facet("KINGDOM_KEY"), facet("CLASS_KEY")


def commune_codes():
    codes = []
    for p in sorted(glob.glob(os.path.join(STATS_DIR, "*_stats.json"))):
        if os.path.basename(p) == "all_communes_stats.json":
            continue
        try:
            with open(p) as f:
                s = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        code = s.get("code") or ""
        if s.get("departement") and len(code) == 5:
            codes.append(code)
    return codes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="ne traiter que N communes (test)")
    ap.add_argument("--delay", type=float, default=0.5, help="délai (s) entre communes")
    ap.add_argument("--force", action="store_true", help="recalculer même si le fichier existe")
    args = ap.parse_args()

    os.makedirs(NATURE_DIR, exist_ok=True)
    codes = commune_codes()
    if args.limit:
        codes = codes[: args.limit]

    done = ok = err = 0
    for code in codes:
        out_path = os.path.join(NATURE_DIR, f"{code}.json")
        if os.path.exists(out_path) and not args.force:
            continue
        done += 1
        try:
            feat = get_contour(code)
            ring = largest_ring(feat["geometry"])
            if not ring:
                raise ValueError("no ring")

            try:
                total, king, cls = query_gbif(contour_wkt(ring))
                mode = "polygon"
            except Exception:
                total, king, cls = query_gbif(bbox_wkt(ring))  # repli emprise rectangulaire
                mode = "bbox"

            record = {
                "code": code,
                "total": total,
                "kingdom": {k: v for k, v in king.items() if k in KINGDOM_KEYS},
                "class": {k: v for k, v in cls.items() if k in CLASS_KEYS},
                "mode": mode,
            }
            with open(out_path, "w") as f:
                json.dump(record, f, ensure_ascii=False, separators=(",", ":"))
            ok += 1
            if ok % 100 == 0:
                print(f"  {ok} communes calculées (dernière {code}: {total} obs., {mode})")
        except Exception as e:  # noqa: BLE001
            err += 1
            print(f"  ! {code}: {e}")
        time.sleep(args.delay)

    print(f"Terminé : {ok} écrites, {err} en erreur, {done} tentées (sur {len(codes)} communes).")


if __name__ == "__main__":
    main()
