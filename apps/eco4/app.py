from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import os

app = FastAPI()

# Serve static files (css, js, images) if needed later
static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/{full_path:path}")
async def serve_spa(full_path: str):
    return FileResponse(os.path.join(os.path.dirname(__file__), "templates", "index.html"))
