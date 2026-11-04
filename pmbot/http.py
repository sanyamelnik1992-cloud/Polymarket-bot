import time
import requests

_session = requests.Session()
_session.headers["User-Agent"] = "pmbot-research/0.1"


def get_json(url, params=None, retries=6, timeout=20):
    delay = 1.0
    for i in range(retries):
        try:
            r = _session.get(url, params=params, timeout=timeout)
            if r.status_code == 429 or r.status_code >= 500:
                ra = r.headers.get("Retry-After")
                time.sleep(float(ra) if ra else delay)
                delay = min(delay * 2, 30)
                continue
            r.raise_for_status()
            return r.json()
        except (requests.ConnectionError, requests.Timeout):
            time.sleep(delay)
            delay = min(delay * 2, 30)
    raise RuntimeError(f"GET failed after {retries} retries: {url} {params}")
