from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, capture, routes, store
from .config import WEB_DIR, ensure_dirs, load_config
from .events import hub
from .providers import get_provider
from .providers.gigaam import GigaAMProvider

log = logging.getLogger("rechepis")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    ensure_dirs()
    hub.bind(asyncio.get_running_loop())
    migrated = await asyncio.to_thread(store.migrate_all)
    recovered = await asyncio.to_thread(store.recover_interrupted)
    if migrated or any(recovered.values()):
        log.info("migrated %d recordings, recovered %s", migrated, recovered)
    gigaam = get_provider("gigaam")
    if isinstance(gigaam, GigaAMProvider):
        gigaam.set_change_callback(routes.publish_providers)
    await routes.manager.start()
    if isinstance(gigaam, GigaAMProvider) and "gigaam" in load_config()["targets"] and gigaam.status()["ready"]:
        gigaam.warm_up()  # the first segment of the day should not wait for the model to load
    try:
        yield
    finally:
        await capture.drain_background()
        await routes.manager.stop()


app = FastAPI(title="Речепись", version=__version__, lifespan=lifespan)
app.include_router(routes.router)


@app.websocket("/ws/capture")
async def ws_capture(websocket: WebSocket) -> None:
    await capture.capture_endpoint(websocket, routes.manager, hub)


@app.websocket("/ws/events")
async def ws_events(websocket: WebSocket) -> None:
    await routes.events_socket(websocket)


@app.middleware("http")
async def no_cache_static(request: Request, call_next):
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")
