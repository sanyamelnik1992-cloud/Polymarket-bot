import json, subprocess, time
def get(u, tries=3, t=12):
    for i in range(tries):
        try:
            out = subprocess.run(["curl", "-s", "-m", str(t), "-A", "research-client", u], capture_output=True, timeout=t + 5).stdout
            if out: return json.loads(out)
        except Exception:
            pass
        time.sleep(0.5 * (i + 1))
    return None
