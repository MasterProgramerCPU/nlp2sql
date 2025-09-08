from __future__ import annotations
import os, json, textwrap, time
from typing import Optional, Dict, Any, List

import requests
import psycopg
from fastapi import FastAPI, Form, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# ───────────────────────── Config ─────────────────────────
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
# Notă: MODEL rămâne pentru compat, dar apelurile aleg modelul real din resolve_model(...)
MODEL       = os.getenv("OLLAMA_MODEL", "qwen2.5:3b-instruct")
NUM_CTX     = int(os.getenv("NUM_CTX", "8192"))
OAI_BUDGET_SECONDS = float(os.getenv("PGTUNER_AI_TIMEOUT", "20"))
NUM_PREDICT = int(os.getenv("PGTUNER_NUM_PREDICT", "256"))
MAX_ROWS_PER_TABLE = 200

# requests fără proxy din env (ca să nu te bage printr-un proxy)
SESSION = requests.Session()
SESSION.trust_env = False

# ... (imports și configurile tale rămân) ...
app = FastAPI(title="PG Config Tuner")

BASE_DIR = os.path.dirname(__file__)

# IMPORTANT: ca la NL→SQL — subapp-ul își montează propriul /static
# astfel încât resursele se servesc la /apps/pg-tuner/static/...
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")

# Șabloane locale sub apps/pg_tuner/templates
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

STATE: Dict[str, Any] = {"dsn": None, "hw": None}

# ───────────────────────── Helpers comune ─────────────────────────
def safe_json(obj: Any, limit: int | None = None) -> str:
    s = json.dumps(obj, default=str, ensure_ascii=False)
    return s if limit is None else s[:limit]

def try_hosts() -> List[str]:
    env_host = os.getenv("OLLAMA_HOST", "").rstrip("/")
    cand: List[str] = []
    if env_host:
        cand.append(env_host)
    for h in ("http://127.0.0.1:11434", "http://localhost:11434"):
        if h not in cand:
            cand.append(h)
    return cand

def first_alive_host(timeout: float = 1.5) -> Optional[str]:
    # încercăm mai întâi API-ul Ollama nativ
    for base in try_hosts():
        try:
            r = SESSION.get(f"{base}/api/tags", timeout=timeout)
            if r.ok:
                return base
        except Exception:
            continue
    # apoi API-ul OpenAI-compat (unele build-uri expun doar /v1/*)
    for base in try_hosts():
        try:
            r = SESSION.get(f"{base}/v1/models", timeout=timeout)
            if r.ok:
                return base
        except Exception:
            continue
    return None

def join_messages_as_prompt(messages: List[Dict[str, str]]) -> str:
    system = "\n".join(m["content"] for m in messages if m.get("role") == "system").strip()
    convo: List[str] = []
    for m in messages:
        role = m.get("role")
        if role == "system":
            continue
        label = "User" if role == "user" else "Assistant"
        convo.append(f"{label}: {m.get('content','')}")
    joined = "\n".join(convo).strip()
    return f"System: {system}\n{joined}\nAssistant:" if system else f"{joined}\nAssistant:"

# ───────────────────────── Modele Ollama ─────────────────────────
def list_models(base: str, timeout: float = 2.0) -> List[str]:
    # Ollama native
    try:
        r = SESSION.get(f"{base}/api/tags", timeout=timeout)
        if r.ok:
            data = r.json() or {}
            return [m.get("name") for m in data.get("models", []) if m.get("name")]
    except Exception:
        pass
    # OpenAI-compat
    try:
        r = SESSION.get(f"{base}/v1/models", timeout=timeout)
        if r.ok:
            data = r.json() or {}
            return [m.get("id") for m in data.get("data", []) if m.get("id")]
    except Exception:
        pass
    return []

PREFERRED_MODELS = [
    "qwen2.5:3b-instruct",
    "llama3.2:3b-instruct",
    "mistral:7b-instruct",
    "phi3:3.8b-mini-instruct",
    "qwen2.5:1.5b-instruct",
]

def resolve_model(base: str) -> Optional[str]:
    """Alege un model existent (din env sau preferințe), altfel primul model instalat."""
    env_model = os.getenv("OLLAMA_MODEL", "").strip()
    models = list_models(base)
    if not models:
        return None  # nu există modele instalate
    if env_model and env_model in models:
        return env_model
    for m in PREFERRED_MODELS:
        if m in models:
            return m
    return models[0]

# ───────────────────────── AI caller cu 4 fallback-uri ─────────────────
def call_ai(messages, stream: bool, num_predict: int, num_ctx: int, budget_seconds: float) -> str:
    """
    Încearcă pe rând:
      1) /api/chat
      2) /api/generate
      3) /v1/chat/completions (OpenAI compat)
      4) /v1/completions
    Streaming + tăiere după budget_seconds.
    Alege automat un model existent din Ollama.
    """
    base = first_alive_host(timeout=1.5)
    if not base:
        return "(AI indisponibil) Niciun endpoint nu a răspuns: " + ", ".join(try_hosts())

    model = resolve_model(base)
    if not model:
        return "(AI indisponibil) Nu există niciun model instalat în Ollama. Rulează, de ex.:\n" \
               "  ollama pull qwen2.5:3b-instruct\n" \
               "sau setează OLLAMA_MODEL către un model existent."

    opts = {"num_ctx": num_ctx, "temperature": 0.2, "num_predict": num_predict}

    # 1) /api/chat
    try:
        body = {"model": model, "messages": messages, "options": opts, "stream": stream}
        if stream:
            t0 = time.perf_counter(); out: List[str] = []
            with SESSION.post(f"{base}/api/chat", json=body, stream=True, timeout=(2, budget_seconds+5)) as r:
                if r.status_code == 404:
                    raise requests.HTTPError("404", response=r)
                r.raise_for_status()
                for line in r.iter_lines(decode_unicode=True):
                    if not line:
                        if (time.perf_counter()-t0) > budget_seconds: break
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        if line.startswith("data:"):
                            try: obj = json.loads(line[5:].strip())
                            except Exception: obj = {}
                        else:
                            obj = {}
                    delta = (obj.get("message") or {}).get("content")
                    if delta: out.append(delta)
                    if (time.perf_counter()-t0) > budget_seconds: break
            txt = "".join(out).strip()
            if not txt: raise RuntimeError("gol")
            if (time.perf_counter()-t0) > budget_seconds:
                txt += "\n\n_(trunchiat pentru a păstra UI-ul responsiv)_"
            return txt
        else:
            r = SESSION.post(f"{base}/api/chat", json=body, timeout=(2, budget_seconds))
            if r.status_code == 404:
                raise requests.HTTPError("404", response=r)
            r.raise_for_status()
            txt = r.json().get("message",{}).get("content","").strip()
            if not txt: raise RuntimeError("gol")
            return txt
    except Exception:
        pass

    # 2) /api/generate
    try:
        prompt = join_messages_as_prompt(messages)
        body = {"model": model, "prompt": prompt, "options": opts, "stream": stream}
        if stream:
            t0 = time.perf_counter(); out: List[str] = []
            with SESSION.post(f"{base}/api/generate", json=body, stream=True, timeout=(2, budget_seconds+5)) as r:
                if r.status_code == 404:
                    raise requests.HTTPError("404", response=r)
                r.raise_for_status()
                for line in r.iter_lines(decode_unicode=True):
                    if not line:
                        if (time.perf_counter()-t0) > budget_seconds: break
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        if line.startswith("data:"):
                            try: obj = json.loads(line[5:].strip())
                            except Exception: obj = {}
                        else:
                            obj = {}
                    delta = obj.get("response")
                    if delta: out.append(delta)
                    if (time.perf_counter()-t0) > budget_seconds: break
            txt = "".join(out).strip()
            if not txt: raise RuntimeError("gol")
            if (time.perf_counter()-t0) > budget_seconds:
                txt += "\n\n_(trunchiat pentru a păstra UI-ul responsiv)_"
            return txt
        else:
            r = SESSION.post(f"{base}/api/generate", json=body, timeout=(2, budget_seconds))
            if r.status_code == 404:
                raise requests.HTTPError("404", response=r)
            r.raise_for_status()
            txt = r.json().get("response","").strip()
            if not txt: raise RuntimeError("gol")
            return txt
    except Exception:
        pass

    # 3) /v1/chat/completions (OpenAI compat)
    try:
        body = {"model": model, "messages": messages, "stream": stream, "temperature": 0.2, "max_tokens": num_predict}
        if stream:
            t0 = time.perf_counter(); out: List[str] = []
            with SESSION.post(f"{base}/v1/chat/completions", json=body, stream=True, timeout=(2, budget_seconds+5)) as r:
                if r.status_code == 404:
                    raise requests.HTTPError("404", response=r)
                r.raise_for_status()
                for line in r.iter_lines(decode_unicode=True):
                    if not line or line.strip() == "" or line.strip() == "data: [DONE]":
                        if (time.perf_counter()-t0) > budget_seconds: break
                        continue
                    if line.startswith("data:"):
                        line = line[5:].strip()
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    choices = obj.get("choices") or []
                    if choices:
                        delta = (choices[0].get("delta") or {}).get("content")
                        if delta: out.append(delta)
                    if (time.perf_counter()-t0) > budget_seconds: break
            txt = "".join(out).strip()
            if not txt: raise RuntimeError("gol")
            if (time.perf_counter()-t0) > budget_seconds:
                txt += "\n\n_(trunchiat pentru a păstra UI-ul responsiv)_"
            return txt
        else:
            r = SESSION.post(f"{base}/v1/chat/completions", json=body, timeout=(2, budget_seconds))
            if r.status_code == 404:
                raise requests.HTTPError("404", response=r)
            r.raise_for_status()
            data = r.json()
            txt = (((data.get("choices") or [{}])[0]).get("message") or {}).get("content","").strip()
            if not txt: raise RuntimeError("gol")
            return txt
    except Exception:
        pass

    # 4) /v1/completions (prompt text simplu)
    try:
        prompt = join_messages_as_prompt(messages)
        body = {"model": model, "prompt": prompt, "stream": stream, "temperature": 0.2, "max_tokens": num_predict}
        if stream:
            t0 = time.perf_counter(); out: List[str] = []
            with SESSION.post(f"{base}/v1/completions", json=body, stream=True, timeout=(2, budget_seconds+5)) as r:
                r.raise_for_status()
                for line in r.iter_lines(decode_unicode=True):
                    if not line or line.strip() == "" or line.strip() == "data: [DONE]":
                        if (time.perf_counter()-t0) > budget_seconds: break
                        continue
                    if line.startswith("data:"):
                        line = line[5:].strip()
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    choices = obj.get("choices") or []
                    if choices:
                        delta = choices[0].get("text")
                        if delta: out.append(delta)
                    if (time.perf_counter()-t0) > budget_seconds: break
            txt = "".join(out).strip()
            if not txt: return "(AI răspuns gol)"
            if (time.perf_counter()-t0) > budget_seconds:
                txt += "\n\n_(trunchiat pentru a păstra UI-ul responsiv)_"
            return txt
        else:
            r = SESSION.post(f"{base}/v1/completions", json=body, timeout=(2, budget_seconds))
            r.raise_for_status()
            data = r.json()
            txt = (((data.get("choices") or [{}])[0]).get("text") or "").strip()
            return txt or "(AI răspuns gol)"
    except Exception as e:
        return f"(AI indisponibil) {e}"

# ───────────────────────── PG colectare ─────────────────────────
def fetch_settings_and_stats(dsn: str) -> Dict[str, Any]:
    q_settings = """
      SELECT name, setting, unit, vartype, boot_val, context, short_desc
      FROM pg_settings ORDER BY name;
    """
    q_version = "SHOW server_version_num;"
    q_bgwriter = """
      SELECT checkpoints_timed, checkpoints_req, checkpoint_write_time, checkpoint_sync_time,
             buffers_checkpoint, buffers_clean, maxwritten_clean, buffers_backend,
             buffers_backend_fsync, buffers_alloc
      FROM pg_stat_bgwriter;
    """
    q_db = """
      SELECT datname, numbackends, blks_read, blks_hit,
             tup_returned, tup_fetched, temp_files, temp_bytes,
             deadlocks, blk_read_time, blk_write_time, stats_reset
      FROM pg_stat_database ORDER BY datname;
    """
    q_io = "SELECT * FROM pg_stat_io"

    info: Dict[str, Any] = {}
    with psycopg.connect(dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(q_version)
            info["server_version_num"] = int(cur.fetchone()[0])

            cur.execute(q_settings)
            cols = [d[0] for d in cur.description]
            keep = {
                "max_connections","shared_buffers","effective_cache_size","work_mem",
                "maintenance_work_mem","checkpoint_completion_target",
                "random_page_cost","effective_io_concurrency",
                "synchronous_commit","wal_compression",
                "autovacuum","autovacuum_naptime","autovacuum_vacuum_cost_limit","autovacuum_vacuum_cost_delay",
            }
            info["settings"] = [dict(zip(cols, r)) for r in cur.fetchall() if r[0] in keep]

            cur.execute(q_bgwriter)
            cols = [d[0] for d in cur.description]
            info["bgwriter"] = [dict(zip(cols, r)) for r in cur.fetchall()]

            cur.execute(q_db)
            cols = [d[0] for d in cur.description]
            info["dbstats"] = [dict(zip(cols, r)) for r in cur.fetchall()[:MAX_ROWS_PER_TABLE]]

            try:
                cur.execute(q_io)
                cols = [d[0] for d in cur.description]
                info["io"] = [dict(zip(cols, r)) for r in cur.fetchall()[:MAX_ROWS_PER_TABLE]]
            except Exception:
                info["io"] = []

    # derivate
    try:
        hits = sum(float(d.get("blks_hit", 0) or 0) for d in info["dbstats"])
        reads = sum(float(d.get("blks_read", 0) or 0) for d in info["dbstats"])
        info["derived_hit_ratio"] = (hits / max(1.0, hits + reads))
    except Exception:
        info["derived_hit_ratio"] = None

    try:
        bg = info["bgwriter"][0] if info["bgwriter"] else {}
        timed = float(bg.get("checkpoints_timed", 0) or 0)
        req   = float(bg.get("checkpoints_req", 0)   or 0)
        info["derived_checkpoints_req_pct"] = (req / max(1.0, timed + req))
    except Exception:
        info["derived_checkpoints_req_pct"] = None

    return info

def pct(val, p) -> int:
    try:
        return int(float(val) * p)
    except Exception:
        return 0

def basic_tune(hw: Dict[str, Any], goal: str, settings: List[Dict[str, Any]], version: int) -> List[Dict[str, str]]:
    ram = int(hw.get("ram_bytes", 8 * 1024**3))
    cores = int(hw.get("cpu_cores", 4))
    ssd = bool(hw.get("ssd", True))

    max_conns = 100
    for s in settings:
        if s.get("name") == "max_connections":
            try:
                max_conns = int(s.get("setting", 100))
            except Exception:
                pass

    def fmt_bytes(b: int) -> str:
        for u, sz in [("TB", 1024**4), ("GB", 1024**3), ("MB", 1024**2), ("kB", 1024)]:
            if b >= sz:
                return f"{int(round(b/sz))}{u}"
        return str(b)

    sb  = min(pct(ram, 0.25), 8 * 1024**3) if version <= 120000 else pct(ram, 0.25)
    ecs = pct(ram, 0.70)
    mwm = min(2 * 1024**3, pct(ram, 0.05)) if goal.lower() == "oltp" else min(4 * 1024**3, pct(ram, 0.10))
    conc_factor = 2 if goal.lower() == "oltp" else 1
    wm  = max(2 * 1024**2, pct(ram, 0.02) // max(10, max_conns // conc_factor))
    rpc = 1.1 if ssd else 3.0
    eioc = 200 if ssd else 2
    cct = 0.9

    recs = [
        {"name":"shared_buffers",               "value": fmt_bytes(sb),  "reason":"~25% RAM"},
        {"name":"effective_cache_size",         "value": fmt_bytes(ecs), "reason":"~70% RAM; ghid planner"},
        {"name":"maintenance_work_mem",         "value": fmt_bytes(mwm), "reason":"VACUUM/CREATE INDEX mai rapid"},
        {"name":"work_mem",                     "value": fmt_bytes(wm),  "reason":"per sort/hash; ajustează vs concurență"},
        {"name":"checkpoint_completion_target", "value": f"{cct}",       "reason":"întinde I/O pe checkpoint"},
        {"name":"random_page_cost",             "value": f"{rpc}",       "reason":"SSD — random I/O mai ieftin"},
        {"name":"effective_io_concurrency",     "value": f"{eioc}",      "reason":"mai mult paralelism I/O pe SSD"},
    ]
    if goal.lower() in ("olap", "mix"):
        recs += [
            {"name":"max_worker_processes",            "value": f"{max(cores, 8)}",            "reason":"paralelism server"},
            {"name":"max_parallel_workers",            "value": f"{max(cores, 8)}",            "reason":"limita totală paralelă"},
            {"name":"max_parallel_workers_per_gather", "value": f"{max(2, min(cores//2, 8))}", "reason":"paralelism per gather"},
        ]
    return recs

def build_ai_payload(goal: str, hw: Dict[str, Any], stats: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "goal": goal,
        "hardware": hw,
        "version": stats.get("server_version_num"),
        "derived": {
            "hit_ratio": stats.get("derived_hit_ratio"),
            "checkpoints_req_pct": stats.get("derived_checkpoints_req_pct"),
        },
        "settings": { s["name"]: s["setting"] for s in stats.get("settings", []) },
        "bgwriter": stats.get("bgwriter", [])[:5],
        "dbstats":  [
            {
                "datname": d.get("datname"),
                "numbackends": d.get("numbackends"),
                "blks_read": d.get("blks_read"),
                "blks_hit": d.get("blks_hit"),
                "temp_files": d.get("temp_files"),
                "temp_bytes": d.get("temp_bytes"),
                "deadlocks": d.get("deadlocks"),
                "blk_read_time": d.get("blk_read_time"),
                "blk_write_time": d.get("blk_write_time"),
                "stats_reset": d.get("stats_reset"),
            } for d in stats.get("dbstats", [])[:8]
        ],
        "io": stats.get("io", [])[:8],
    }

def ai_recommendations_streamed(goal: str, hw: Dict[str, Any], stats: Dict[str, Any]) -> str:
    system = textwrap.dedent("""
      Ești un consultant PostgreSQL. Vorbești în română, concis, cu bullet points.
      Țel: recomandă valori pentru parametri în funcție de hardware/scop și metrice.
      Fii specific: „parametru = valoare” + motiv. Menționează doar parametrii relevanți.
      Dacă valorile curente sunt deja ok, spune „menține”.
    """).strip()
    messages = [
        {"role": "system", "content": system},
        {"role": "user",   "content": safe_json(build_ai_payload(goal, hw, stats), limit=60_000)}
    ]
    return call_ai(messages, stream=True, num_predict=NUM_PREDICT, num_ctx=NUM_CTX, budget_seconds=OAI_BUDGET_SECONDS)

# ───────────────────────── Rute ─────────────────────────
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse("connect.html", {"request": request})

@app.post("/connect", response_class=HTMLResponse)
def connect(request: Request,
            host: str = Form(...), port: str = Form(...),
            user: str = Form(...), password: str = Form(""),
            dbname: str = Form(...),
            ram_gb: float = Form(16.0),
            cpu_cores: int = Form(8),
            ssd: Optional[str] = Form("on"),
            goal: str = Form("OLTP")):
    dsn = f"postgres://{user}:{password}@{host}:{port}/{dbname}"
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1"); cur.fetchone()
    except Exception as e:
        return templates.TemplateResponse("connect.html", {"request": request, "error": str(e)})
    STATE["dsn"] = dsn
    STATE["hw"]  = {"ram_bytes": int(ram_gb * 1024**3), "cpu_cores": int(cpu_cores), "ssd": bool(ssd)}
    return templates.TemplateResponse("tuner.html", {"request": request, "goal": goal})

@app.post("/tune", response_class=HTMLResponse)
def tune(request: Request, goal: str = Form(...)):
    if not STATE.get("dsn"):
        raise HTTPException(400, "Neconectat la DB")
    hw = dict(STATE.get("hw") or {})
    hw["goal"] = goal

    t0 = time.perf_counter()
    stats = fetch_settings_and_stats(STATE["dsn"])
    print(f"[pg_tuner] stats fetched in { (time.perf_counter()-t0)*1000:.0f}ms")

    heuristics = basic_tune(hw, goal, stats.get("settings", []), stats.get("server_version_num", 120000))
    return templates.TemplateResponse("partials/result.html", {
        "request": request, "goal": goal, "heuristics": heuristics,
        "version": stats.get("server_version_num"),
        "ai_timeout": int(OAI_BUDGET_SECONDS),
    })

@app.post("/ai", response_class=HTMLResponse)
def ai(request: Request, goal: str = Form(...)):
    if not STATE.get("dsn"):
        raise HTTPException(400, "Neconectat la DB")
    hw = dict(STATE.get("hw") or {})
    hw["goal"] = goal
    stats = fetch_settings_and_stats(STATE["dsn"])
    ai_text = ai_recommendations_streamed(goal, hw, stats)
    return templates.TemplateResponse("partials/ai.html", {"request": request, "ai": ai_text})

# ─── diag
@app.get("/ollama/ping")
def ping_ollama():
    base = first_alive_host(timeout=1.0)
    if base:
        return {"ok": True, "host": base}
    return {"ok": False, "tried": try_hosts()}

@app.get("/ollama/models")
def ollama_models():
    base = first_alive_host(timeout=1.0)
    if not base:
        return {"ok": False, "tried": try_hosts()}
    return {"ok": True, "host": base, "models": list_models(base)}

@app.get("/ollama/diag")
def diag_ollama():
    base = first_alive_host(timeout=1.5)
    out: Dict[str, Any] = {"base": base, "tried": try_hosts()}
    if not base:
        return JSONResponse(out)
    # /api endpoints
    try:
        r = SESSION.post(f"{base}/api/chat", json={"model": MODEL, "messages":[{"role":"user","content":"ping"}], "stream": False}, timeout=4)
        out["api_chat"] = r.status_code
    except Exception as e:
        out["api_chat_err"] = str(e)
    try:
        r = SESSION.post(f"{base}/api/generate", json={"model": MODEL, "prompt":"ping", "stream": False}, timeout=4)
        out["api_generate"] = r.status_code
    except Exception as e:
        out["api_generate_err"] = str(e)
    # /v1 endpoints
    try:
        r = SESSION.post(f"{base}/v1/chat/completions", json={"model": MODEL, "messages":[{"role":"user","content":"ping"}], "stream": False}, timeout=4)
        out["v1_chat_compl"] = r.status_code
    except Exception as e:
        out["v1_chat_compl_err"] = str(e)
    try:
        r = SESSION.post(f"{base}/v1/completions", json={"model": MODEL, "prompt":"ping", "stream": False}, timeout=4)
        out["v1_compl"] = r.status_code
    except Exception as e:
        out["v1_compl_err"] = str(e)
    # listează modele reale (ce va folosi resolve_model)
    out["models"] = list_models(base)
    return JSONResponse(out)
