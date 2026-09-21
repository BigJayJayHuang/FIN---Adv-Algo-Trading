"""
Local data tool (not graded, not needed on GitHub Pages).

    python3 local_server.py
    open http://127.0.0.1:8765/data.html

Serves this folder plus /api/status, a summary of raw_data.pkl (what LSEG actually returned).
On github.io the Data page shows "Data connection required" because this server is not there.
"""
import json
import os
import pickle
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from rics import parse_call_ric

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "raw_data.pkl")


def status():
    if not os.path.exists(RAW):
        return {"ok": False, "error": "raw_data.pkl not found. Run fetch_data.py first."}
    with open(RAW, "rb") as f:
        p = pickle.load(f)
    st = p["stock"]
    rows = []
    for ric, df in p["options"].items():
        info = parse_call_ric(ric)
        if not info:
            continue
        both = df["BID"].notna() & df["ASK"].notna()
        rows.append({"ric": ric, "expiry": info["expiry"].isoformat(), "strike": info["strike"], "bars": int(len(df)),
                     "quoted": int(both.sum()), "traded": int(df["TRDPRC_1"].notna().sum()),
                     "first": str(df.index.min()), "last": str(df.index.max())})
    rows.sort(key=lambda r: (r["expiry"], r["strike"]))
    return {"ok": True, "meta": {k: p["meta"].get(k) for k in ("source", "fetched_at", "interval", "stock_ric")},
            "stock_bars": int(len(st)), "stock_first": str(st.index.min()), "stock_last": str(st.index.max()),
            "tried": len(p.get("tried", [])), "rics": rows}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=HERE, **k)

    def do_GET(self):
        if self.path.split("?")[0] == "/api/status":
            body = json.dumps(status()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            super().do_GET()


if __name__ == "__main__":
    print("http://127.0.0.1:8765/data.html   (ctrl-c to stop)")
    ThreadingHTTPServer(("127.0.0.1", 8765), Handler).serve_forever()
