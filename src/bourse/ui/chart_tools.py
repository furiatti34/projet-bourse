"""Outils des graphiques : remplace la barre grise de Plotly par une barre en verre.

Chaque graphique est placé dans un conteneur « chartbox ». Au survol, une barre sort de derrière
le graphique (voir outils_graphiques.html) : zoom, déplacement, recadrage, photo, plein écran,
et « Réglages », qui ouvre le panneau ci-dessous (abscisse, ordonnée, affichage).
Les réglages sont mémorisés par graphique pendant la session.
"""
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

TOOLBAR = Path(__file__).with_name("outils_graphiques.html")

# La barre de Plotly reste dans la page (cachée) : nos boutons en verre la pilotent.
PLOT_CONFIG = {
    "displayModeBar": True, "displaylogo": False, "scrollZoom": False,
    "modeBarButtons": [["zoom2d", "pan2d", "zoomIn2d", "zoomOut2d", "autoScale2d", "resetScale2d", "toImage"]],
    "toImageButtonOptions": {"format": "png", "filename": "projet-bourse", "scale": 2},
}

LINE_RANGES = {"Tout": None, "Aujourd'hui": 1, "5 jours": 5, "1 mois": 30, "3 mois": 91}
LINE_STYLES = ["Ligne", "Aire", "Points"]
PRICE_STYLES = ["Chandeliers", "Barres", "Ligne", "Aire"]
WIDTHS = {"Fine": 1.2, "Normale": 2.0, "Épaisse": 3.2}

DEFAULTS = {"plage": "Tout", "periode": "1 mois", "unite": "%", "echelle": "Linéaire", "cote": "Droite",
            "zero": False, "weekends": True, "style": None, "epaisseur": "Normale", "grille": True,
            "legende": True, "reticule": True, "marqueurs": True, "prix_moyen": True, "series": None}


FULL_HEIGHT = 800   # hauteur du graphique en plein écran (px)


def _toggle_full(key: str) -> None:
    st.session_state[f"full_{key}"] = not st.session_state.get(f"full_{key}", False)


@contextmanager
def chart_box(key: str):
    """Conteneur d'un graphique : la barre d'outils en verre s'y accroche.
    Il contient aussi un bouton caché « plein écran » : la barre le déclenche pour que Python
    redessine le graphique en grand (Streamlit impose sinon la hauteur d'origine)."""
    with st.container(key=f"chartbox_{key}"):
        st.button("Plein écran", key=f"fullbtn_{key}", on_click=_toggle_full, args=(key,))
        yield


def settings_panel(key: str, *, kind: str, series: list[str] | None = None,
                   periods: list[str] | None = None, units: bool = False) -> dict:
    """Panneau « Réglages » (ouvert par la barre d'outils). kind : "courbes" ou "prix".
    Renvoie les choix de l'utilisateur."""
    def k(name):
        return f"cfg_{key}_{name}"

    for name, value in DEFAULTS.items():               # valeurs par défaut, une seule fois
        st.session_state.setdefault(k(name), value)
    if st.session_state[k("style")] is None:
        st.session_state[k("style")] = "Chandeliers" if kind == "prix" else "Ligne"
    if series is not None and st.session_state[k("series")] is None:
        st.session_state[k("series")] = list(series)

    with st.popover("Réglages", icon=":material/tune:", key=f"pop_{key}"):
        left, right = st.columns(2, gap="large")
        with left:
            st.markdown('<div class="cfg-title">Abscisse · le temps</div>', unsafe_allow_html=True)
            if kind == "prix":
                st.selectbox("Période", periods, key=k("periode"))
            else:
                st.segmented_control("Plage affichée", list(LINE_RANGES), key=k("plage"), required=True)
            st.toggle("Masquer les week-ends", key=k("weekends"), help="Le marché est fermé le week-end.")

            st.markdown('<div class="cfg-title">Ordonnée · les valeurs</div>', unsafe_allow_html=True)
            if units:
                st.segmented_control("Unité", ["%", "€"], key=k("unite"), required=True,
                                     format_func=lambda u: "Performance (%)" if u == "%" else "Valeur (€)")
            log_ok = not (units and st.session_state[k("unite")] == "%")
            st.segmented_control("Échelle", ["Linéaire", "Logarithmique"], key=k("echelle"), required=True,
                                 disabled=not log_ok,
                                 help="Logarithmique : une même hausse en % a la même hauteur partout. "
                                      "Impossible en % (valeurs négatives).")
            st.segmented_control("Axe des valeurs", ["Gauche", "Droite"], key=k("cote"), required=True)
            st.toggle("Faire partir l'axe de zéro", key=k("zero"))
        with right:
            st.markdown('<div class="cfg-title">Affichage</div>', unsafe_allow_html=True)
            st.segmented_control("Type de graphique", PRICE_STYLES if kind == "prix" else LINE_STYLES,
                                 key=k("style"), required=True)
            st.segmented_control("Épaisseur des traits", list(WIDTHS), key=k("epaisseur"), required=True)
            if series:
                st.multiselect("Courbes affichées", series, key=k("series"))
            c1, c2 = st.columns(2)
            c1.toggle("Grille", key=k("grille"))
            c2.toggle("Légende", key=k("legende"))
            c1.toggle("Réticule", key=k("reticule"), help="Lignes qui suivent la souris")
            c2.toggle("Achats / ventes", key=k("marqueurs"))
            if kind == "prix":
                c1.toggle("Prix moyen", key=k("prix_moyen"), help="Prix d'achat moyen, frais inclus")
            if st.button("Réinitialiser", icon=":material/restart_alt:", key=f"reset_{key}", width="stretch"):
                for name in DEFAULTS:
                    st.session_state.pop(k(name), None)
                st.rerun()

    prefs = {name: st.session_state[k(name)] for name in DEFAULTS}
    prefs["log"] = prefs["echelle"] == "Logarithmique" and log_ok
    return prefs


def _rgba(hex_color: str, alpha: float) -> str:
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return hex_color
    return f"rgba({int(h[0:2], 16)}, {int(h[2:4], 16)}, {int(h[4:6], 16)}, {alpha})"


def apply_settings(fig: go.Figure, prefs: dict, *, category_x: bool = False) -> go.Figure:
    """Applique les réglages communs (axes, grille, légende, traits…) à un graphique déjà construit."""
    width = WIDTHS[prefs["epaisseur"]]
    series = prefs.get("series")
    ys = []
    for trace in fig.data:
        is_marker = trace.type == "scatter" and trace.mode == "markers"
        if is_marker:
            trace.visible = prefs["marqueurs"]
            continue
        if series is not None and trace.name not in series and trace.type == "scatter":
            trace.visible = False
            continue
        if trace.type == "scatter":
            trace.line.width = width
            if trace.y is not None:
                ys.extend(v for v in trace.y if v is not None and not pd.isna(v))
            if prefs["style"] == "Points":
                trace.mode = "lines+markers"
                trace.marker = dict(size=max(3, width * 2), color=trace.line.color)
            elif prefs["style"] == "Aire" and trace.line.dash in (None, "solid"):
                trace.fill = "tozeroy"
                trace.fillcolor = _rgba(trace.line.color or "#3987e5", 0.14)
        elif trace.type in ("candlestick", "ohlc"):
            trace.increasing.line.width = trace.decreasing.line.width = max(1, width / 1.6)

    fig.update_yaxes(type="log" if prefs["log"] else "linear", side="right" if prefs["cote"] == "Droite" else "left",
                     showgrid=prefs["grille"], rangemode="tozero" if prefs["zero"] else "normal")
    # En « aire » l'aire descend jusqu'à zéro : on garde l'axe centré sur les données (sauf si demandé)
    if prefs["style"] == "Aire" and not prefs["zero"] and ys and not prefs["log"]:
        lo, hi = min(ys), max(ys)
        pad = (hi - lo) * 0.08 or abs(hi) * 0.01 or 1
        fig.update_yaxes(range=[lo - pad, hi + pad])
    fig.update_layout(showlegend=prefs["legende"] and len([t for t in fig.data if t.visible is not False]) > 1)

    # épaisseur négative : Plotly ne dessine pas de liseré blanc autour du réticule
    spikes = dict(showspikes=prefs["reticule"], spikemode="across", spikesnap="cursor", spikethickness=-1,
                  spikedash="dot", spikecolor="rgba(200, 225, 255, 0.45)")
    fig.update_xaxes(**spikes)
    fig.update_yaxes(**spikes)
    fig.update_layout(hoverdistance=50, spikedistance=-1)

    if not category_x:
        if prefs["weekends"]:
            fig.update_xaxes(rangebreaks=[dict(bounds=["sat", "mon"])])
        else:
            fig.update_xaxes(rangebreaks=[])
        days = LINE_RANGES.get(prefs["plage"])
        xs = [x for t in fig.data if t.visible is not False and t.x is not None for x in t.x]
        if days and xs:
            end = pd.Timestamp(max(xs))
            fig.update_xaxes(range=[end - timedelta(days=days), end])
    return fig


def show(fig: go.Figure, key: str) -> None:
    if st.session_state.get(f"full_{key}"):
        fig.update_layout(height=FULL_HEIGHT)
    st.plotly_chart(fig, width="stretch", key=f"plot_{key}", config=PLOT_CONFIG)


def load_toolbar() -> None:
    """Barre d'outils en verre, à charger une fois par page (styles, puis script)."""
    st.html(TOOLBAR.with_suffix(".css"))
    st.html(TOOLBAR, unsafe_allow_javascript=True)
