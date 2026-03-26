from __future__ import annotations

import hmac
from contextlib import asynccontextmanager
from pathlib import Path

import modal
from fastapi import FastAPI, Header, HTTPException, Request

from bot import (
    BotStateStore,
    Settings,
    build_webhook_url,
    clear_bot_webhook,
    configure_bot_webhook,
    configure_logging,
    create_app,
    initialize_application,
    process_raw_update,
    shutdown_application,
)


APP_NAME = "mr-cleaner"
WEBHOOK_LABEL = "mr-cleaner-webhook"
WEBHOOK_PATH = "/telegram/webhook"
STATE_VOLUME_NAME = "mr-cleaner-state"
STATE_MOUNT_PATH = "/state"
STATE_FILE_PATH = Path(f"{STATE_MOUNT_PATH}/bot_state.json")
SECRET_NAME = "mr-cleaner-env"


app = modal.App(APP_NAME)
image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install_from_requirements("requirements.txt")
    .pip_install("fastapi>=0.115.0,<1.0")
    .add_local_python_source("bot", copy=True)
)
state_volume = modal.Volume.from_name(STATE_VOLUME_NAME, create_if_missing=True)
env_secret = modal.Secret.from_name(SECRET_NAME)


def build_state_store() -> BotStateStore:
    return BotStateStore(STATE_FILE_PATH, after_save=state_volume.commit.aio)


@asynccontextmanager
async def lifespan(web_app: FastAPI):
    settings = Settings.from_env()
    configure_logging(settings.debug)
    telegram_app = create_app(
        settings,
        state_store=build_state_store(),
        with_updater=False,
    )
    await initialize_application(telegram_app, start_background_tasks=False)
    web_app.state.settings = settings
    web_app.state.telegram_app = telegram_app
    try:
        yield
    finally:
        await shutdown_application(telegram_app, stop_background_tasks=False)


web_app = FastAPI(lifespan=lifespan)


@web_app.get("/healthz")
async def healthz() -> dict[str, bool]:
    return {"ok": True}


@web_app.post(WEBHOOK_PATH)
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict[str, bool]:
    settings: Settings = request.app.state.settings

    if settings.webhook_secret:
        received = x_telegram_bot_api_secret_token or ""
        if not hmac.compare_digest(received, settings.webhook_secret):
            raise HTTPException(status_code=403, detail="invalid webhook secret")

    try:
        payload = await request.json()
    except Exception as exc:  # noqa: BLE001 - FastAPI should still return 400 for bad payloads
        raise HTTPException(status_code=400, detail="invalid json body") from exc

    processed = await process_raw_update(request.app.state.telegram_app, payload)
    if not processed:
        raise HTTPException(status_code=400, detail="invalid telegram update")

    return {"ok": True}


@app.function(
    image=image,
    secrets=[env_secret],
    volumes={STATE_MOUNT_PATH: state_volume},
    env={"STATE_FILE": str(STATE_FILE_PATH)},
    min_containers=1,
    max_containers=1,
    scaledown_window=60,
)
@modal.concurrent(max_inputs=20)
@modal.asgi_app(label=WEBHOOK_LABEL)
def webhook_app():
    return web_app


@app.function(
    image=image,
    secrets=[env_secret],
)
async def configure_webhook(webhook_base_url: str) -> dict[str, object]:
    settings = Settings.from_env()
    configure_logging(settings.debug)
    webhook_url = build_webhook_url(webhook_base_url, WEBHOOK_PATH)
    webhook_info = await configure_bot_webhook(
        settings,
        webhook_url=webhook_url,
        secret_token=settings.webhook_secret,
    )
    return {
        "webhook_url": webhook_url,
        "webhook_info": webhook_info,
    }


@app.function(
    image=image,
    secrets=[env_secret],
)
async def clear_webhook(drop_pending_updates: bool = False) -> dict[str, object]:
    settings = Settings.from_env()
    configure_logging(settings.debug)
    return await clear_bot_webhook(
        settings,
        drop_pending_updates=drop_pending_updates,
    )
