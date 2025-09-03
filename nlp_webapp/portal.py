from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# importă aplicația existentă
from app import app as nl2sql_app

site = FastAPI(title="NeoStack — Apps")
site.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# montează aplicațiile sub prefixe
site.mount("/apps/nl2sql", nl2sql_app)

@site.get("/", response_class=HTMLResponse)
def home(request: Request):
    # poți popula dinamic această listă cu alte apps
    apps = [
        {
            "slug": "nl2sql",
            "path": "/apps/nl2sql/",
            "title": "NL→SQL Assistant",
            "subtitle": "Vorbești natural → primești SQL.",
            "tags": ["FastAPI", "PostgreSQL", "Ollama", "Cytoscape"],
            "status": "live",
        },
        # {"slug":"altapp","path":"/apps/altapp/","title":"Altă aplicație","subtitle":"…","tags":["tag1"],"status":"beta"}
    ]
    return templates.TemplateResponse("home.html", {"request": request, "apps": apps})
