"""Calibrate the bot's fair value against the book mid from the 1 Hz 'fv' journal trace.

Fits  P(Up) = sigmoid(a + b*logit(model) + c*logit(mid))  and reports log loss for model / mid / blends
and the best simple blend weight w in  w*model + (1-w)*mid.
usage: python scripts/calibrate_fv.py JOURNAL_RUN_DIR [JOURNAL_RUN_DIR ...]
"""
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pmbot.gamma import fetch_event, parse_event, slug_for  # noqa: E402

sys.path.insert(0, os.path.dirname(__file__))
from edge_test import fit_logreg, logit  # noqa: E402


def ll(p, y):
    p = np.clip(p, 1e-3, 1 - 1e-3)
    return float(-(y * np.log(p) + (1 - y) * np.log(1 - p)).mean())


if __name__ == "__main__":
    rows = []
    for d in sys.argv[1:]:
        for line in open(os.path.join(d, "events.jsonl")):
            e = json.loads(line)
            if e["type"] == "fv" and e.get("mid") is not None:
                rows.append((e["key"], e["rel"], e["p"], e["mid"]))
    df = pd.DataFrame(rows, columns=["key", "rel", "p", "mid"])
    win = {}
    for key in df.key.unique():
        a, s = key.split("-")
        ev = fetch_event(slug_for(a, int(s)))
        m = parse_event(ev) if ev else None
        if m and m["winner"]:
            win[key] = 1.0 if m["winner"] == "Up" else 0.0
    df["y"] = df.key.map(win)
    df = df.dropna(subset=["y"])
    df = df[(df.rel >= 5) & (df.rel < 295) & df.mid.between(0.02, 0.98)]
    print(f"windows {df.key.nunique()}, samples {len(df)}")
    df["tb"] = pd.cut(df.rel, [0, 60, 120, 180, 240, 300])
    print("log loss:  model {:.4f}   mid {:.4f}".format(ll(df.p, df.y), ll(df.mid, df.y)))
    for w in (0.25, 0.5, 0.75):
        print(f"  blend w={w}: {ll(w * df.p + (1 - w) * df.mid, df.y):.4f}")
    X = np.column_stack([logit(df.p.values), logit(df.mid.values)])
    coef = fit_logreg(X, df.y.values)
    print(f"logistic: a={coef[0]:+.3f}  b_model={coef[1]:+.3f}  c_mid={coef[2]:+.3f}")
    # overconfidence check: regress logit(y-ish) on logit(model) alone -> slope < 1 means the model is too extreme
    c1 = fit_logreg(logit(df.p.values)[:, None], df.y.values)
    print(f"model-only calibration slope: {c1[1]:.3f} (1.0 = calibrated, < 1 = overconfident)")
    print(df.groupby("tb", observed=True).apply(lambda x: pd.Series({
        "n": len(x), "ll_model": ll(x.p, x.y), "ll_mid": ll(x.mid, x.y),
        "ll_blend50": ll(0.5 * x.p + 0.5 * x.mid, x.y)})).round(4).to_string())
