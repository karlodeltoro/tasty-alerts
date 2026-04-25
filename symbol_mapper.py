"""
symbol_mapper.py — Maps between Tastytrade DXLink and Schwab OCC option symbols.

Tastytrade DXLink format:  ./ESZ25C5800:XCME
Schwab OCC format:         SPXW  251219C05800000

/ES futures options → SPXW/SPX equity options (same strike, same underlying price ~5800).
"""
from datetime import date

def build_schwab_option_symbol(strike: float, expiry_date: date, opt_type: str) -> str:
    """
    Build Schwab OCC option symbol for SPXW/SPX.
    Format: {SYMBOL:<6}{YYMMDD}{C|P}{STRIKE*1000:08d}
    Uses SPXW for DTE <= 45 (weeklies), SPX for > 45 (monthlies).
    /ES and SPX trade at same price level — strikes map 1:1.
    """
    dte = (expiry_date - date.today()).days
    underlying = "SPXW" if dte <= 45 else "SPX"
    exp_str = expiry_date.strftime("%y%m%d")
    strike_int = int(round(strike * 1000))
    return f"{underlying:<6}{exp_str}{opt_type}{strike_int:08d}"

def tt_meta_to_schwab_symbol(meta: dict) -> str | None:
    """Convert Tastytrade contract metadata dict → Schwab OCC symbol."""
    try:
        strike   = float(meta['strike'])
        expiry   = meta['expiry_date']
        opt_type = 'C' if meta.get('is_call') else 'P'
        if not isinstance(expiry, date):
            return None
        return build_schwab_option_symbol(strike, expiry, opt_type)
    except (KeyError, TypeError, ValueError):
        return None
