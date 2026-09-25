"""Panneau des alertes : un bouton « globe » en haut à droite ouvre un volet en verre par la droite
(un tiers de l'écran). Les gros titres y sont empilés, le plus récent en haut ; un clic sur un
bloc ouvre l'article. Le script et les styles sont dans panneau_alertes.html / .css.
"""
import html
import json
from pathlib import Path

import pandas as pd
import streamlit as st

PANEL = Path(__file__).with_name("panneau_alertes.html")


def load_panel() -> None:
    """Styles et script du volet, à charger une fois par page."""
    st.html(PANEL.with_suffix(".css"))
    st.html(PANEL, unsafe_allow_javascript=True)


def bell(container, conn, limit: int = 60) -> None:
    """Affiche le bouton « globe » ; les alertes voyagent dans son attribut data-alerts.
    L'icône est dessinée par le script (le filtre de Streamlit retire les <svg> de st.html)."""
    rows = pd.read_sql("SELECT id, created, published, level, score, title_fr, title, sources, reasons, url"
                       " FROM alerts WHERE is_test = 0 ORDER BY id DESC LIMIT ?", conn, params=(limit,))
    alerts = [{k: (None if pd.isna(v) else v) for k, v in r.items()} for r in rows.to_dict("records")]
    data = html.escape(json.dumps(alerts, ensure_ascii=False, default=str), quote=True)
    container.html(f'<button type="button" class="alert-bell" aria-label="Ouvrir les alertes" '
                   f'data-alerts="{data}"><span class="alert-badge" hidden></span></button>')
