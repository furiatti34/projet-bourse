"""Tableaux en verre avec bulles d'aide.

Double-clic ou clic droit (clic à deux doigts sur un pavé tactile) sur une case : une bulle explique
cette valeur, à côté de la case. Un clic ailleurs (ou Échap) la ferme. Rien ne s'affiche au simple survol.
Un clic sur un titre de colonne trie le tableau. Script et styles : bulles_tableaux.html / .css.
"""
import html
from dataclasses import dataclass
from pathlib import Path

import streamlit as st

SCRIPT = Path(__file__).with_name("bulles_tableaux.html")


def load_tables() -> None:
    """Styles et script des bulles, à charger une fois par page."""
    st.html(SCRIPT.with_suffix(".css"))
    st.html(SCRIPT, unsafe_allow_javascript=True)


@dataclass
class Col:
    title: str
    help: str                 # ce que veut dire la colonne (montré dans chaque bulle de la colonne)
    align: str = "left"       # left, right ou center
    width: str = ""           # largeur CSS facultative


@dataclass
class Cell:
    text: str                 # contenu affiché, en HTML sûr (échapper les textes venus de l'extérieur)
    help: str = ""            # explication de CETTE valeur ; « \n » = nouveau paragraphe
    sort: float | str | None = None   # valeur de tri (par défaut : le texte)
    tone: str = ""            # up (gain), down (perte), muted (secondaire)
    wrap: bool = False        # texte long autorisé à passer à la ligne


def _attr(text: str) -> str:
    return html.escape(text, quote=True)


def glass_table(cols: list[Col], rows: list[tuple[str, list[Cell]]], max_height: int | None = None) -> None:
    """rows = [(nom de la ligne, [cases])] ; le nom de la ligne titre les bulles."""
    head = "".join(
        f'<th class="gt-{c.align}" data-help="{_attr(c.help)}"'
        f'{f" style=\"width:{c.width}\"" if c.width else ""}>{html.escape(c.title)}</th>' for c in cols)
    body = []
    for label, cells in rows:
        tds = "".join(
            f'<td class="gt-{col.align}{f" gt-{cell.tone}" if cell.tone else ""}{" gt-wrap-ok" if cell.wrap else ""}" data-help="{_attr(cell.help)}"'
            f' data-sort="{_attr(str(cell.sort if cell.sort is not None else cell.text))}">{cell.text}</td>'
            for col, cell in zip(cols, cells))
        body.append(f'<tr data-label="{_attr(label)}">{tds}</tr>')
    style = f' style="max-height:{max_height}px"' if max_height else ""
    st.html(f'<div class="gt-wrap"{style}><table class="gt"><thead><tr>{head}</tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table></div>'
            '<div class="gt-hint">Double-clic ou clic droit sur une case : explication · '
            'clic sur un titre de colonne : trier</div>')
