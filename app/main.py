from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .api import admin, public
from .config import PROJECT_ROOT, Settings, get_settings
from .database import Base, apply_compatibility_migrations, create_database
from .security import SecurityManager
from .services.import_service import AccountImportService
from .services.export_tasks import ExportTaskService
from .services.operations import cleanup_operation_data, mark_interrupted_tasks
from .services.redemption import RedemptionService
from .services.validator import TokenValidator


logger = logging.getLogger(__name__)
STATIC_DIR = PROJECT_ROOT / "app" / "static"


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or get_settings()
    engine, session_factory = create_database(resolved_settings)
    security = SecurityManager(resolved_settings.credential_secret, resolved_settings.cdk_pepper)
    validator = TokenValidator(resolved_settings, security)
    import_service = AccountImportService(security, validator)
    redemption_service = RedemptionService(session_factory, security, validator)
    export_service = ExportTaskService(session_factory, security, resolved_settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        Base.metadata.create_all(engine)
        apply_compatibility_migrations(engine)
        interrupted = await asyncio.to_thread(mark_interrupted_tasks, session_factory, resolved_settings)
        if interrupted:
            logger.warning("marked %s interrupted operation tasks as failed", interrupted)
        await asyncio.to_thread(cleanup_operation_data, session_factory, resolved_settings)
        released = await asyncio.to_thread(redemption_service.recover_stale_reservations)
        if released:
            logger.warning("released %s stale account reservations", released)

        async def maintenance_loop() -> None:
            while True:
                await asyncio.sleep(6 * 60 * 60)
                await asyncio.to_thread(cleanup_operation_data, session_factory, resolved_settings)

        maintenance_task = asyncio.create_task(maintenance_loop())
        yield
        maintenance_task.cancel()
        try:
            await maintenance_task
        except asyncio.CancelledError:
            pass
        engine.dispose()

    app = FastAPI(title="CDK Loader", version="0.1.0", lifespan=lifespan)
    app.state.settings = resolved_settings
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.security = security
    app.state.validator = validator
    app.state.import_service = import_service
    app.state.redemption_service = redemption_service
    app.state.export_service = export_service
    app.state.running_tasks = set()

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "Prefer"],
    )
    app.include_router(admin.router)
    app.include_router(public.router)

    @app.get("/health", tags=["system"])
    def health():
        return {"status": "ok", "validation_mode": resolved_settings.validation_mode}

    if STATIC_DIR.exists():
        assets_dir = STATIC_DIR / "assets"
        if assets_dir.exists():
            app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        index = STATIC_DIR / "index.html"
        if index.exists():
            return FileResponse(index, headers={"Cache-Control": "no-cache"})
        return {
            "message": "Frontend has not been built. Run: cd frontend && npm install && npm run build",
            "path": path,
        }

    return app


app = create_app()
