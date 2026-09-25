"""Onglet « Passé » : les mêmes robots, replacés à une date passée, sur les vrais historiques.

L'interface ne fait que lancer une simulation (programme séparé, voir bourse.backtest.simulation)
et lire sa base au fil de l'eau : la course du présent n'est jamais touchée.
"""
import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

from bourse.backtest import simulation as sim
from bourse.data.prices import name_of

TZ = "Europe/Paris"
STATUS = {"en_attente": "⏳ au départ", "preparation": "⚙️ préparation", "en_cours": "▶️ en cours",
          "terminee": "✅ terminée", "arretee": "⏹️ arrêtée", "arret_demande": "⏹️ arrêt demandé",
          "erreur": "⚠️ erreur"}
RUNNING = ("en_attente", "preparation", "en_cours", "arret_demande")
MONTHS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre",
          "novembre", "décembre"]
DAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
EARLIEST = date(2012, 1, 1)

# Ce qui diffère TOUJOURS du présent (les écarts propres à chaque simulation s'ajoutent en dessous)
KNOWN_GAPS = [
    ("Cours d'une journée : ouverture et clôture seulement",
     "Pour les années passées, Yahoo ne garde qu'une barre par jour. Un ordre passé pendant la séance est "
     "exécuté au cours de clôture (au présent : dans les minutes qui suivent). Un ordre passé la nuit est "
     "exécuté au cours d'ouverture, comme au présent. Les stop-loss ne sont vérifiés qu'à l'ouverture et à la "
     "clôture : une chute pendant la séance est vue plus tard."),
    ("Ouvertures douteuses ignorées",
     "Quand le cours d'ouverture est identique à la clôture, ou qu'il n'y a eu aucun échange ce jour-là, "
     "l'ouverture est ignorée : ce pourrait être la clôture recopiée, que personne ne connaissait le matin."),
    ("Moins d'actualités qu'au présent",
     "Les archives d'époque contiennent beaucoup moins d'articles (souvent 100 à 300 par jour, contre plus "
     "de 500 au présent). Moins d'articles = moins de sujets couverts par plusieurs médias, donc des alertes "
     "plus rares. Les robots réagissent donc moins aux nouvelles que dans la course du présent."),
    ("Actualités vues en retard, jamais en avance",
     "Google Actualités ne donne que la date des articles anciens, pas l'heure : ils ne sont montrés aux "
     "robots qu'à la fin de leur journée. Les articles des flux archivés (BBC, CNBC, Le Monde…) ne sont "
     "montrés qu'à l'heure où l'archive les a vus (quelques fois par jour). Communiqués de la Fed : heure "
     "exacte. BCE : le soir même. Conséquence : Kamikaze (infos de moins de 3 h) et Audacieux (moins de "
     "6 h) réagissent rarement aux alertes dans le passé."),
    ("Sources manquantes selon les jours",
     "Chaque source n'a pas été archivée tous les jours (voir le tableau de couverture de la simulation). "
     "Investing.com, South China Morning Post, Economic Times et France 24 sont peu ou pas archivés."),
    ("Titres d'alertes non traduits",
     "Pour aller vite, les alertes gardent leur titre d'origine (souvent en anglais), sauf s'il existe un "
     "article français sur le sujet. Cela ne change aucune décision : les robots lisent le vocabulaire "
     "d'origine, comme au présent."),
    ("Placements qui n'existaient pas encore",
     "Un placement créé après la date simulée (Bitcoin, Ethereum, Inde, Chine, semi-conducteurs…) est "
     "invisible jusqu'à sa création : les robots ne peuvent ni l'étudier ni l'acheter avant."),
    ("Cours ajustés des divisions de titres",
     "Yahoo recalcule les anciens cours après chaque division de titre (et certains ETF à levier en ont eu "
     "beaucoup) : le prix unitaire peut différer de celui de l'époque, mais les gains et pertes en % sont les "
     "mêmes. Les dividendes ne sont pas versés, comme dans la course du présent."),
    ("Robot Éco absent", "Exclu pour l'instant (son IA connaît déjà la suite de l'histoire)."),
]


def _open(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _long_date(iso: str) -> str:
    t = pd.Timestamp(iso).tz_convert(TZ)
    return f"{DAYS[t.weekday()]} {t.day} {MONTHS[t.month - 1]} {t.year}, {t:%H}h{t:%M}"


def _label(path: Path, info: dict) -> str:
    start, end = date.fromisoformat(info["debut"]), date.fromisoformat(info["fin"])
    return (f"{start:%d/%m/%Y} → {end:%d/%m/%Y} · {len(info.get('robots', []))} robots · "
            f"{STATUS.get(info.get('statut'), info.get('statut'))}")


def render(ui) -> None:
    """ui : les outils de mise en forme de l'application (couleurs, graphiques, formats)."""
    st.title("Le passé")
    st.caption("Les mêmes robots, replacés à une date passée, sur les vrais cours et les actualités de "
               "l'époque. Ils ne connaissent jamais la suite : chaque cours, chaque article ne leur parvient "
               "qu'à l'heure où il a été publié. Des années de Bourse se jouent en quelques minutes.")

    robots = [name for name, cfg in ui.configs.items() if cfg.get("strategie") in sim.ROBOTS_ALLOWED]
    spectateur = getattr(ui, "spectateur", False)   # site en ligne : on regarde, on ne lance rien
    if spectateur:
        st.info("👁️ Version spectateur : voici les simulations déjà faites par le propriétaire. "
                "Lancer une nouvelle simulation n'est possible que sur son PC.")
    else:
        with st.container(key="card_newsim"):
            st.markdown("#### Remonter le temps")
            today = date.today()
            default_start = date(today.year - 10, today.month, min(today.day, 28))
            c1, c2, c3 = st.columns([1, 1, 2.2], vertical_alignment="bottom")
            start = c1.date_input("Départ", default_start, min_value=EARLIEST, max_value=today - timedelta(days=30),
                                  format="DD/MM/YYYY", key="sim_start")
            if st.session_state.get("sim_end") and st.session_state["sim_end"] < start + timedelta(days=7):
                del st.session_state["sim_end"]   # arrivée devenue antérieure au départ
            end = c2.date_input("Arrivée", min(start + timedelta(days=365), today - timedelta(days=1)),
                                min_value=start + timedelta(days=7), max_value=today - timedelta(days=1),
                                format="DD/MM/YYYY", key="sim_end")
            chosen = c3.multiselect("Robots", robots, default=robots, key="sim_robots",
                                    format_func=lambda n: n.replace("Robot ", ""))
            years = max((end - start).days / 365, 0.02)
            st.caption(f"Durée estimée : environ {max(1, round(years * 1.5))} min de calcul pour "
                       f"{(end - start).days} jours simulés, plus le téléchargement des actualités d'époque "
                       "s'il en manque (quelques secondes par jour manquant). Vous pouvez quitter la page : "
                       "la simulation continue.")
            if st.button("Remonter le temps", type="primary", icon=":material/history:", disabled=not chosen):
                path = sim.create(start, end, chosen)
                sim.launch(path)
                st.session_state["sim_selected"] = path.name
                st.rerun()

    carnet = sim.PROJECT_ROOT / "data" / "labo" / "carnet.md"
    if carnet.exists():
        with st.expander("Carnet du labo : les améliorations essayées sur les robots", icon=":material/science:"):
            st.markdown(carnet.read_text(encoding="utf-8"))

    sims = sim.list_simulations()
    if not sims:
        st.info("Aucune simulation pour l'instant : choisissez une date de départ et lancez-vous.")
        return
    names = [p.name for p, _ in sims]
    if st.session_state.get("sim_selected") not in names:
        st.session_state["sim_selected"] = names[0]
    labels = {p.name: _label(p, info) for p, info in sims}
    selected = st.selectbox("Simulation affichée", names, key="sim_selected", format_func=lambda n: labels[n])
    path = next(p for p, _ in sims if p.name == selected)
    info = dict(sims)[path]

    @st.fragment(run_every=3 if info.get("statut") in RUNNING else None)
    def live():
        conn = _open(path)
        try:
            show_simulation(ui, conn, path)
        finally:
            conn.close()
    live()


def show_simulation(ui, conn: sqlite3.Connection, path: Path) -> None:
    info = sim.get_info(conn)
    status = info.get("statut")
    progress = float(info.get("progression") or 0)
    now_iso = info.get("heure_simulee")

    # ---- où en est-on dans le temps ----
    with st.container(key="card_timebar"):
        left, right = st.columns([4, 1], vertical_alignment="center")
        with left:
            st.markdown(f'<div class="tr-sub">Nous sommes le</div><div class="tr-hero" style="font-size:2.1rem">'
                        f'{_long_date(now_iso) if now_iso else _long_date(info["debut"] + "T00:00:00+00:00")}</div>',
                        unsafe_allow_html=True)
            st.progress(progress, text=f"{STATUS.get(status, status)} · {progress:.0%} · {info.get('message', '')}")
            if info.get("duree_s"):
                st.caption(f"Calcul : {info['duree_s'] / 60:.1f} min · {info.get('alertes', 0)} alerte(s) déclenchée(s)"
                           " par la veille d'époque")
        with right:
            if getattr(ui, "spectateur", False):
                pass   # site en ligne : ni arrêt ni relance
            elif status in RUNNING and status != "arret_demande":
                if st.button("Arrêter", icon=":material/stop_circle:", width="stretch", key="sim_stop"):
                    sim.set_info(conn, statut="arret_demande")
                    st.rerun()
            elif status in ("terminee", "arretee", "erreur"):
                if st.button("Relancer à l'identique", icon=":material/replay:", width="stretch", key="sim_again",
                             help="Nouvelle simulation, mêmes dates et mêmes robots (utile si des actualités "
                                  "d'époque ont été téléchargées entre-temps)."):
                    new = sim.create(date.fromisoformat(info["debut"]), date.fromisoformat(info["fin"]),
                                     info["robots"])
                    sim.launch(new)
                    st.session_state["sim_selected"] = new.name
                    st.rerun(scope="app")
    if status == "erreur":
        st.error(info.get("message", "Erreur") + f" — détails dans {path.with_suffix('.log').name}")

    portfolios = conn.execute("SELECT * FROM portfolios ORDER BY id").fetchall()
    snaps = pd.read_sql("SELECT portfolio_id, time, value_eur, cash_eur, benchmark_eur FROM snapshots", conn)
    if not portfolios or snaps.empty:
        st.info("⏳ Préparation : chargement des cours et des actualités d'époque…")
        _gaps(info)
        return
    snaps["time"] = pd.to_datetime(snaps["time"], utc=True, format="ISO8601").dt.tz_convert(TZ)
    color = {p["id"]: ui.SERIES[i % len(ui.SERIES)] for i, p in enumerate(portfolios)}
    last = snaps.sort_values("time").groupby("portfolio_id").last()
    first_bench = snaps.sort_values("time").groupby("portfolio_id")["benchmark_eur"].first()
    rows = []
    for p in portfolios:
        if p["id"] not in last.index:
            continue
        s = last.loc[p["id"]]
        perf = (s["value_eur"] / p["initial_cash"] - 1) * 100
        bench = (s["benchmark_eur"] / first_bench.loc[p["id"]] - 1) * 100
        rows.append({"p": p, "value": s["value_eur"], "cash": s["cash_eur"], "perf": perf, "bench": bench})
    rows.sort(key=lambda r: -r["perf"])

    # ---- la course ----
    if rows:
        lead = rows[0]
        st.markdown(f'<div class="tr-sub">En tête</div><div class="tr-hero">{lead["p"]["name"]}</div>'
                    f'<div class="tr-perf" style="margin-top:.3rem">{ui.signed(ui.pct(lead["perf"]), lead["perf"] >= 0)}'
                    f'&nbsp;&nbsp;<span style="color:{ui.MUTED}">depuis le {date.fromisoformat(info["debut"]):%d/%m/%Y}'
                    f'</span></div>', unsafe_allow_html=True)
        st.write("")
    cols = st.columns(max(len(rows), 1))
    for col, r in zip(cols, sorted(rows, key=lambda r: r["p"]["id"])):
        p, diff = r["p"], r["perf"] - r["bench"]
        cfg = ui.configs.get(p["name"], {})
        with col.container(key=f"card_sim_{p['id']}"):
            st.markdown(
                f'<div class="tr-name"><span class="tr-dot" style="background:{color[p["id"]]}"></span>'
                f'{p["name"].replace("Robot ", "")}</div>'
                f'<div class="tr-risk">{ui.risk_label(cfg.get("risque", ""))}</div>'
                f'<div class="tr-value">{ui.eur2(r["value"])}</div>'
                f'<div class="tr-perf">{ui.signed(ui.pct(r["perf"]), r["perf"] >= 0)}</div>'
                f'<div class="tr-sub tr-card-sub">Face à l\'indice : {ui.signed(ui.pct(diff), diff >= 0)}<br>'
                f'liquidités {r["cash"] / r["value"]:.0%}</div>', unsafe_allow_html=True)

    st.markdown("### Performance depuis le départ")
    with ui.chart_box("simcourse"):
        prefs = ui.settings_panel("simcourse", kind="courbes", units=True,
                                  series=[p["name"] for p in portfolios] + ["Indice mondial"])
        in_pct = prefs["unite"] == "%"
        lines = []
        for p in portfolios:
            s = snaps[snaps["portfolio_id"] == p["id"]].set_index("time").sort_index()
            if len(s) < 2:
                continue
            value = (s["value_eur"] / p["initial_cash"] - 1) * 100 if in_pct else s["value_eur"]
            lines.append((p["name"], value, color[p["id"]], "solid"))
        if lines:
            s = snaps[snaps["portfolio_id"] == portfolios[0]["id"]].set_index("time").sort_index()
            bench = s["benchmark_eur"] / s["benchmark_eur"].iloc[0] * portfolios[0]["initial_cash"]
            bench = (bench / portfolios[0]["initial_cash"] - 1) * 100 if in_pct else bench
            fig = ui.line_chart(lines + [("Indice mondial", bench, ui.INDEX_COLOR, "dash")],
                                "Performance (%)" if in_pct else "Valeur (€)")
            fig.update_xaxes(tickformat="%d/%m/%Y", hoverformat="%d/%m/%Y %H:%M")
            ui.show(ui.apply_settings(fig, prefs), "simcourse")
    if not lines:
        st.info("⏳ Les courbes apparaissent après la première journée de Bourse simulée.")

    # ---- chaque robot ----
    st.markdown("### Ce que chaque robot a fait")
    tabs = st.tabs([p["name"].replace("Robot ", "") for p in portfolios])
    for tab, p in zip(tabs, portfolios):
        with tab:
            _robot(ui, conn, p)

    _coverage(info)
    _gaps(info)


def _robot(ui, conn, p) -> None:
    state = json.loads(p["state"] or "{}")
    thinking = state.get("reflexion")
    if thinking and thinking.get("lines"):
        st.caption(f"Sa réflexion le {_long_date(thinking['time'])}")
        st.markdown("\n".join(f"- {line}" for line in thinking["lines"]))
    orders = pd.read_sql("SELECT * FROM orders WHERE portfolio_id = ? AND status = 'EXECUTE' ORDER BY id DESC",
                         conn, params=(p["id"],))
    left, right = st.columns([1.15, 1])
    with left:
        st.markdown(f"**Ordres exécutés** · {len(orders)}")
        if not orders.empty:
            local = lambda col: pd.to_datetime(orders[col], utc=True, format="ISO8601").dt.tz_convert(TZ)  # noqa: E731
            st.dataframe(pd.DataFrame({
                "Décision": local("created").dt.strftime("%d/%m/%Y %H:%M"),
                "Exécution": local("filled_at").dt.strftime("%d/%m/%Y %H:%M"),
                "Sens": orders["side"], "Titre": [name_of(t) for t in orders["ticker"]],
                "Quantité": orders["filled_qty"], "Prix": orders["fill_price"].round(4),
                "Frais (€)": orders["fees_eur"].round(2), "Motif": orders["reason"]}),
                hide_index=True, width="stretch", height=320)
            st.caption("L'exécution est toujours postérieure à la décision, au premier cours publié après elle.")
    with right:
        journal = pd.read_sql("SELECT time, message FROM journal WHERE portfolio_id = ? ORDER BY rowid DESC LIMIT 60",
                              conn, params=(p["id"],))
        st.markdown("**Journal** (le plus récent en haut)")
        with st.container(height=360, border=False):
            if journal.empty:
                st.write("Rien pour l'instant.")
            for _, row in journal.iterrows():
                t = pd.Timestamp(row["time"]).tz_convert(TZ)
                st.markdown(f"`{t:%d/%m/%Y %H:%M}` {row['message']}")


def _coverage(info: dict) -> None:
    cov = info.get("couverture")
    if not cov:
        return
    with st.expander("Actualités d'époque disponibles pour cette période", icon=":material/newspaper:"):
        st.caption("Part des journées de la période pour lesquelles chaque source a été retrouvée dans les "
                   "archives. Le téléchargement des archives de la BBC, CNBC, Le Monde… continue en tâche de fond : "
                   "une simulation relancée plus tard peut en avoir davantage.")
        df = pd.DataFrame([{"Source": k, "Journées téléchargées": v["telecharge"],
                            "Journées avec des articles": v["avec_articles"]} for k, v in cov.items()])
        st.dataframe(df.style.format({"Journées téléchargées": "{:.0%}", "Journées avec des articles": "{:.0%}"}),
                     hide_index=True, width="stretch")


def _gaps(info: dict) -> None:
    with st.expander("Tous les écarts avec la course du présent (à lire)", icon=":material/rule:"):
        st.caption("La simulation fait tourner exactement le même code que le présent. Voici tout ce qui "
                   "diffère malgré tout, et pourquoi.")
        for title, text in KNOWN_GAPS:
            st.markdown(f"**{title}.** {text}")
        extra = info.get("ecarts") or []
        if extra:
            st.markdown("**Propres à cette simulation :**")
            st.markdown("\n".join(f"- {e}" for e in extra))
