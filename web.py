"""Read-only local dashboard web server (http://localhost:8797). PAPER TRADING ONLY. Bound to 127.0.0.1."""
import os
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from common import BASE, init_db
import dashboard_data

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
S = os.path.join(BASE, "static")
NOCACHE = {"Cache-Control": "no-store"}

@app.get("/")
def index():
    return FileResponse(os.path.join(S, "index.html"), headers=NOCACHE)

@app.get("/static/{name}")
def static(name: str):
    if name not in ("style.css", "app.js", "chart.umd.min.js"):
        raise HTTPException(404)
    return FileResponse(os.path.join(S, name), headers=NOCACHE)

@app.get("/api/state")
def state():
    return JSONResponse(dashboard_data.build(), headers=NOCACHE)

@app.get("/data.json")
def data_json():
    p = os.path.join(BASE, "site", "data.json")
    if not os.path.exists(p):
        raise HTTPException(404)
    return FileResponse(p, headers=NOCACHE)

init_db()
