"""Mode « Gratuit » d'Éco 1 : un modèle d'IA qui tourne sur ce PC (Ollama), sans clé ni frais.

Mêmes outils que le mode Claude (bibliothèque d'économistes, données du site), sauf la recherche web.
Plus lent et moins fin qu'un grand modèle, mais entièrement gratuit et hors ligne (hors cours de Bourse).
"""
import json
import logging
import re
from typing import Iterator

import requests

from bourse.eco import bibliotheque
from bourse.eco.agent import STATUS, SYSTEM, TOOLS, Event, SiteData, run_tool

log = logging.getLogger(__name__)

OLLAMA = "http://localhost:11434"
MAX_TOOL_ROUNDS = 5
MAX_RESULT_CHARS = 3500      # sur ce PC, chaque caractère à lire coûte du temps
CONTEXT_TOKENS = 16384
WEB_TOOLS = [  # web gratuit, sans clé : moteur DuckDuckGo (paquet ddgs) + lecture directe des pages
    {"name": "chercher_web", "description": "Recherche sur tout le web (presse, blogs, sites officiels, New York "
                                            "Times, Les Échos…). Renvoie titres, adresses et extraits.",
     "input_schema": {"type": "object", "properties": {"requete": {"type": "string"}}, "required": ["requete"]}},
    {"name": "actualites", "description": "Articles de presse récents (derniers jours) sur un sujet, avec leur date.",
     "input_schema": {"type": "object", "properties": {"requete": {"type": "string"}}, "required": ["requete"]}},
    {"name": "lire_page", "description": "Lit le texte d'une page web à partir de son adresse (après une recherche).",
     "input_schema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}},
]
LOCAL_TOOLS = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                  "parameters": t["input_schema"]}} for t in TOOLS + WEB_TOOLS]
LOCAL_SYSTEM = SYSTEM + ("\n\nTu tournes sur le PC du propriétaire. Pour le web, utilise `chercher_web`, `actualites` "
                         "et `lire_page` (tout site est permis). Fais au plus 4 appels d'outils avant de répondre, "
                         "avec des mots-clés précis. Cite tes sources en lien Markdown [titre](url), jamais par un "
                         "doc_id. N'invente jamais d'adresse : uniquement celles données par les extraits et les outils.")
STATUS = {**STATUS, "chercher_web": "🌐 Cherche sur le web", "actualites": "📰 Lit l'actualité",
          "lire_page": "🌐 Lit une page web"}


def _web(name: str, args: dict) -> str:
    from ddgs import DDGS   # importé ici : seul le mode gratuit en a besoin
    if name == "chercher_web":
        results = DDGS().text(args["requete"], region="fr-fr", max_results=8)
        return "\n\n".join(f"[{r['title']}]({r['href']})\n{r.get('body', '')}" for r in results) or "Aucun résultat."
    if name == "actualites":
        results = DDGS().news(args["requete"], region="fr-fr", max_results=8)
        return "\n\n".join(f"[{r['title']}]({r['url']}) · {r.get('source', '')} · {(r.get('date') or '')[:10]}\n"
                           f"{r.get('body', '')}" for r in results) or "Aucun article."
    session = requests.Session()
    session.headers["User-Agent"] = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                     "(KHTML, like Gecko) Chrome/140.0 Safari/537.36")
    try:
        text, author = bibliotheque._page_text(args["url"], session)
    except requests.RequestException as exc:
        return (f"Page illisible ({exc}). Beaucoup de journaux (New York Times…) bloquent la lecture automatique : "
                "appuie-toi sur l'extrait de la recherche ou cherche le même sujet ailleurs.")
    return f"{args['url']}{f' · {author}' if author else ''}\n\n{text}" if text else "Page vide."
LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")


def _drop_invented_links(text: str, known: str) -> str:
    """Un petit modèle invente parfois des liens : on ne garde que ceux qu'il a vraiment lus."""
    return LINK_RE.sub(lambda m: m.group(0) if m.group(2) in known else m.group(1), text)


def available(model: str) -> tuple[bool, str]:
    """Ollama est-il lancé, et le modèle téléchargé ?"""
    try:
        names = [m["name"] for m in requests.get(f"{OLLAMA}/api/tags", timeout=3).json().get("models", [])]
    except requests.RequestException:
        return False, "Ollama n'est pas lancé (menu Démarrer → Ollama)."
    if model not in names:
        return False, f"Le modèle {model} n'est pas téléchargé : ouvrez un terminal et tapez « ollama pull {model} »."
    return True, ""


def ask_local(model: str, history: list[dict], data: SiteData) -> Iterator[Event]:
    """Même contrat que agent.ask : des événements, puis « fin » avec les messages à ajouter à l'historique."""
    # Un petit modèle cherche moins bien : on lui donne d'emblée les passages les plus proches de la question.
    question = history[-1]["content"] if isinstance(history[-1]["content"], str) else ""
    data_tools = {t["name"] for t in TOOLS}
    source = bibliotheque.detect_source(question, data.settings["eco1"]["sources"])
    hits = bibliotheque.search(data.conn, question, limit=5)
    if source:   # auteur cité : ses textes en premier, puis ceux de toute la bibliothèque
        focused = " ".join(w for w in question.split()
                           if w.strip("?,.!«»").lower() not in {x.lower() for x in source.split()})
        own = bibliotheque.search(data.conn, focused, source=source, limit=3)
        hits = own + [h for h in hits if h["doc_id"] not in {o["doc_id"] for o in own}][:3]
    new: list[dict] = []
    if hits:
        yield Event("outil", f"📚 {len(hits)} passages trouvés d'emblée dans la bibliothèque")
        extracts = "\n\n---\n\n".join(
            f"[{h['title']}]({h['url']}) · {h['author'] or 'auteur inconnu'} · {h['source']} · "
            f"{(h['published'] or '')[:10]}\n{h['body'][:900]}" for h in hits)
        new.append({"role": "system", "content": "Extraits de la bibliothèque proches de la question (cite-les par "
                                                 "leur lien s'ils servent ; cherche davantage si besoin) :\n\n"
                                                 + extracts})
    wrote, answer = False, ""
    seen: set[str] = set()      # un petit modèle refait parfois la même recherche en boucle
    for round_ in range(MAX_TOOL_ROUNDS):
        last_round = round_ == MAX_TOOL_ROUNDS - 1 or len(seen) >= 4
        text, thinking, calls = "", "", []
        with requests.post(f"{OLLAMA}/api/chat", stream=True, timeout=(5, 600), json={
                "model": model, "stream": True, "think": False,   # réfléchir = 2× plus lent
                "tools": [] if last_round else LOCAL_TOOLS,     # dernier tour : il doit répondre
                "options": {"num_ctx": CONTEXT_TOKENS, "temperature": 0.4},
                "messages": [{"role": "system", "content": LOCAL_SYSTEM}] + history + new
                + ([{"role": "system", "content": "Tu as assez cherché : réponds maintenant à la question avec ce "
                                                  "que tu as trouvé."}] if last_round and new else [])}) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line:
                    continue
                chunk = json.loads(line)
                if chunk.get("error"):
                    raise RuntimeError(chunk["error"])
                msg = chunk.get("message", {})
                if msg.get("thinking"):
                    thinking += msg["thinking"]
                    yield Event("reflexion", msg["thinking"])
                if msg.get("content"):
                    piece = ("\n\n" if wrote and not text else "") + msg["content"]
                    text += msg["content"]
                    answer += piece
                    wrote = True
                    yield Event("texte", piece)
                calls += msg.get("tool_calls") or []
        new.append({"role": "assistant", "content": text, **({"tool_calls": calls} if calls else {})})
        if not calls:
            known = "\n".join(m["content"] for m in new if m["role"] in ("system", "tool"))
            cleaned = _drop_invented_links(answer, known)
            if cleaned != answer:
                yield Event("corrige", cleaned)
                new[-1]["content"] = _drop_invented_links(text, known)
            yield Event("fin", messages=new)
            return
        for call in calls:
            name, args = call["function"]["name"], call["function"].get("arguments") or {}
            if isinstance(args, str):
                args = json.loads(args or "{}")
            detail = args.get("requete") or args.get("ticker") or ""
            yield Event("outil", f"{STATUS.get(name, name)}{f' : « {detail} »' if detail else ''}")
            signature = json.dumps([name, args], sort_keys=True, ensure_ascii=False)
            if signature in seen:
                new.append({"role": "tool", "tool_name": name,
                            "content": "Déjà fait, même résultat que plus haut. Change de mots-clés ou réponds."})
                continue
            seen.add(signature)
            try:
                result = _web(name, args) if name in STATUS and name not in data_tools else run_tool(name, args, data)
            except Exception as exc:    # un petit modèle se trompe parfois d'argument : on lui explique
                log.warning("Outil local %s en échec : %s", name, exc)
                result = f"Erreur : {exc}"
            if len(result) > MAX_RESULT_CHARS:
                result = result[:MAX_RESULT_CHARS] + "\n[… tronqué]"
            new.append({"role": "tool", "tool_name": name, "content": result})
    yield Event("erreur", "Éco 1 a atteint sa limite de recherches pour cette question.", messages=new)
