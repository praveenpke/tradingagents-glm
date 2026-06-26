"""
Local dashboard server for the multi-agent stock analyst.

  python server.py        ->  http://localhost:4400

- Add tickers from the UI; each runs the full TradingAgents pipeline on GLM 5.2.
- Tiles show LIVE per-agent progress (Market -> Fundamentals -> Debate -> Trader -> Risk -> PM).
- Finished analyses are cached locally as markdown in reports/ and shown as tiles across restarts.
- Click a tile to read the full report.

No extra dependencies — stdlib http.server + threads. Markdown is rendered in the browser.
"""
import json
import os
import queue
import sys
import threading
import traceback
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"))
with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
    CONF = json.load(f)

LLM = CONF["llm"]
ANALYSTS = tuple(CONF["analysts"])
OUT_DIR = os.path.join(HERE, CONF.get("output_dir", "reports"))
STATE_FILE = os.path.join(HERE, "dashboard_state.json")
PORT = int(CONF.get("dashboard", {}).get("port", 4400))
MAX_WORKERS = int(CONF.get("dashboard", {}).get("max_workers", 2))

os.makedirs(OUT_DIR, exist_ok=True)

# ---- TradingAgents wiring (reliability patch: timeout + retries) ----
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG

_orig_kwargs = TradingAgentsGraph._get_provider_kwargs
def _patched_kwargs(self):
    k = _orig_kwargs(self)
    k.setdefault("timeout", float(CONF.get("request_timeout_seconds", 150)))
    k.setdefault("max_retries", int(CONF.get("max_retries", 3)))
    return k
TradingAgentsGraph._get_provider_kwargs = _patched_kwargs

def _base_config():
    cfg = DEFAULT_CONFIG.copy()
    cfg["llm_provider"] = LLM["provider"]
    cfg["deep_think_llm"] = LLM["deep_think_model"]
    cfg["quick_think_llm"] = LLM["quick_think_model"]
    cfg["backend_url"] = LLM.get("backend_url")
    cfg["max_debate_rounds"] = CONF.get("max_debate_rounds", 1)
    return cfg

# Pipeline stages shown on the tiles (in order). Each maps to a state key that appears
# once that agent has produced output.
STAGES = [
    ("Market Analyst", lambda s: bool(s.get("market_report"))),
    ("Fundamentals Analyst", lambda s: bool(s.get("fundamentals_report"))),
    ("Bull vs Bear Debate", lambda s: bool((s.get("investment_debate_state") or {}).get("bull_history"))),
    ("Research Manager", lambda s: bool((s.get("investment_debate_state") or {}).get("judge_decision")) or bool(s.get("investment_plan"))),
    ("Trader", lambda s: bool(s.get("trader_investment_plan"))),
    ("Risk Committee", lambda s: bool((s.get("risk_debate_state") or {}).get("history"))),
    ("Portfolio Manager", lambda s: bool(s.get("final_trade_decision"))),
]
STAGE_NAMES = [n for n, _ in STAGES]

def _current_stage(state):
    """Return (in-progress agent name, number of agents completed)."""
    completed = 0
    for name, done in STAGES:
        try:
            if done(state):
                completed += 1
        except Exception:
            pass
    in_progress = STAGE_NAMES[completed] if completed < len(STAGE_NAMES) else STAGE_NAMES[-1]
    return in_progress, completed

def _normalize_decision(text):
    t = (text or "").lower()
    if any(w in t for w in ["buy", "overweight", "bullish", "long", "accumulate"]):
        return "BUY"
    if any(w in t for w in ["sell", "underweight", "bearish", "short", "reduce", "exit"]):
        return "SELL"
    return "HOLD"

def _build_markdown(ticker, run_date, analysts, model, final_state, decision):
    def sub(d, k):
        return (d or {}).get(k, "") if isinstance(d, dict) else ""
    inv = final_state.get("investment_debate_state", {})
    risk = final_state.get("risk_debate_state", {})
    sections = [
        ("📈 Market / Technical Analysis", final_state.get("market_report")),
        ("💼 Fundamentals Analysis", final_state.get("fundamentals_report")),
        ("📰 News Analysis", final_state.get("news_report")),
        ("💬 Sentiment Analysis", final_state.get("sentiment_report")),
        ("🐂 Bull Researcher", sub(inv, "bull_history")),
        ("🐻 Bear Researcher", sub(inv, "bear_history")),
        ("⚖️ Research Manager — Verdict", sub(inv, "judge_decision") or final_state.get("investment_plan")),
        ("🧮 Trader — Investment Plan", final_state.get("trader_investment_plan")),
        ("🔥 Risk: Aggressive", sub(risk, "aggressive_history") or sub(risk, "risky_history")),
        ("🛡️ Risk: Conservative", sub(risk, "conservative_history") or sub(risk, "safe_history")),
        ("➖ Risk: Neutral", sub(risk, "neutral_history")),
        ("🏛️ Portfolio Manager — Final Trade Decision", sub(risk, "judge_decision") or final_state.get("final_trade_decision")),
    ]
    out = [f"# {ticker} — TradingAgents Report\n",
           f"**Date analyzed:** {run_date}  \n**Model:** {model} (GLM)  \n**Analysts:** {', '.join(analysts)}\n",
           f"## ✅ FINAL DECISION: {str(decision).strip()}\n\n---\n"]
    for title, body in sections:
        if body and str(body).strip():
            out.append(f"## {title}\n\n{str(body).strip()}\n\n---\n")
    return "\n".join(out)

def _summary_from_state(final_state):
    pm = (final_state.get("risk_debate_state") or {}).get("judge_decision") or final_state.get("final_trade_decision") or ""
    pm = " ".join(str(pm).split())
    return pm[:260]

# ---- Job registry ----
JOBS = {}          # key f"{ticker}|{date}" -> dict
LOCK = threading.Lock()
WORK = queue.Queue()

def _key(ticker, run_date):
    return f"{ticker.upper()}|{run_date}"

def _save_state():
    with LOCK:
        slim = {k: {kk: vv for kk, vv in v.items() if kk != "_state"} for k, v in JOBS.items()}
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(slim, f, indent=2)
    except Exception:
        pass

def _set(key, **fields):
    with LOCK:
        JOBS.setdefault(key, {})
        JOBS[key].update(fields)
    _save_state()

def _run_job(ticker, run_date):
    key = _key(ticker, run_date)
    _set(key, status="running", stage=STAGE_NAMES[0], stageIndex=0, error=None,
         startedAt=datetime.now().isoformat(timespec="seconds"))
    model = LLM["deep_think_model"]
    try:
        ta = TradingAgentsGraph(selected_analysts=ANALYSTS, debug=False, config=_base_config())
        final_state = None
        # Preferred: stream for live per-agent progress.
        try:
            past = ta.memory_log.get_past_context(ticker)
            instrument = ta.resolve_instrument_context(ticker, "stock")
            init = ta.propagator.create_initial_state(
                ticker, run_date, asset_type="stock",
                past_context=past, instrument_context=instrument)
            args = ta.propagator.get_graph_args()
            merged = {}
            for chunk in ta.graph.stream(init, **args):
                merged.update(chunk)
                name, idx = _current_stage(merged)
                _set(key, stage=name, stageIndex=idx)
            final_state = merged
            decision = ta.process_signal(final_state["final_trade_decision"])
        except Exception:
            # Fallback: blocking run (no live stage), still produces a full result.
            _set(key, stage="Analyzing", stageIndex=0)
            final_state, decision = ta.propagate(ticker, run_date)

        md = _build_markdown(ticker, run_date, ANALYSTS, model, final_state, decision)
        outfile = os.path.join(OUT_DIR, f"{ticker.upper()}_{run_date}.md")
        with open(outfile, "w", encoding="utf-8") as f:
            f.write(md)

        _set(key, status="done", stage="Portfolio Manager", stageIndex=len(STAGES),
             decision=str(decision).strip(), badge=_normalize_decision(str(decision)),
             summary=_summary_from_state(final_state),
             reportFile=os.path.basename(outfile),
             finishedAt=datetime.now().isoformat(timespec="seconds"))
    except Exception as e:
        _set(key, status="error", error=f"{type(e).__name__}: {e}".strip())
        traceback.print_exc()

def _worker():
    while True:
        ticker, run_date = WORK.get()
        try:
            _run_job(ticker, run_date)
        finally:
            WORK.task_done()

for _ in range(MAX_WORKERS):
    threading.Thread(target=_worker, daemon=True).start()

# ---- Load cached reports + prior state on startup ----
def _bootstrap():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                for k, v in json.load(f).items():
                    if v.get("status") == "running":
                        v["status"] = "error"; v["error"] = "interrupted (server restart)"
                    JOBS[k] = v
        except Exception:
            pass
    # Scan reports/ for any cached markdown not already tracked.
    for fn in os.listdir(OUT_DIR):
        if not fn.endswith(".md"):
            continue
        base = fn[:-3]
        if "_" not in base:
            continue
        ticker, run_date = base.rsplit("_", 1)
        key = _key(ticker, run_date)
        if key in JOBS and JOBS[key].get("status") == "done":
            continue
        try:
            txt = open(os.path.join(OUT_DIR, fn), encoding="utf-8").read()
        except Exception:
            continue
        decision = ""
        for line in txt.splitlines():
            if line.startswith("## ✅ FINAL DECISION:"):
                decision = line.split(":", 1)[1].strip()
                break
        JOBS[key] = {
            "ticker": ticker.upper(), "date": run_date, "status": "done",
            "stage": "Portfolio Manager", "stageIndex": len(STAGES),
            "decision": decision, "badge": _normalize_decision(decision),
            "summary": "", "reportFile": fn,
        }
    _save_state()

_bootstrap()

# ---- HTTP ----
DASH_HTML = os.path.join(HERE, "dashboard", "index.html")

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, (bytes, bytearray)) else (
            body if isinstance(body, str) else json.dumps(body)).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            try:
                return self._send(200, open(DASH_HTML, encoding="utf-8").read(),
                                  "text/html; charset=utf-8")
            except FileNotFoundError:
                return self._send(500, "dashboard/index.html missing", "text/plain")
        if u.path == "/api/jobs":
            with LOCK:
                jobs = [dict(v, key=k) for k, v in JOBS.items()]
            jobs.sort(key=lambda j: j.get("startedAt") or j.get("date") or "", reverse=True)
            return self._send(200, {"jobs": jobs, "stages": STAGE_NAMES,
                                    "model": LLM["deep_think_model"], "analysts": list(ANALYSTS),
                                    "maxWorkers": MAX_WORKERS})
        if u.path == "/api/report":
            q = parse_qs(u.query)
            fn = (q.get("file") or [""])[0]
            safe = os.path.basename(fn)
            path = os.path.join(OUT_DIR, safe)
            if safe.endswith(".md") and os.path.exists(path):
                return self._send(200, open(path, encoding="utf-8").read(),
                                  "text/markdown; charset=utf-8")
            return self._send(404, "report not found", "text/plain")
        return self._send(404, "not found", "text/plain")

    def do_POST(self):
        u = urlparse(self.path)
        b = self._body()
        if u.path == "/api/run":
            ticker = str(b.get("ticker", "")).strip().upper()
            run_date = str(b.get("date", "")).strip() or CONF.get("date") or date.today().isoformat()
            if not ticker or not ticker.replace(".", "").replace("-", "").isalnum():
                return self._send(400, {"error": "invalid ticker"})
            key = _key(ticker, run_date)
            with LOCK:
                cur = JOBS.get(key, {}).get("status")
            if cur in ("queued", "running"):
                return self._send(200, {"ok": True, "note": "already running"})
            _set(key, ticker=ticker, date=run_date, status="queued", stage="Queued",
                 stageIndex=0, decision="", badge="", summary="", error=None, reportFile=None)
            WORK.put((ticker, run_date))
            return self._send(200, {"ok": True, "key": key})
        if u.path == "/api/remove":
            key = str(b.get("key", ""))
            delete_file = bool(b.get("deleteReport"))
            with LOCK:
                job = JOBS.pop(key, None)
            if job and delete_file and job.get("reportFile"):
                try:
                    os.remove(os.path.join(OUT_DIR, job["reportFile"]))
                except Exception:
                    pass
            _save_state()
            return self._send(200, {"ok": True})
        return self._send(404, {"error": "not found"})

def main():
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"\n  ◎ Equity Observatory  →  http://localhost:{PORT}")
    print(f"  model: {LLM['deep_think_model']}  ·  analysts: {', '.join(ANALYSTS)}  ·  workers: {MAX_WORKERS}\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  shutting down…")
        srv.shutdown()

if __name__ == "__main__":
    main()
