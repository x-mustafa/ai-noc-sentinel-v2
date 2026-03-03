from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from contextlib import asynccontextmanager
import logging
import os

from app.config import settings
from app.database import close_pool, get_pending_migrations, run_migration
from app.routers import auth, zabbix, nodes, users, discover, import_router, chat, office, workflows, vault, ms365, incidents, messages, runbooks, sla, watchlist, escalations, changes, nocboard, alert_rules, reports, observability
from app.services.rate_limit import close_rate_limiter
from app.services.workflow_engine import start_engine, stop_engine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting NOC Sentinel Python backend...")
    embedded_engine_started = False
    # License check — runs before anything else
    from app.license_check import check_license
    check_license()
    logger.info("License verified.")
    pending = await get_pending_migrations()
    if pending:
        if settings.db_auto_migrate_on_startup:
            applied = await run_migration()
            logger.info(f"DB migrations done. Applied: {', '.join(applied)}")
        else:
            pending_ids = ", ".join(item["migration_id"] for item in pending)
            raise SystemExit(
                "Pending database migrations detected. "
                f"Run `python tools/migrate.py` before starting the app. Pending: {pending_ids}"
            )
    else:
        logger.info("DB schema up to date.")
    if settings.embedded_scheduler_enabled:
        logger.warning(
            "Embedded workflow scheduler is enabled in the web process. "
            "Use `python tools/run_worker.py` for production deployments."
        )
        embedded_engine_started = await start_engine()
    else:
        logger.info(
            "Embedded workflow scheduler is disabled. "
            "Start `python tools/run_worker.py` for scheduled automation."
        )
    yield
    if embedded_engine_started:
        await stop_engine()
    await close_rate_limiter()
    await close_pool()
    logger.info("Shutdown complete.")


app = FastAPI(title="NOC Sentinel", version="2.0.0", lifespan=lifespan)

# ── Security headers middleware ────────────────────────────────────────────────
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["X-Frame-Options"]        = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"]        = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"]     = "geolocation=(), camera=(), microphone=()"
        return response

app.add_middleware(SecurityHeadersMiddleware)

# ── Audit log middleware ───────────────────────────────────────────────────────
_AUDIT_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_AUDIT_SKIP_PREFIXES = ("/api/auth/session", "/api/health")

class AuditLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        method = request.method
        path   = request.url.path
        if method in _AUDIT_METHODS and not any(path.startswith(p) for p in _AUDIT_SKIP_PREFIXES):
            try:
                session  = request.session if hasattr(request, "session") else {}
                user_id  = session.get("username") or session.get("user_id") or "anonymous"
                ip       = (request.headers.get("X-Forwarded-For") or
                            request.headers.get("X-Real-IP") or
                            (request.client.host if request.client else "unknown"))
                import asyncio
                asyncio.create_task(_write_audit(user_id, method, path, ip, response.status_code))
            except Exception:
                pass
        return response


async def _write_audit(user_id: str, method: str, path: str, ip: str, status: int):
    try:
        from app.database import execute
        await execute(
            "INSERT INTO audit_log (user_id, method, path, ip, status_code) VALUES (%s,%s,%s,%s,%s)",
            (user_id, method, path, ip, status),
        )
    except Exception:
        pass  # never let audit failure break the request

app.add_middleware(AuditLogMiddleware)

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.app_secret,
    max_age=settings.session_max_age,
    same_site="lax",
    https_only=settings.session_https_only,
    session_cookie="noc_session",
)

# ── API routers ────────────────────────────────────────────────────────────────
app.include_router(auth.router,          prefix="/api/auth",      tags=["auth"])
app.include_router(zabbix.router,        prefix="/api/zabbix",    tags=["zabbix"])
app.include_router(nodes.router,         prefix="/api/nodes",     tags=["nodes"])
app.include_router(nodes.layout_router,  prefix="/api/layout",    tags=["layout"])
app.include_router(users.router,         prefix="/api/users",     tags=["users"])
app.include_router(discover.router,      prefix="/api/discover",  tags=["discover"])
app.include_router(import_router.router, prefix="/api/import",    tags=["import"])
app.include_router(chat.router,          prefix="/api/chat",      tags=["chat"])
app.include_router(office.router,        prefix="/api/office",    tags=["office"])
app.include_router(workflows.router,     prefix="/api/workflows", tags=["workflows"])
app.include_router(vault.router)
app.include_router(ms365.router,         prefix="/api/ms365",     tags=["ms365"])
app.include_router(incidents.router,     prefix="/api/incidents", tags=["incidents"])
app.include_router(messages.router,     prefix="/api/messages",  tags=["messages"])
app.include_router(runbooks.router,     prefix="/api/runbooks",  tags=["runbooks"])
app.include_router(sla.router,          prefix="/api/sla",        tags=["sla"])
app.include_router(watchlist.router,    prefix="/api/office",     tags=["watchlist"])
app.include_router(escalations.router,  prefix="/api/office",     tags=["escalations"])
app.include_router(changes.router,      prefix="/api/office",     tags=["changes"])
app.include_router(nocboard.router,     prefix="/api/office",     tags=["nocboard"])
app.include_router(alert_rules.router,  prefix="/api",            tags=["alert-rules"])
app.include_router(reports.router,      prefix="/api",            tags=["reports"])
app.include_router(observability.router, prefix="/api/observability", tags=["observability"])

# ── Health check ───────────────────────────────────────────────────────────────
@app.get("/api/health", tags=["health"])
async def health():
    """Liveness + readiness probe. Returns 200 if DB is reachable."""
    try:
        from app.database import fetch_one
        await fetch_one("SELECT 1")
        payload = {"status": "ok", "db": "connected"}
        if settings.embedded_scheduler_enabled:
            payload["workflow_engine"] = {"mode": "embedded", "status": "running"}
        else:
            worker = await fetch_one(
                "SELECT status, details, TIMESTAMPDIFF(SECOND, updated_at, NOW()) AS age_seconds "
                "FROM service_heartbeats WHERE service_name=%s",
                ("workflow_worker",),
            )
            if worker:
                payload["workflow_engine"] = {
                    "mode": "external",
                    "status": worker["status"],
                    "details": worker.get("details") or "",
                    "age_seconds": worker.get("age_seconds"),
                }
            else:
                payload["workflow_engine"] = {"mode": "external", "status": "missing"}
        return payload
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return JSONResponse({"status": "error", "db": str(e)}, status_code=503)

# ── Static files ───────────────────────────────────────────────────────────────
static_dir = os.path.join(os.path.dirname(__file__), "..", "static")
static_dir = os.path.normpath(static_dir)
if os.path.isdir(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


_NO_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"}


@app.get("/")
async def index():
    html_path = os.path.join(static_dir, "index.html")
    if os.path.isfile(html_path):
        return FileResponse(html_path, headers=_NO_CACHE)
    return JSONResponse({"status": "NOC Sentinel API running", "docs": "/docs"})


@app.get("/{path:path}")
async def spa_fallback(path: str):
    if path.startswith("api/"):
        return JSONResponse({"error": "Not found"}, status_code=404)
    # Guard against path traversal
    file_path = os.path.normpath(os.path.join(static_dir, path))
    if not file_path.startswith(static_dir + os.sep) and file_path != static_dir:
        return JSONResponse({"error": "Not found"}, status_code=404)
    if os.path.isfile(file_path):
        return FileResponse(file_path)
    # SPA fallback — return index.html for all non-API, non-file routes
    html_path = os.path.join(static_dir, "index.html")
    if os.path.isfile(html_path):
        return FileResponse(html_path, headers=_NO_CACHE)
    return JSONResponse({"error": "Frontend not built yet"}, status_code=404)
