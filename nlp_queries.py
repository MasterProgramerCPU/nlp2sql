#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Asistent NL→SQL local (Ollama + PostgreSQL) cu:
- autentificare interactivă (parolă mascată),
- conversație liberă în română despre baza curentă (modelul știe schema),
- generare SQL la nevoie (în bloc ```sql```), cu întrebare explicită înainte de execuție,
- execuție read-only cu timeout și afișare cu Rich,
- diagramă ASCII clară (cutii pentru tabele + linii FK rutate curat).
"""

# =========================
# 0) IMPORTURI ȘI SETĂRI
# =========================

import os, re, textwrap, time, math
from typing import Dict, List, Tuple
import requests
import psycopg                             # psycopg3 (driver Postgres)
from getpass import getpass               # pentru parolă mascată

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.syntax import Syntax
from rich import box

# Config minimal — schimbă după nevoie
OLLAMA_HOST = "http://localhost:11434"    # endpoint-ul local Ollama
MODEL       = "qwen2.5:7b-instruct"       # model bun pe română
NUM_CTX     = 16384                       # fereastră de context pt LLM (dacă modelul suportă)
STATEMENT_TIMEOUT = "30s"                 # protecție execuție SQL

console = Console()

# =======================================
# 1) INTROSPECȚIE SCHEMĂ (coloane + FK)
# =======================================

# Query pentru toate coloanele din toate tabelele (exceptând sistemul)
SCHEMA_COLS = """
SELECT table_schema, table_name, column_name, data_type
FROM information_schema.columns
WHERE table_schema NOT IN ('pg_catalog','information_schema')
ORDER BY table_schema, table_name, ordinal_position;
"""

# Query pentru toate cheile străine (FK)
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

def load_schema(conn) -> Tuple[Dict[str, Dict], List[Tuple[str,str,str,str,str,str]]]:
    """
    Citește schema DB și o întoarce în două structuri:
      - tables: dict "<schema>.<table>" -> {"columns":[(nume, tip), ...]}
      - fks: listă de muchii FK: (src_schema, src_table, src_col, dst_schema, dst_table, dst_col)
    """
    tables: Dict[str, Dict] = {}
    with conn.cursor() as cur:
        cur.execute(SCHEMA_COLS)
        for sch, tbl, col, typ in cur.fetchall():
            key = f"{sch}.{tbl}"
            tables.setdefault(key, {"columns": []})
            tables[key]["columns"].append((col, typ))

        cur.execute(SCHEMA_FKS)
        fks = cur.fetchall()

    return tables, fks

def schema_text_for_llm(tables: Dict[str, Dict], fks: List[Tuple]) -> str:
    """
    Formatează schema într-un text compact pe care îl dăm în system prompt,
    ca LLM-ul să aibă contextul exact al tabelelor/coloanelor/FK-urilor.
    """
    out = []
    for key in sorted(tables.keys()):
        out.append(f"[{key}]")
        for c, t in tables[key]["columns"]:
            out.append(f"  - {c} {t}")
        out.append("")
    if fks:
        out.append("[Foreign Keys]")
        for s_sch, s_tbl, s_col, d_sch, d_tbl, d_col in fks:
            out.append(f"  {s_sch}.{s_tbl}.{s_col} -> {d_sch}.{d_tbl}.{d_col}")
    return "\n".join(out)

# =======================================
# 2) PROMPT-UL DE SYSTEM PENTRU LLM
# =======================================

def build_system_prompt(schema_text: str) -> str:
    """
    Setează comportamentul asistentului:
    - Răspunde în română (concise, cu diacritice),
    - Poate genera SQL într-un bloc ```sql``` (un singur query),
    - Poate inspecta datele (la cerere) pentru a evidenția tendințe/pattern-uri,
    - Include schema bazei.
    """
    return textwrap.dedent(f"""
    Ești un asistent tehnic pentru PostgreSQL. Vorbești DOAR în limba română, concis, cu diacritice.
    Poți răspunde liber despre structura și logica bazei, indexare, normalizare și strategii de interogare.
    • Când e util, GENEREAZĂ UN SINGUR query SQL într-un bloc:
      ```sql
      -- SQL aici
      ```
      Apoi explică pe scurt ce face.
    • La cererea utilizatorului, POȚI INSPECTA DATELE pentru a evidenția tendințe/pattern-uri (ex: agregări, top-N, serii temporale).

    Context de schemă (tabele, coloane, FK):
    ------------------------------------------------
    {schema_text}
    """).strip()

# =======================================
# 3) APEL LA OLLAMA (HTTP, fără librăria ollama)
# =======================================

def ollama_chat(model: str, system_prompt: str, user_prompt: str) -> str:
    """
    Trimite system+user la Ollama și întoarce textul de răspuns.
    Folosim stream=False ca să fie ușor de parsat (evităm NDJSON).
    """
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "options": {"num_ctx": NUM_CTX, "temperature": 0.3},
        "stream": False
    }
    r = requests.post(f"{OLLAMA_HOST.rstrip('/')}/api/chat", json=payload, timeout=180)
    r.raise_for_status()
    return r.json()["message"]["content"]

# Regex pentru a extrage blocul ```sql ... ```
_SQL_CODEBLOCK = re.compile(r"```sql\s*(.*?)\s*```", re.I | re.S)

def extract_sql(text: str) -> str:
    """Întoarce SQL-ul din blocul ```sql``` dacă există, altfel string gol."""
    m = _SQL_CODEBLOCK.search(text)
    return m.group(1).strip() if m else ""

# =======================================
# 4) EXECUȚIE READ-ONLY + AFIȘARE REZULTATE
# =======================================

def run_query_readonly(dsn: str, sql: str):
    """
    Rulează SQL-ul în mod sigur: read-only + timeout.
    Returnează antetele, rândurile și durata execuției.
    """
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

def render_rows(headers, rows, dur):
    """Afișează frumos rezultatele într-un tabel Rich."""
    if not headers:
        console.print(Panel("(fără set de rezultate)", title="Rezultate", border_style="magenta"))
        return
    table = Table(title=f"Rezultate • {len(rows)} rânduri • {dur*1000:.1f} ms",
                  box=box.SIMPLE_HEAVY, header_style="bold", expand=True)
    for h in headers:
        table.add_column(str(h))
    for r in rows:
        table.add_row(*[("" if v is None else str(v)) for v in r])
    console.print(table)

# =======================================
# 5) DIAGRAMĂ ASCII — CUTII + LINII FK CURATE
# =======================================

def build_table_box(title: str, columns: List[Tuple[str,str]]) -> List[str]:
    """
    Textul unei cutii (titlu = schema.tabel, sub el coloanele: tip).
    Folosim caractere box-drawing Unicode.
    """
    lines = [f"{c}: {t}" for c, t in columns]
    inner = max(len(title), *(len(s) for s in lines) if lines else [0], 8)
    top  = "┌" + "─"*(inner+2) + "┐"
    head = "│ " + title.ljust(inner) + " │"
    sep  = "├" + "─"*(inner+2) + "┤" if lines else None
    body = ["│ " + s.ljust(inner) + " │" for s in lines]
    bot  = "└" + "─"*(inner+2) + "┘"
    out  = [top, head]
    if sep: out.append(sep)
    out += body
    out.append(bot)
    return out

def draw_on_canvas(canvas, x, y, ch):
    """Desenează un caracter pe canvas dacă e în interior."""
    H = len(canvas); W = len(canvas[0])
    if 0 <= y < H and 0 <= x < W:
        canvas[y][x] = ch

def draw_box(canvas, x, y, box_lines):
    """Desenează cutia la poziția (x,y)."""
    for i, line in enumerate(box_lines):
        for j, ch in enumerate(line):
            draw_on_canvas(canvas, x+j, y+i, ch)

def safe_h(canvas, x1, x2, y):
    """
    Desenează o linie orizontală '─' doar prin spații (sau combină cu '│' în '┼'),
    ca să nu stricăm marginile cutiilor.
    """
    if x1 > x2: x1, x2 = x2, x1
    H = len(canvas); W = len(canvas[0])
    if not (0 <= y < H): return
    for x in range(max(0,x1), min(W-1,x2)+1):
        cur = canvas[y][x]
        if cur == " ":
            canvas[y][x] = "─"
        elif cur in "│┼":
            canvas[y][x] = "┼"

def safe_v(canvas, x, y1, y2):
    """
    Desenează o linie verticală '│' doar prin spații (sau combină cu '─' în '┼').
    """
    if y1 > y2: y1, y2 = y2, y1
    H = len(canvas); W = len(canvas[0])
    if not (0 <= x < W): return
    for y in range(max(0,y1), min(H-1,y2)+1):
        cur = canvas[y][x]
        if cur == " ":
            canvas[y][x] = "│"
        elif cur in "─┼":
            canvas[y][x] = "┼"

def ascii_diagram(tables: Dict[str, Dict], fks: List[Tuple], focus: str = "") -> str:
    """
    Desenează diagrama ASCII:
      - tabele așezate în 2 coloane, cu spațiu („gutter”) între ele,
      - fiecare FK e rutat în 3 segmente: orizontal din cutie -> vertical pe mid_x -> orizontal spre țintă,
      - vârful săgeții e în afara cutiei țintă (nu peste border).
    """
    keys = sorted(tables.keys())
    if focus and focus in keys:
        keys.remove(focus)
        keys.insert(0, focus)

    # Layout de bază
    COLS  = 2
    GAP_X = 8   # spațiu orizontal între coloane (gutter mare)
    GAP_Y = 2   # spațiu vertical între rânduri

    # Construim cutiile și aflăm dimensiuni max
    boxes, widths, heights = {}, [], []
    for k in keys:
        b = build_table_box(k, tables[k]["columns"])
        boxes[k] = b
        widths.append(max(len(s) for s in b))
        heights.append(len(b))

    col_width  = max(widths) + GAP_X
    row_height = max(heights) + GAP_Y
    rows = math.ceil(len(keys)/COLS)

    # Dimensiuni canvas
    W = col_width*COLS + GAP_X
    H = row_height*rows + GAP_Y + 2
    canvas = [list(" " * W) for _ in range(H)]
    mid_x = W // 2  # „coloana” centrală pentru rute

    # Poziționăm cutiile; reținem dreptunghiurile și centrele
    rects: Dict[str, Tuple[int,int,int,int]] = {}
    for idx, k in enumerate(keys):
        r = idx // COLS
        c = idx % COLS
        x = c*col_width + GAP_X//2
        y = r*row_height + GAP_Y//2
        b = boxes[k]
        draw_box(canvas, x, y, b)
        w = max(len(s) for s in b); h = len(b)
        rects[k] = (x, y, x + w - 1, y + h - 1)

    # Trasează muchii FK: out -> mid_x -> in (fără să intrăm în cutii)
    for s_sch, s_tbl, s_col, d_sch, d_tbl, d_col in fks:
        src = f"{s_sch}.{s_tbl}"
        dst = f"{d_sch}.{d_tbl}"
        if src not in rects or dst not in rects:
            continue

        la, ta, ra, ba = rects[src]
        lb, tb, rb, bb = rects[dst]
        ay = (ta + ba) // 2      # „nivel” vertical al sursei
        by = (tb + bb) // 2      # „nivel” vertical al țintei

        # Alege punctul de ieșire (imediat în afara marginii cutiei)
        ax = ra + 1 if ra < mid_x else la - 1
        # Alege punctul de intrare (imediat în afara marginii țintei)
        bx = lb - 1 if lb > mid_x else rb + 1
        arrow_char = "▶" if bx > mid_x else "◀"

        # 1) ieșire orizontală până la coloana centrală
        safe_h(canvas, ax, mid_x, ay)
        # 2) urcăm/coborâm pe coloana centrală
        safe_v(canvas, mid_x, ay, by)
        # 3) intrare orizontală până la marginea țintei (în exterior)
        safe_h(canvas, mid_x, bx, by)
        # semn de săgeată la capăt (nu în cutie)
        if 0 <= by < H and 0 <= bx < W:
            canvas[by][bx] = arrow_char

    # Returnează textul „desenului”
    return "\n".join("".join(row).rstrip() for row in canvas)

# =======================================
# 6) UI HELPERS (banner, răspuns, sql)
# =======================================

def render_banner(dbname, user, host, port):
    title = f"NL→SQL Assistant • baza: {dbname} • user: {user} • host: {host}:{port}"
    console.print(Panel(title, title="Conectat", border_style="cyan",
                        subtitle="Ollama + PostgreSQL", subtitle_align="right"))

def render_llm_reply(reply: str):
    console.print(Panel(reply.strip(), title="Răspuns model", border_style="white"))

def render_sql(sql: str):
    console.print(Panel(Syntax(sql, "sql"), title="SQL detectat", border_style="cyan"))

def render_help():
    txt = "\n".join([
        "Comenzi:",
        "  :diagram             – diagrama ASCII a întregii baze",
        "  :diagram schema.tbl  – diagramă cu focus pe un tabel",
        "  :refresh             – reîncarcă schema",
        "  :q                   – ieșire",
        "",
        "În rest, întreabă liber despre baza de date (tendințe, pattern-uri, explicații).",
        "Dacă modelul include un bloc ```sql```, ți se cere voie înainte de execuție (read-only)."
    ])
    console.print(Panel(txt, title="Ajutor", border_style="green"))

# =======================================
# 7) MAIN — FLUXUL APLICAȚIEI
# =======================================

def main():
    # a) Colectăm datele de conectare (parola este mascată)
    console.print("[bold]Conectare la Postgres[/bold] (lasă gol pentru valori implicite)")
    host = input("Host [localhost]: ") or "localhost"
    port = input("Port [5432]: ") or "5432"
    user = input("User [postgres]: ") or "postgres"
    password = getpass("Parolă: ")
    dbname = input("Baza de date [postgres]: ") or "postgres"
    dsn = f"postgres://{user}:{password}@{host}:{port}/{dbname}"

    # b) Încărcăm schema (coloane + FK)
    console.print("\nÎncarc schema...")
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            tables, fks = load_schema(conn)
    except Exception as e:
        console.print(Panel(str(e), title="Eroare conectare / introspecție", border_style="red"))
        return

    # c) Pregătim contextul pentru LLM
    render_banner(dbname, user, host, port)
    render_help()
    schema_text = schema_text_for_llm(tables, fks)
    system_prompt = build_system_prompt(schema_text)

    # d) Buclă interactivă: conversație + comenzi speciale
    while True:
        try:
            user_prompt = input("\nTU> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not user_prompt:
            continue
        if user_prompt == ":q":
            break

        # Comanda: desen ASCII
        if user_prompt.startswith(":diagram"):
            parts = user_prompt.split()
            focus = parts[1] if len(parts) > 1 else ""
            art = ascii_diagram(tables, fks, focus)
            console.print(Panel(art, title="Diagrama ASCII", border_style="yellow"))
            continue

        # Comanda: reîncarcă schema
        if user_prompt == ":refresh":
            console.print("Reîncarc schema...")
            try:
                with psycopg.connect(dsn, autocommit=True) as conn:
                    tables, fks = load_schema(conn)
                schema_text = schema_text_for_llm(tables, fks)
                system_prompt = build_system_prompt(schema_text)
                console.print(Panel("Schema reîncărcată.", border_style="green"))
            except Exception as e:
                console.print(Panel(str(e), title="Eroare refresh", border_style="red"))
            continue

        # Conversație liberă cu modelul (despre schema curentă)
        try:
            reply = ollama_chat(MODEL, system_prompt, user_prompt)
        except Exception as e:
            console.print(Panel(str(e), title="Eroare Ollama", border_style="red"))
            continue

        # Afișăm răspunsul asistentului
        render_llm_reply(reply)

        # Dacă există bloc ```sql```, îl arătăm și cerem acordul pentru execuție
        sql = extract_sql(reply)
        if sql:
            render_sql(sql)
            ans = input("Rulez SQL-ul detectat? [y/N]: ").strip().lower()
            if ans == "y":
                try:
                    headers, rows, dur = run_query_readonly(dsn, sql)
                    render_rows(headers, rows, dur)
                except Exception as e:
                    console.print(Panel(str(e), title="Eroare execuție", border_style="red"))

if __name__ == "__main__":
    main()
