"""Traduction automatique (service gratuit Google Traduction, sans compte)."""
import logging
import time
from functools import lru_cache

from deep_translator import GoogleTranslator, MyMemoryTranslator

log = logging.getLogger(__name__)

PAUSE_SECONDS = 0.4  # le service gratuit refuse trop de demandes rapprochées
_last_call = 0.0


def translate(text: str, source_lang: str = "auto", target_lang: str = "fr") -> str:
    """Traduit un texte. En cas d'échec (pas d'internet…), renvoie le texte d'origine."""
    if not text or source_lang == target_lang:
        return text
    return _translate_cached(text, target_lang)


@lru_cache(maxsize=2048)
def _translate_cached(text: str, target_lang: str) -> str:
    # 1er choix : Google Traduction ; secours : MyMemory (limité à ~5000 caractères/jour)
    translators = [
        lambda: GoogleTranslator(source="auto", target=target_lang),
        lambda: MyMemoryTranslator(source="en-GB", target=f"{target_lang}-FR"),
    ]
    global _last_call
    error = None
    for make_translator in translators:
        time.sleep(max(0.0, _last_call + PAUSE_SECONDS - time.monotonic()))
        _last_call = time.monotonic()
        try:
            return make_translator().translate(text) or text
        except Exception as exc:
            error = exc
    log.warning("Traduction impossible : %s", str(error)[:80])
    return text
