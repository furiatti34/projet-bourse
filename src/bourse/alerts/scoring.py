"""Évaluation de l'importance d'un sujet d'actualité (score de 0 à 100).

Le logiciel regroupe les articles qui parlent du même sujet, puis combine
plusieurs indices pour juger de l'importance :
  1. la gravité du vocabulaire (crise, krach, intervention, sanctions…) ;
  2. le nombre de médias différents qui en parlent ;
  3. une réaction inhabituelle du marché lié (ex. : le yen si l'article parle du Japon) ;
  4. une source officielle (banque centrale) ;
  5. le lien avec l'économie (un sujet purement politique compte moins).
"""
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache

from bourse.news.models import Article

from .market import Move

# --- Vocabulaire (sans accents, en minuscules). « * » final = début de mot. ---
SEVERE_TERMS = [
    "crash*", "krach", "crisis", "crise*", "default*", "defaut de paiement", "collapse*", "effondr*",
    "war", "wars", "guerre*", "invasion", "invad*", "missile*", "nuclear", "nucleaire*",
    "emergency", "urgence", "bank run", "panic*", "panique", "contagion", "intervention*",
    "interven*", "sanction*", "embargo*", "blockade", "blocus", "coup d'etat", "bankrupt*",
    "faillite*", "bailout*", "sauvetage", "recession*", "plunge*", "plong*", "plummet*",
    "freefall", "chute libre", "record low", "plus bas historique", "devaluation*",
    "capital control*", "hyperinflation", "shutdown", "airstrike*", "frappe*", "attaque*",
]
MODERATE_TERMS = [
    "tariff*", "droits de douane", "surtax*", "rate hike*", "hausse des taux", "rate cut*",
    "baisse des taux", "inflation", "volatil*", "sell-off", "selloff", "tumbl*", "slump*",
    "sink*", "soar*", "surg*", "flamb*", "envol*", "record high", "downgrad*", "degrad*",
    "tension*", "escalat*", "escalade", "threat*", "menace*", "warn*", "alert*", "alarm*",
    "turmoil", "rout", "bond market", "sovereign", "debt", "dette*", "deficit*", "stimulus",
    "relance", "strike*", "greve*", "protest*", "election*", "slowdown", "ralentissement",
]
ECONOMIC_TERMS = [
    "econom*", "market*", "marche*", "stock*", "bourse*", "share*", "action*", "currenc*",
    "devise*", "monnaie*", "yen", "dollar*", "euro*", "yuan", "rate*", "taux", "inflation",
    "trade", "commerc*", "export*", "import*", "tariff*", "oil", "petrol*", "gas", "gaz",
    "bank*", "banque*", "bond*", "obligat*", "debt", "dette*", "gdp", "pib", "growth",
    "croissance", "investor*", "investisseur*", "price*", "prix", "supply", "chip*",
    "semiconductor*", "energ*", "fed", "ecb", "bce", "boj", "opec", "opep", "wall street",
    "nasdaq", "nikkei", "index", "indice*", "recession*", "budget*", "fiscal*", "sanction*",
    "embargo*", "yield*", "rendement*", "commodit*", "matieres premieres",
]

STOPWORDS = set(
    "the and for with from that this are was were has have its into over after amid says said "
    "new will would could about more than what when how why who not but just their they his her "
    "les des une pour par sur dans avec qui que est sont aux ses son leur plus pas face apres "
    "selon cette entre comme fait etre".split()
)


def normalize(text: str) -> str:
    """Minuscules, sans accents, apostrophes unifiées."""
    text = unicodedata.normalize("NFKD", text.replace("’", "'"))
    return "".join(c for c in text if not unicodedata.combining(c)).lower()


def compile_terms(terms: list[str]) -> list[tuple[str, re.Pattern]]:
    compiled = []
    for term in terms:
        word = normalize(term)
        if word.endswith("*"):
            pattern = r"(?<![a-z0-9])" + re.escape(word[:-1])
        else:
            pattern = r"(?<![a-z0-9])" + re.escape(word) + r"(?![a-z0-9])"
        compiled.append((word.rstrip("*"), re.compile(pattern)))
    return compiled


@lru_cache
def _keyword_patterns(keywords: tuple[str, ...]) -> list[tuple[str, re.Pattern]]:
    return compile_terms(list(keywords))


_SEVERE = compile_terms(SEVERE_TERMS)
_MODERATE = compile_terms(MODERATE_TERMS)
_ECONOMIC = compile_terms(ECONOMIC_TERMS)


def matches(text: str, compiled: list[tuple[str, re.Pattern]]) -> list[str]:
    return [word for word, pattern in compiled if pattern.search(text)]


def tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", normalize(text)) if len(w) > 2 and w not in STOPWORDS}


def same_story(a: set[str], b: set[str]) -> bool:
    """Deux titres parlent-ils du même sujet ? (mots en commun)"""
    if not a or not b:
        return False
    common = len(a & b)
    return common / len(a | b) >= 0.4 or (common >= 3 and common / min(len(a), len(b)) >= 0.5)


# --- Regroupement des articles en « sujets » ---

@dataclass
class Story:
    articles: list[Article]
    score: int = 0
    level: str = ""
    reasons: list[str] = field(default_factory=list)
    severe_terms: list[str] = field(default_factory=list)  # vocabulaire de crise détecté
    moves: list[Move] = field(default_factory=list)        # marchés liés au sujet
    title_fr: str = ""                                     # rempli lors de l'alerte

    @property
    def published(self) -> datetime:
        """Heure de la première publication sur ce sujet."""
        return min(a.published for a in self.articles)

    @property
    def lead(self) -> Article:
        """Article représentatif : une source officielle en priorité, sinon le plus ancien."""
        official = [a for a in self.articles if a.official]
        return official[0] if official else min(self.articles, key=lambda a: a.published)

    @property
    def sources(self) -> list[str]:
        return sorted({a.source for a in self.articles})


def cluster(articles: list[Article]) -> list[Story]:
    groups: list[tuple[list[Article], list[set[str]]]] = []
    for article in sorted(articles, key=lambda a: a.published):
        tok = tokens(article.title)
        for members, member_tokens in groups:
            if any(same_story(tok, t) for t in member_tokens):
                members.append(article)
                member_tokens.append(tok)
                break
        else:
            groups.append(([article], [tok]))
    return [Story(members) for members, _ in groups]


# --- Calcul du score ---

def score_story(story: Story, moves: dict[str, Move], strong_threshold: int = 70,
                alert_threshold: int = 50) -> Story:
    text = normalize(" ".join(f"{a.title} {a.summary}" for a in story.articles))
    reasons = []

    # 1. Gravité du vocabulaire (max 35)
    severe, moderate = matches(text, _SEVERE), matches(text, _MODERATE)
    gravity = min(35, 15 * len(severe) + 7 * len(moderate))
    if severe or moderate:
        reasons.append("Vocabulaire : " + ", ".join((severe + moderate)[:5]))

    # 2. Couverture médiatique (max 30)
    n_sources = len(story.sources)
    coverage = {1: 0, 2: 10, 3: 18, 4: 24}.get(n_sources, 30)
    if n_sources > 1:
        reasons.append(f"Couvert par {n_sources} médias différents")

    # 3. Réaction du marché lié (max 30)
    market = 0
    linked = [m for m in moves.values() if m.keywords and matches(text, _keyword_patterns(tuple(m.keywords)))]
    unusual = sorted((m for m in linked if m.unusual), key=lambda m: -abs(m.zscore))
    if unusual:
        best = unusual[0]
        market = 25 if abs(best.zscore) >= 3 else 15
        reasons.append(f"{best.name} : {best.change_pct:+.2f} % aujourd'hui "
                       f"({abs(best.zscore):.1f}× un mouvement normal)")
    vix = moves.get("^VIX")
    if vix and vix.zscore >= 2:
        market += 5
        reasons.append(f"Nervosité générale des marchés (VIX {vix.change_pct:+.1f} %)")
    market = min(market, 30)

    # 4. Source officielle
    official = 10 if any(a.official for a in story.articles) else 0
    if official:
        reasons.append("Source officielle : " + story.lead.source)

    score = gravity + coverage + market + official

    # 5. Lien avec l'économie : sinon le sujet compte moitié moins
    if not matches(text, _ECONOMIC) and not linked:
        score //= 2
        reasons.append("Peu de lien direct avec l'économie")

    story.score = min(100, score)
    story.level = ("FORTE" if story.score >= strong_threshold
                   else "IMPORTANTE" if story.score >= alert_threshold else "")
    story.reasons = reasons
    story.severe_terms = severe
    story.moves = linked
    return story
