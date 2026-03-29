"""
options_math.py — Black-76 delta e volatilidad implícita para opciones de futuros.

Black-76 es la variante de Black-Scholes para futuros: usa el precio del futuro F
en lugar del precio spot y asume r=0 en el descuento (simplificación válida para
opciones sobre futuros que liquidan al precio del futuro).
"""
import math


def _norm_cdf(x: float) -> float:
    """Distribución normal acumulada, implementada con math.erfc (sin scipy)."""
    return 0.5 * math.erfc(-x / math.sqrt(2))


def black76_delta(F: float, K: float, T: float, sigma: float, is_call: bool) -> float:
    """
    Delta Black-76 de una opción de futuro.

    F     : precio actual del futuro subyacente
    K     : precio de ejercicio (strike)
    T     : tiempo a expiración en años (fraccional)
    sigma : volatilidad implícita anualizada (ej. 0.20 = 20%)
    is_call: True = call, False = put

    Retorna:
      call → delta ∈ (0, 1)
      put  → delta ∈ (-1, 0)
    """
    if T <= 0 or sigma <= 0:
        # En expiración: delta binario
        if is_call:
            return 1.0 if F > K else 0.0
        else:
            return -1.0 if F < K else 0.0
    d1 = (math.log(F / K) + 0.5 * sigma ** 2 * T) / (sigma * math.sqrt(T))
    return _norm_cdf(d1) if is_call else _norm_cdf(d1) - 1.0


def _black76_price(F: float, K: float, T: float, sigma: float, is_call: bool) -> float:
    """Precio teórico Black-76 (r=0; válido para opciones que liquidan al futuro)."""
    if T <= 0 or sigma <= 0:
        return max(F - K, 0.0) if is_call else max(K - F, 0.0)
    d1 = (math.log(F / K) + 0.5 * sigma ** 2 * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if is_call:
        return F * _norm_cdf(d1) - K * _norm_cdf(d2)
    else:
        return K * _norm_cdf(-d2) - F * _norm_cdf(-d1)


def implied_vol(
    F: float,
    K: float,
    T: float,
    market_price: float,
    is_call: bool,
    fallback: float = 0.20,
    max_iter: int = 60,
    tol: float = 0.01,
) -> float:
    """
    Volatilidad implícita estimada por bisección sobre el modelo Black-76.

    tol está en unidades de precio (0.01 = $0.01, apropiado para ES y GC futures options).
    Retorna `fallback` si:
      - Los inputs son inválidos (T<=0, precio<=0, etc.)
      - El precio de mercado ≤ valor intrínseco (opción deep ITM sin tiempo)
      - La búsqueda no converge a una solución en [0.001, 10.0]
    """
    if T <= 0 or market_price <= 0 or F <= 0 or K <= 0:
        return fallback

    intrinsic = max(F - K, 0.0) if is_call else max(K - F, 0.0)
    if market_price <= intrinsic:
        return fallback

    low, high = 0.001, 10.0

    # Verificar que el precio está dentro del rango alcanzable
    if _black76_price(F, K, T, low, is_call) > market_price:
        return fallback
    if _black76_price(F, K, T, high, is_call) < market_price:
        return fallback

    for _ in range(max_iter):
        mid = (low + high) / 2.0
        diff = _black76_price(F, K, T, mid, is_call) - market_price
        if abs(diff) < tol:
            return mid
        if diff < 0:
            low = mid
        else:
            high = mid

    result = (low + high) / 2.0
    return result if 0.001 <= result <= 10.0 else fallback
