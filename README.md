# NL→SQL Assistant (Ollama + PostgreSQL)

Un **asistent conversațional în limba română** care:

* se conectează la o bază de date **PostgreSQL**;
* primește întrebări în limbaj natural și propune **interogări SQL** cu ajutorul unui model local prin **Ollama**;
* execută **doar la cererea utilizatorului** query‑urile sugerate (mod **read‑only** + `statement_timeout`);
* afișează rezultatele în terminal cu **[Rich](https://github.com/Textualize/rich)**;
* poate genera o **diagramă ASCII** cu tabele, coloane și **FK‑uri**.

---

## 🚀 Funcționalități

* **Conversație liberă în română** despre structura bazei, normalizare, indexare, „cum aș scrie un query pentru…”, etc.
* **Generare SQL** în blocuri marcate `sql` în răspunsul AI; dacă este detectat SQL, aplicația cere confirmare înainte de execuție.
* **Execuție sigură**: `SET default_transaction_read_only = on` + `SET statement_timeout`.
* **Diagramă ASCII**: cutii (box‑drawing) pentru tabele/coloane + rute curate între tabele pentru FK‑uri.
* **Comenzi speciale** în prompt:

  * `:diagram` – schema completă (ASCII);
  * `:diagram schema.tabel` – diagramă cu **focus** pe un tabel;
  * `:refresh` – reîncarcă schema din DB;
  * `:q` – ieșire.

---

## 📦 Cerințe

* **Ollama** instalat și pornit local (`ollama serve`).
* Un model potrivit pe română (recomandat: **Qwen 2.5 Instruct** 7B/14B).
* **Python 3.9+**.
* Pachete Python: `requests`, `psycopg[binary]`, `rich`.

---

## 🛠️ Instalare

```bash
# 1) pornește Ollama
ollama serve

# 2) trage un model bun pe română
ollama pull qwen2.5:7b-instruct

# 3) instalează dependențele în proiectul tău
pip install requests psycopg[binary] rich
```

---

## 💬 Utilizare – exemple

````text
TU> Arată-mi numele clientului, data și totalul pentru toate comenzile din august 2024.

[Răspuns model]
```sql
SELECT c.name, o.order_date, o.total_amount
FROM customers c
JOIN orders o ON o.customer_id = c.customer_id
WHERE o.order_date >= '2024-08-01' AND o.order_date < '2024-09-01';
````

Explicație: listează comenzile din august 2024, cu numele clientului și totalul.

SQL detectat. Rulez? \[y/N]: y

Rezultate • 3 rânduri • 12.4 ms
name          | order\_date  | total\_amount
\--------------+-------------+-------------
Ana Popescu   | 2024-08-15  | 3800.00
Mihai Ionescu | 2024-08-20  | 2800.00
...

````

### Comenzi speciale
```text
TU> :diagram
# diagrama ASCII a întregii baze

TU> :diagram public.orders
# diagrama cu focus pe un tabel

TU> :refresh
# reîncarcă schema după modificări

TU> :q
# ieșire
````

---

## 🧭 Diagramă ASCII – exemplu

> Exemplu generic (output real depinde de schema ta):

```text
┌───────────────────────┐
│ public.customers      │
├───────────────────────┤
│ customer_id: integer  │
│ name: text            │
│ email: text           │
│ city: text            │
└───────────────────────┘
                ───────────────────────────────────────────────▶
┌───────────────────────┐                                     │
│ public.orders         │                                     │
├───────────────────────┤                                     │
│ order_id: integer     │                                     │
│ customer_id: integer  │ ◀────────────────────────────────────
│ order_date: date      │
│ total_amount: numeric │
└───────────────────────┘
```

---

## 🔐 Siguranță

* Query‑urile **nu** se execută automat: aplicația îți cere mereu acordul.
* Execuția este **read‑only** și cu **timeout** (configurabil), pentru a evita blocaje sau modificări accidentale.

---

## ⚙️ Setări implicite & unde se schimbă

* **Model** (`MODEL`): `qwen2.5:7b-instruct` – poți schimba rapid din constantă.
* **Ollama host** (`OLLAMA_HOST`): `http://localhost:11434`.
* **Context LLM** (`NUM_CTX`): `16384`.
* **Timeout SQL** (`STATEMENT_TIMEOUT`): `30s`.

---

## ❗ Limitări & recomandări

* Pentru baze foarte mari, schema textuală poate depăși contextul modelului: filtrează tabelele relevante sau folosește un model cu fereastră de context mai mare.
* Diagrama ASCII este intenționat simplă; pentru sute de tabele, recomandăm desen cu focus pe `schema.tabel` sau diagrame pe porțiuni.
* Dacă ai activat streaming în alt cod, aici folosim `stream=False` pentru răspunsul LLM, ca parsingul blocului `sql` să fie stabil.

---

## 🧩 Structura proiectului (varianta monolitică)

* **`nl2sql_assistant_ascii_v2_explicat.py`** – un singur fișier cu:

  * conectare interactivă (parolă mascată);
  * introspecție schemă (coloane + FK);
  * prompt de system ce permite atât discuții, cât și generare SQL;
  * apel Ollama (HTTP, fără librăria `ollama`);
  * execuție SQL **doar la cerere** (read‑only + timeout) și afișare cu Rich;
  * **diagramă ASCII** (cutii + rute curate între tabele pentru FK‑uri);
  * comenzi `:diagram`, `:refresh`, `:q`.

---

## 🧪 Bază de test sugerată (opțional)

Poți crea rapid o bază minimală pentru demo (în `psql`):

```sql
CREATE DATABASE shopdb; \c shopdb;

CREATE TABLE customers (
  customer_id SERIAL PRIMARY KEY,
  name        TEXT NOT NULL,
  email       TEXT,
  city        TEXT
);

CREATE TABLE orders (
  order_id    SERIAL PRIMARY KEY,
  customer_id INT REFERENCES customers(customer_id),
  order_date  DATE NOT NULL,
  total_amount NUMERIC(10,2) NOT NULL
);

INSERT INTO customers (name, email, city) VALUES
('Ana Popescu','ana@example.com','București'),
('Mihai Ionescu','mihai@example.com','Cluj');

INSERT INTO orders (customer_id, order_date, total_amount) VALUES
(1,'2024-08-15',3800.00),
(2,'2024-08-20',2800.00);
```

