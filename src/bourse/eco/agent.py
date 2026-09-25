"""Éco 1 : l'assistant économiste du Projet Bourse (API Claude d'Anthropic).

À chaque question, Claude choisit lui-même ses outils :
  - la bibliothèque d'économistes (recherche plein texte, lecture d'un article entier) ;
  - les données du site : climat du marché, portefeuilles et robots, alertes, historique d'un cours ;
  - le web (recherche et lecture de pages), pour l'actualité toute fraîche.
Puis il répond en citant ses sources et en confrontant les écoles de pensée.
"""
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterator

import anthropic
import pandas as pd

from bourse.data.prices import history, name_of
from bourse.eco import bibliotheque

log = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 15
MAX_ARTICLE_CHARS = 24_000
# $ par million de jetons (entrée, sortie) ; lecture du cache = 10 % de l'entrée, écriture = 125 %
PRICES = {"claude-opus-5": (5.0, 25.0), "claude-fable-5-1": (10.0, 50.0), "claude-opus-5-5": (4.0, 20.0),
          "claude-sonnet-5": (2.0, 10.0)}

SYSTEM = """Tu es Éco 1, l'assistant économiste du « Projet Bourse », un logiciel personnel d'analyse boursière \
où des robots investissent de l'argent FICTIF (paper trading) avec des cours réels. Tu parles à son propriétaire, \
un particulier qui apprend à gérer ses finances. Réponds en français, clairement, sans jargon non expliqué.

Tes ressources :
- une bibliothèque de textes d'économistes d'écoles différentes (libéraux comme Charles Gave et l'Institut des \
Libertés, keynésiens comme Paul Krugman, hétérodoxes, banques centrales, spécialistes de la valorisation…). \
Commence par `catalogue_bibliotheque` si tu ne sais pas ce qu'elle contient. Cherche avec plusieurs requêtes \
courtes, en français ET en anglais (les sources sont dans les deux langues), puis lis en entier les articles \
les plus utiles avec `lire_article` ;
- les données du site : `etat_du_marche` (note de climat, signaux, placements suivis), `portefeuilles` \
(robots et portefeuille manuel), `alertes_recentes`, `historique_cours` ;
- le web, pour l'actualité récente ou un chiffre que ni la bibliothèque ni le site ne donnent.

Ta méthode :
1. Vérifie les faits et les chiffres avec les outils plutôt que de mémoire ; donne les dates.
2. Confronte les écoles : quand les économistes divergent, présente honnêtement chaque thèse et ses arguments, \
dis qui la défend et ce qui les départage. Distingue les faits, les prévisions et les opinions. Signale quand une \
prévision passée d'un auteur ne s'est pas réalisée si tu le sais.
3. Relie l'analyse à la situation concrète du site (climat, robots, placements) quand c'est pertinent.
4. Cite tes sources en liens Markdown [titre](url) avec l'auteur et la date.
5. Termine par l'essentiel en deux ou trois lignes, et ce que ça implique concrètement.

Garde-fous : tu n'es pas un conseiller en investissement agréé. Tu peux expliquer, comparer, proposer des pistes \
et des scénarios, mais rappelle les risques (perte en capital, levier, frais, horizon) et les principes de base \
(épargne de précaution, diversification, frais bas, horizon long) quand la question touche à l'argent réel. \
Ne promets jamais de rendement."""


def _schema(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required if required is not None else list(props),
            "additionalProperties": False}


TOOLS = [
    {"name": "catalogue_bibliotheque", "strict": True,
     "description": "Liste les sources de la bibliothèque : école de pensée, langue, nombre d'articles, "
                    "période couverte, principaux auteurs.",
     "input_schema": _schema({})},
    {"name": "chercher_bibliotheque", "strict": True,
     "description": "Recherche plein texte dans la bibliothèque d'économistes (accents ignorés, un mot au moins "
                    "doit figurer). Renvoie les passages les plus pertinents avec leur doc_id. Utiliser des "
                    "mots-clés courts, et refaire la recherche en anglais pour les sources anglophones.",
     "input_schema": _schema({
         "requete": {"type": "string", "description": "Mots-clés, ex. « inflation or dollar »"},
         "source": {"type": "string", "description": "Partie du nom d'une source pour filtrer, ou chaîne vide"},
         "nombre": {"type": "integer", "description": "Nombre de passages (1 à 15)"}})},
    {"name": "lire_article", "strict": True,
     "description": "Texte complet d'un article de la bibliothèque (doc_id donné par la recherche).",
     "input_schema": _schema({"doc_id": {"type": "integer"}})},
    {"name": "etat_du_marche", "strict": True,
     "description": "Analyse du marché du site, recalculée toutes les 15 minutes : note de climat (-100 tempête "
                    "à +100 optimisme), signaux (tendance, VIX, crédit, taux, dollar, cuivre/or, ton des "
                    "actualités), et tous les placements suivis (variations, tendance, RSI, élan, chute).",
     "input_schema": _schema({})},
    {"name": "portefeuilles", "strict": True,
     "description": "Les portefeuilles fictifs du site (robots et portefeuille manuel) : valeur, performance, "
                    "écart avec l'indice mondial, placements détenus, liquidités, dernière réflexion des robots.",
     "input_schema": _schema({})},
    {"name": "alertes_recentes", "strict": True,
     "description": "Alertes d'actualité détectées par la veille du site (score sur 100, sources, raisons).",
     "input_schema": _schema({"heures": {"type": "integer", "description": "Fenêtre en heures (1 à 336)"}})},
    {"name": "historique_cours", "strict": True,
     "description": "Historique d'un titre (code Yahoo Finance, ex. IUSQ.DE, ^GSPC, GC=F, EURUSD=X, MC.PA) : "
                    "variations, plus haut/bas, moyennes mobiles, volatilité et une série de points.",
     "input_schema": _schema({
         "ticker": {"type": "string"},
         "periode": {"type": "string", "enum": ["1mo", "6mo", "1y", "5y", "max"]}})},
]
WEB_TOOLS = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 5},
             {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 5}]
WEB_TOOLS_BASIC = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5},
                   {"type": "web_fetch_20250910", "name": "web_fetch", "max_uses": 5}]
STATUS = {"catalogue_bibliotheque": "📚 Consulte le catalogue de la bibliothèque",
          "chercher_bibliotheque": "🔎 Cherche dans la bibliothèque", "lire_article": "📖 Lit un article",
          "etat_du_marche": "🌡️ Étudie le climat du marché", "portefeuilles": "💼 Regarde les portefeuilles",
          "alertes_recentes": "🔔 Relit les alertes", "historique_cours": "📈 Étudie un cours",
          "web_search": "🌐 Cherche sur le web", "web_fetch": "🌐 Lit une page web"}


@dataclass
class SiteData:
    """Accès aux données du site (fonctions fournies par l'interface, qui ont leur propre cache)."""
    conn: sqlite3.Connection
    settings: dict
    market_view: Callable
    portfolios: Callable[[], list[dict]]


@dataclass
class Event:
    kind: str                  # reflexion / texte / outil / fin / erreur
    text: str = ""
    messages: list = field(default_factory=list)   # « fin » : messages à ajouter à l'historique
    cost: float = 0.0


# ---------------------------------------------------------------- outils

def _fmt(x: float, d: int = 1) -> str:
    return f"{x:+.{d}f}"


def _market(data: SiteData) -> str:
    mv = data.market_view()
    lines = [f"Analyse du {mv.time:%d/%m/%Y %H:%M} UTC. Note de climat {mv.risk_score:+d}/100 ({mv.mood}). "
             f"{mv.headline()}", "", "Signaux (lecture de -100 à +100, poids) :"]
    lines += [f"- {f.name} : {f.value * 100:+.0f} (poids {f.weight:g}) — {f.detail}" for f in mv.factors]
    lines += ["", "Placements suivis (variations en %) :",
              "nom | ticker | famille | levier | 1j | 5j | 1m | 3m | 6m | >moy200j | RSI | élan | vs plus haut 1 an"]
    for a in sorted(mv.assets.values(), key=lambda a: -a.sprint):
        lines.append(f"{a.name} | {a.ticker} | {a.family} | {a.leverage} | {_fmt(a.ret_1d)} | {_fmt(a.ret_5d)} | "
                     f"{_fmt(a.ret_1m)} | {_fmt(a.ret_3m)} | {_fmt(a.ret_6m)} | {'oui' if a.above_ma200 else 'non'} | "
                     f"{a.rsi:.0f} | {_fmt(a.sprint)} | {_fmt(a.drawdown)}")
    n = mv.news
    lines += ["", f"Actualités : {n.n_articles} articles lus en 24 h, {n.severe_share:.0%} au vocabulaire de crise"
              + (f" (habituellement {n.baseline_share:.0%})" if n.baseline_share is not None else "") + "."]
    return "\n".join(lines)


def _alerts(data: SiteData, hours: int) -> str:
    hours = max(1, min(hours, 336))
    since = (datetime.now(timezone.utc) - pd.Timedelta(hours=hours)).isoformat()
    rows = data.conn.execute("SELECT created, level, score, title_fr, title, sources, reasons, url FROM alerts"
                             " WHERE is_test = 0 AND created >= ? ORDER BY score DESC LIMIT 25", (since,)).fetchall()
    if not rows:
        return f"Aucune alerte ces {hours} dernières heures."
    return "\n\n".join(f"[{r['created'][:16]}] {r['level']} {r['score']}/100 : {r['title_fr'] or r['title']}\n"
                       f"Sources : {r['sources']}\nRaisons : {r['reasons']}\n{r['url']}" for r in rows)


def _prices(ticker: str, period: str) -> str:
    ticker = ticker.strip().upper()
    bars = history(ticker, period, "1d" if period in ("1mo", "6mo", "1y") else "1wk")
    if bars.empty:
        return f"Aucun cours trouvé pour {ticker}."
    c = bars["Close"].dropna()
    step = max(1, len(c) // 30)
    points = ", ".join(f"{t:%d/%m/%y} {v:.2f}" for t, v in c.iloc[::step].items())
    rets = c.pct_change().dropna()
    per_year = 252 if period in ("1mo", "6mo", "1y") else 52
    return (f"{ticker} ({name_of(ticker)}) sur {period} : dernier {c.iloc[-1]:.2f} le {c.index[-1]:%d/%m/%Y}, "
            f"variation {(c.iloc[-1] / c.iloc[0] - 1) * 100:+.1f} %, plus haut {c.max():.2f}, plus bas {c.min():.2f}, "
            f"écart au plus haut {(c.iloc[-1] / c.max() - 1) * 100:+.1f} %, moyenne 50 pts {c.iloc[-50:].mean():.2f}, "
            f"moyenne 200 pts {c.iloc[-200:].mean():.2f}, volatilité annualisée "
            f"{rets.std() * per_year ** 0.5 * 100:.1f} %.\nPoints : {points}")


def run_tool(name: str, args: dict, data: SiteData) -> str:
    if name == "catalogue_bibliotheque":
        return json.dumps(bibliotheque.catalogue(data.conn, data.settings), ensure_ascii=False)
    if name == "chercher_bibliotheque":
        hits = bibliotheque.search(data.conn, args["requete"], args.get("source", ""),
                                   max(1, min(int(args.get("nombre") or 8), 15)))
        if not hits:
            return "Aucun passage trouvé. Essayez d'autres mots-clés (synonymes, autre langue) ou sans filtre."
        return "\n\n---\n\n".join(
            f"doc_id {h['doc_id']} · {h['title']} · {h['author'] or 'auteur inconnu'} · {h['source']} · "
            f"{(h['published'] or '')[:10]} · {h['url']}\n{h['body']}" for h in hits)
    if name == "lire_article":
        doc = bibliotheque.document(data.conn, int(args["doc_id"]))
        if not doc:
            return "Article introuvable."
        text = doc["text"]
        if len(text) > MAX_ARTICLE_CHARS:
            text = text[:MAX_ARTICLE_CHARS] + "\n[… article tronqué]"
        return (f"{doc['title']}\n{doc['author'] or ''} · {doc['source']} · {(doc['published'] or '')[:10]} · "
                f"{doc['url']}\n\n{text}")
    if name == "etat_du_marche":
        return _market(data)
    if name == "portefeuilles":
        return json.dumps(data.portfolios(), ensure_ascii=False, default=str)
    if name == "alertes_recentes":
        return _alerts(data, int(args.get("heures") or 48))
    if name == "historique_cours":
        return _prices(args["ticker"], args["periode"])
    raise ValueError(f"outil inconnu : {name}")


# ---------------------------------------------------------------- boucle

def api_key(secrets: dict | None = None) -> str | None:
    import os
    return os.environ.get("ANTHROPIC_API_KEY") or (secrets or {}).get("ANTHROPIC_API_KEY")


def _echo(blocks: list[dict]) -> list[dict]:
    """Contenu à renvoyer à l'API. Après un passage de relais à un autre modèle (bloc « fallback »), les blocs
    internes du modèle qui a décliné, placés avant le relais, ne doivent pas être renvoyés."""
    last = max((i for i, b in enumerate(blocks) if b.get("type") == "fallback"), default=None)
    if last is None:
        return blocks
    paired = {b.get("tool_use_id") for b in blocks if b.get("type", "").endswith("_tool_result")}
    keep = []
    for i, b in enumerate(blocks):
        t = b.get("type")
        if i < last and (t in ("thinking", "redacted_thinking", "tool_use")
                         or (t == "server_tool_use" and b.get("id") not in paired)
                         or t not in ("text", "server_tool_use") and not t.endswith("_tool_result")):
            continue
        keep.append(b)
    return keep


def _cost(model: str, usage) -> float:
    price_in, price_out = PRICES.get(model, (5.0, 25.0))
    cached_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cached_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    return ((usage.input_tokens + cached_read * 0.1 + cached_write * 1.25) * price_in
            + usage.output_tokens * price_out) / 1e6


def ask(client: anthropic.Anthropic, model: str, effort: str, history: list[dict], data: SiteData,
        web: bool = True) -> Iterator[Event]:
    """Répond au dernier message de `history` (liste de messages API). Produit des événements pour l'affichage ;
    le dernier (« fin ») contient les messages à ajouter à l'historique."""
    system = SYSTEM + f"\n\nNous sommes le {datetime.now():%d/%m/%Y}."
    messages = list(history)
    new: list[dict] = []
    cost = 0.0
    # Options « maximum » d'abord ; si l'API en refuse une (modèle qui ne la gère pas), on retombe sur l'essentiel.
    full = {"tools": TOOLS + (WEB_TOOLS if web else []), "fallbacks": "default",
            "betas": ["server-side-fallback-2026-07-01"]}
    basic = {"tools": TOOLS + (WEB_TOOLS_BASIC if web else [])}
    options = full
    wrote = False           # du texte déjà écrit lors d'un tour précédent : on sépare les paragraphes
    for _ in range(MAX_TOOL_ROUNDS):
        fresh = True
        try:
            with client.beta.messages.stream(
                    model=model, max_tokens=64000, system=system, messages=messages + new,
                    thinking={"type": "adaptive", "display": "summarized"}, output_config={"effort": effort},
                    cache_control={"type": "ephemeral"}, **options) as stream:
                for event in stream:
                    if event.type == "thinking":
                        yield Event("reflexion", event.thinking)
                    elif event.type == "text":
                        yield Event("texte", ("\n\n" if wrote and fresh else "") + event.text)
                        fresh, wrote = False, True
                    elif event.type == "content_block_start" and event.content_block.type == "server_tool_use":
                        yield Event("outil", STATUS.get(event.content_block.name, event.content_block.name))
                final = stream.get_final_message()
        except anthropic.BadRequestError as exc:
            if options is full:
                log.warning("Options avancées refusées, nouvel essai sans : %s", exc)
                options = basic
                continue
            raise
        cost += _cost(model, final.usage)
        if final.stop_reason == "refusal":
            yield Event("erreur", "Éco 1 a décliné cette demande. Reformulez-la autrement.", cost=cost)
            return
        content = _echo([b.model_dump(mode="json", exclude_none=True) for b in final.content])
        if final.stop_reason == "max_tokens":        # un appel d'outil coupé net ne peut pas être exécuté
            content = [b for b in content if b.get("type") != "tool_use"] or [{"type": "text", "text": "…"}]
        new.append({"role": "assistant", "content": content})
        if final.stop_reason == "pause_turn":          # recherche web longue : on relance pour continuer
            continue
        kept = {b.get("id") for b in content if b.get("type") == "tool_use"}   # appels écartés par _echo exclus
        tool_uses = [b for b in final.content if b.type == "tool_use" and b.id in kept]
        if not tool_uses or final.stop_reason == "max_tokens":
            yield Event("fin", messages=new, cost=cost)
            return
        results = []
        for block in tool_uses:
            status = STATUS.get(block.name, block.name)
            detail = block.input.get("requete") or block.input.get("ticker") or ""
            yield Event("outil", f"{status}{f' : « {detail} »' if detail else ''}")
            try:
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": run_tool(block.name, block.input, data)})
            except Exception as exc:    # une donnée indisponible ne doit pas bloquer la réponse
                log.warning("Outil %s en échec : %s", block.name, exc)
                results.append({"type": "tool_result", "tool_use_id": block.id, "is_error": True,
                                "content": f"Erreur : {exc}"})
        new.append({"role": "user", "content": results})
    yield Event("erreur", "Éco 1 a atteint sa limite de recherches pour cette question.", messages=new, cost=cost)
