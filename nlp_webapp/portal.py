# portal.py (agregator)
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from apps.nl2sql.app import app as nl2sql_app   # sub-app

site = FastAPI(title="DataVerse")
site.mount("/static", StaticFiles(directory="static"), name="static")  # portal.css

templates = Jinja2Templates(directory="templates")

@site.get("/", response_class=HTMLResponse)
def home(request: Request):
    apps = [
        {"slug":"nl2sql","path":"/apps/nl2sql/","title":"NL→SQL Assistant","subtitle":"Vorbești natural → primești SQL.","tags":["FastAPI","PostgreSQL","Ollama","Cytoscape"],"status":"live"},
    ]
    return templates.TemplateResponse("home.html", {"request": request, "apps": apps})

# montează aplicația sub prefix
site.mount("/apps/nl2sql", nl2sql_app)
