"""How often the live daily stop (-$5) fires for a lock rule: Monte Carlo of one UTC day of entries.
A loss costs ~$4.6 (5 shares at ~0.92), a win pays ~$0.34 (L3 replay average), ~90 entries a day.
usage: daystop_mc.py [ENTRIES_PER_DAY] [WIN_USD] [LOSS_USD]"""
import random
import sys

n = int(sys.argv[1]) if len(sys.argv) > 1 else 90
win = float(sys.argv[2]) if len(sys.argv) > 2 else 0.34
loss = float(sys.argv[3]) if len(sys.argv) > 3 else 4.6
random.seed(7)


def day(p_loss, stop):
    c = 0.0
    for _ in range(n):
        c += -loss if random.random() < p_loss else win
        if c <= -stop:
            return True, c
    return False, c


print(f"{n} entries/day, win +${win}, loss -${loss}; break-even loss rate {win / (win + loss):.1%}")
for p in (0.02, 0.032, 0.04, 0.05, 0.06, 0.08):
    for stop in (5.0, 10.0):
        r = [day(p, stop) for _ in range(40000)]
        print(f"loss rate {p:5.1%}  stop -${stop:<4.0f}  days stopped {sum(s for s, _ in r) / len(r):6.1%}  "
              f"mean day {sum(c for _, c in r) / len(r):+6.2f}$")
