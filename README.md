# DataVerse: NL→SQL Assistant & PG Config Tuner (FastAPI + Ollama + PostgreSQL)

Două aplicații web FastAPI montate într-un singur portal:

- NL→SQL Assistant: întrebi în română, primești SQL (SELECT‑only), poți rula interogări în mod read‑only și vezi o diagramă ER interactivă.
- PG Config Tuner: colectează setări/statistici din Postgres și propune valori recomandate (heuristici + opțional AI via Ollama).

—

## Caracteristici

- Generare SQL din limbaj natural cu modele Ollama locale (Qwen, Mistral, Llama etc.).
- Execuție sigură: `default_transaction_read_only = on` + `statement_timeout` înainte de rulare.
- HTMX pentru interacțiuni rapide (răspunsuri parțiale), Cytoscape + Dagre pentru diagrama ER.
- Sub‑aplicații izolate (șabloane și statice proprii), agregate în portal comun.

—

## Structură repo

- `nlp_webapp/portal.py` – aplicația principală FastAPI; montează sub‑apps:
  - `/apps/nl2sql` → `nlp_webapp/apps/nl2sql/app.py`
  - `/apps/pg-tuner` → `nlp_webapp/apps/pg_tuner/app.py`
- `nlp_webapp/templates/home.html` – pagina de start a portalului.
- `nlp_webapp/static/portal.css` – stiluri portal.
- `nlp_webapp/apps/nl2sql/` – aplicația NL→SQL:
  - `app.py` – rute, conectare DB, apel Ollama, execuție SELECT.
  - `templates/` – `base.html`, `connect.html`, `chat.html` + partials.
  - `static/` – `style.css`, `app.js` (Cytoscape/Dagre cfg, UI mici).
- `nlp_webapp/apps/pg_tuner/` – PG Tuner:
  - `app.py` – rute, colectare statistici, heuristici, AI fallback.
  - `templates/` – `base.html`, `connect.html`, `tuner.html` + partials.
  - `static/` – `tuner.css`, `tuner.js`.

—

## Cerințe

- Python 3.9+
- Pachete Python: `fastapi`, `uvicorn[standard]`, `psycopg[binary]`, `requests`, `sqlparse`.
- Ollama instalat și pornit local (`ollama serve`) + cel puțin un model instalat.
- Un server PostgreSQL accesibil.

—

## Instalare rapidă

```bash
python -m venv .venv && source .venv/bin/activate
pip install fastapi "uvicorn[standard]" psycopg[binary] requests sqlparse

# pornește Ollama dacă nu rulează deja
ollama serve &

# opțional: trage un model
ollama pull qwen2.5:3b-instruct
```

—

## Rulează portalul

```bash
uvicorn nlp_webapp.portal:site --reload
```

- Portal: `http://127.0.0.1:8000/`
- NL→SQL Assistant: `http://127.0.0.1:8000/apps/nl2sql/`
- PG Config Tuner: `http://127.0.0.1:8000/apps/pg-tuner/`

Alternativ, individual:

```bash
uvicorn nlp_webapp.apps.nl2sql.app:app --reload
uvicorn nlp_webapp.apps.pg_tuner.app:app --reload
```

—

## Variabile de mediu

- Comun:
  - `OLLAMA_HOST` (implicit: `http://localhost:11434`)
  - `OLLAMA_MODEL` (ex: `qwen2.5:3b-instruct`, `mistral:7b-instruct`)
  - `NUM_CTX` (mărimea contextului; NL→SQL: implicit `16384`)
- PG Tuner:
  - `PGTUNER_AI_TIMEOUT` (secunde pentru streaming AI; implicit `20`)
  - `PGTUNER_NUM_PREDICT` (max tokens generate; implicit `256`)

Exemplu rulare:

```bash
export OLLAMA_HOST=http://127.0.0.1:11434
export OLLAMA_MODEL=qwen2.5:3b-instruct
uvicorn nlp_webapp.portal:site --reload
```

—

## NL→SQL Assistant – flux

1) Conectare DB (formular): host, port, user, parolă (opțional), dbname.
2) Chat: pui o întrebare în română; aplicația cheamă Ollama și extrage blocul
   ```sql ... ``` din răspuns.
3) Execuție: doar SELECT, cu protecții:
   - respinge DDL/DML (`INSERT|UPDATE|DELETE|ALTER|DROP|...`)
   - setează `default_transaction_read_only = on` și `statement_timeout = '20s'`
4) Diagramă ER: Cytoscape + Dagre, focus opțional, toggle coloane/etichete FK.

Endpointuri utile:

- `POST /apps/nl2sql/connect` – salvează DSN și introspectează schema.
- `POST /apps/nl2sql/ask` – generează răspuns + SQL (HTMX partial).
- `POST /apps/nl2sql/run` – execută SELECT și returnează rezultate (HTMX partial).
- `GET  /apps/nl2sql/schema.json` – schema curentă pentru client (grafic).

—

## PG Config Tuner – flux

1) Conectare DB + hardware: RAM (GB), CPU cores, SSD, scop (OLTP/OLAP/MIX).
2) Tune (heuristic): calculează recomandări de bază în funcție de HW și versiune PG.
3) AI (opțional): cere recomandări LLM cu context din setări/statistici.
4) Conf: generează un `postgresql.conf` minimal pe baza recomandărilor.

Diag Ollama:

- `GET  /apps/pg-tuner/ollama/ping`
- `GET  /apps/pg-tuner/ollama/models`
- `GET  /apps/pg-tuner/ollama/diag`

—

## Note despre statice și șabloane

- Fiecare sub‑app își montează propriul `"/static"` (ex.: `.../apps/nl2sql/app.py`).
- În șabloane, referă resursele local, fără slash la început: `static/style.css`, `static/app.js`.
- Evită `href="/static/..."` în sub‑app — ar indica staticele portalului, nu ale sub‑app‑ului.
- Dacă preferi URL‑uri generate, folosește: `{{ request.url_for('static', path='style.css') }}`.

—

## Troubleshooting

- CSS/JS nu se încarcă în NL→SQL
  - Cauză comună: mount greșit (ex.: `/nl2sqlstatic`). Corect: mount la `"/static"` în sub‑app. În `base.html` folosește căi relative (`static/...`).
- Ollama indisponibil / răspuns gol
  - Verifică `ollama serve`, modelul instalat și `OLLAMA_HOST`.
  - Pentru PG Tuner, vezi `ollama/*` endpoints (ping, models, diag).
- Conexiune DB eșuată
  - Confirmă host/port/user/parolă și accesul de rețea.
  - NL→SQL respinge non‑SELECT; erorile de sintaxă din SQL generat sunt raportate în UI.

—

## Securitate

- Execuție SQL doar SELECT, în tranzacție read‑only + timeout.
- Fără salvare parole pe disc; DSN ținut în memorie pentru sesiune.

—

## Roadmap scurt (idei)

- Autocomplete/istoric întrebări în NL→SQL.
- Export CSV/JSON pentru rezultate.
- Persistență conexiuni per sesiune utilizator.

