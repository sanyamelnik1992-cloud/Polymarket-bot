"""Does a move against the side over the last 10-30 s separate losers? (reports/59: not proven). usage: REPLAY_GRID=dir python reports/59_tools/trend_scan.py"""
import sys, os; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from base import *
g=load_all()
L4=L3
def plain(g, a_min=0.90, extra=None):
    ok=base_ok(g)&(g.p>=.9)
    if extra is not None: ok=ok&extra(g)
    return run(g,ok,tier_limit(g.p.values,L3),a_min=a_min)
print("== A. does 'moved against us' separate losers? entries of each rule bucketed by the feature (bp over the last N s, + = toward the strike)")
sets={"live rule":run(*live_rule(g)),"plain L3 (no floor, no pause)":plain(g),"L4 (asks from 0.50)":plain(g,0.5)}
for nm,e in sets.items():
    print(line(nm,e))
    for f in ("tr_bn10","tr_bn20","tr_bn30","tr_cl20","tr_cl30"):
        out=f"   {f}: "
        for lo,hi in ((-99,0),(0,1),(1,2),(2,3),(3,99)):
            x=e[(e[f]>=lo)&(e[f]<hi)]
            out+=f"[{lo},{hi}) {len(x)}/{int((~x.won).sum())} {x.pnl.sum():+.1f} | "
        print(out)
    for f in ("tr_bn20","tr_cl20","tr_cl30"):
        z=e[f]/e.sigma_bp.clip(lower=0.5)
        out=f"   {f}/sigma: "
        for lo,hi in ((-99,0),(0,2),(2,4),(4,6),(6,99)):
            x=e[(z>=lo)&(z<hi)]
            out+=f"[{lo},{hi}) {len(x)}/{int((~x.won).sum())} {x.pnl.sum():+.1f} | "
        print(out)
print("== B. the filter inside the rule (later steps of the window may still enter)")
for nm,mk in (("live rule",lambda ex: run(*live_rule(g,extra=ex))),("plain L3",lambda ex: plain(g,extra=ex)),("L4",lambda ex: plain(g,0.5,extra=ex))):
    print(line(nm+" as is",mk(None)))
    for f in ("tr_bn20","tr_cl20","tr_bn30","tr_cl30"):
        for thr in (1.5,2.0,2.5,3.0,4.0):
            print(line(f"  {nm}: {f} <= {thr} bp",mk(lambda G,f=f,thr=thr: G[f]<=thr)))
    for thr in (1.5,2.0,3.0):
        print(line(f"  {nm}: max(bn20,cl20) <= {thr}",mk(lambda G,thr=thr: np.maximum(G.tr_bn20,G.tr_cl20)<=thr)))
        print(line(f"  {nm}: max(bn20,bn30) <= {thr}",mk(lambda G,thr=thr: np.maximum(G.tr_bn20,G.tr_bn30)<=thr)))
