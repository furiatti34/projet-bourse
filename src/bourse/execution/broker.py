"""Modèle commun de courtier : les stratégies ne connaissent que ces méthodes.
Le même code de stratégie pourra donc tourner en paper trading et, plus tard, en backtest."""
from abc import ABC, abstractmethod
from dataclasses import dataclass

BUY, SELL = "ACHAT", "VENTE"


@dataclass
class Holding:
    ticker: str
    quantity: float
    cost_eur: float  # prix de revient total, frais inclus


class Broker(ABC):
    @abstractmethod
    def place_order(self, ticker: str, side: str, quantity: float | None = None,
                    amount_eur: float | None = None, reason: str = "") -> int:
        """Passe un ordre au marché. Sans quantité ni montant : tout le cash (achat)
        ou toute la position (vente)."""

    @abstractmethod
    def cash(self) -> float: ...

    @abstractmethod
    def holdings(self) -> dict[str, Holding]: ...

    @abstractmethod
    def total_value(self) -> float: ...
