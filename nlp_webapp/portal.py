from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# sub-apps
from apps.nl2sql.app import app as nl2sql_app
from apps.pg_tuner.app import app as pg_tuner_app  # <-- nou

site = FastAPI(title="DataVerse")
site.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# montează aplicațiile
site.mount("/apps/nl2sql", nl2sql_app)
site.mount("/apps/pg-tuner", pg_tuner_app)  # <-- nou

@site.get("/", response_class=HTMLResponse)
def home(request: Request):
    apps = [
        {
            "slug": "nl2sql",
            "path": "/apps/nl2sql/",
            "title": "NL→SQL Assistant",
            "subtitle": "Vorbești natural → primești SQL",
            "status": "live",
            "tags": ["FastAPI", "PostgreSQL", "Ollama", "Diagram"]
        },
        {
            "slug": "pg-tuner",
            "path": "/apps/pg-tuner/",
            "title": "PG Config Tuner",
            "subtitle": "Recomandări Postgres după hardware & scop",
            "status": "beta",
            "tags": ["pg_settings", "bgwriter", "Ollama"]
        },
    ]
    return templates.TemplateResponse("home.html", {"request": request, "apps": apps})
