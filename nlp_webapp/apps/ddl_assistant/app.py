from __future__ import annotations
import os, re, textwrap, time, json
from typing import Dict, Any, Optional, List, Tuple

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
STATE: Dict[str, Any] = {"dsn": None, "tables": {}, "fks": []}

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

# ────────────────────────────── DB Introspection ───────────────────────
def introspect_schema(dsn: str) -> Tuple[Dict[str, Any], List[Tuple[str, str, str, str, str, str]]]:
    """
    Returnează:
      tables: dict { "schema.table": { "columns": [(name,type), ...] } }
      fks:    list  [(src_schema, src_table, src_col, dst_schema, dst_table, dst_col), ...]
    """
    tables: Dict[str, Any] = {}
    fks: List[Tuple[str, str, str, str, str, str]] = []

    q_cols = """
    SELECT
      c.table_schema, c.table_name, c.column_name,
      CASE
        WHEN c.data_type ILIKE 'character varying' THEN 'varchar('||COALESCE(c.character_maximum_length::text,'')||')'
        WHEN c.data_type ILIKE 'character' THEN 'char('||COALESCE(c.character_maximum_length::text,'')||')'
        ELSE c.data_type
      END AS col_type
    FROM information_schema.columns c
    JOIN information_schema.tables t
      ON t.table_schema = c.table_schema AND t.table_name = c.table_name
    WHERE t.table_type='BASE TABLE' AND t.table_schema NOT IN ('pg_catalog','information_schema')
    ORDER BY c.table_schema, c.table_name, c.ordinal_position;
    """

    q_fks = """
    SELECT
      src_ns.nspname  AS src_schema,
      src_tbl.relname AS src_table,
      src_col.attname AS src_column,
      dst_ns.nspname  AS dst_schema,
      dst_tbl.relname AS dst_table,
      dst_col.attname AS dst_column
    FROM pg_constraint fk
    JOIN pg_class src_tbl ON fk.conrelid = src_tbl.oid
    JOIN pg_namespace src_ns ON src_tbl.relnamespace = src_ns.oid
    JOIN pg_class dst_tbl ON fk.confrelid = dst_tbl.oid
    JOIN pg_namespace dst_ns ON dst_tbl.relnamespace = dst_ns.oid
    JOIN unnest(fk.conkey) WITH ORDINALITY AS src(attnum, ord) ON true
    JOIN unnest(fk.confkey) WITH ORDINALITY AS dst(attnum, ord) ON src.ord = dst.ord
    JOIN pg_attribute src_col ON src_col.attrelid = src_tbl.oid AND src_col.attnum = src.attnum
    JOIN pg_attribute dst_col ON dst_col.attrelid = dst_tbl.oid AND dst_col.attnum = dst.attnum
    WHERE fk.contype = 'f'
      AND src_ns.nspname NOT IN ('pg_catalog','information_schema');
    """

    import psycopg
    with psycopg.connect(dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(q_cols)
            for sch, tbl, col, typ in cur.fetchall():
                key = f"{sch}.{tbl}"
                tables.setdefault(key, {"columns": []})
                tables[key]["columns"].append((col, typ))

            cur.execute(q_fks)
            for row in cur.fetchall():
                fks.append(tuple(row))

    return tables, fks

def schema_summary_text(tables: Dict[str, Any], fks: List[Tuple[str, str, str, str, str, str]], limit_tables: int = 80, limit_cols: int = 32) -> str:
    lines: List[str] = []
    for tname in sorted(list(tables.keys()))[:limit_tables]:
        cols = [c[0] for c in tables[tname].get("columns", [])][:limit_cols]
        lines.append(f"{tname}: [{', '.join(cols)}]")
    if fks:
        lines.append("Foreign Keys:")
        for (s_sch, s_tbl, s_col, d_sch, d_tbl, d_col) in fks[:200]:
            lines.append(f"  {s_sch}.{s_tbl}.{s_col} -> {d_sch}.{d_tbl}.{d_col}")
    return "\n".join(lines)

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
    # Încearcă să introspectezi schema pentru context (non‑fatal dacă eșuează)
    try:
        tables, fks = introspect_schema(dsn)
        STATE["tables"], STATE["fks"] = tables, fks
    except Exception:
        STATE["tables"], STATE["fks"] = {}, []
    return templates.TemplateResponse("designer.html", {"request": request})

@app.get("/guide", response_class=HTMLResponse)
async def guide(request: Request):
    return templates.TemplateResponse("guide.html", {"request": request})

@app.post("/generate", response_class=HTMLResponse)
async def generate(request: Request,
                   spec: str = Form(...),
                   use_schema: Optional[str] = Form(None),
                   migration: Optional[str] = Form(None)):
    try:
        system = build_system_prompt()
        ctx = []
        ctx.append("DESCRIERE:\n" + spec)

        # Include schema existentă ca referință, dacă e bifat și avem ceva în memorie
        if use_schema and STATE.get("tables"):
            ctx.append("SCHEMA EXISTENTĂ (referință, nu o modifica; păstrează denumirile):\n" +
                       schema_summary_text(STATE.get("tables", {}), STATE.get("fks", [])))

        # Cerințe de bază + mod migration dacă e selectat
        req = [
            "CERINȚE:",
            "- Postgres SQL doar DDL.",
            "- Include CREATE TABLE, PK, FK, INDEX relevante.",
            "- Nume explicite pentru constrângeri/indici.",
        ]
        if migration:
            req.append("- MOD MIGRATION: propune ALTER TABLE/CREATE INDEX pentru a ajunge la noul model; evită DROP destructive.")
            req.append("- Fără CREATE TABLE duplicate dacă tabelul există deja în schema de referință.")
        user_msg = textwrap.dedent("\n\n".join(["\n".join(ctx), "\n".join(req)])).strip()

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
