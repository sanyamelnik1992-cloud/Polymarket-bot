"""Polymarket taker fee (fee schedule v2): fee = C * rate * p * (1 - p), makers pay 0."""
from .config import CRYPTO_FEE_RATE


def taker_fee(shares: float, price: float, rate: float = CRYPTO_FEE_RATE) -> float:
    """Fee in USDC for a taker fill of `shares` at `price` (rounded to 5 dp like the protocol)."""
    return round(shares * rate * price * (1.0 - price), 5)


def fee_per_share(price: float, rate: float = CRYPTO_FEE_RATE) -> float:
    return rate * price * (1.0 - price)


def breakeven_prob(price: float, rate: float = CRYPTO_FEE_RATE) -> float:
    """Win probability needed for a taker BUY at `price` to break even after fees."""
    return price + fee_per_share(price, rate)


def maker_rebate_equiv(shares: float, price: float, rate: float = CRYPTO_FEE_RATE) -> float:
    """Fee-equivalent a maker fill generates; the maker gets rebate_share * (own / total) of the pool."""
    return shares * rate * price * (1.0 - price)
