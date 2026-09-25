"""Interface du Projet Bourse (paper trading, ARGENT FICTIF).
Lancement : double-clic sur lancer_interface.bat (ou `streamlit run src/bourse/ui/app.py`)."""
import html
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def _forget_modified_modules() -> None:
    """Si un de nos fichiers a changé depuis son chargement, oublie tous nos modules : les imports ci-dessous
    relisent alors la nouvelle version. Streamlit ne le fait pas toujours, d'où des « cannot import name »
    après une mise à jour. (Tous à la fois : un module inchangé garderait sinon l'ancienne version des autres.)"""
    ours = {name: m for name, m in sys.modules.items() if name.startswith("bourse") and getattr(m, "__file__", None)}
    changed = False
    for module in ours.values():
        try:
            mtime = Path(module.__file__).stat().st_mtime
        except OSError:
            continue
        changed |= module.__dict__.setdefault("__loaded_mtime__", mtime) != mtime
    if changed:
        for name in ours:
            del sys.modules[name]


_forget_modified_modules()

import pandas as pd  # noqa: E402
import plotly.graph_objects as go  # noqa: E402
import streamlit as st  # noqa: E402

from bourse.analysis import build_view  # noqa: E402
from bourse.config import db_path, load_settings  # noqa: E402
from bourse.data.prices import clear_cache, day_change_pct, fx_to_eur, history, name_of, quote  # noqa: E402
from bourse.database import connect  # noqa: E402
from bourse import en_ligne  # noqa: E402
from bourse.eco import agent as eco_agent  # noqa: E402
from bourse.eco import bibliotheque  # noqa: E402
from bourse.eco import local as eco_local  # noqa: E402
from bourse.execution.broker import BUY, SELL  # noqa: E402
from bourse.execution.cycle import ensure_portfolios, make_broker, portfolio_configs, register_names  # noqa: E402
from bourse.execution.paper_broker import FILLED, PENDING  # noqa: E402
from bourse.performance.curve import equity_curve  # noqa: E402
from bourse.strategies import STRATEGIES  # noqa: E402
from bourse.ui import passe  # noqa: E402
from bourse.ui.alerts_panel import bell, load_panel  # noqa: E402
from bourse.ui.chart_tools import apply_settings, chart_box, load_toolbar, settings_panel, show  # noqa: E402
from bourse.ui.tableaux import Cell, Col, glass_table, load_tables  # noqa: E402

# Direction artistique sombre et épurée. Palette des portefeuilles validée pour les daltoniens
# sur fond noir (une couleur fixe par portefeuille) ; l'indice de référence en gris pointillé ;
# vert/rouge réservés aux gains/pertes.
SERIES = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181"]
INDEX_COLOR = "#6e6e73"
GAIN, LOSS = "#30d158", "#ff453a"
UP, DOWN = GAIN, LOSS
INK, INK2, MUTED = "#ffffff", "#c3c2b7", "#8e8e93"
GRID = "rgba(150, 190, 255, 0.07)"
GLASS = "rgba(5, 11, 26, 0.72)"          # verre sombre : lisible au-dessus du fond animé
GLASS_LINE = "rgba(140, 185, 255, 0.10)"
BACKGROUND = Path(__file__).with_name("fond_riviere.html")
TZ = "Europe/Paris"
RISK_BADGE = {"faible": "Risque faible", "moyen": "Risque moyen", "élevé": "Risque élevé",
              "extrême": "Risque extrême"}
RISK_LEVEL = {"faible": 1, "moyen": 2, "élevé": 3, "extrême": 4}
P_RACE, P_ROBOT, P_MARKET, P_MINE, P_ECO = "Course", "Robots", "Marché", "Mon portefeuille", "Éco 1"
E_PRESENT, E_PAST = "Présent", "Passé"

CSS = f"""
<style>
#MainMenu, footer, header[data-testid="stHeader"], [data-testid="stToolbar"],
[data-testid="stDecoration"], [data-testid="stStatusWidget"] {{ display: none !important; }}
/* fond animé (canvas WebGL #river-bg, voir fond_riviere.html) : la page devient transparente */
html {{ background: #030814; }}
body {{ background: transparent !important; }}
.stApp, [data-testid="stAppViewContainer"], [data-testid="stMain"], [data-testid="stBottom"] > div
    {{ background: transparent !important; }}
body.river-fallback {{ background:
    radial-gradient(60% 45% at 20% 85%, rgba(23, 84, 190, .35), transparent 70%),
    radial-gradient(50% 40% at 80% 20%, rgba(38, 163, 219, .22), transparent 70%),
    linear-gradient(45deg, #02060f, #051333 55%, #030814); background-attachment: fixed; }}
.block-container {{ padding-top: 1.4rem; padding-bottom: 4rem; max-width: 1180px; }}
h1, h3, .tr-hero {{ text-shadow: 0 1px 18px rgba(0, 0, 0, .55); }}
html, body {{ font-feature-settings: "tnum"; -webkit-font-smoothing: antialiased; }}
h1 {{ font-size: 2.3rem !important; letter-spacing: -0.035em; padding: 0.4rem 0 0.1rem !important; }}
h3 {{ font-size: 1.1rem !important; letter-spacing: -0.01em; margin-top: 1.8rem !important; }}
h4 {{ font-size: 1rem !important; }}
[data-testid="stCaptionContainer"] p {{ color: {MUTED} !important; }}
hr {{ border-color: {GLASS_LINE} !important; margin: 0.6rem 0 1rem !important; }}

/* navigation en pilules */
div[role="radiogroup"] {{ gap: 0.4rem; flex-wrap: wrap; }}
div[role="radiogroup"] label {{ background: {GLASS}; border: 1px solid {GLASS_LINE}; border-radius: 999px;
    padding: 0.45rem 1.05rem !important; margin: 0 !important; transition: background .15s ease;
    backdrop-filter: blur(12px); }}
div[role="radiogroup"] label:hover {{ background: rgba(20, 38, 75, .8); }}
[data-testid="stRadioOption"] > div > div:not([data-testid="stMarkdownContainer"]) {{ display: none; }}
div[role="radiogroup"] label p {{ color: {INK2}; font-weight: 500; }}
div[role="radiogroup"] label[data-selected="true"] {{ background: {INK}; }}
div[role="radiogroup"] label[data-selected="true"] p {{ color: #000 !important; font-weight: 600; }}

/* chiffres */
[data-testid="stMetricLabel"] p {{ color: {MUTED} !important; font-size: 0.82rem !important; }}
[data-testid="stMetricValue"] {{ font-size: 1.85rem !important; letter-spacing: -0.025em; }}

/* cartes */
[class*="st-key-card"] {{ background: {GLASS}; border: 1px solid {GLASS_LINE}; border-radius: 20px;
    padding: 1.1rem 1.2rem 1rem; backdrop-filter: blur(18px) saturate(115%);
    box-shadow: 0 10px 40px rgba(0, 0, 0, .35), inset 0 1px 0 rgba(180, 215, 255, .06); }}
[data-testid="stPlotlyChart"] {{ background: {GLASS}; border: 1px solid {GLASS_LINE}; border-radius: 20px;
    padding: 0; overflow: hidden; backdrop-filter: blur(18px); }}
[data-testid="stDataFrame"], [data-testid="stExpander"] details, [data-testid="stForm"] {{
    border-radius: 16px; background: {GLASS}; backdrop-filter: blur(18px); }}
[data-testid="stMetric"] {{ background: {GLASS}; border: 1px solid {GLASS_LINE}; border-radius: 18px;
    padding: .8rem 1rem; backdrop-filter: blur(18px); }}
[data-testid="stCaptionContainer"] p, .tr-sub {{ text-shadow: 0 1px 10px rgba(0, 0, 0, .6); }}
.tr-name {{ font-weight: 600; font-size: 1rem; display: flex; align-items: center; gap: .5rem; }}
.tr-dot {{ width: 9px; height: 9px; border-radius: 50%; display: inline-block; }}
.tr-risk {{ color: {MUTED}; font-size: .8rem; margin-top: .15rem; }}
.tr-value {{ font-size: 1.75rem; font-weight: 600; letter-spacing: -0.03em; margin-top: .9rem; }}
.tr-perf {{ font-weight: 600; font-size: .95rem; }}
.tr-sub {{ color: {MUTED}; font-size: .82rem; margin-top: .55rem; line-height: 1.45; }}
.tr-card-sub {{ min-height: 5.6em; margin-bottom: .6rem; }}
.tr-brand {{ font-weight: 700; font-size: 1.05rem; letter-spacing: -0.02em; }}
.tr-hero {{ font-size: 3rem; font-weight: 700; letter-spacing: -0.04em; line-height: 1.05; }}

/* classement : échelle des gains en verre */
.rk {{ display: flex; flex-direction: column; gap: .5rem; }}
.rk-row {{ display: grid; align-items: center; gap: 1rem; padding: .85rem 1.2rem;
    grid-template-columns: 2.4rem minmax(0, 1.5fr) minmax(0, 1.3fr) 8.5rem 6.5rem 7rem;
    background: {GLASS}; border: 1px solid {GLASS_LINE}; border-radius: 16px; backdrop-filter: blur(18px);
    font-variant-numeric: tabular-nums; transition: transform .2s ease, border-color .2s ease; }}
.rk-row:hover {{ transform: translateX(4px); border-color: rgba(140, 185, 255, .28); }}
.rk-pos {{ width: 2.1rem; height: 2.1rem; border-radius: 50%; display: grid; place-items: center;
    font-weight: 700; color: {INK2}; border: 1px solid rgba(140, 185, 255, .22); }}
.rk-who {{ font-weight: 600; display: flex; align-items: center; flex-wrap: wrap; column-gap: .5rem;
    overflow: hidden; }}
.rk-risk {{ flex-basis: 100%; color: {MUTED}; font-size: .74rem; font-weight: 400; margin-top: .1rem; }}
.rk-val {{ text-align: right; font-weight: 600; }}
.rk-perf, .rk-gap {{ text-align: right; font-weight: 600; font-size: .9rem; }}
.rk-bar {{ position: relative; height: 6px; border-radius: 999px; background: rgba(140, 185, 255, .07); }}
.rk-mid {{ position: absolute; left: 50%; top: -4px; bottom: -4px; width: 1px; background: rgba(140, 185, 255, .3); }}
.rk-fill {{ position: absolute; top: 0; bottom: 0; border-radius: 999px; }}
.rk-fill.up {{ background: {GAIN}; box-shadow: 0 0 10px rgba(48, 209, 88, .45); }}
.rk-fill.down {{ background: {LOSS}; box-shadow: 0 0 10px rgba(255, 69, 58, .45); }}
/* le premier : pastille blanche et barre illuminée, comme la question en cours dans le jeu */
.rk-first {{ border-color: rgba(140, 185, 255, .35);
    background: linear-gradient(100deg, rgba(57, 135, 229, .28), {GLASS} 55%);
    box-shadow: 0 0 34px rgba(57, 135, 229, .22), inset 0 1px 0 rgba(180, 215, 255, .1); }}
.rk-first .rk-pos {{ background: {INK}; color: #000; border-color: {INK}; box-shadow: 0 0 16px rgba(255, 255, 255, .45); }}
/* l'indice : un palier en pointillé, pas une vraie ligne du classement */
.rk-bench {{ background: transparent; backdrop-filter: none; border: 1px dashed rgba(142, 142, 147, .45);
    padding-top: .55rem; padding-bottom: .55rem; color: {MUTED}; }}
.rk-bench .rk-pos {{ border: none; color: {MUTED}; }}
.rk-bench .rk-who, .rk-bench .rk-perf {{ color: {MUTED}; font-weight: 500; }}
.rk-bench .rk-fill {{ background: {INDEX_COLOR}; box-shadow: none; }}
@media (max-width: 760px) {{
    .rk-row {{ grid-template-columns: 2.2rem minmax(0, 1fr) auto; gap: .7rem; }}
    .rk-bar, .rk-val, .rk-gap {{ display: none; }}
}}
@media (prefers-reduced-motion: reduce) {{ .rk-row {{ transition: none; }} }}

/* boutons */
.stButton button {{ background: rgba(20, 38, 75, .55); border: 1px solid {GLASS_LINE}; color: {INK};
    font-weight: 500; backdrop-filter: blur(12px); }}
.stButton button:hover {{ background: rgba(35, 62, 115, .75); color: {INK}; border: 1px solid {GLASS_LINE}; }}
.stButton button p {{ font-size: .88rem; }}

/* Éco 1 : bulles de conversation en verre */
[data-testid="stChatMessage"] {{ background: {GLASS}; border: 1px solid {GLASS_LINE}; border-radius: 18px;
    padding: .9rem 1.1rem; backdrop-filter: blur(18px); }}
[data-testid="stChatInput"] > div {{ background: {GLASS}; border: 1px solid {GLASS_LINE}; backdrop-filter: blur(18px); }}
</style>
"""
PERIODS = {"1 jour": ("1d", "5m"), "5 jours": ("5d", "15m"), "1 mois": ("1mo", "1h"),
           "6 mois": ("6mo", "1d"), "1 an": ("1y", "1d"), "5 ans": ("5y", "1wk"), "Max": ("max", "1mo")}

st.set_page_config(page_title="Projet Bourse", page_icon="📈", layout="wide",
                   initial_sidebar_state="collapsed")
st.html(CSS)
st.html(BACKGROUND, unsafe_allow_javascript=True)   # fond animé « courant d'eau »
load_toolbar()                                      # barre d'outils en verre des graphiques
load_panel()                                        # volet des alertes (cloche en haut à droite)
load_tables()                                       # bulles d'aide des tableaux (double-clic / clic droit)
# Site en ligne : copie des données en lecture seule, aucune action (voir bourse/en_ligne.py)
SPECTATEUR = en_ligne.preparer()
settings = load_settings()
configs = portfolio_configs(settings)
register_names(settings)
conn = connect(db_path(settings))
bibliotheque.init(conn)


# ---------------- outils ----------------

def eur(x: float) -> str:
    return f"{x:,.0f} €".replace(",", " ")


def eur2(x: float) -> str:
    return f"{x:,.2f} €".replace(",", " ").replace(".", ",")


def pct(x: float) -> str:
    return f"{x:+.2f} %".replace(".", ",")


def num(decimals: int = 2, sign: bool = False, suffix: str = ""):
    """Formateur de tableau à la française : 1 234,56."""
    def fmt(v):
        if pd.isna(v):
            return "–"
        text = f"{v:{'+' if sign else ''},.{decimals}f}".replace(",", "\u202f").replace(".", ",")
        return text + suffix
    return fmt


def colored(text: str, positive: bool) -> str:
    return f":green[{text}]" if positive else f":red[{text}]"


def risk_label(risk: str) -> str:
    """« Risque élevé ●●●○ » : niveau lisible sans couleur."""
    level = RISK_LEVEL.get(risk, 0)
    return f'{RISK_BADGE.get(risk, "")} <span style="letter-spacing:1px">{"●" * level}{"○" * (4 - level)}</span>'


def signed(text: str, positive: bool) -> str:
    """Texte coloré gain/perte avec une flèche (la couleur n'est jamais le seul indice)."""
    return f'<span style="color:{GAIN if positive else LOSS}">{"▲" if positive else "▼"} {text}</span>'


def to_local(values) -> pd.Series:
    return pd.to_datetime(values, utc=True, format="ISO8601").dt.tz_convert(TZ)


def color_gains(value):
    if pd.isna(value) or value == 0:
        return ""
    return f"color: {GAIN if value > 0 else LOSS}; font-weight: 600"


@st.cache_data(ttl=900, show_spinner=False)
def market_view():
    return build_view(settings, conn)


@st.cache_data(ttl=3600, show_spinner=False)
def load_portfolios() -> list[dict]:
    return [dict(p) for p in ensure_portfolios(conn, settings)]


@st.cache_data(ttl=120, show_spinner=False)
def curve_of(portfolio_id: int) -> pd.DataFrame:
    row = conn.execute("SELECT * FROM portfolios WHERE id = ?", (portfolio_id,)).fetchone()
    df = equity_curve(conn, row)
    df.index = df.index.tz_convert(TZ)
    return df


@st.cache_data(ttl=120, show_spinner=False)
def summary_of(portfolio_id: int) -> dict:
    p = next(p for p in load_portfolios() if p["id"] == portfolio_id)
    broker = make_broker(conn, conn.execute("SELECT * FROM portfolios WHERE id = ?", (portfolio_id,)).fetchone(),
                         settings)
    positions = broker.positions()
    cash = broker.cash()
    total = cash + sum(x["valeur_eur"] for x in positions)
    bench = p["initial_cash"] * quote(p["benchmark"]).price_eur / p["benchmark_start"]
    return {"positions": positions, "cash": cash, "total": total,
            "perf": (total / p["initial_cash"] - 1) * 100,
            "bench_perf": (bench / p["initial_cash"] - 1) * 100}


def ranking_html(ranking: list[dict]) -> str:
    """Classement façon « échelle des gains » : une barre de verre par robot, le premier mis en lumière,
    et l'indice mondial glissé à sa place comme un palier en pointillé."""
    # le palier = l'indice depuis le départ de la course (comme la courbe grise) ; l'écart de chaque robot,
    # lui, se mesure depuis son propre départ (un robot ajouté plus tard part de l'indice du jour)
    bench = summary_of(min(ranking, key=lambda r: r["created"])["id"])["bench_perf"]
    widest = max([abs(summary_of(r["id"])["perf"]) for r in ranking] + [abs(bench), 0.01])

    def bar(perf: float) -> str:
        width = abs(perf) / widest * 50
        side = f"left:50%;width:{width:.1f}%" if perf >= 0 else f"right:50%;width:{width:.1f}%"
        return (f'<div class="rk-bar"><span class="rk-mid"></span>'
                f'<span class="rk-fill {"up" if perf >= 0 else "down"}" style="{side}"></span></div>')

    bench_row = (f'<div class="rk-row rk-bench"><div class="rk-pos">—</div>'
                 f'<div class="rk-who">Indice mondial<div class="rk-risk">le palier à battre</div></div>'
                 f'{bar(bench)}<div class="rk-val"></div>'
                 f'<div class="rk-perf">{pct(bench)}</div><div class="rk-gap"></div></div>')
    rows, bench_done = [], False
    for i, r in enumerate(ranking, 1):
        s = summary_of(r["id"])
        if not bench_done and bench > s["perf"]:
            rows.append(bench_row)
            bench_done = True
        diff = s["perf"] - s["bench_perf"]
        rows.append(
            f'<div class="rk-row{" rk-first" if i == 1 else ""}"><div class="rk-pos">{i}</div>'
            f'<div class="rk-who"><span class="tr-dot" style="background:{color_of[r["id"]]}"></span>'
            f'{r["name"].replace("Robot ", "")}'
            f'<div class="rk-risk">{risk_label(configs[r["name"]].get("risque", ""))}</div></div>'
            f'{bar(s["perf"])}<div class="rk-val">{eur2(s["total"])}</div>'
            f'<div class="rk-perf">{signed(pct(s["perf"]), s["perf"] >= 0)}</div>'
            f'<div class="rk-gap">{signed(pct(diff).replace(" %", " pts"), diff >= 0)}'
            f'<div class="rk-risk">vs indice</div></div></div>')
    if not bench_done:
        rows.append(bench_row)
    return '<div class="rk">' + "".join(rows) + "</div>"


def line_chart(lines: list[tuple], yaxis_title: str, height: int = 420) -> go.Figure:
    """lines = [(nom, série, couleur, style)] — un seul axe, survol unifié."""
    fig = go.Figure()
    for name, serie, color, dash in lines:
        fig.add_trace(go.Scatter(x=serie.index, y=serie.values, name=name, mode="lines",
                                 line=dict(color=color, width=2, dash=dash),
                                 hovertemplate="%{y:,.2f}<extra>" + name + "</extra>"))
    dark_layout(fig, height)
    fig.update_layout(hovermode="x unified", legend=dict(orientation="h", y=1.12, x=0, font=dict(color=INK2)))
    fig.update_xaxes(rangebreaks=[dict(bounds=["sat", "mon"])])
    fig.update_yaxes(ticksuffix=" %" if "%" in yaxis_title else " €")
    return fig


def dark_layout(fig: go.Figure, height: int) -> None:
    """Style commun : fond transparent, grille à peine visible, axe des valeurs à droite."""
    fig.update_layout(height=height, margin=dict(l=18, r=14, t=44, b=16), template="plotly_dark",
                      paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                      font=dict(family="Inter, sans-serif", color=MUTED, size=12),
                      hoverlabel=dict(bgcolor="#1c1c1e", bordercolor="#1c1c1e", font=dict(color=INK)))
    fig.update_xaxes(showgrid=False, zeroline=False, showline=False, tickformat="%d/%m<br>%H:%M",
                     hoverformat="%d/%m %H:%M")
    fig.update_yaxes(gridcolor=GRID, zeroline=False, showline=False, side="right")


def positions_table(positions: list[dict], key: str) -> str | None:
    """Tableau des placements. Cliquer sur une ligne la sélectionne : renvoie son ticker
    (par défaut le premier placement)."""
    if not positions:
        st.write("Aucun placement en ce moment (tout est en liquidités).")
        return None
    df = pd.DataFrame(positions)
    df.insert(1, "nom", [name_of(t) for t in df["ticker"]])
    df.insert(4, "jour", [day_change_pct(t) for t in df["ticker"]])
    df = df[["ticker", "nom", "quantite", "cours", "jour", "devise", "valeur_eur",
             "prix_revient_eur", "gain_eur", "gain_pct"]]
    df.columns = ["Titre", "Nom", "Quantité", "Cours actuel", "Aujourd'hui (%)", "Devise", "Valeur (€)",
                  "Prix de revient (€)", "Gain (€)", "Gain (%)"]
    styled = (df.style.map(color_gains, subset=["Aujourd'hui (%)", "Gain (€)", "Gain (%)"])
              .format({"Quantité": num(0), "Cours actuel": num(2), "Aujourd'hui (%)": num(2, sign=True, suffix=" %"),
                       "Valeur (€)": num(2), "Prix de revient (€)": num(2), "Gain (€)": num(2, sign=True),
                       "Gain (%)": num(2, sign=True, suffix=" %")}, na_rep="–"))
    event = st.dataframe(styled, hide_index=True, width="stretch", on_select="rerun",
                         selection_mode="single-row", key=key)
    rows = event.selection.rows if event and event.selection else []
    st.caption("👆 Cliquez sur une ligne (case à gauche) pour afficher la courbe de ce placement.")
    return df["Titre"].iloc[rows[0] if rows else 0]


# ---------------- tableaux de la page Marché (bulles d'aide au double-clic) ----------------

SIGNAL_HELP = {
    "Tendance mondiale": "Regarde si l'indice mondial est au-dessus de ses moyennes des 50 et 200 derniers jours. "
                         "Au-dessus des deux = le marché monte depuis des mois ; c'est le signal le plus important.",
    "Largeur du marché": "Parmi les grands marchés d'actions suivis (États-Unis, Europe, Japon…), combien montent ? "
                         "Une hausse portée par tous est plus solide qu'une hausse portée par un seul pays.",
    "Peur (VIX)": "Le VIX mesure la peur des investisseurs (il monte quand ils paient cher pour se protéger). "
                  "Sous 15 : calme. Au-dessus de 25 : inquiétude. Au-dessus de 35 : panique.",
    "Panique à court terme": "Compare la peur pour le mois qui vient à la peur pour les 3 prochains mois. "
                             "D'habitude on craint plus le lointain ; si le mois qui vient fait plus peur, "
                             "c'est une panique immédiate.",
    "Crédit": "Compare les obligations d'entreprises fragiles à celles des États. Quand les prêteurs se méfient "
              "des entreprises fragiles, c'est souvent un signe avant-coureur de difficultés.",
    "Cuivre / or": "Le cuivre sert à l'industrie, l'or sert de refuge. Cuivre qui gagne sur l'or = économie qui "
                   "accélère ; or qui gagne = les investisseurs se protègent.",
    "Taux américains": "Taux d'intérêt de l'État américain à 10 ans. Une montée rapide rend les emprunts plus chers "
                       "et pèse sur les actions.",
    "Dollar": "Quand le monde a peur, l'argent se réfugie dans le dollar. Un dollar qui flambe est donc plutôt "
              "un mauvais signe pour les marchés.",
    "Ton des actualités": "Part des articles lus par la veille (toutes langues) qui emploient un vocabulaire de "
                          "crise, comparée aux jours précédents.",
    "Alertes": "Les alertes récentes de la veille. Une alerte forte pèse beaucoup au début, puis de moins en "
               "moins pendant 48 heures.",
}
FAMILY_HELP = {
    "actions": "Actions : des parts d'entreprises. Rapporte le plus sur le long terme, mais peut chuter fort.",
    "obligations": "Obligations : des prêts à des États ou des entreprises. Bouge peu, sert à amortir les chutes.",
    "monetaire": "Monétaire : presque de l'épargne. Rapporte le taux d'intérêt du jour et ne baisse quasiment jamais.",
    "or": "Or : valeur refuge, monte souvent quand le monde a peur.",
    "matieres": "Matières premières (pétrole…) : très sensibles à l'économie et à la géopolitique.",
    "crypto": "Cryptomonnaie : extrêmement volatile, peut faire ±10 % en une journée.",
}
FAMILY_NAMES = {"monetaire": "monétaire", "matieres": "matières prem."}
EXCHANGES = {"DE": "Francfort (Xetra)", "PA": "Paris", "MI": "Milan", "L": "Londres", "AS": "Amsterdam",
             "SW": "Zurich", "MC": "Madrid", "BR": "Bruxelles"}


def fr(x: float, decimals: int = 1, sign: bool = True) -> str:
    """Nombre à la française pour les tableaux : virgule et vrai signe moins."""
    return f"{x:{'+' if sign else ''}.{decimals}f}".replace(".", ",").replace("-", "−")


def leverage_help(lev: int) -> str:
    if lev == 1:
        return "×1 : suit simplement le marché. Si l'indice fait +1 %, ce placement fait environ +1 %."
    if lev > 1:
        return (f"×{lev} : chaque jour, fait {lev} fois le mouvement de l'indice. +1 % → environ +{lev} %, "
                f"mais −1 % → environ −{lev} %. Sur plusieurs jours agités, il peut perdre même si l'indice "
                "revient à son point de départ.")
    return (f"×{-lev} baisse : gagne quand le marché baisse. −1 % sur l'indice → environ +{-lev} % ici, "
            "et l'inverse quand ça monte. Un pari sur la chute, pas un placement à garder.")


def meter(value: float) -> str:
    """Petite jauge centrée : vers la droite en vert si positif, vers la gauche en rouge sinon."""
    w = min(abs(value), 100) / 2
    pos = f"left:50%;width:{w:.0f}%" if value >= 0 else f"right:50%;width:{w:.0f}%"
    return f'<span class="gt-meter"><span style="{pos};background:{GAIN if value >= 0 else LOSS}"></span></span>'


def signals_table(factors: list) -> None:
    total = sum(f.weight for f in factors) or 1
    cols = [Col("Signal", "Chaque signal est un indice sur la santé du marché ; ensemble, ils donnent la note de climat."),
            Col("Lecture", "De −100 (très mauvais signe) à +100 (très bon signe). 0 = neutre.", "right", "13rem"),
            Col("Poids", "La part de ce signal dans la note de climat.", "right", "5rem"),
            Col("Détail", "Les chiffres réels mesurés au dernier passage des robots.")]
    rows = []
    for f in sorted(factors, key=lambda f: -f.weight):
        v = f.value * 100
        mood = ("très bon signe" if v >= 60 else "plutôt bon signe" if v >= 25 else
                "très mauvais signe" if v <= -60 else "plutôt mauvais signe" if v <= -25 else "neutre")
        explain = SIGNAL_HELP.get(f.name, "")
        share = f.weight / total
        rows.append((f.name, [
            Cell(f"{f.icon} {html.escape(f.name)}", explain, f.name, wrap=True),
            Cell(f"{meter(v)}{fr(v, 0)}", f"{fr(v, 0)} sur 100 : {mood}.\n{explain}", v,
                 "up" if v >= 25 else "down" if v <= -25 else ""),
            Cell(f"{share:.0%}", f"Ce signal compte pour {share:.0%} de la note de climat.", f.weight, "muted"),
            Cell(html.escape(f.detail), f"{f.detail[:1].upper()}{f.detail[1:]}.\n{explain}", f.detail, wrap=True),
        ]))
    glass_table(cols, rows)


def assets_table(assets: list) -> None:
    ranked = sorted(assets, key=lambda a: -a.sprint)
    rank = {a.ticker: i for i, a in enumerate(ranked, 1)}

    def move(a, value: float, period: str) -> Cell:
        verb = "gagné" if value >= 0 else "perdu"
        text = f"{a.name} a {verb} {fr(abs(value), sign=False)} % {period}."
        if abs(a.leverage) > 1:
            text += f"\nC'est un placement à levier ×{abs(a.leverage)} : le mouvement est amplifié."
        return Cell(fr(value), text, value, "up" if value > 0 else "down" if value < 0 else "")

    def rsi_help(r: float) -> str:
        if r < 30:
            return f"RSI {r:.0f} : a trop baissé trop vite. Un rebond arrive souvent (le Robot Opportuniste aime ça)."
        if r > 70:
            return f"RSI {r:.0f} : s'est emballé, a monté trop vite. Une pause ou une baisse est fréquente ensuite."
        return f"RSI {r:.0f} : zone normale, ni emballé ni en chute libre."

    cols = [Col("Placement", "Le nom du placement suivi par les robots."),
            Col("Titre", "Le code boursier du placement ; la fin indique la Bourse où il est coté."),
            Col("Famille", "Le type de placement : actions, obligations, or, matières premières, crypto…"),
            Col("Levier", "Combien de fois le placement amplifie les mouvements du marché.", "center"),
            Col("1 j (%)", "Variation depuis la veille.", "right"),
            Col("5 j (%)", "Variation sur une semaine de Bourse.", "right"),
            Col("1 mois (%)", "Variation sur un mois (21 jours de Bourse).", "right"),
            Col("3 mois (%)", "Variation sur trois mois (63 jours de Bourse).", "right"),
            Col("Tendance", "Au-dessus ou en dessous de la moyenne des 200 derniers jours : la tendance de fond."),
            Col("RSI", "Indicateur de 0 à 100. Sous 30 : a trop baissé trop vite. Au-dessus de 70 : s'est emballé.",
                "right"),
            Col("Élan", "Élan court terme : ce qui monte le plus vite en ce moment : 20 % de la variation du jour, 50 % de la "
                              "semaine, 30 % du mois. Le Robot Kamikaze achète les deux meilleurs.", "right"),
            Col("Vs plus haut (%)", "L'écart avec le plus haut des 12 derniers mois.", "right")]
    rows = []
    for a in ranked:
        suffix = a.ticker.rsplit(".", 1)[1] if "." in a.ticker else ""
        place = EXCHANGES.get(suffix, "une Bourse étrangère" if suffix else "un marché mondial")
        up = a.above_ma200
        family = FAMILY_HELP.get(a.family, a.family)
        sprint_txt = f"Élan {fr(a.sprint)} : {rank[a.ticker]}ᵉ sur {len(ranked)}."
        if rank[a.ticker] <= 2 and a.sprint > 0:
            sprint_txt += "\nUn des deux plus rapides : le Robot Kamikaze le regarde de près."
        if a.drawdown > -0.5:
            dd_txt = "À son plus haut de l'année (ou presque)."
        else:
            dd_txt = f"À {fr(abs(a.drawdown), sign=False)} % sous son plus haut des 12 derniers mois."
            if a.drawdown < -20:
                dd_txt += " C'est une chute importante."
        rows.append((a.name, [
            Cell(html.escape(a.name), f"{a.name} ({a.ticker}).\n{family}\n{leverage_help(a.leverage)}", a.name,
                 wrap=True),
            Cell(html.escape(a.ticker), f"Code {a.ticker} : coté à {place}.", a.ticker, "muted"),
            Cell(FAMILY_NAMES.get(a.family, html.escape(a.family)), family, a.family, "muted"),
            Cell(f"×{a.leverage}" if a.leverage > 0 else f"×{-a.leverage} baisse", leverage_help(a.leverage),
                 a.leverage, "down" if a.leverage < 0 else ""),
            move(a, a.ret_1d, "depuis la veille"),
            move(a, a.ret_5d, "en 5 jours"),
            move(a, a.ret_1m, "en 1 mois"),
            move(a, a.ret_3m, "en 3 mois"),
            Cell("↗ hausse" if up else "↘ baisse",
                 f"Cours {fr(a.price, 2, False)}, moyenne des 200 derniers jours {fr(a.ma200, 2, False)} : "
                 + ("au-dessus, la tendance de fond est à la hausse." if up else
                    "en dessous, la tendance de fond est à la baisse."), int(up), "up" if up else "down"),
            Cell(f"{a.rsi:.0f}", rsi_help(a.rsi), a.rsi, "down" if a.rsi > 70 else "up" if a.rsi < 30 else ""),
            Cell(fr(a.sprint), sprint_txt, a.sprint, "up" if a.sprint > 0 else "down" if a.sprint < 0 else ""),
            Cell(fr(a.drawdown), dd_txt, a.drawdown, "muted"),
        ]))
    glass_table(cols, rows, max_height=620)


def is_intraday(period: str) -> bool:
    return PERIODS[period][1] in ("5m", "15m", "30m", "1h")


def price_chart(ticker: str, period: str, trades: pd.DataFrame | None = None,
                avg_price: float | None = None, style: str = "Chandeliers") -> go.Figure | None:
    """Cours d'un titre (chandeliers, barres, ligne ou aire). Achats ▲ / ventes ▼ et prix d'achat moyen superposés."""
    bars = history(ticker, *PERIODS[period])
    if bars.empty:
        return None
    intraday = is_intraday(period)
    # En intraday, axe « catégorie » : les nuits et week-ends (marché fermé) disparaissent
    x = bars.index.tz_convert(TZ).strftime("%d/%m %H:%M") if intraday else bars.index
    ohlc = dict(x=x, open=bars["Open"], high=bars["High"], low=bars["Low"], close=bars["Close"], name=ticker,
                increasing=dict(line=dict(color=UP)), decreasing=dict(line=dict(color=DOWN)))
    if style == "Chandeliers":
        ohlc["increasing"]["fillcolor"], ohlc["decreasing"]["fillcolor"] = UP, DOWN
        fig = go.Figure(go.Candlestick(**ohlc))
    elif style == "Barres":
        fig = go.Figure(go.Ohlc(**ohlc))
    else:  # ligne / aire : verte si le titre monte sur la période, rouge sinon
        color = UP if bars["Close"].iloc[-1] >= bars["Close"].iloc[0] else DOWN
        fig = go.Figure(go.Scatter(x=x, y=bars["Close"], name=ticker, mode="lines", line=dict(color=color, width=2),
                                   hovertemplate="%{y:,.2f}<extra>" + ticker + "</extra>"))
    if trades is not None and not trades.empty:
        times = pd.to_datetime(trades["filled_at"], utc=True, format="ISO8601")
        visible = (times >= bars.index[0].tz_convert("UTC")) & (times <= bars.index[-1].tz_convert("UTC")
                                                               + pd.Timedelta(days=1))
        for side, symbol, color in [(BUY, "triangle-up", UP), (SELL, "triangle-down", DOWN)]:
            t = trades[visible & (trades["side"] == side)]
            if t.empty:
                continue
            when = pd.to_datetime(t["filled_at"], utc=True, format="ISO8601")
            if intraday:  # placer le triangle sur la barre correspondante
                pos = bars.index.tz_convert("UTC").searchsorted(when).clip(0, len(bars) - 1)
                tx = x[pos]
            else:
                tx = when.dt.tz_convert(bars.index.tz)
            fig.add_trace(go.Scatter(
                x=tx, y=t["fill_price"], mode="markers", name=side.capitalize(),
                marker=dict(symbol=symbol, size=14, color=color, line=dict(color="#000000", width=2)),
                customdata=t[["filled_qty", "reason"]].values,
                hovertemplate="%{customdata[0]:.0f} titres à %{y:.2f}<br>%{customdata[1]}<extra>" + side + "</extra>"))
    if avg_price:
        fig.add_hline(y=avg_price, line=dict(color=MUTED, dash="dot", width=1.5),
                      annotation_text=f"Prix d'achat moyen {avg_price:,.2f}", annotation_position="top left")
    dark_layout(fig, 440)
    fig.update_layout(xaxis_rangeslider_visible=False, showlegend=False)
    if intraday:
        fig.update_xaxes(type="category", nticks=8)
    return fig


def placement_view(ticker: str, trades: pd.DataFrame, positions: list[dict], key: str) -> None:
    """Courbe d'un placement ; période, type de graphique et axes réglables (barre d'outils → Réglages)."""
    held = next((p for p in positions if p["ticker"] == ticker), None)
    avg = None
    if held and held["quantite"]:
        avg = held["prix_revient_eur"] / held["quantite"] / fx_to_eur(held["devise"])
    title = st.empty()   # titre au-dessus du cadre : la barre d'outils doit sortir pile au bord du graphique
    with chart_box(key):
        prefs = settings_panel(key, kind="prix", periods=list(PERIODS))
        period = prefs["periode"]
        title.markdown(f"#### {ticker} · {name_of(ticker)} <span style='color:{MUTED};font-weight:500'>· {period}</span>",
                       unsafe_allow_html=True)
        with st.spinner("Chargement des cours…"):
            fig = price_chart(ticker, period, trades[trades["ticker"] == ticker] if not trades.empty else None,
                              avg if prefs["prix_moyen"] else None, style=prefs["style"])
        if fig is None:
            st.error(f"Aucun cours disponible pour {ticker}.")
            return
        show(apply_settings(fig, prefs, category_x=is_intraday(period)), key)
    st.caption("▲ achat · ▼ vente (survolez pour le motif) · pointillé : prix d'achat moyen, frais inclus · "
               "passez la souris sur le graphique pour les outils et les réglages.")


def go_to_robot(name: str) -> None:
    st.session_state["page"] = P_ROBOT
    st.session_state["robot"] = name


portfolios = load_portfolios()
robots = [p for p in portfolios if p["strategy"]]
color_of = {p["id"]: SERIES[i % len(SERIES)] for i, p in enumerate(portfolios)}

# ---------------- navigation (en haut de page, toujours visible) ----------------
if st.session_state.get("page") not in (P_RACE, P_ROBOT, P_MARKET, P_MINE, P_ECO):
    st.session_state["page"] = P_RACE
brand, nav, refresh, bell_col = st.columns([1.4, 6.9, 0.45, 0.5], vertical_alignment="center")
brand.markdown('<div class="tr-brand">Projet Bourse</div>', unsafe_allow_html=True)
nav_col, epoch_col = nav.columns([5.1, 1.8], vertical_alignment="center")
# Présent : la course en direct. Passé : les mêmes robots replacés à une date passée (voir ui/passe.py)
epoch = epoch_col.radio("Époque", [E_PRESENT, E_PAST], horizontal=True, label_visibility="collapsed", key="epoque")
if epoch == E_PRESENT:
    page = nav_col.radio("Aller à", [P_RACE, P_ROBOT, P_MARKET, P_MINE, P_ECO],
                         horizontal=True, label_visibility="collapsed", key="page")
else:
    page = None
    nav_col.markdown(f'<span style="color:{MUTED}">Simulation dans le passé · la course du présent continue '
                     'sans être touchée</span>', unsafe_allow_html=True)
# bouton « Rafraîchir » : icône bicolore dessinée par panneau_alertes.html (même style que le globe)
if refresh.button("Rafraîchir", key="refreshbtn"):
    st.cache_data.clear()
    clear_cache()   # les cours Yahoo gardés en mémoire (sinon jusqu'à 10 min de retard)
    st.rerun()
bell(bell_col.container(key="bellcol"), conn)   # alertes : bouton « globe », volet qui s'ouvre par la droite
st.caption("Argent fictif · cours réels · aucun lien avec un vrai courtier"
           + (" · 👁️ version spectateur : consultation seulement, données mises à jour toutes les 15 min"
              if SPECTATEUR else ""))

if epoch == E_PAST:
    passe.render(SimpleNamespace(spectateur=SPECTATEUR,
        configs=configs, SERIES=SERIES, INDEX_COLOR=INDEX_COLOR, MUTED=MUTED, signed=signed, pct=pct, eur2=eur2,
        risk_label=risk_label, line_chart=line_chart, chart_box=chart_box, settings_panel=settings_panel,
        apply_settings=apply_settings, show=show))
    st.stop()


# ================= La course des robots =================
if page == P_RACE:
    st.title("Qui veut gagner des millions")
    start = to_local(pd.Series([robots[0]["created"]])).iloc[0] if robots else None
    st.caption(f"Chaque robot a reçu **{eur(settings['general']['capital_fictif'])} fictifs** "
               f"le {start:%d/%m/%Y à %H:%M}. Cours réels, frais réels. Objectif : battre l'indice mondial "
               "(le gris pointillé).")
    if robots:
        leader = max(robots, key=lambda r: summary_of(r["id"])["perf"])
        lead = summary_of(leader["id"])
        st.markdown(f'<div class="tr-sub">En tête</div><div class="tr-hero">{leader["name"]}</div>'
                    f'<div class="tr-perf" style="margin-top:.3rem">{signed(pct(lead["perf"]), lead["perf"] >= 0)}'
                    f'&nbsp;&nbsp;<span style="color:{MUTED}">depuis le départ</span></div>',
                    unsafe_allow_html=True)
        st.write("")

    cols = st.columns(len(robots))
    for col, robot in zip(cols, robots):
        s = summary_of(robot["id"])
        cfg = configs[robot["name"]]
        risk = cfg.get("risque", "")
        diff = s["perf"] - s["bench_perf"]
        held = ", ".join(f"{name_of(x['ticker'])} {x['valeur_eur'] / s['total']:.0%}" for x in s["positions"])
        with col.container(key=f"card_race_{robot['id']}"):
            st.markdown(
                f'<div class="tr-name"><span class="tr-dot" style="background:{color_of[robot["id"]]}"></span>'
                f'{robot["name"].replace("Robot ", "")}</div>'
                f'<div class="tr-risk" style="color:{MUTED}">{risk_label(risk)}</div>'
                f'<div class="tr-value">{eur2(s["total"])}</div>'
                f'<div class="tr-perf">{signed(pct(s["perf"]), s["perf"] >= 0)}</div>'
                f'<div class="tr-sub tr-card-sub">Face à l\'indice : {signed(pct(diff), diff >= 0)}<br>'
                f'{held or "Aucun placement"} · liquidités {s["cash"] / s["total"]:.0%}</div>',
                unsafe_allow_html=True)
            st.button("Voir le détail", key=f"open_{robot['id']}", width="stretch",
                      on_click=go_to_robot, args=(robot["name"],))

    st.markdown("### Performance depuis le départ")
    with chart_box("course"):
        prefs = settings_panel("course", kind="courbes", units=True,
                               series=[r["name"] for r in robots] + ["Indice mondial"])
        in_pct = prefs["unite"] == "%"
        with st.spinner("Calcul des courbes à partir des vrais cours…"):
            lines, bench_line = [], None
            for robot in robots:
                curve = curve_of(robot["id"])
                if curve.empty:
                    continue
                value, bench = curve["value"], curve["benchmark"]
                if in_pct:
                    value = (value / robot["initial_cash"] - 1) * 100
                    bench = (bench / robot["initial_cash"] - 1) * 100
                lines.append((robot["name"], value, color_of[robot["id"]], "solid"))
                if bench_line is None:
                    bench_line = ("Indice mondial", bench, INDEX_COLOR, "dash")
        if lines and len(lines[0][1]) >= 2:
            fig = line_chart(lines + [bench_line], "Performance (%)" if in_pct else "Valeur (€)")
            show(apply_settings(fig, prefs), "course")
    if not (lines and len(lines[0][1]) >= 2):
        st.info("⏳ Les courbes démarrent au premier cours publié après le départ "
                "(les cours de Francfort arrivent avec environ 20 minutes de retard).")

    st.markdown("### Classement")
    if robots:
        st.markdown(ranking_html(sorted(robots, key=lambda r: -summary_of(r["id"])["perf"])),
                    unsafe_allow_html=True)


# ================= Détail d'un robot =================
elif page == P_ROBOT:
    names = [r["name"] for r in robots]
    robot = robots[names.index(st.selectbox("Robot", names, key="robot"))]
    cfg = configs[robot["name"]]
    st.title(robot["name"])
    risk = cfg.get("risque", "")
    st.markdown(f'<span style="color:{MUTED}">{risk_label(risk)}</span>'
                f'<span style="color:{MUTED}"> · {STRATEGIES[robot["strategy"]].description}</span>',
                unsafe_allow_html=True)

    s = summary_of(robot["id"])
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Valeur", eur2(s["total"]), pct(s["perf"]))
    c2.metric("Gain / perte", eur2(s["total"] - robot["initial_cash"]))
    c3.metric("Liquidités", eur2(s["cash"]))
    c4.metric("Face à l'indice", pct(s["perf"] - s["bench_perf"]),
              help=f"L'indice mondial a fait {pct(s['bench_perf'])} sur la même période")

    state = json.loads(conn.execute("SELECT state FROM portfolios WHERE id = ?", (robot["id"],)).fetchone()[0] or "{}")
    thinking = state.get("reflexion")
    st.markdown("### Ce qu'il pense en ce moment")
    if thinking and thinking.get("lines"):
        when = to_local(pd.Series([thinking["time"]])).iloc[0]
        with st.container(key="card_thinking"):
            st.caption(f"Son dernier passage : {when:%d/%m à %H:%M} (il réfléchit toutes les 15 minutes)")
            st.markdown("\n".join(f"- {line}" for line in thinking["lines"]))
    else:
        st.info("⏳ Il n'a pas encore réfléchi avec la nouvelle analyse du marché (prochain passage "
                "dans 15 minutes au plus).")

    orders = pd.read_sql("SELECT * FROM orders WHERE portfolio_id = ? ORDER BY id", conn, params=(robot["id"],))
    fills = orders[orders["status"] == FILLED].copy()

    st.markdown("### Ses placements")
    selected = positions_table(s["positions"], key=f"pos_{robot['id']}")
    past = sorted(set(fills["ticker"]) - {p["ticker"] for p in s["positions"]})
    if past:
        old = st.selectbox("… ou un ancien placement (déjà revendu)", ["—"] + past, key=f"past_{robot['id']}")
        selected = old if old != "—" else selected
    if selected:
        placement_view(selected, fills, s["positions"], key=f"period_{robot['id']}")

    st.markdown("### Valeur totale du portefeuille")
    curve = curve_of(robot["id"])
    if len(curve) >= 2:
        box_key = f"valeur_{robot['id']}"
        with chart_box(box_key):
            prefs = settings_panel(box_key, kind="courbes", units=True,
                                   series=[robot["name"], "Indice mondial (même capital)"])
            if prefs["unite"] == "%":
                curve = (curve / robot["initial_cash"] - 1) * 100
            fig = line_chart([(robot["name"], curve["value"], color_of[robot["id"]], "solid"),
                              ("Indice mondial (même capital)", curve["benchmark"], INDEX_COLOR, "dash")],
                             "Performance (%)" if prefs["unite"] == "%" else "Valeur (€)")
            if not fills.empty:
                fills["t"] = to_local(fills["filled_at"])
                for side, symbol, color in [(BUY, "triangle-up", UP), (SELL, "triangle-down", DOWN)]:
                    f = fills[fills["side"] == side]
                    if f.empty:
                        continue
                    y = [curve["value"].asof(t) if t >= curve.index[0] else curve["value"].iloc[0] for t in f["t"]]
                    fig.add_trace(go.Scatter(
                        x=f["t"], y=y, mode="markers", name=side.capitalize(),
                        marker=dict(symbol=symbol, size=13, color=color, line=dict(color="#000000", width=2)),
                        customdata=f[["filled_qty", "ticker", "fill_price", "reason"]].values,
                        hovertemplate="%{customdata[0]:.0f} × %{customdata[1]} à %{customdata[2]:.2f}"
                                      "<br>%{customdata[3]}<extra>" + side + "</extra>"))
            show(apply_settings(fig, prefs), box_key)
        st.caption("▲ achat · ▼ vente — survolez un triangle pour voir pourquoi le robot a agi.")
    else:
        st.info("⏳ La courbe démarre au premier cours publié après le départ.")

    st.markdown("### Journal : ce que le robot a pensé et fait")
    journal = pd.read_sql("SELECT time, message FROM journal WHERE portfolio_id = ? ORDER BY rowid DESC LIMIT 80",
                          conn, params=(robot["id"],))
    if journal.empty:
        st.write("Rien pour l'instant.")
    else:
        journal["time"] = to_local(journal["time"]).dt.strftime("%d/%m %H:%M")
        for _, row in journal.iterrows():
            st.markdown(f"`{row['time']}` {row['message']}")

    with st.expander("Preuve qu'il n'y a pas de triche", icon=":material/verified:"):
        st.markdown(
            "- **Cours réels** (Yahoo Finance), **argent fictif**, frais réalistes (0,1 %, minimum 1 €).\n"
            "- Un ordre est exécuté au **premier cours publié APRÈS la décision** : le robot ne peut jamais "
            "acheter à un prix d'avant l'information. Marché fermé = l'ordre attend l'ouverture.\n"
            "- Les alertes sont lues avec leur **heure de publication** : une info ancienne compte moins, "
            "voire plus du tout.\n"
            "- La courbe est recalculée à partir des ordres exécutés et des cours publiés, rien d'autre.")
        if not fills.empty:
            proof = pd.DataFrame({
                "N°": fills["id"], "Décision": to_local(fills["created"]).dt.strftime("%d/%m %H:%M:%S"),
                "Exécution": to_local(fills["filled_at"]).dt.strftime("%d/%m %H:%M:%S"),
                "Sens": fills["side"], "Titre": fills["ticker"], "Quantité": fills["filled_qty"],
                "Prix": fills["fill_price"], "Frais (€)": fills["fees_eur"], "Motif": fills["reason"]})
            st.dataframe(proof.style.format({"Quantité": num(0), "Prix": num(4), "Frais (€)": num(2)}),
                         hide_index=True, width="stretch")
            st.caption("L'heure d'exécution est toujours postérieure à l'heure de décision.")
        waiting = orders[orders["status"] == PENDING]
        if not waiting.empty:
            st.write(f"⏳ {len(waiting)} ordre(s) en attente du prochain cours publié.")


# ================= Analyse du marché =================
elif page == P_MARKET:
    st.title("Analyse du marché")
    st.caption("Ce que les robots étudient avant chaque décision (recalculé toutes les 15 minutes). "
               "Chacun en tire ses propres conclusions selon son caractère.")
    with st.spinner("Analyse de tous les placements et indicateurs…"):
        try:
            mv = market_view()
        except Exception as exc:
            st.error(f"Analyse indisponible : {exc}")
            st.stop()
    score = mv.risk_score
    c1, c2, c3 = st.columns(3)
    c1.metric("Note de climat", f"{score:+d} / 100", help="-100 = tempête, +100 = optimisme")
    c2.metric("Humeur du marché", mv.mood)
    c3.metric("Articles lus (24 h)", f"{mv.news.n_articles:,}".replace(",", " "),
              help="Toutes les actualités lues par la veille, pas seulement celles qui ont donné une alerte")

    st.markdown("### Les signaux qui composent la note")
    signals_table(mv.factors)

    st.markdown("### Tous les placements étudiés")
    assets_table(list(mv.assets.values()))


# ================= Mon portefeuille =================
elif page == P_MINE:
    mine = next(p for p in portfolios if not p["strategy"])
    broker = make_broker(conn, conn.execute("SELECT * FROM portfolios WHERE id = ?", (mine["id"],)).fetchone(),
                         settings)
    if not SPECTATEUR and broker.process_pending():   # un ordre vient d'être traité : recalculer valeur et courbe
        summary_of.clear()
        curve_of.clear()
    st.title("Mon portefeuille")
    if SPECTATEUR:
        st.caption("👁️ Le portefeuille géré à la main par le propriétaire : vous pouvez le consulter et étudier "
                   "n'importe quel titre, mais pas passer d'ordre.")
    s = summary_of(mine["id"])
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Valeur", eur2(s["total"]), pct(s["perf"]))
    c2.metric("Gain / perte", eur2(s["total"] - mine["initial_cash"]))
    c3.metric("Liquidités", eur2(s["cash"]))
    c4.metric("Face à l'indice", pct(s["perf"] - s["bench_perf"]))
    st.markdown("### Mes placements")
    selected = positions_table(s["positions"], key="pos_mine")

    st.markdown("### Étudier un titre" if SPECTATEUR else "### Trader")
    my_fills = pd.read_sql("SELECT * FROM orders WHERE portfolio_id = ? AND status = ?", conn,
                           params=(mine["id"], FILLED))
    left, right = st.columns([3, 1])
    with left:
        ticker = st.text_input("Titre (code Yahoo Finance)", selected or "IUSQ.DE").strip().upper()
        st.caption("Exemples : MC.PA (LVMH) · AAPL (Apple) · 7203.T (Toyota) · 0700.HK (Tencent) · "
                   "IUSQ.DE (monde) · 4GLD.DE (or) — ou cliquez sur un de vos placements ci-dessus.")
        try:
            q = quote(ticker)
        except Exception:
            q = None
        if q is None:
            st.error(f"Aucun cours trouvé pour « {ticker} ». Vérifiez le code du titre.")
        else:
            placement_view(ticker, my_fills, s["positions"], key="period_mine")
    with right:
        if q is not None:
            st.metric(ticker, f"{q.price:,.2f} {q.currency}".replace(",", " "))
            if q.currency != "EUR":
                st.caption(f"≈ {eur2(q.price_eur)}")
            st.caption("🟢 Marché ouvert" if q.market_open else "🔴 Marché fermé : l'ordre attendra l'ouverture")
            held = broker.holdings().get(ticker)
            st.caption(f"{'Le propriétaire détient' if SPECTATEUR else 'Vous détenez'} : "
                       f"{int(held.quantity) if held else 0} titre(s)")
            if not SPECTATEUR:
                with st.form("ordre"):
                    side = st.radio("Sens", [BUY, SELL], horizontal=True)
                    mode = st.radio("Taille", ["Quantité", "Montant en €"], horizontal=True)
                    size = st.number_input("Valeur", min_value=0.0, value=1.0)
                    unit = q.price * fx_to_eur(q.currency)
                    qty_est = int(size) if mode == "Quantité" else int(size // unit)
                    st.caption(f"Estimation : {qty_est} titre(s) ≈ {eur2(qty_est * unit)} + frais")
                    if st.form_submit_button("Envoyer l'ordre", type="primary", width="stretch"):
                        if size <= 0:
                            st.error("Indiquez une valeur positive.")
                        else:
                            broker.place_order(ticker, side, quantity=size if mode == "Quantité" else None,
                                               amount_eur=size if mode != "Quantité" else None,
                                               reason="Ordre manuel")
                            st.info("⏳ Ordre enregistré. Il sera exécuté au premier cours publié après maintenant "
                                    "(souvent 5 à 20 minutes, ou à l'ouverture si le marché est fermé). "
                                    "Cliquez sur « Rafraîchir » pour suivre.")

    st.markdown("### Mes ordres")
    orders = pd.read_sql("SELECT * FROM orders WHERE portfolio_id = ? ORDER BY id DESC", conn, params=(mine["id"],))
    for _, o in orders[orders["status"] == PENDING].iterrows():
        c1, c2 = st.columns([5, 1])
        size_txt = f"{o['quantity']:g} titres" if o["quantity"] else eur2(o["amount_eur"]) if o["amount_eur"] else "tout"
        c1.write(f"⏳ #{o['id']} · {o['side']} {o['ticker']} · {size_txt}")
        if not SPECTATEUR and c2.button("Annuler", key=f"cancel{o['id']}"):
            broker.cancel_order(int(o["id"]))
            st.cache_data.clear()
            st.rerun()
    if not orders.empty:
        view = pd.DataFrame({
            "N°": orders["id"], "Passé le": to_local(orders["created"]).dt.strftime("%d/%m %H:%M"),
            "Sens": orders["side"], "Titre": orders["ticker"], "Statut": orders["status"],
            "Quantité": orders["filled_qty"], "Prix": orders["fill_price"], "Devise": orders["currency"],
            "Frais (€)": orders["fees_eur"], "Remarque": orders["note"]})
        st.dataframe(view.style.format({"Quantité": num(0), "Prix": num(2), "Frais (€)": num(2)},
                                       na_rep=""), hide_index=True, width="stretch")


# ================= Éco 1 : l'assistant économiste =================
elif page == P_ECO:
    SECRETS = Path(__file__).resolve().parents[3] / ".streamlit" / "secrets.toml"
    eco = settings["eco1"]
    SUGGESTIONS = ["Que pense Charles Gave de l'or et de l'euro en ce moment ? Qui le contredit ?",
                   "Les économistes voient-ils venir une récession ? Confronte les écoles.",
                   "Analyse le climat actuel du marché : lequel de mes robots est le mieux placé ?",
                   "Je débute : comment organiser mon épargne avant d'investir en Bourse ?"]

    def eco_portfolios() -> list[dict]:
        out = []
        for p in load_portfolios():
            s = summary_of(p["id"])
            state = json.loads(conn.execute("SELECT state FROM portfolios WHERE id = ?", (p["id"],)).fetchone()[0]
                               or "{}")
            out.append({"nom": p["name"], "strategie": p["strategy"] or "manuel (le propriétaire)",
                        "risque": configs.get(p["name"], {}).get("risque"), "depart": p["created"][:10],
                        "valeur_eur": round(s["total"], 2), "perf_pct": round(s["perf"], 2),
                        "indice_mondial_pct": round(s["bench_perf"], 2), "liquidites_eur": round(s["cash"], 2),
                        "placements": [{"ticker": x["ticker"], "nom": name_of(x["ticker"]),
                                        "valeur_eur": round(x["valeur_eur"], 2), "gain_pct": round(x["gain_pct"], 2)}
                                       for x in s["positions"]],
                        "derniere_reflexion": (state.get("reflexion") or {}).get("lines", [])})
        return out

    def save_message(conv: int, message: dict, display: str | None, model: str) -> None:
        conn.execute("INSERT INTO eco_chat (conv, time, role, content, display, model) VALUES (?, ?, ?, ?, ?, ?)",
                     (conv, pd.Timestamp.now(tz="UTC").isoformat(), message["role"],
                      json.dumps(message["content"], ensure_ascii=False), display, model))
        conn.commit()

    def secret_key() -> str | None:
        try:
            return eco_agent.api_key(dict(st.secrets))
        except Exception:     # pas de fichier secrets.toml
            return eco_agent.api_key()

    st.title("Éco 1")
    st.caption("L'assistant économiste : il lit une bibliothèque d'économistes d'écoles différentes, étudie les "
               "données du site et l'actualité, puis confronte les points de vue. Ce n'est pas un conseil "
               "en investissement.")

    # ---- réglages et bibliothèque ----
    catalogue = bibliotheque.catalogue(conn, settings)
    n_docs = sum(c["articles"] for c in catalogue)
    modes = list(eco["modes"])
    c_mode, c_new, c_info = st.columns([2.2, 1.3, 3], vertical_alignment="bottom")
    if SPECTATEUR:   # version en ligne : on relit les conversations du propriétaire, sans en commencer
        mode = modes[0]
        convs = [r[0] for r in conn.execute("SELECT DISTINCT conv FROM eco_chat ORDER BY conv DESC")] or [1]
        st.session_state["eco_conv"] = c_mode.selectbox("Conversation", convs,
                                                        format_func=lambda c: f"Conversation n° {c}")
    else:
        mode = c_mode.segmented_control("Puissance", modes, default=modes[0], key="eco_mode",
                                        help="Gratuit : une IA qui tourne sur ce PC (0 €, sans web, plus lente). "
                                             "Standard et Maximum : Claude d'Anthropic, quelques centimes par "
                                             "question (Maximum ≈ deux fois plus cher).") or modes[0]
    model, effort = eco["modes"][mode]["modele"], eco["modes"][mode].get("effort")
    if "eco_conv" not in st.session_state:   # on reprend la dernière conversation
        st.session_state["eco_conv"] = conn.execute("SELECT COALESCE(MAX(conv), 1) FROM eco_chat").fetchone()[0]
    if not SPECTATEUR and c_new.button("Nouvelle conversation", icon=":material/add_comment:", width="stretch"):
        st.session_state["eco_conv"] = conn.execute("SELECT COALESCE(MAX(conv), 0) + 1 FROM eco_chat").fetchone()[0]
        st.rerun()
    c_info.caption(f"📚 {n_docs:,} articles de {sum(1 for c in catalogue if c['articles'])} sources".replace(",", " ")
                   + (" · 🌐 recherche web activée" if eco.get("recherche_web", True)
                      and not model.startswith("ollama:") else ""))

    with st.expander(f"La bibliothèque ({n_docs:,} articles)".replace(",", " "), icon=":material/menu_book:",
                     expanded=n_docs == 0):
        if n_docs == 0:
            st.info("La bibliothèque est vide. Lancez la première lecture : elle remonte aussi les archives "
                    "(plusieurs minutes, on lit poliment une page par seconde). Ensuite, la veille automatique "
                    f"la tient à jour toutes les {eco.get('mise_a_jour_heures', 12)} heures.")
        st.dataframe(pd.DataFrame([{"Source": c["source"], "École": c["ecole"], "Langue": c["langue"],
                                    "Articles": c["articles"], "Du": c["du"], "Au": c["au"],
                                    "Auteurs": ", ".join(c["auteurs"][:3]),
                                    "État": f"⚠️ {c['erreur'][:60]}" if c["erreur"] else ""} for c in catalogue]),
                     hide_index=True, width="stretch")
        if not SPECTATEUR and st.button("Mettre à jour la bibliothèque maintenant", icon=":material/sync:"):
            with st.status("Lecture des économistes…", expanded=True) as status:
                line = st.empty()
                added = bibliotheque.update_all(conn, settings,
                                                progress=lambda src, title: line.caption(f"{src} · {title}"))
                status.update(label=f"{sum(added.values())} nouveaux articles", state="complete", expanded=False)
            st.rerun()
        st.caption("Usage personnel : les textes restent sur ce PC et ne sont jamais republiés. "
                   "Sources modifiables dans config/settings.yaml (section eco1).")

    # ---- conversation ----
    conv = st.session_state["eco_conv"]
    rows = conn.execute("SELECT * FROM eco_chat WHERE conv = ? ORDER BY id", (conv,)).fetchall()
    if SPECTATEUR:   # lecture seule : jamais de question (elle serait payée par le propriétaire)
        st.info("👁️ Version spectateur : vous pouvez relire les conversations du propriétaire avec Éco 1, "
                "mais pas lui poser de question." if rows else
                "👁️ Version spectateur : le propriétaire n'a pas encore de conversation avec Éco 1 à montrer.")
        for r in rows:
            if r["display"]:
                with st.chat_message(r["role"], avatar=None if r["role"] == "user" else ":material/insights:"):
                    st.markdown(r["display"])
        st.stop()
    if rows and rows[-1]["model"] != model:     # chaque modèle a son propre format de conversation
        started = next((m for m, c in eco["modes"].items() if c["modele"] == rows[-1]["model"]), rows[-1]["model"])
        st.caption(f"Cette conversation a commencé en mode {started} : le nouveau mode s'appliquera à la "
                   "prochaine conversation.")
        model = rows[-1]["model"]
    local = model.startswith("ollama:")
    if local:
        ready, why = eco_local.available(model.removeprefix("ollama:"))
        if not ready:
            st.warning(why)
        key = None
    else:
        # ---- clé d'API ----
        key = secret_key()
        if not key:
            with st.container(key="card_eco_key"):
                st.markdown("#### Brancher Éco 1 sur Claude")
                st.markdown("Éco 1 a besoin d'une **clé d'API Anthropic** : créez-la sur "
                            "[console.anthropic.com](https://console.anthropic.com/settings/keys) (compte à créditer, "
                            "comptez quelques centimes par question). Elle est enregistrée uniquement sur ce PC, "
                            "dans `.streamlit/secrets.toml`.")
                new_key = st.text_input("Clé d'API", type="password", placeholder="sk-ant-…")
                if st.button("Enregistrer la clé", type="primary") and new_key.strip():
                    SECRETS.parent.mkdir(exist_ok=True)
                    lines = [ln for ln in (SECRETS.read_text(encoding="utf-8").splitlines() if SECRETS.exists() else [])
                             if not ln.startswith("ANTHROPIC_API_KEY")]
                    lines.append(f'ANTHROPIC_API_KEY = "{new_key.strip()}"')
                    SECRETS.write_text("\n".join(lines) + "\n", encoding="utf-8")
                    os.environ["ANTHROPIC_API_KEY"] = new_key.strip()
                    st.rerun()
        ready = bool(key)
    for r in rows:
        if r["display"]:
            with st.chat_message(r["role"], avatar=None if r["role"] == "user" else ":material/insights:"):
                st.markdown(r["display"])

    question = st.chat_input("Posez votre question à Éco 1…", disabled=not ready)
    if not rows and ready:
        cols = st.columns(2)
        for i, text in enumerate(SUGGESTIONS):
            if cols[i % 2].button(text, key=f"eco_sugg_{i}", width="stretch"):
                question = text
    if question and ready:
        with st.chat_message("user"):
            st.markdown(question)
        user_msg = ({"role": "user", "content": question} if local else
                    {"role": "user", "content": [{"type": "text", "text": question}]})
        history_msgs = [{"role": r["role"], "content": json.loads(r["content"])} for r in rows] + [user_msg]
        data = eco_agent.SiteData(conn=conn, settings=settings, market_view=market_view, portfolios=eco_portfolios)
        if local:
            events = eco_local.ask_local(model.removeprefix("ollama:"), history_msgs, data)
        else:
            events = eco_agent.ask(eco_agent.anthropic.Anthropic(api_key=key), model, effort, history_msgs, data,
                                   web=eco.get("recherche_web", True))
        with st.chat_message("assistant", avatar=":material/insights:"):
            steps = st.status("Éco 1 réfléchit…", expanded=False)
            answer_box = st.empty()
            answer, thinking, final = "", "", None
            try:
                for ev in events:
                    if ev.kind == "reflexion":
                        thinking += ev.text
                    elif ev.kind == "texte":
                        answer += ev.text
                        answer_box.markdown(answer + "▌")
                    elif ev.kind == "outil":
                        steps.update(label=ev.text + "…")
                        steps.write(ev.text)
                    elif ev.kind == "corrige":     # liens inventés retirés par le mode gratuit
                        answer = ev.text
                    else:
                        final = ev
            except eco_agent.anthropic.AuthenticationError:
                st.error("Clé d'API refusée. Corrigez-la dans .streamlit/secrets.toml (ou supprimez ce fichier "
                         "pour la saisir de nouveau).")
            except eco_agent.anthropic.RateLimitError:
                st.error("Trop de demandes d'un coup : réessayez dans une minute.")
            except eco_agent.anthropic.APIStatusError as exc:
                st.error(f"L'API Claude a répondu une erreur ({exc.status_code}) : {exc.message}")
            except eco_agent.anthropic.APIConnectionError:
                st.error("Pas de connexion à l'API Claude. Vérifiez Internet.")
            except (eco_local.requests.RequestException, RuntimeError) as exc:
                st.error(f"L'IA locale (Ollama) a échoué : {exc}")
            if thinking:
                with steps:
                    st.markdown("**Sa réflexion**")
                    st.caption(thinking)
            steps.update(label="Recherches et réflexion", state="complete", expanded=False)
            answer_box.markdown(answer)
            if final is not None and final.kind == "erreur":
                st.warning(final.text)
            if final is not None and final.messages:
                st.caption(f"Gratuit · {model.removeprefix('ollama:')} sur ce PC" if local
                           else f"Coût de cette réponse ≈ {final.cost:.3f} $ · {model}")
                save_message(conv, user_msg, question, model)
                shown = max(i for i, m in enumerate(final.messages) if m["role"] == "assistant")
                for i, msg in enumerate(final.messages):
                    save_message(conv, msg, answer if i == shown and answer else None, model)

