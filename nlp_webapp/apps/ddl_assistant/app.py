from __future__ import annotations
import os, re, textwrap, time, json
from typing import Dict, Any, Optional, List

import requests
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# ────────────────────────────── App setup ──────────────────────────────
app = FastAPI(title="DDL Assistant")
BASE = os.path.dirname(__file__)
app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE, "templates"))

# ────────────────────────────── State & Config ─────────────────────────
STATE: Dict[str, Any] = {"dsn": None}

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
MODEL       = os.getenv("OLLAMA_MODEL", "qwen2.5:3b-instruct")
NUM_CTX     = int(os.getenv("NUM_CTX", "8192"))

# Reuse a robust HTTP session (no proxies)
SESSION = requests.Session()
SESSION.trust_env = False

SQL_BLOCK = re.compile(r"```sql\s*(.*?)\s*```", re.I | re.S)

def extract_sql(text: str) -> Optional[str]:
    m = SQL_BLOCK.search(text or "")
    return m.group(1).strip() if m else None

# ────────────────────────────── Ollama helpers (fallbacks) ─────────────
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
    for base in try_hosts():
        try:
            r = SESSION.get(f"{base}/api/tags", timeout=timeout)
            if r.ok:
                return base
        except Exception:
            continue
    for base in try_hosts():
        try:
            r = SESSION.get(f"{base}/v1/models", timeout=timeout)
            if r.ok:
                return base
        except Exception:
            continue
    return None

def list_models(base: str, timeout: float = 2.0) -> List[str]:
    try:
        r = SESSION.get(f"{base}/api/tags", timeout=timeout)
        if r.ok:
            data = r.json() or {}
            return [m.get("name") for m in data.get("models", []) if m.get("name")]
    except Exception:
        pass
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
    env_model = os.getenv("OLLAMA_MODEL", "").strip()
    models = list_models(base)
    if not models:
        return None
    if env_model and env_model in models:
        return env_model
    for m in PREFERRED_MODELS:
        if m in models:
            return m
    return models[0]

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

def call_ai(messages, stream: bool, num_predict: int, num_ctx: int, budget_seconds: float = 25.0) -> str:
    base = first_alive_host(timeout=1.5) or OLLAMA_HOST
    model = resolve_model(base)
    if not model:
        return "(AI indisponibil) Niciun model Ollama instalat sau endpoint inactiv."
    opts = {"num_ctx": num_ctx, "temperature": 0.2, "num_predict": num_predict}
    # 1) /api/chat
    try:
        body = {"model": model, "messages": messages, "options": opts, "stream": stream}
        r = SESSION.post(f"{base}/api/chat", json=body, timeout=(2, budget_seconds))
        if r.status_code == 404:
            raise requests.HTTPError("404", response=r)
        r.raise_for_status()
        return (r.json().get("message") or {}).get("content", "").strip()
    except Exception:
        pass
    # 2) /api/generate
    try:
        body = {"model": model, "prompt": join_messages_as_prompt(messages), "options": opts, "stream": stream}
        r = SESSION.post(f"{base}/api/generate", json=body, timeout=(2, budget_seconds))
        if r.status_code == 404:
            raise requests.HTTPError("404", response=r)
        r.raise_for_status()
        return r.json().get("response", "").strip()
    except Exception:
        pass
    # 3) /v1/chat/completions
    try:
        body = {"model": model, "messages": messages, "stream": False, "temperature": 0.2, "max_tokens": num_predict}
        r = SESSION.post(f"{base}/v1/chat/completions", json=body, timeout=(2, budget_seconds))
        if r.status_code == 404:
            raise requests.HTTPError("404", response=r)
        r.raise_for_status()
        data = r.json()
        return (((data.get("choices") or [{}])[0]).get("message") or {}).get("content", "").strip()
    except Exception:
        pass
    # 4) /v1/completions
    try:
        body = {"model": model, "prompt": join_messages_as_prompt(messages), "stream": False, "temperature": 0.2, "max_tokens": num_predict}
        r = SESSION.post(f"{base}/v1/completions", json=body, timeout=(2, budget_seconds))
        r.raise_for_status()
        data = r.json()
        return (((data.get("choices") or [{}])[0]).get("text") or "").strip() or "(AI răspuns gol)"
    except Exception as e:
        return f"(AI indisponibil) {e}"

# ────────────────────────────── Prompts ────────────────────────────────
def build_system_prompt() -> str:
    return textwrap.dedent(
        """
        Ești un asistent pentru proiectarea schemelor PostgreSQL. Vorbești în română, concis.
        ŢEL: dintr-o descriere a datelor, propune DDL corect și portabil pentru PostgreSQL.
        REGULI:
        - Folosește doar DDL: CREATE TABLE, PRIMARY KEY, FOREIGN KEY, UNIQUE, INDEX (CREATE INDEX), CHECK.
        - Fără DML, fără DROP/ALTER destructive. Fără INSERT.
        - Normalizează rezonabil (1NF/2NF), dar păstrează pragmatic cerința.
        - Chei primare: integer/bigint auto (GENERATED BY DEFAULT AS IDENTITY) sau naturale dacă e specificat.
        - Nume explicite pentru constrângeri și indici.
        - Tipuri: preferă `text`, `varchar(n)`, `numeric(p,s)`, `timestamp with time zone`, `date`, `boolean`.
        - Indexează FK-urile și coloanele de căutare frecventă.
        - Returnează EXACT:
          ### DDL
          ```sql
          -- tabele, constrângeri, indici
          ```
          ### Note
          - bullets scurte justificând deciziile
        """
    ).strip()

# ────────────────────────────── Routes UI ──────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("connect.html", {"request": request})

@app.post("/connect", response_class=HTMLResponse)
async def connect(request: Request,
                  host: str = Form(...),
                  port: str = Form(...),
                  user: str = Form(...),
                  password: str = Form(""),
                  dbname: str = Form(...)):
    # Păstrăm DSN doar pentru context; nu executăm DDL din aplicație.
    dsn = f"postgres://{user}:{password}@{host}:{port}/{dbname}"
    STATE["dsn"] = dsn
    return templates.TemplateResponse("designer.html", {"request": request})

@app.get("/guide", response_class=HTMLResponse)
async def guide(request: Request):
    return templates.TemplateResponse("guide.html", {"request": request})

@app.post("/generate", response_class=HTMLResponse)
async def generate(request: Request, spec: str = Form(...)):
    try:
        system = build_system_prompt()
        user_msg = textwrap.dedent(f"""
        DESCRIERE:
        {spec}

        CERINȚE:
        - Postgres SQL doar DDL.
        - include CREATE TABLE, chei primare/secundare, FK, indici relevanți.
        - nume explicite pentru constrângeri/indici.
        """).strip()

        content = call_ai([
            {"role": "system", "content": system},
            {"role": "user", "content": user_msg},
        ], stream=False, num_predict=512, num_ctx=NUM_CTX) or ""
        ddl_sql = extract_sql(content) or ""

        # încearcă să extragi secțiunea "### Note" dacă există
        import re as _re
        notes = ""
        parts = _re.split(r"(?i)###\s*Note", content, maxsplit=1)
        if len(parts) == 2:
            notes = parts[1].strip()

        return templates.TemplateResponse("partials/reply.html", {
            "request": request,
            "ddl": ddl_sql,
            "notes": notes,
            "model": MODEL,
        })

    except Exception as e:
        return templates.TemplateResponse("partials/reply.html", {
            "request": request,
            "error": str(e)
        })
