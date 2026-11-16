import json, time, urllib.parse
from lib import get
Q = ["10.0 or above earthquake", "9.0 or above earthquake", "Jesus Christ return", "Lai Ching-te out", "moon landing in 2026", "US recession 2026",
     "acquire part of Greenland", "Trump out as President before 2027", "aliens exist", "Trump endorse JD Vance", "Reza Pahlavi lead Iran", "Epstein confirmed alive", "Apple new product line", "replace SHA-256", "Satoshi move any Bitcoin"]
for q in Q:
    d = get("https://gamma-api.polymarket.com/public-search?q=" + urllib.parse.quote(q) + "&limit_per_type=4&events_status=active")
    hits = []
    for e in (d or {}).get("events", [])[:4]:
        for m in e.get("markets", [])[:6]:
            if m.get("closed"): continue
            hits.append((m.get("question"), m.get("bestBid"), m.get("bestAsk"), m.get("endDate", "")[:10], round(float(m.get("liquidityNum") or m.get("liquidity") or 0)), m.get("slug"), json.loads(m.get("clobTokenIds") or "[]")[:1]))
    print("==", q)
    for h in hits[:5]: print("   ", h[0], "| bid", h[1], "ask", h[2], "| end", h[3], "| liq $", h[4])
    time.sleep(0.2)
