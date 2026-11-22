"""Bewertungskennzahlen. Reine Funktionen; ungültige Basis (<=0) ergibt None statt Unsinn."""
from __future__ import annotations


def market_cap(price: float | None, shares: float | None) -> float | None:
    if price is None or shares is None or shares <= 0:
        return None
    return price * shares


def pe_ratio(price: float | None, eps_ttm: float | None) -> float | None:
    if price is None or eps_ttm is None or eps_ttm <= 0:
        return None  # bei Verlust ist ein KGV nicht aussagekräftig
    return price / eps_ttm


def ps_ratio(cap: float | None, revenue_ttm: float | None) -> float | None:
    if cap is None or revenue_ttm is None or revenue_ttm <= 0:
        return None
    return cap / revenue_ttm


def p_fcf_ratio(cap: float | None, fcf_ttm: float | None) -> float | None:
    if cap is None or fcf_ttm is None or fcf_ttm <= 0:
        return None
    return cap / fcf_ttm


def price_change(history: list[tuple], days: int | None = None, ytd: bool = False) -> float | None:
    """history: aufsteigend sortierte (date, close). Änderung gegenüber dem letzten Kurs
    vor dem Stichtag (days zurück bzw. Jahresanfang). None, wenn die Historie nicht reicht."""
    if not history:
        return None
    last_day, last = history[-1]
    if ytd:
        base_candidates = [c for d, c in history if d.year < last_day.year]
    else:
        from datetime import timedelta
        cutoff = last_day - timedelta(days=days or 0)
        if history[0][0] > cutoff:
            return None
        base_candidates = [c for d, c in history if d <= cutoff]
    if not base_candidates or base_candidates[-1] <= 0:
        return None
    return last / base_candidates[-1] - 1
