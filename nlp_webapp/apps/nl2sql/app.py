from __future__ import annotations
import os, re, json, time, uuid, textwrap
from typing import Dict, Any, List, Tuple, Optional

import requests
import psycopg
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# ────────────────────────────── App setup ──────────────────────────────
app = FastAPI(title="NL→SQL Assistant")
BASE = os.path.dirname(__file__)
# Mount static under /static so URLs like
# /apps/nl2sql/static/... resolve correctly from templates
app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE, "templates"))

# ────────────────────────────── State & Config ─────────────────────────
STATE: Dict[str, Any] = {
    "dsn": None,
    # "tables": {"public.customers": {"columns":[("id","integer"),("name","text"), ...]}}
    "tables": {},
    # "fks": [(src_schema, src_table, src_col, dst_schema, dst_table, dst_col), ...]
    "fks": [],
}

# Ollama
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
MODEL       = os.getenv("OLLAMA_MODEL", "qwen2.5:7b-instruct")
NUM_CTX     = int(os.getenv("NUM_CTX", "16384"))

# ────────────────────────────── Helpers ────────────────────────────────
SQL_BLOCK   = re.compile(r"```sql\s*(.*?)\s*```", re.I | re.S)
DDL_DML     = re.compile(r"\b(INSERT|UPDATE|DELETE|ALTER|DROP|TRUNCATE|CREATE|MERGE|COPY|CALL|DO|GRANT|REVOKE|VACUUM)\b", re.I)
SELECT_ONLY = re.compile(r"^\s*(WITH\s+|SELECT\s+)", re.I)

# --- SQL formatting helpers -----------------------------------------------
def _format_sql_fallback(s: str) -> str:
    # Uppercase keywords + quebra pe clauze mari. Simplu, fără dependențe.
    kw = r"\b(select|from|where|group by|order by|having|limit|offset|join|left join|right join|inner join|outer join|on|and|or|union all|union)\b"
    import re as _re
    t = _re.sub(kw, lambda m: m.group(1).upper(), s, flags=_re.I)
    t = _re.sub(r"\s+", " ", t).strip()
    # newline înaintea/după clauze majore
    t = _re.sub(r"\s+(FROM|WHERE|GROUP BY|ORDER BY|HAVING|LIMIT|OFFSET|UNION ALL|UNION)\b", r"\n\1 ", t, flags=_re.I)
    t = _re.sub(r"\s+(INNER JOIN|LEFT JOIN|RIGHT JOIN|JOIN|OUTER JOIN)\b", r"\n\1 ", t, flags=_re.I)
    t = _re.sub(r"\s+(AND|OR)\b", r"\n    \1 ", t, flags=_re.I)
    return t

def format_sql(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    try:
        import sqlparse  # pip install sqlparse
        return sqlparse.format(s, reindent=True, keyword_case="upper", identifier_case=None)
    except Exception:
        return _format_sql_fallback(s)

def extract_sql(text: str) -> Optional[str]:
    m = SQL_BLOCK.search(text or "")
    return m.group(1).strip() if m else None

def connect_ok(dsn: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")

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

def ensure_connected() -> None:
    if not STATE.get("dsn"):
        raise HTTPException(status_code=400, detail="Neconectat la DB")

# ────────────────────────────── Routes UI ──────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("connect.html", {"request": request})

@app.get("/chat", response_class=HTMLResponse)
async def chat(request: Request):
    return templates.TemplateResponse("chat.html", {"request": request})

# ────────────────────────────── Connect / Schema ───────────────────────
@app.post("/connect", response_class=HTMLResponse)
async def connect(
    request: Request,
    host: str = Form(...),
    port: str = Form(...),
    user: str = Form(...),
    password: str = Form(""),
    dbname: str = Form(...)
):
    dsn = f"postgres://{user}:{password}@{host}:{port}/{dbname}"
    try:
        connect_ok(dsn)
        STATE["dsn"] = dsn
        STATE["tables"], STATE["fks"] = introspect_schema(dsn)
        return RedirectResponse(url=str(request.url_for("chat")), status_code=303)
    except Exception as e:
        return templates.TemplateResponse("connect.html", {"request": request, "error": str(e)})

@app.post("/refresh-schema", response_class=HTMLResponse)
async def refresh_schema(request: Request):
    ensure_connected()
    try:
        STATE["tables"], STATE["fks"] = introspect_schema(STATE["dsn"])
        return RedirectResponse(url=str(request.url_for("chat")), status_code=303)
    except Exception as e:
        return templates.TemplateResponse("chat.html", {"request": request, "error": f"Schema refresh failed: {e}"})

@app.get("/schema.json", response_class=JSONResponse)
async def schema_json():
    return {
        "tables": STATE.get("tables", {}),
        "fks": [
            {"src_schema": s_sch, "src_table": s_tbl, "src_column": s_col,
             "dst_schema": d_sch, "dst_table": d_tbl, "dst_column": d_col}
            for (s_sch, s_tbl, s_col, d_sch, d_tbl, d_col) in STATE.get("fks", [])
        ],
    }

# ────────────────────────────── Prompting robust ───────────────────────
def build_system_prompt() -> str:
    return textwrap.dedent("""
        Ești un asistent NL→SQL pentru PostgreSQL. Vorbești în română.
        REGULI:
        - Folosește DOAR tabelele și coloanele din schema dată mai jos.
        - Dacă cerința implică tabele/coloane care NU există în schema dată, cere clarificări și NU produce SQL.
        - SQL-ul trebuie să fie doar SELECT (poate include CTE-uri).
        - Evită funcții/coloane inventate. Fără ORM, fără sintaxă non-Postgres.
        - Întotdeauna returnează EXACT un singur bloc:
          ### SQL
          ```sql
          SELECT ...
          ```
          ### Explicație
          - bullet points scurte, în română.
    """).strip()

def schema_summary(limit_tables: int = 60, limit_cols: int = 18) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    for tname, obj in list(STATE.get("tables", {}).items())[:limit_tables]:
        cols = [c[0] for c in obj.get("columns", [])][:limit_cols]
        out[tname] = cols
    return out

# ────────────────────────────── Chat & LLM ─────────────────────────────
@app.post("/ask", response_class=HTMLResponse)
async def ask(request: Request, prompt: str = Form(...)):
    try:
        sys = build_system_prompt()
        schema_hint = schema_summary()

        user_msg = textwrap.dedent(f"""
        Cerință: {prompt}

        Schema disponibilă (tabel: [coloane]):
        {json.dumps(schema_hint, ensure_ascii=False)}
        """).strip()

        r = requests.post(f"{OLLAMA_HOST.rstrip('/')}/api/chat", json={
            "model": MODEL,
            "messages": [
                {"role": "system", "content": sys},
                {"role": "user", "content": user_msg}
            ],
            "options": {
                "temperature": 0.1,
                "top_p": 0.9,
                "num_ctx": NUM_CTX
            },
            "stream": False
        }, timeout=180)
        r.raise_for_status()
        content = r.json()["message"]["content"]

        maybe_sql = extract_sql(content)
        pretty_sql = format_sql(maybe_sql)
        explanation = ""
        # încearcă să extragi o secțiune "### Explicație" dacă a venit cu markdown
        parts = re.split(r"(?i)###\s*Explicație", content, maxsplit=1)
        if len(parts) == 2:
            explanation = parts[1].strip()

        # dacă nu există SQL și nici nu e secțiune de clarificări, trimite conținutul ca răspuns text
        answer = explanation or (content if not maybe_sql else "")

        token = uuid.uuid4().hex[:8]
        return templates.TemplateResponse("partials/reply.html", {
            "request": request,
            "question": prompt,
            "model": MODEL,
            "sql": maybe_sql,
            "sql_formatted": pretty_sql,
            "answer": answer,
            "token": token,
        })

    except Exception as e:
        return templates.TemplateResponse("partials/reply.html", {
            "request": request,
            "error": str(e),
        })

# ────────────────────────────── Run (read-only) ────────────────────────
@app.post("/run", response_class=HTMLResponse)
async def run_sql(request: Request, sql: str = Form(...)):
    ensure_connected()
    s = (sql or "").strip().rstrip(";")
    if DDL_DML.search(s) or not SELECT_ONLY.search(s):
        return templates.TemplateResponse("partials/results.html", {
            "request": request, "error": "Doar SELECT este permis."
        })

    try:
        t0 = time.perf_counter()
        with psycopg.connect(STATE["dsn"], autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute("SET default_transaction_read_only = on")
                cur.execute("SET statement_timeout = '20s'")
                cur.execute(s)
                cols = [d[0] for d in cur.description] if cur.description else []
                rows_raw = cur.fetchall()
        dt_ms = (time.perf_counter() - t0) * 1000.0
        rows = [dict(zip(cols, r)) for r in rows_raw] if cols else []

        return templates.TemplateResponse("partials/results.html", {
            "request": request,
            "cols": cols,
            "rows": rows,
            "duration_ms": dt_ms
        })

    except Exception as e:
        return templates.TemplateResponse("partials/results.html", {
            "request": request, "error": str(e)
        })
