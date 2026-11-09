"""CPU / memory profile of bot processes (paper only, public feeds). usage: PROF_CWD=<repo dir> python reports/48_tools/prof.py WARM MEAS name=cmd [name=cmd ...]
Each cmd runs from the worktree dir. CPU% = utime+stime delta over MEAS seconds as a share of ONE core; RSS in MB."""
import os, shlex, signal, subprocess, sys, time
warm, meas = float(sys.argv[1]), float(sys.argv[2])
cwd = os.environ["PROF_CWD"]
CLK = os.sysconf("SC_CLK_TCK")
def cpu(pid):
    try:
        f = open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()
        return (int(f[11]) + int(f[12])) / CLK, int(f[11]) / CLK, int(f[12]) / CLK
    except Exception:
        return None
def rss(pid):
    try:
        for l in open(f"/proc/{pid}/status"):
            if l.startswith("VmRSS"): return int(l.split()[1]) / 1024
    except Exception:
        return None
procs = {}
for a in sys.argv[3:]:
    name, cmd = a.split("=", 1)
    procs[name] = subprocess.Popen(shlex.split(cmd), cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
time.sleep(warm)
c0 = {n: cpu(p.pid) for n, p in procs.items()}; t0 = time.time()
time.sleep(meas)
c1 = {n: cpu(p.pid) for n, p in procs.items()}; dt = time.time() - t0
print(f"{'процесс':14s} {'CPU % ядра':>10s} {'user':>6s} {'sys':>6s} {'RSS МБ':>7s}  (за {dt:.0f} с после прогрева {warm:.0f} с)")
tot = 0.0
for n, p in procs.items():
    if c0[n] and c1[n]:
        d = [(b - a) / dt * 100 for a, b in zip(c0[n], c1[n])]
        tot += d[0]
        print(f"{n:14s} {d[0]:10.1f} {d[1]:6.1f} {d[2]:6.1f} {rss(p.pid) or 0:7.0f}")
    else:
        print(f"{n:14s} процесс завершился")
print(f"{'ИТОГО':14s} {tot:10.1f}")
for p in procs.values():
    try: os.killpg(p.pid, signal.SIGINT)
    except Exception: pass
time.sleep(3)
for p in procs.values():
    try: os.killpg(p.pid, signal.SIGKILL)
    except Exception: pass
