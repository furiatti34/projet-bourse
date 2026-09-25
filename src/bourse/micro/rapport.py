"""Rapport lisible (français) à partir de data/micro/resultats.json.

    python -m bourse.micro.rapport
"""
import json
import sys

from . import MICRO_DIR


def pct(x: float) -> str:
    return f"{x * 100:+.1f} %"


def pb(x: float) -> str:
    return f"{x / 100:+.3f} %"          # points de base → pourcentage par opération


def rapport(res: dict | None = None) -> str:
    res = res or json.loads((MICRO_DIR / "resultats.json").read_text(encoding="utf-8"))
    L = res["lignes"]
    scen = list(dict.fromkeys(l["frais"] for l in L))
    out = [f"# Microtrading — banc d'essai du {res['date'][:10]}", "",
           f"Paires : {', '.join(res['paires'])}. Données jusqu'au "
           f"{min(res['fin_donnees'].values())[:16].replace('T', ' ')} (UTC).",
           f"Périodes : " + " ; ".join(f"{k} {a} → {b}" for k, (a, b) in res["periodes"].items()), "",
           f"{len({l['strategie'] for l in L})} stratégies × 2 façons d'entrer × {len(scen)} scénarios de frais "
           f"= {res['nombre_tests']} essais.", ""]
    out += ["## Référence : acheter et garder (sans rien faire)", ""]
    for per, v in res["acheter_garder"].items():
        out.append(f"- {per} : " + ", ".join(f"{p} {pct(x)}" for p, x in v.items()))
    out.append("")
    for fr in scen:
        lignes = [l for l in L if l["frais"] == fr]
        e = [l for l in lignes if l["periodes"]["entrainement"]["reussi"]]
        v = [l for l in e if l["periodes"]["validation"]["reussi"]]
        x = [l for l in v if l["periodes"].get("examen", {}).get("reussi")]
        out += [f"## Frais : {fr}", "",
                f"- Survivantes de l'entraînement : **{len(e)}** / {len(lignes)}",
                f"- … puis de la validation : **{len(v)}**",
                f"- … puis de l'examen : **{len(x)}**", ""]
        best = sorted(lignes, key=lambda l: -l["periodes"]["entrainement"]["moyenne_pb"])[:8]
        out += ["Les 8 meilleures à l'entraînement (gain moyen NET par opération) :", "",
                "| Stratégie | Entrée | Entraînement | Validation | Examen | Opérations (entr.) |",
                "|---|---|---|---|---|---|"]
        for l in best:
            P = l["periodes"]
            cell = lambda k: (pb(P[k]["moyenne_pb"]) + (" ✅" if P[k]["reussi"] else " ❌")) if k in P else "—"
            out.append(f"| {l['strategie']} | {l['entree']} | {cell('entrainement')} | {cell('validation')} | "
                       f"{cell('examen')} | {P['entrainement']['operations']} |")
        out.append("")
        if v:
            out += ["Détail des survivantes de la validation :", ""]
            for l in v:
                out.append(f"### {l['strategie']} ({l['entree']})")
                out.append(l["description"])
                for per, P in l["periodes"].items():
                    out.append(f"- **{per}** : {P['operations']} opérations, {P['gagnantes']:.0%} gagnantes, "
                               f"{pb(P['moyenne_pb'])} par opération")
                    for p, b in P["paires"].items():
                        out.append(f"  - {p} : {b['operations']} op. ({b['par_jour']:.1f}/jour), total "
                                   f"{pct(b['total'])}, pire baisse {pct(b['pire_baisse'])}")
                out.append("")
    m = res["modele"]
    out += ["## Ce que le modèle IA a retenu (entraînement 2021-2023)", "",
            f"Minutes suivies d'une hausse de +0,30 % en 30 min : {m['taux_base']:.1%} du temps.", ""]
    out += [f"- {n} : {'+' if w > 0 else '−'}{abs(w):.2f}" for n, w in m["principales"]]
    return "\n".join(out)


if __name__ == "__main__":
    texte = rapport()
    (MICRO_DIR / "rapport.md").write_text(texte, encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    print(texte)
