import pandas as pd, numpy as np, re, collections
d = pd.read_pickle("gross.pkl")
z = d[d.comp == 0].copy()
print("zero-comp markets", len(z), "pool", round(z.rate.sum()))
print("pool size quantiles 10/50/90/99:", np.round(z.rate.quantile([.1, .5, .9, .99]).values, 1))
print("days to end quantiles 10/25/50/75/90:", np.round(z.days.quantile([.1, .25, .5, .75, .9]).values, 1))
print("price in [0.10,0.90]:", int(((z.p >= .1) & (z.p <= .9)).sum()), " price <0.03 or >0.97:", int(((z.p < .03) | (z.p > .97)).sum()))
print("spread quantiles:", np.round(z.spread.quantile([.1, .25, .5, .75, .9]).values, 3), " vol24==0:", int((z.vol == 0).sum()))
print("created (date) top:", collections.Counter(str(c)[:10] for c in z.created).most_common(8))
def cat(q):
    q = q.lower()
    rules = [("crypto up/down", r"up or down"), ("sports game", r"\bvs\.?\b|o/u|spread:|handicap|game \d|map \d|\bset \d|match|moneyline|win on 20"),
             ("temperature/weather", r"temperature|°|rain|snow|wind|hurricane|precipitation"), ("price target", r"\bprice of\b|\bhit\b.*\$|above \$|below \$|close above|close below|market cap|fdv|valuation|reach \$|dip to"),
             ("econ data", r"inflation|gdp|cpi|unemployment|rate|fed |yield|payroll|jobs"), ("election/politics", r"election|nominat|president|senate|governor|party|seats|vote|primary|minister|parliament"),
             ("culture", r"netflix|billboard|oscar|box office|album|song|movie|show|grammy|spotify|youtube|tweet|post"), ("tech/ai", r"gpt|gemini|claude|openai|anthropic|model|release|launch|ipo"),
             ("geopolitics", r"ceasefire|russia|ukraine|iran|israel|china|war|strike|invade|blockade")]
    for name, rx in rules:
        if re.search(rx, q): return name
    return "other"
for name, df in (("ZERO comp", z), ("comp > 0", d[d.comp > 0])):
    df = df.assign(cat=df.q.map(cat))
    g = df.groupby("cat").agg(n=("rate", "size"), pool=("rate", "sum"), med_rate=("rate", "median"), med_days=("days", "median"), med_spread=("spread", "median"), med_vol=("vol", "median"), inrange=("p", lambda s: ((s >= .1) & (s <= .9)).mean()))
    print("\n", name); print(g.sort_values("pool", ascending=False).round(2).to_string())
zz = z[(z.p >= .1) & (z.p <= .9) & (z.days > 2)].sort_values("rate", ascending=False)
print("\nzero-comp, price 0.10-0.90, > 2 days left:", len(zz), "pool", round(zz.rate.sum()))
pd.set_option("display.width", 250)
print(zz.head(40)[["rate", "v", "ms", "p", "spread", "vol", "chg", "days", "q"]].assign(q=zz.q.str[:75]).round(3).to_string(index=False))
