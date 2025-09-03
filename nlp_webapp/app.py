from __future__ import annotations
import os, re, textwrap, time
from typing import Dict, List, Tuple, Optional

import requests
import psycopg
from fastapi import FastAPI, Form, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi import FastAPI, Form, Request, HTTPException
from fastapi.responses import RedirectResponse

# ── Config ────────────────────────────────────────────────────────────────────
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
MODEL       = os.getenv("OLLAMA_MODEL", "qwen2.5:7b-instruct")
NUM_CTX     = int(os.getenv("NUM_CTX", "16384"))
STATEMENT_TIMEOUT = os.getenv("STATEMENT_TIMEOUT", "30s")

STATE = {
    "dsn": None,                  # type: Optional[str]
    "tables": {},                 # type: Dict[str, Dict]
    "fks": [],                    # type: List[Tuple[str,str,str,str,str,str]]
    "system_prompt": "",
}

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# ── Schema introspection ──────────────────────────────────────────────────────
SCHEMA_COLS = """
SELECT table_schema, table_name, column_name, data_type
FROM information_schema.columns
WHERE table_schema NOT IN ('pg_catalog','information_schema')
ORDER BY table_schema, table_name, ordinal_position;
"""
SCHEMA_FKS = """
SELECT
  tc.table_schema AS src_schema,
  tc.table_name   AS src_table,
  kcu.column_name AS src_column,
  ccu.table_schema AS dst_schema,
  ccu.table_name   AS dst_table,
  ccu.column_name  AS dst_column
FROM information_schema.table_constraints AS tc
JOIN information_schema.key_column_usage AS kcu
  ON tc.constraint_name = kcu.constraint_name
 AND tc.table_schema = kcu.table_schema
JOIN information_schema.constraint_column_usage AS ccu
  ON ccu.constraint_name = tc.constraint_name
 AND ccu.table_schema = tc.table_schema
WHERE tc.constraint_type = 'FOREIGN KEY'
ORDER BY tc.table_schema, tc.table_name, kcu.ordinal_position;
"""

def load_schema(dsn: str) -> tuple[Dict[str, Dict], List[Tuple[str,str,str,str,str,str]]]:
    tables: Dict[str, Dict] = {}
    with psycopg.connect(dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_COLS)
            for sch, tbl, col, typ in cur.fetchall():
                key = f"{sch}.{tbl}"
                tables.setdefault(key, {"schema": sch, "table": tbl, "columns": []})
                tables[key]["columns"].append({"name": col, "type": typ})
            cur.execute(SCHEMA_FKS)
            fks = cur.fetchall()
    return tables, fks

def schema_text_for_llm(tables: Dict[str, Dict], fks: List[Tuple]) -> str:
    lines: List[str] = []
    for key in sorted(tables.keys()):
        lines.append(f"[{key}]")
        for col in tables[key]["columns"]:
            lines.append(f"  - {col['name']} {col['type']}")
        lines.append("")
    if fks:
        lines.append("[Foreign Keys]")
        for s_sch, s_tbl, s_col, d_sch, d_tbl, d_col in fks:
            lines.append(f"  {s_sch}.{s_tbl}.{s_col} -> {d_sch}.{d_tbl}.{d_col}")
    return "\n".join(lines)

def build_system_prompt(schema_text: str) -> str:
    return textwrap.dedent(f"""
    Ești un asistent tehnic pentru PostgreSQL. Vorbești DOAR în limba română, concis, cu diacritice.
    Poți răspunde liber despre structura și logica bazei, indexare, normalizare și strategii de interogare.
    • Când e util, GENEREAZĂ UN SINGUR query SQL într-un bloc:
      ```sql
      -- SQL aici
      ```
      Apoi explică pe scurt ce face.
    • La cererea utilizatorului, POȚI INSPECTA DATELE pentru a evidenția tendințe/pattern-uri (agregări, top-N, serii temporale).

    Context de schemă (tabele, coloane, FK):
    ------------------------------------------------
    {schema_text}
    """).strip()

# ── Ollama (non-stream) ──────────────────────────────────────────────────────
_SQL_CODEBLOCK = re.compile(r"```sql\s*(.*?)\s*```", re.I | re.S)

def ollama_chat(system_prompt: str, user_prompt: str) -> str:
    r = requests.post(f"{OLLAMA_HOST.rstrip('/')}/api/chat", json={
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt},
        ],
        "options": {"num_ctx": NUM_CTX, "temperature": 0.3},
        "stream": False
    }, timeout=200)
    r.raise_for_status()
    return r.json()["message"]["content"]

def extract_sql(text: str) -> str:
    m = _SQL_CODEBLOCK.search(text)
    return m.group(1).strip() if m else ""

# ── Execuție read-only ───────────────────────────────────────────────────────
def run_query_readonly(dsn: str, sql: str):
    with psycopg.connect(dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SET default_transaction_read_only = on")
            cur.execute(f"SET statement_timeout = '{STATEMENT_TIMEOUT}'")
            t0 = time.perf_counter()
            cur.execute(sql)
            rows = cur.fetchall() if cur.description else []
            headers = [d[0] for d in cur.description] if cur.description else []
            dur = time.perf_counter() - t0
    return headers, rows, dur

# ── Routes ───────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("connect.html", {"request": request})

@app.post("/connect", response_class=HTMLResponse)
async def connect(request: Request,
                  host: str = Form(...), port: str = Form(...),
                  user: str = Form(...), password: str = Form(""),
                  dbname: str = Form(...)):
    dsn = f"postgres://{user}:{password}@{host}:{port}/{dbname}"
    try:
        tables, fks = load_schema(dsn)
    except Exception as e:
        return templates.TemplateResponse("connect.html", {"request": request, "error": str(e)})

    STATE["dsn"] = dsn
    STATE["tables"] = tables
    STATE["fks"] = fks
    STATE["system_prompt"] = build_system_prompt(schema_text_for_llm(tables, fks))

    # IMPORTANT: redirect relativ la app montata (pastreaza /apps/nl2sql)
    return RedirectResponse(url=request.url_for("chat"), status_code=303)

@app.get("/chat", response_class=HTMLResponse)
async def chat(request: Request):
    if not STATE["dsn"]:
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse("chat.html", {"request": request})

@app.post("/ask", response_class=HTMLResponse)
async def ask(request: Request, prompt: str = Form(...)):
    if not STATE["dsn"]:
        raise HTTPException(400, "Neconectat la DB")
    try:
        reply = ollama_chat(STATE["system_prompt"], prompt)
    except Exception as e:
        reply = f"Eroare Ollama: {e}"
    sql = extract_sql(reply)

    # generează ID stabil pt. secțiunea de rezultate (evită filtre inexistente în Jinja)
    import hashlib
    result_id = hashlib.md5(sql.encode()).hexdigest()[:10] if sql else None

    return templates.TemplateResponse("partials/reply.html", {
        "request": request,
        "reply": reply,
        "sql": sql,
        "result_id": result_id,
    })

@app.post("/run", response_class=HTMLResponse)
async def run_sql(request: Request, sql: str = Form(...)):
    if not STATE["dsn"]:
        raise HTTPException(400, "Neconectat la DB")
    try:
        headers, rows, dur = run_query_readonly(STATE["dsn"], sql)
        return templates.TemplateResponse("partials/results.html", {
            "request": request,
            "headers": headers,
            "rows": rows,
            "dur": dur,
        })
    except Exception as e:
        return templates.TemplateResponse("partials/results.html", {
            "request": request,
            "headers": [],
            "rows": [],
            "dur": 0,
            "error": str(e),
        })

# NOU: JSON pentru diagrama interactivă (Cytoscape)
@app.get("/schema.json", response_class=JSONResponse)
async def schema_json():
    if not STATE["dsn"]:
        raise HTTPException(400, "Neconectat la DB")
    return {
        "tables": STATE["tables"],
        "fks": [
            {"src_schema": s_sch, "src_table": s_tbl, "src_column": s_col,
             "dst_schema": d_sch, "dst_table": d_tbl, "dst_column": d_col}
            for (s_sch, s_tbl, s_col, d_sch, d_tbl, d_col) in STATE["fks"]
        ],
    }
