#!/usr/bin/env bash
set -euo pipefail

# sed -i compat (macOS vs Linux)
if [[ "$(uname)" == "Darwin" ]]; then SED_INPLACE=(-i ''); else SED_INPLACE=(-i); fi

echo "==> 1) Creez structura țintă apps/…"
mkdir -p apps/nl2sql/templates/partials apps/nl2sql/static
mkdir -p apps/pg_tuner/templates/partials apps/pg_tuner/static

echo "==> 2) Mut NL→SQL în apps/nl2sql/…"
[[ -f app.py ]] && mv app.py apps/nl2sql/app.py
if [[ -d templates/nl2sql ]]; then
  rsync -a templates/nl2sql/ apps/nl2sql/templates/
  rm -rf templates/nl2sql
fi
if [[ -d static/nl2sql ]]; then
  rsync -a static/nl2sql/ apps/nl2sql/static/
  rm -rf static/nl2sql
fi

echo "==> 3) Portal la rădăcină (home.html + portal.css rămân)"
# nimic de mutat aici

echo "==> 4) Patch portal.py (import + mount)"
if [[ -f portal.py ]]; then
  sed "${SED_INPLACE[@]}" \
    -e "s|from app import app as nl2sql_app|from apps.nl2sql.app import app as nl2sql_app|g" \
    portal.py || true

  # montează /apps/nl2sql dacă lipsește
  if ! grep -q "/apps/nl2sql" portal.py; then
    awk '
      BEGIN{ done=0 }
      /site *= *FastAPI\(.*\)/ && done==0 { print; print "site.mount(\"/apps/nl2sql\", nl2sql_app)"; done=1; next }
      { print }
    ' portal.py > portal.py.tmp && mv portal.py.tmp portal.py
  fi

  # mount pentru /static (portal.css) dacă lipsește
  if ! grep -q "site.mount(\"/static\"" portal.py && ! grep -q "site.mount('\!/static'" portal.py; then
    awk '
      BEGIN{ done=0 }
      /Jinja2Templates\(directory *=/ && done==0 { print "site.mount(\"/static\", StaticFiles(directory=\"static\"), name=\"static\")"; print; done=1; next }
      { print }
    ' portal.py > portal.py.tmp && mv portal.py.tmp portal.py
  fi
fi

echo "==> 5) Patch apps/nl2sql/app.py (static unic + templates locale)"
APP_NL="apps/nl2sql/app.py"
if [[ -f "$APP_NL" ]]; then
  # asigură importurile
  grep -q "from fastapi.staticfiles import StaticFiles" "$APP_NL" || \
    sed "${SED_INPLACE[@]}" "1,40{s|from fastapi import FastAPI|from fastapi import FastAPI\nfrom fastapi.staticfiles import StaticFiles|}" "$APP_NL"
  grep -q "from fastapi.templating import Jinja2Templates" "$APP_NL" || \
    sed "${SED_INPLACE[@]}" "1,60{s|from fastapi import FastAPI|from fastapi import FastAPI\nfrom fastapi.templating import Jinja2Templates|}" "$APP_NL"
  grep -q "import os" "$APP_NL" || sed "${SED_INPLACE[@]}" "1s|^|import os\n|" "$APP_NL"

  # definește BASE după prima apariție a app = FastAPI(...)
  if ! grep -q "BASE = os.path.dirname(__file__)" "$APP_NL"; then
    awk '
      BEGIN{done=0}
      /app *= *FastAPI\(.*\)/ && done==0 { print; print "BASE = os.path.dirname(__file__)"; done=1; next }
      { print }
    ' "$APP_NL" > "$APP_NL.tmp" && mv "$APP_NL.tmp" "$APP_NL"
  fi

  # elimină montările vechi app.mount(...) existente (dacă sunt)
  awk '
    BEGIN{ }
    !/app\.mount\(.*\)/ { print }
  ' "$APP_NL" > "$APP_NL.tmp" && mv "$APP_NL.tmp" "$APP_NL"

  # adaugă montarea corectă /nl2sqlstatic + templates locale după linia BASE =
  awk '
    BEGIN{added=0; addedTpl=0}
    /BASE *= *os\.path\.dirname\(.*\)/ && added==0 {
      print
      print "app.mount(\"/nl2sqlstatic\", StaticFiles(directory=os.path.join(BASE, \"static\")), name=\"nl2sqlstatic\")"
      added=1; next
    }
    /Jinja2Templates\(directory *=/ { foundTpl=1 }
    { print }
    END{
      if (!addedTpl) {
        print "templates = Jinja2Templates(directory=os.path.join(BASE, \"templates\"))"
      }
    }
  ' "$APP_NL" > "$APP_NL.tmp" && mv "$APP_NL.tmp" "$APP_NL"
fi

echo "==> 6) Patch referințe CSS/JS în templates NL→SQL"
BASE_TPL="apps/nl2sql/templates/base.html"
if [[ -f "$BASE_TPL" ]]; then
  sed "${SED_INPLACE[@]}" \
    -e "s|url_for(['\"]static['\"], *path=['\"]style\.css['\"])|url_for('nl2sqlstatic', path='style.css')|g" \
    -e "s|url_for([\"']static[\"'], *path=[\"']style\.css[\"'])|url_for('nl2sqlstatic', path='style.css')|g" \
    -e "s|url_for(['\"]static['\"], *path=['\"]app\.js['\"])|url_for('nl2sqlstatic', path='app.js')|g" \
    -e "s|url_for([\"']static[\"'], *path=[\"']app\.js[\"'])|url_for('nl2sqlstatic', path='app.js')|g" \
    "$BASE_TPL" || true
fi

echo "==> DONE. Structura nouă:"
cat <<'TREE'
nlp_webapps/
├── portal.py
├── templates/
│   └── home.html
├── static/
│   └── portal.css
└── apps/
    ├── nl2sql/
    │   ├── app.py
    │   ├── templates/
    │   │   ├── base.html
    │   │   ├── chat.html
    │   │   ├── connect.html
    │   │   └── partials/
    │   │       ├── reply.html
    │   │       └── results.html
    │   └── static/
    │       ├── app.js
    │       └── style.css
    └── pg_tuner/
        ├── app.py            (de adăugat ulterior)
        ├── templates/
        │   ├── base.html
        │   ├── connect.html
        │   ├── tuner.html
        │   └── partials/
        │       └── result.html
        └── static/
            ├── tuner.css
            └── tuner.js
TREE

echo
echo "Verifică apoi cu:"
echo "  uvicorn portal:site --reload"
echo "și deschide /apps/nl2sql/nl2sqlstatic/style.css (status 200)."
