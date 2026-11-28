"""Money is an integer count of minor units plus an ISO 4217 currency. Never floats."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal

# Currencies whose minor unit is not 1/100. Everything else defaults to 2 decimals.
_EXPONENTS: dict[str, int] = {
    "BHD": 3,
    "CLP": 0,
    "IQD": 3,
    "ISK": 0,
    "JOD": 3,
    "JPY": 0,
    "KRW": 0,
    "KWD": 3,
    "LYD": 3,
    "OMR": 3,
    "PYG": 0,
    "TND": 3,
    "UGX": 0,
    "VND": 0,
    "XAF": 0,
    "XOF": 0,
}

_SYMBOLS: dict[str, str] = {
    "USD": "$",
    "EUR": "€",
    "GBP": "£",
    "INR": "₹",
    "JPY": "¥",
    "AUD": "A$",
    "CAD": "C$",
    "SGD": "S$",
    "AED": "AED ",
}


def exponent(currency: str) -> int:
    return _EXPONENTS.get(currency.upper(), 2)


@dataclass(frozen=True, slots=True)
class Money:
    minor: int
    currency: str

    def __post_init__(self) -> None:
        if len(self.currency) != 3 or not self.currency.isalpha():
            raise ValueError(f"invalid currency code: {self.currency!r}")
        object.__setattr__(self, "currency", self.currency.upper())

    @classmethod
    def from_major(cls, amount: Decimal | int | str, currency: str) -> Money:
        exp = exponent(currency)
        quantum = Decimal(1).scaleb(-exp)
        minor = Decimal(str(amount)).quantize(quantum, rounding=ROUND_HALF_EVEN) * (10**exp)
        return cls(int(minor), currency)

    @property
    def major(self) -> Decimal:
        return Decimal(self.minor).scaleb(-exponent(self.currency))

    def __add__(self, other: Money) -> Money:
        self._check(other)
        return Money(self.minor + other.minor, self.currency)

    def __sub__(self, other: Money) -> Money:
        self._check(other)
        return Money(self.minor - other.minor, self.currency)

    def _check(self, other: Money) -> None:
        if other.currency != self.currency:
            raise ValueError(f"currency mismatch: {self.currency} vs {other.currency}")

    def format(self) -> str:
        symbol = _SYMBOLS.get(self.currency)
        exp = exponent(self.currency)
        text = f"{self.major:,.{exp}f}"
        return f"{symbol}{text}" if symbol else f"{text} {self.currency}"


def micro_usd_to_usd(micro: int) -> Decimal:
    """Model-call costs are tracked in micro-dollars (1e-6 USD) to stay integral."""
    return Decimal(micro).scaleb(-6)


def format_money(amount_minor: int, currency: str) -> str:
    """A minor-unit amount as text, e.g. 150000 INR -> "INR 1,500.00"."""
    return Money(int(amount_minor), currency).format()
