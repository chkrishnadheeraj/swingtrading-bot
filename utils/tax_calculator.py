"""
NSE Tax & Charges Calculator — Budget 2024 Ready
=================================================
Computes the full cost of a round-trip equity delivery trade on NSE:
  Buy-side:  STT (0.1%) + Stamp Duty (0.015%) + Txn Charge (0.00322%)
             + SEBI Fee (0.0001%) + GST 18% on fees
  Sell-side: STT (0.1%) + Txn Charge (0.00322%) + SEBI Fee (0.0001%)
             + GST 18% on fees

Then estimates Income Tax:
  BUSINESS mode → 30% slab + 4% cess = 31.2% effective
  STCG mode     → 20% flat + 4% cess = 20.8% effective

Tax is only applied to net profit (charges already deducted).
Losses get 0 tax — the bot does not model carry-forward.
"""

from config import settings

_T = settings.TAX_CONFIG


# ── NSE delivery charge rates ───────────────────────────────────────────────
_STT_RATE    = 0.001       # 0.1%  — both buy and sell for delivery
_STAMP_RATE  = 0.00015     # 0.015% — buy side only
_TXN_RATE    = 0.0000322   # 0.00322% — NSE transaction charge, both sides
_SEBI_RATE   = 0.000001    # 0.0001% — SEBI turnover fee, both sides
_GST_RATE    = 0.18        # 18% on (brokerage + txn charge + SEBI fee)


def calculate_charges(entry_price: float, exit_price: float, quantity: int) -> dict:
    """
    Returns a breakdown of all NSE delivery charges for one round-trip trade.

    Args:
        entry_price: Buy price per share (₹)
        exit_price:  Sell price per share (₹)
        quantity:    Number of shares

    Returns dict with:
        buy_charges, sell_charges, total_charges, breakdown (itemised)
    """
    buy_value  = entry_price * quantity
    sell_value = exit_price  * quantity
    brokerage  = _T["BROKERAGE_PER_ORDER"]

    # ── Buy side ──────────────────────────────────────────────────────────
    b_stt      = buy_value  * _STT_RATE
    b_stamp    = buy_value  * _STAMP_RATE
    b_txn      = buy_value  * _TXN_RATE
    b_sebi     = buy_value  * _SEBI_RATE
    b_gst      = (brokerage + b_txn + b_sebi) * _GST_RATE
    buy_total  = brokerage + b_stt + b_stamp + b_txn + b_sebi + b_gst

    # ── Sell side ─────────────────────────────────────────────────────────
    s_stt      = sell_value * _STT_RATE
    s_txn      = sell_value * _TXN_RATE
    s_sebi     = sell_value * _SEBI_RATE
    s_gst      = (brokerage + s_txn + s_sebi) * _GST_RATE
    sell_total = brokerage + s_stt + s_txn + s_sebi + s_gst

    total = buy_total + sell_total

    return {
        "buy_charges":   round(buy_total,  2),
        "sell_charges":  round(sell_total, 2),
        "total_charges": round(total,      2),
        "breakdown": {
            "stt":         round(b_stt  + s_stt,  2),
            "stamp_duty":  round(b_stamp,          2),
            "txn_charges": round(b_txn  + s_txn,  2),
            "sebi_fee":    round(b_sebi + s_sebi,  2),
            "gst":         round(b_gst  + s_gst,   2),
            "brokerage":   round(brokerage * 2,    2),
        },
    }


def calculate_tax(net_after_charges: float) -> dict:
    """
    Estimates income tax on net profit (after charges).
    Returns 0 tax for losses — carry-forward is not modelled here.

    Args:
        net_after_charges: Net P&L after all exchange charges (₹)

    Returns dict with tax_deducted, tax_type, effective_rate
    """
    if net_after_charges <= 0:
        return {
            "tax_type":       _T["TAX_TYPE"],
            "tax_deducted":   0.0,
            "effective_rate": 0.0,
        }

    rate       = _T["BUSINESS_SLAB"] if _T["TAX_TYPE"] == "BUSINESS" else _T["STCG_RATE"]
    tax_amount = net_after_charges * rate
    cess       = tax_amount * _T["CESS_RATE"]
    total_tax  = tax_amount + cess

    return {
        "tax_type":       _T["TAX_TYPE"],
        "tax_deducted":   round(total_tax, 2),
        "effective_rate": round(rate * (1 + _T["CESS_RATE"]), 4),
    }


def net_pnl(entry_price: float, exit_price: float, quantity: int) -> dict:
    """
    Full round-trip P&L breakdown for one trade.

    Returns:
        gross_pnl           — raw (exit - entry) * qty
        total_charges       — all NSE fees combined
        charges_breakdown   — itemised (STT, stamp, txn, SEBI, GST, brokerage)
        net_after_charges   — gross_pnl minus charges
        tax_deducted        — estimated income tax (0 for losses)
        tax_type            — "BUSINESS" or "STCG"
        effective_rate      — e.g. 0.312 for 30% + 4% cess
        net_in_hand         — what actually lands in your account
    """
    gross            = (exit_price - entry_price) * quantity
    charges          = calculate_charges(entry_price, exit_price, quantity)
    net_after        = gross - charges["total_charges"]
    tax              = calculate_tax(net_after)
    net_in_hand      = net_after - tax["tax_deducted"]

    return {
        "gross_pnl":          round(gross,         2),
        "total_charges":      charges["total_charges"],
        "charges_breakdown":  charges["breakdown"],
        "net_after_charges":  round(net_after,     2),
        "tax_deducted":       tax["tax_deducted"],
        "tax_type":           tax["tax_type"],
        "effective_rate":     tax["effective_rate"],
        "net_in_hand":        round(net_in_hand,   2),
    }
