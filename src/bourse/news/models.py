from dataclasses import dataclass
from datetime import datetime
from hashlib import sha1


@dataclass
class Article:
    """Un article d'actualité, quelle que soit sa source."""

    title: str
    url: str
    source: str
    published: datetime  # toujours en UTC
    summary: str = ""
    lang: str = "en"
    official: bool = False  # publié par une banque centrale

    @property
    def id(self) -> str:
        return sha1((self.url or self.title).encode("utf-8")).hexdigest()
