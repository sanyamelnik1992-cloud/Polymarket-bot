"""Exit on a reversal (sell at our side's best bid once it falls to X) vs holding, on the entries of early_books.py
(early_books_res.json: each entry has the bid path, one value a second, from the entry to the end). 10 shares an entry."""
import json, sys
R = json.load(open(sys.argv[1] if len(sys.argv) > 1 else "early_books_res.json"))
fee = lambda p: 0.07 * p * (1 - p)
for key, ent in R.items():
    base = sum(10 * ((1.0 if e["won"] else 0.0) - e["px"] - fee(e["px"])) for e in ent)
    print(f"\n{key}: {len(ent)} entries, hold to the end ${base:+.2f}")
    for X in (0.9, 0.8, 0.7, 0.5, 0.3):
        tot = stops = false_stops = 0
        for e in ent:
            sell = next((b for b in e["path"] if 0 < b <= X), None)
            if sell is None:
                tot += 10 * ((1.0 if e["won"] else 0.0) - e["px"] - fee(e["px"]))
            else:
                stops += 1; false_stops += e["won"]
                tot += 10 * (sell - fee(sell) - e["px"] - fee(e["px"]))
        print(f"   sell when our bid <= {X}: ${tot:+.2f}  stops {stops} (the side still won: {false_stops})")
