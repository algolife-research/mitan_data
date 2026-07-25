#!/usr/bin/env python3
"""Agrège les stats communales (stats_v2/*_stats.json) par département.

Produit :
- stats_v2/departements.json : synthèse nationale et par département
- stats_v2/dept/{code}_communes.json : liste légère des communes du département,
  triée par taux de coupe annuel décroissant (pour l'interface du site)

Usage : python3 scripts/aggregate_departements.py
À relancer après chaque mise à jour des stats communales.
"""

import glob
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATS_DIR = os.path.join(ROOT, "stats_v2")
DEPT_DIR = os.path.join(STATS_DIR, "dept")
NAMES_PATH = os.path.join(ROOT, "scripts", "dept_names.json")

with open(NAMES_PATH) as f:
    DEPT_NAMES = json.load(f)


def main():
    depts = {}
    communes_by_dept = {}
    national = {"surface_ha": 0.0, "foret_ha": 0.0, "perturb_ha_annuel": 0.0, "n_communes": 0}

    files = sorted(
        p for p in glob.glob(os.path.join(STATS_DIR, "*_stats.json"))
        if os.path.basename(p) != "all_communes_stats.json"
    )
    skipped = 0
    for path in files:
        try:
            with open(path) as f:
                s = json.load(f)
        except (json.JSONDecodeError, OSError):
            skipped += 1
            continue

        code = s.get("code") or ""
        dept = s.get("departement")
        foret = s.get("foret") or {}
        pert = s.get("perturbations") or {}
        # Les fichiers sans champ departement (anciennes communes fusionnées)
        # portent des stats incohérentes (perturbations > surface communale) :
        # on les écarte de l'agrégation.
        if not dept:
            skipped += 1
            continue

        surface = float(s.get("surface_ha") or 0)
        foret_ha = float(foret.get("surface_ha") or 0)
        perturb_an = float(pert.get("perturb_ha_annuel") or 0)

        d = depts.setdefault(dept, {"surface_ha": 0.0, "foret_ha": 0.0, "perturb_ha_annuel": 0.0, "n_communes": 0})
        d["surface_ha"] += surface
        d["foret_ha"] += foret_ha
        d["perturb_ha_annuel"] += perturb_an
        d["n_communes"] += 1

        national["surface_ha"] += surface
        national["foret_ha"] += foret_ha
        national["perturb_ha_annuel"] += perturb_an
        national["n_communes"] += 1

        if s.get("nom"):
            communes_by_dept.setdefault(dept, []).append({
                "code": code,
                "nom": s.get("nom"),
                "taux_coupe_annuel": pert.get("taux_coupe_annuel"),
                "taux_boisement": foret.get("taux_boisement"),
                "foret_ha": round(foret_ha, 1),
                "score": foret.get("score"),
            })

    def finalize(d):
        foret_ha = d["foret_ha"]
        return {
            "surface_ha": round(d["surface_ha"], 1),
            "foret_ha": round(foret_ha, 1),
            "perturb_ha_annuel": round(d["perturb_ha_annuel"], 1),
            "taux_coupe_annuel": round(100.0 * d["perturb_ha_annuel"] / foret_ha, 3) if foret_ha > 0 else None,
            "taux_boisement": round(100.0 * foret_ha / d["surface_ha"], 1) if d["surface_ha"] > 0 else None,
            "n_communes": d["n_communes"],
        }

    out = {
        "source": "stats_v2 (détections S. Mermoz et al., Copernicus Sentinel-2, IGN)",
        "periode": "2018-2025",
        "definition_taux": "perturb_ha_annuel cumulé / surface forestière cumulée, en % par an",
        "national": finalize(national),
        "departements": {},
    }
    for code in sorted(depts):
        entry = finalize(depts[code])
        entry["nom"] = DEPT_NAMES.get(code, code)
        out["departements"][code] = entry

    os.makedirs(DEPT_DIR, exist_ok=True)
    with open(os.path.join(STATS_DIR, "departements.json"), "w") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))

    for code, rows in communes_by_dept.items():
        rows.sort(key=lambda r: (r["taux_coupe_annuel"] is None, -(r["taux_coupe_annuel"] or 0)))
        with open(os.path.join(DEPT_DIR, f"{code}_communes.json"), "w") as f:
            json.dump(rows, f, ensure_ascii=False, separators=(",", ":"))

    nat = out["national"]
    print(f"{len(files)} fichiers lus, {skipped} ignorés, {len(depts)} départements")
    print(f"National : {nat['foret_ha']:.0f} ha de forêt, taux de coupe {nat['taux_coupe_annuel']}%/an")
    tops = sorted(out["departements"].items(), key=lambda kv: -(kv[1]["taux_coupe_annuel"] or 0))[:5]
    for code, d in tops:
        print(f"  top {code} {d['nom']} : {d['taux_coupe_annuel']}%/an ({d['foret_ha']:.0f} ha de forêt)")


if __name__ == "__main__":
    main()
