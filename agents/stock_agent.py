"""
stock_agent.py
──────────────
Stock Agent — fetches real-time stock data via yfinance.

Supported query types:
  price   → current price + today's change
  summary → price + market cap + 52-week range + volume

yfinance is synchronous so all calls run in a thread pool executor
to avoid blocking the async event loop.

Pakistani stocks use suffix .KA (e.g. OGDC.KA, PSO.KA, HBL.KA)
US stocks use plain ticker (e.g. AAPL, TSLA, GOOGL)
"""

import asyncio
import yfinance as yf


# ─────────────────────────────────────────────────────────────────────
#  FETCH HELPERS
# ─────────────────────────────────────────────────────────────────────

async def _fetch_stock(ticker_symbol: str) -> dict:
    """Fetch stock data in a thread pool to avoid blocking async loop."""
    loop = asyncio.get_event_loop()

    def _sync_fetch():
        ticker    = yf.Ticker(ticker_symbol)
        fast_info = ticker.fast_info
        try:
            full_info = ticker.info
            name      = full_info.get("longName") or full_info.get("shortName") or ticker_symbol
        except Exception:
            name = ticker_symbol
        return fast_info, name

    fast_info, name = await loop.run_in_executor(None, _sync_fetch)
    return {"fast_info": fast_info, "name": name}


# ─────────────────────────────────────────────────────────────────────
#  MAIN HANDLER
# ─────────────────────────────────────────────────────────────────────

async def handle(
    sender: str,
    intent: dict,
    user: dict,
    send_message
):
    """
    Main entry point called from query_agent.
    intent fields used:
      ticker           → stock ticker symbol (e.g. AAPL, OGDC.KA)
      stock_query_type → 'price' | 'summary'
    """
    ticker_symbol  = (intent.get("ticker") or intent.get("merchant") or "").upper().strip()
    query_type     = intent.get("stock_query_type", "price")
    currency_pref  = user.get("base_currency", "PKR")

    if not ticker_symbol:
        await send_message(sender,
            "Which stock would you like to check?\n\n"
            "Try:\n"
            "_show me stock AAPL_\n"
            "_Apple stock price_\n"
            "_OGDC.KA summary_\n"
            "_what is Tesla stock_\n\n"
            "💡 *Pakistani stocks* use .KA suffix:\n"
            "OGDC.KA · PSO.KA · HBL.KA · ENGRO.KA"
        )
        return

    await send_message(sender, f"_Fetching {ticker_symbol}..._")

    try:
        data      = await _fetch_stock(ticker_symbol)
        fast_info = data["fast_info"]
        name      = data["name"]
    except Exception as e:
        print(f"STOCK AGENT error: {e}")
        await send_message(sender,
            f"Couldn't fetch *{ticker_symbol}*.\n\n"
            "Make sure it's a valid ticker:\n"
            "• US stocks: AAPL, TSLA, GOOGL, MSFT\n"
            "• Pakistan: OGDC.KA, PSO.KA, HBL.KA"
        )
        return

    price = getattr(fast_info, "last_price", None)
    if not price:
        await send_message(sender,
            f"No price data found for *{ticker_symbol}*.\n"
            "Try the exact ticker symbol.")
        return

    prev_close = getattr(fast_info, "previous_close", price)
    change     = price - prev_close
    change_pct = (change / prev_close * 100) if prev_close else 0
    arrow      = "📈" if change >= 0 else "📉"
    sign       = "+" if change >= 0 else ""
    currency   = getattr(fast_info, "currency", "USD")

    if query_type == "summary":
        market_cap       = getattr(fast_info, "market_cap", None)
        fifty_two_low    = getattr(fast_info, "year_low", None)
        fifty_two_high   = getattr(fast_info, "year_high", None)
        volume           = getattr(fast_info, "three_month_average_volume", None)

        cap_str   = f"{currency} {market_cap/1e9:.2f}B" if market_cap else "N/A"
        range_str = (
            f"{currency} {fifty_two_low:,.2f} – {fifty_two_high:,.2f}"
            if fifty_two_low and fifty_two_high else "N/A"
        )
        vol_str = f"{volume/1e6:.1f}M" if volume else "N/A"

        message = (
            f"{arrow} *{ticker_symbol}* — {name}\n\n"
            f"💰 Price:       {currency} {price:,.2f}\n"
            f"📊 Change:      {sign}{currency} {abs(change):,.2f} ({sign}{change_pct:.2f}%)\n"
            f"🏦 Market Cap:  {cap_str}\n"
            f"📅 52-wk Range: {range_str}\n"
            f"📦 Avg Volume:  {vol_str}"
        )
    else:
        message = (
            f"{arrow} *{ticker_symbol}* — {name}\n\n"
            f"💰 {currency} {price:,.2f}\n"
            f"   {sign}{currency} {abs(change):,.2f}  ({sign}{change_pct:.2f}% today)"
        )

    await send_message(sender, message)