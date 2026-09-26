"""Onglet « Microtrading » : le banc d'essai des stratégies de scalping et le paper trading en direct.

Lecture seule : l'onglet lit data/micro/resultats.json (banc d'essai) et data/micro/direct.db
(robots en direct, qui tournent dans un programme séparé : bourse.micro.direct).
"""
import json
import sqlite3
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from bourse.config import PROJECT_ROOT

MICRO = PROJECT_ROOT / "data" / "micro"
TZ = "Europe/Paris"


def _local(ts: pd.Series) -> pd.Series:
    return pd.to_datetime(ts, unit="s", utc=True).dt.tz_convert(TZ)


def render(ui) -> None:
    st.title("Microtrading")
    st.caption("Des robots qui achètent et revendent en quelques minutes des cryptos (Bitcoin, Ethereum, Solana), "
               "sur les vrais prix de Binance, avec 10 € FICTIFS chacun. Frais, glissement et ordres non servis "
               "sont comptés comme chez un vrai courtier.")
    if getattr(ui, "spectateur", False):
        st.info("👁️ Le microtrading n'est visible que sur le PC du propriétaire.")
        return
    _direct(ui)
    _banc()


# ----------------------------------------------------------------- en direct

def _direct(ui) -> None:
    base = MICRO / "direct.db"
    st.markdown("### En direct (paper trading)")
    if not base.exists():
        st.info("Les robots en direct ne sont pas encore démarrés.")
        return

    @st.fragment(run_every=30)
    def live():
        conn = sqlite3.connect(f"file:{base}?mode=ro", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            robots = conn.execute("SELECT * FROM robots ORDER BY nom").fetchall()
            ops = pd.read_sql("SELECT * FROM operations ORDER BY id", conn)
            journal = pd.read_sql("SELECT * FROM journal ORDER BY id DESC LIMIT 40", conn)
        finally:
            conn.close()
        dernier = max((r["dernier_t"] or 0) for r in robots) if robots else 0
        retard = datetime.now(timezone.utc).timestamp() - dernier - 60
        if (MICRO / "PAUSE").exists():
            st.info("⏸️ Microtrading en pause (priorité au long terme). Les robots sont arrêtés ; tout est gardé "
                    "tel quel et reprendra là où il s'est arrêté.")
        elif retard > 300:
            st.warning(f"⏸️ Les robots ne tournent pas (dernière minute traitée il y a {retard / 60:.0f} min). "
                       "Ils redémarrent tout seuls à l'ouverture de session.")
        else:
            st.caption("🟢 Les robots tournent : une vérification par minute.")
        cols = st.columns(max(1, len(robots)))
        for col, r in zip(cols, robots):
            o = ops[ops["robot"] == r["nom"]]
            etat = json.loads(r["etat"] or "{}")
            en_cours = {"position": "📈 en position", "achat_limite": "⏳ ordre posé",
                        "achat_marche": "⏳ achat en cours"}.get(etat.get("etape"), "💤 à l'affût")
            col.metric(r["nom"], f"{r['cash']:.4f} €", f"{(r['cash'] / r['capital_depart'] - 1) * 100:+.2f} %")
            col.caption(f"{en_cours} · {len(o)} opérations"
                        + (f" · {(o['rendement'] > 0).mean():.0%} gagnantes" if len(o) else ""))
        if len(ops):
            fig = go.Figure()
            for i, r in enumerate(robots):
                o = ops[ops["robot"] == r["nom"]]
                if not len(o):
                    continue
                x = pd.concat([_local(pd.Series([o["entree_t"].iloc[0]])), _local(o["sortie_t"])])
                y = [r["capital_depart"]] + list(o["capital_apres"])
                fig.add_trace(go.Scatter(x=x, y=y, name=r["nom"], mode="lines",
                                         line=dict(color=ui.SERIES[i % len(ui.SERIES)], width=2, shape="hv"),
                                         hovertemplate="%{y:.4f} €<extra>" + r["nom"] + "</extra>"))
            ui.dark_layout(fig, 340)
            fig.update_layout(hovermode="x unified", legend=dict(orientation="h", y=1.12, x=0))
            fig.update_yaxes(ticksuffix=" €")
            st.plotly_chart(fig, use_container_width=True)
            t = ops.sort_values("id", ascending=False).head(30)
            st.dataframe(pd.DataFrame({
                "Robot": t["robot"], "Paire": t["paire"],
                "Achat": _local(t["entree_t"]).dt.strftime("%d/%m %H:%M"),
                "Vente": _local(t["sortie_t"]).dt.strftime("%d/%m %H:%M"),
                "Prix achat": t["px_in"].round(2), "Prix vente": t["px_out"].round(2), "Motif": t["motif"],
                "Résultat (frais compris)": (t["rendement"] * 100).map(lambda v: f"{v:+.3f} %"),
                "Capital": t["capital_apres"].map(lambda v: f"{v:.4f} €")}),
                hide_index=True, use_container_width=True)
        _comparaison(base)
        with st.expander("Journal des robots"):
            for _, j in journal.iterrows():
                st.text(f"{j['t'][5:16].replace('T', ' ')}  {j['robot'] or '—'} : {j['message']}")

    live()


def _comparaison(base) -> None:
    """Chaque exécution simulée, comparée aux vrais prix acheteur/vendeur relevés au même moment."""
    conn = sqlite3.connect(f"file:{base}?mode=ro", uri=True, timeout=10)
    try:
        ex = pd.read_sql("SELECT * FROM executions ORDER BY id DESC", conn)
        rel = pd.read_sql("SELECT paire, (ask - bid) / ((ask + bid) / 2) AS ecart FROM releves", conn)
    except Exception:
        return
    finally:
        conn.close()
    with st.expander("Simulation contre marché réel (prix acheteur/vendeur observés)"):
        if len(rel):
            e = rel.groupby("paire")["ecart"].agg(["mean", "median", "max", "count"]).reset_index()
            st.caption("Écart entre le meilleur prix vendeur et le meilleur prix acheteur, relevé à chaque minute :")
            st.dataframe(pd.DataFrame({"Paire": e["paire"], "Écart moyen": (e["mean"] * 100).map("{:.4f} %".format),
                                       "Médian": (e["median"] * 100).map("{:.4f} %".format),
                                       "Maximum": (e["max"] * 100).map("{:.4f} %".format),
                                       "Relevés": e["count"]}), hide_index=True, use_container_width=True)
        if not len(ex):
            st.caption("Pas encore d'exécution au marché comparable (il faut que le PC soit allumé au moment du signal).")
            return
        achat = ex["evenement"].str.startswith("achat")
        # Positif = la simulation a payé plus cher (ou reçu moins) que le marché observé : elle est prudente
        ex["prudence"] = np.where(achat, ex["prix_simule"] / ex["ask"] - 1, 1 - ex["prix_simule"] / ex["bid"])
        st.caption(f"{len(ex)} exécutions comparées. En moyenne, la simulation est "
                   f"{'plus prudente' if ex['prudence'].mean() >= 0 else 'plus OPTIMISTE'} que le marché de "
                   f"{abs(ex['prudence'].mean()) * 100:.3f} % par ordre.")
        st.dataframe(pd.DataFrame({
            "Robot": ex["robot"], "Quand": _local(ex["t"]).dt.strftime("%d/%m %H:%M"), "Ordre": ex["evenement"],
            "Prix simulé": ex["prix_simule"].round(4), "Acheteur observé": ex["bid"], "Vendeur observé": ex["ask"],
            "Simulation − marché": (ex["prudence"] * 100).map("{:+.3f} %".format)}),
            hide_index=True, use_container_width=True)


# ----------------------------------------------------------------- banc d'essai

def _banc() -> None:
    f = MICRO / "resultats.json"
    st.markdown("### Banc d'essai : toutes les stratégies sur 5 ans")
    if not f.exists():
        st.info("Le banc d'essai n'a pas encore été lancé.")
        return
    res = json.loads(f.read_text(encoding="utf-8"))
    L = res["lignes"]
    st.caption(f"{len({l['strategie'] for l in L})} stratégies (indicateurs, figures de chandeliers, figures "
               f"chartistes, effets d'horloge, modèle IA) × 2 façons d'acheter × 4 niveaux de frais = "
               f"{res['nombre_tests']} essais, sur {', '.join(res['paires'])}. Une stratégie doit gagner à "
               "l'entraînement (2021-2023), puis en validation (2024 - mi-2025) ; seules les survivantes passent "
               "l'examen final (mi-2025 - aujourd'hui).")
    scen = list(dict.fromkeys(l["frais"] for l in L))
    rows = []
    for fr in scen:
        x = [l for l in L if l["frais"] == fr]
        e = [l for l in x if l["periodes"]["entrainement"]["reussi"]]
        v = [l for l in e if l["periodes"]["validation"]["reussi"]]
        ex = [l for l in v if l["periodes"].get("examen", {}).get("reussi")]
        rows.append({"Frais": fr, "Essais": len(x), "✅ Entraînement": len(e), "✅ Validation": len(v),
                     "✅ Examen": len(ex)})
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
    fr = st.selectbox("Détail pour les frais", scen, key="micro_frais")
    x = [l for l in L if l["frais"] == fr]
    x.sort(key=lambda l: -l["periodes"]["entrainement"]["moyenne_pb"])
    fmt = lambda P, k: (f"{P[k]['moyenne_pb'] / 100:+.3f} % {'✅' if P[k]['reussi'] else '❌'}") if k in P else "—"
    st.dataframe(pd.DataFrame([{
        "Stratégie": l["strategie"], "Famille": l["famille"], "Achat": l["entree"],
        "Entraînement": fmt(l["periodes"], "entrainement"), "Validation": fmt(l["periodes"], "validation"),
        "Examen": fmt(l["periodes"], "examen"),
        "Opérations (entr.)": l["periodes"]["entrainement"]["operations"],
        "Gagnantes (entr.)": f"{l['periodes']['entrainement']['gagnantes']:.0%}"} for l in x]),
        hide_index=True, use_container_width=True, height=420)
    st.caption("Chiffres = gain moyen NET par opération (après frais et glissement). À 0,10 % de frais, un "
               "aller-retour coûte déjà environ 0,24 % : une stratégie doit gagner plus que ça, en moyenne, à chaque fois.")
    rapport = MICRO / "rapport.md"
    if rapport.exists():
        with st.expander("Rapport complet"):
            st.markdown(rapport.read_text(encoding="utf-8"))
    sources = MICRO / "sources.md"
    if sources.exists():
        with st.expander("Sources : études et méthodes utilisées"):
            st.markdown(sources.read_text(encoding="utf-8"))
