# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import contextlib
import logging
import os
import uuid
from collections.abc import AsyncIterator

import google.auth
from dotenv import load_dotenv
from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from google.adk.runners import Runner
from google.genai import types
from pydantic import BaseModel, Field

from app.app_utils import services
from app.app_utils.telemetry import setup_telemetry
from app.app_utils.typing import Feedback
from app.auth import (
    AUTHORIZED_HOUSEHOLD_STATE_KEY,
    AuthenticatedUser,
    AuthorizationError,
    get_current_user,
    get_or_create_twilio_session_binding,
    list_broadcast_whatsapp_recipients,
    provision_twilio_household_from_env,
    raise_authorization_http,
    require_session_for_household,
    resolve_authorized_household,
    resolve_twilio_sender,
    seed_local_demo_identity,
)
from app.auth.twilio_media import MediaFetchError, download_twilio_media
from app.auth.twilio_signature import (
    TwilioWebhookRejected,
    load_twilio_webhook_config,
    parse_twilio_form,
    verify_twilio_webhook,
    webhook_dispatch_fields,
)
from app.interfaces import (
    PERSISTED_HOUSEHOLD_ID_FIELD,
    PERSISTED_INVOCATION_ID_FIELD,
    PERSISTED_RECEIPT_ID_FIELD,
)

load_dotenv()
setup_telemetry()

# Safe Google Cloud Logging setup with fallback
try:
    from google.cloud import logging as google_cloud_logging

    _, project_id = google.auth.default()
    logging_client = google_cloud_logging.Client()
    logger = logging_client.logger(__name__)
    has_gcp_logging = True
except Exception:
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger("fast_api_app")
    has_gcp_logging = False

allow_origins = (
    os.getenv("ALLOW_ORIGINS", "").split(",") if os.getenv("ALLOW_ORIGINS") else None
)

AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    from app.agent import app as adk_app

    # Internal ADK Runner only — do not mount ADK/A2A HTTP routes.
    runner = Runner(
        app=adk_app,
        session_service=services.get_session_service(),
        artifact_service=services.get_artifact_service(),
        auto_create_session=False,
    )
    app.state.runner = runner
    app.state.agent_app_name = adk_app.name
    seed_local_demo_identity()
    # Controlled provisioning only — runtime authz/broadcast use DB membership.
    provision_twilio_household_from_env()
    yield


app: FastAPI = FastAPI(
    title="family-receipt-agent",
    description="API for interacting with the Agent family-receipt-agent",
    lifespan=lifespan,
)

if allow_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allow_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


# --- Schemas ---


class MessageRequest(BaseModel):
    message: str
    household_id: str | None = None
    session_id: str | None = None
    # Ignored for identity — retained only so forged clients cannot confuse callers.
    user_id: str | None = Field(
        default=None,
        description="Ignored. Authenticated identity comes from credentials.",
    )


# --- Session helpers ---


async def _resolve_agent_session(
    *,
    user: AuthenticatedUser,
    household_id: str,
    session_id: str | None,
    channel: str = "web",
):
    """Resume an owned session or create a new server-owned session."""
    runner = app.state.runner
    agent_app_name = app.state.agent_app_name
    trusted_state = {AUTHORIZED_HOUSEHOLD_STATE_KEY: household_id}

    if session_id and session_id.strip():
        try:
            require_session_for_household(user, session_id.strip(), household_id)
        except AuthorizationError as exc:
            raise_authorization_http(exc)

        session = await runner.session_service.get_session(
            app_name=agent_app_name,
            session_id=session_id.strip(),
            user_id=user.user_id,
        )
        if session is None:
            # DB binding exists but ADK session is gone — do not transfer ownership.
            raise_authorization_http(
                AuthorizationError("Conversation session is unavailable")
            )
        session.state[AUTHORIZED_HOUSEHOLD_STATE_KEY] = household_id
        return session

    new_session_id = str(uuid.uuid4())
    session = await runner.session_service.create_session(
        user_id=user.user_id,
        app_name=agent_app_name,
        session_id=new_session_id,
        state=trusted_state,
    )
    from app import db

    db.create_conversation_session(
        session.id,
        user.user_id,
        household_id,
        channel=channel,
    )
    return session


async def _collect_agent_response(
    content: types.Content, user_id: str, session_id: str
):
    runner = app.state.runner
    response_text = ""
    async for event in runner.run_async(
        new_message=content, user_id=user_id, session_id=session_id
    ):
        node_name = event.node_info.name if event.node_info else ""
        if node_name in ("response_agent", "query_agent"):
            if event.content and event.content.parts:
                for part in event.content.parts:
                    if part.text:
                        response_text += part.text
        elif node_name == "invalid_receipt_responder":
            if event.output:
                response_text += str(event.output)
    return response_text


def _authorized_household(
    user: AuthenticatedUser, requested_household_id: str | None
) -> str:
    try:
        return resolve_authorized_household(user, requested_household_id)
    except AuthorizationError as exc:
        raise_authorization_http(exc)
        raise  # pragma: no cover


# --- Custom Routes ---


@app.post("/agent/message")
async def agent_message(
    req: MessageRequest,
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Executes the agent workflow with user input."""
    household_id = _authorized_household(user, req.household_id)
    session = await _resolve_agent_session(
        user=user,
        household_id=household_id,
        session_id=req.session_id,
        channel="web",
    )
    content = types.Content(role="user", parts=[types.Part.from_text(text=req.message)])
    response_text = await _collect_agent_response(content, user.user_id, session.id)
    return {
        "response": response_text,
        "session_id": session.id,
        "household_id": household_id,
    }


@app.post("/agent/upload")
async def agent_upload(
    file: UploadFile = File(...),
    household_id: str | None = Form(None),
    session_id: str | None = Form(None),
    user_id: str | None = Form(None),  # ignored — auth via credentials
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Executes the agent workflow with an uploaded receipt file."""
    _ = user_id  # intentionally ignored
    resolved_household = _authorized_household(user, household_id)
    session = await _resolve_agent_session(
        user=user,
        household_id=resolved_household,
        session_id=session_id,
        channel="web",
    )

    file_bytes = await file.read()
    content_type = file.content_type or "image/jpeg"
    parts = [
        types.Part.from_bytes(data=file_bytes, mime_type=content_type),
        types.Part.from_text(text="Analyze this receipt image."),
    ]
    content = types.Content(role="user", parts=parts)
    response_text = await _collect_agent_response(content, user.user_id, session.id)
    return {
        "response": response_text,
        "session_id": session.id,
        "household_id": resolved_household,
    }


def _mask_phone(value: str | None) -> str:
    """Keep a short suffix so logs stay correlatable without the full number."""
    if not isinstance(value, str) or not value:
        return "redacted"
    digits = "".join(ch for ch in value if ch.isdigit())
    if len(digits) < 4:
        return "redacted"
    return f"***{digits[-4:]}"


def twilio_send_message(
    account_sid: str, auth_token: str, from_num: str, to_num: str, body: str
):
    import requests

    sys_logger = logging.getLogger("twilio_integration")
    destination = _mask_phone(to_num)
    sys_logger.info("Starting Twilio WhatsApp send to %s", destination)
    try:
        resp = requests.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json",
            auth=(account_sid, auth_token),
            data={"From": from_num, "To": to_num, "Body": body},
            timeout=15,
        )
        sys_logger.info(
            "Twilio WhatsApp send to %s status code: %s",
            destination,
            resp.status_code,
        )
        if resp.status_code not in (200, 201):
            sys_logger.error(
                "Twilio WhatsApp send to %s failed with status %s",
                destination,
                resp.status_code,
            )
    except Exception:
        sys_logger.error("Twilio WhatsApp send to %s failed", destination)


_MEDIA_FAILURE_REPLY = (
    "I couldn't read that image. Please send a JPEG, PNG, or WebP photo."
)


def _reply_media_failure(sender: str, twilio_number: str) -> None:
    """Private note to the sender. Never a household broadcast."""
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
    if not account_sid or not auth_token or not twilio_number:
        return
    twilio_send_message(
        account_sid,
        auth_token,
        twilio_number,
        sender,
        _MEDIA_FAILURE_REPLY,
    )


def _output_field(output, name: str):
    if isinstance(output, dict):
        return output.get(name)
    return getattr(output, name, None)


def _persisted_receipt_for_run(event, run_id: str, household_id: str) -> int | None:
    """Receipt id only when this event records this run's insert for this household."""
    node_name = event.node_info.name if getattr(event, "node_info", None) else ""
    if node_name != "memory_engine" or not run_id or not household_id:
        return None
    if getattr(event, "invocation_id", None) != run_id:
        return None
    output = getattr(event, "output", None)
    receipt_id = _output_field(output, PERSISTED_RECEIPT_ID_FIELD)
    output_household = _output_field(output, PERSISTED_HOUSEHOLD_ID_FIELD)
    output_invocation = _output_field(output, PERSISTED_INVOCATION_ID_FIELD)
    if type(receipt_id) is not int or receipt_id <= 0:
        return None
    if output_household != household_id or output_invocation != run_id:
        return None
    from app import db

    if not db.receipt_matches_run(receipt_id, household_id, run_id):
        return None
    return receipt_id


async def process_webhook_async(
    From: str, To: str, Body: str | None, MediaUrl0: str | None
):
    """Background task: membership-gated WhatsApp agent run + optional receipt broadcast.

    The HTTP webhook verifies the Twilio signature before scheduling this task.
    Unknown senders are silently ignored (no agent run). Media downloads use
    SEC-02B checks. Household broadcast happens only when memory_engine reports
    a persisted receipt id from a successful save.
    """
    sys_logger = logging.getLogger("twilio_integration")
    runner = app.state.runner
    agent_app_name = app.state.agent_app_name

    sender = resolve_twilio_sender(From)
    if sender is None:
        sys_logger.info("Ignoring WhatsApp message from unregistered sender")
        return

    user_id = sender.user.user_id
    household_id = sender.household_id
    binding = get_or_create_twilio_session_binding(user_id, household_id)
    session_id = binding["session_id"]
    trusted_state = {AUTHORIZED_HOUSEHOLD_STATE_KEY: household_id}

    try:
        session = await runner.session_service.get_session(
            app_name=agent_app_name, session_id=session_id, user_id=user_id
        )
        if not session:
            session = await runner.session_service.create_session(
                user_id=user_id,
                app_name=agent_app_name,
                session_id=session_id,
                state=trusted_state,
            )
        else:
            session.state[AUTHORIZED_HOUSEHOLD_STATE_KEY] = household_id

        parts = []
        text_message = Body.strip() if Body else ""

        if MediaUrl0:
            try:
                downloaded = download_twilio_media(MediaUrl0)
            except MediaFetchError as exc:
                sys_logger.info("Twilio media download rejected: %s", exc.reason)
                downloaded = None
            if downloaded is not None:
                parts.append(
                    types.Part.from_bytes(
                        data=downloaded.data, mime_type=downloaded.content_type
                    )
                )
                if not text_message:
                    text_message = "Analyze this receipt image."
            elif not text_message:
                _reply_media_failure(From, To)
                return

        if text_message:
            parts.append(types.Part.from_text(text=text_message))

        if not parts:
            sys_logger.warning("No parts to process in background webhook.")
            return

        content = types.Content(role="user", parts=parts)
        response_text = ""
        produced_text = False
        persisted_receipt_id = None
        run_id = str(uuid.uuid4())

        async for event in runner.run_async(
            new_message=content,
            user_id=user_id,
            session_id=session.id,
            invocation_id=run_id,
        ):
            node_name = event.node_info.name if event.node_info else ""
            receipt_id = _persisted_receipt_for_run(event, run_id, household_id)
            if receipt_id is not None:
                persisted_receipt_id = receipt_id
            if node_name in ("response_agent", "query_agent"):
                if event.content and event.content.parts:
                    for part in event.content.parts:
                        if part.text:
                            response_text += part.text
                            produced_text = True
            elif node_name == "invalid_receipt_responder":
                if event.output:
                    response_text += str(event.output)
                    produced_text = True

        if not response_text:
            response_text = "Sorry, I encountered an issue processing your request."

        account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
        auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
        twilio_number = (
            To  # Use the actual incoming receiver address (To) as outbound From channel
        )

        if account_sid and auth_token and twilio_number:
            # 1. Always reply privately to the sender
            twilio_send_message(
                account_sid, auth_token, twilio_number, From, response_text
            )

            # 2. Broadcast only after this run inserted a receipt for this household.
            if produced_text and persisted_receipt_id is not None:
                for member in list_broadcast_whatsapp_recipients(
                    household_id, exclude_whatsapp=From
                ):
                    twilio_send_message(
                        account_sid,
                        auth_token,
                        twilio_number,
                        member,
                        response_text,
                    )
    except Exception as exc:
        sys_logger.error("Error in process_webhook_async: %s", type(exc).__name__)


_EMPTY_TWIML = """<?xml version="1.0" encoding="UTF-8"?>
    <Response></Response>"""


def _empty_twiml() -> Response:
    return Response(content=_EMPTY_TWIML, media_type="application/xml")


@app.post("/twilio/webhook")
async def twilio_webhook(request: Request, background_tasks: BackgroundTasks):
    """Receive a Twilio WhatsApp webhook after signature verification.

    Not Firebase-authenticated. X-Twilio-Signature is checked against
    TWILIO_AUTH_TOKEN and the exact TWILIO_WEBHOOK_PUBLIC_URL before any
    sender lookup or background work. A valid request still returns empty
    TwiML immediately. Media downloads are restricted by SEC-02B. At most two
    redirects are followed, and only to api.twilio.com or mms.twiliocdn.com.
    """
    sys_logger = logging.getLogger("twilio_integration")
    try:
        load_twilio_webhook_config()
        params = parse_twilio_form(await request.form())
        verify_twilio_webhook(params, request.headers.get("X-Twilio-Signature"))
        fields = webhook_dispatch_fields(params)
    except TwilioWebhookRejected:
        sys_logger.info("Rejected Twilio webhook")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden"
        ) from None
    except Exception:
        sys_logger.info("Rejected Twilio webhook")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden"
        ) from None

    if fields is None:
        # Authenticated, but From/To are missing or repeated. Do not look up a sender.
        sys_logger.info("Authenticated Twilio webhook had no unambiguous message")
        return _empty_twiml()

    sender, recipient, body, media_url = fields
    sys_logger.info(
        "Received Twilio webhook from %s to %s. Offloading to background task.",
        _mask_phone(sender),
        _mask_phone(recipient),
    )
    background_tasks.add_task(process_webhook_async, sender, recipient, body, media_url)
    return _empty_twiml()


@app.get("/purchases")
def get_purchases(
    household_id: str | None = None,
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Returns past purchases for an authorized household."""
    from app import db

    resolved = _authorized_household(user, household_id)
    return db.get_all_purchases(household_id=resolved)


@app.get("/agent/runs")
def get_runs(
    limit: int = 20,
    household_id: str | None = None,
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Returns logged agent execution history for an authorized household."""
    from app import db

    resolved = _authorized_household(user, household_id)
    return db.get_agent_runs(limit=limit, household_id=resolved)


@app.get("/health")
def health():
    """Checks the health of database connection and MCP deals lookup."""
    from app import db
    from app.interfaces import registry

    db_ok = False
    try:
        with db.get_connection() as conn:
            conn.execute("SELECT 1")
            db_ok = True
    except Exception:
        pass

    mcp_ok = False
    try:
        registry.deals_client.lookup_price("test_nonexistent_product_healthcheck")
        mcp_ok = True
    except Exception:
        pass

    if not db_ok or not mcp_ok:
        return Response(
            content=f'{{"status": "unhealthy", "database": {str(db_ok).lower()}, "mcp": {str(mcp_ok).lower()}}}',
            status_code=500,
            media_type="application/json",
        )

    return {"status": "healthy", "database": db_ok, "mcp": mcp_ok}


@app.get("/ready")
def ready():
    """Checks the readiness of the application."""
    res = health()
    if isinstance(res, Response) and res.status_code == 500:
        return res
    return {"status": "ready"}


@app.post("/feedback")
def collect_feedback(feedback: Feedback) -> dict[str, str]:
    if has_gcp_logging:
        logger.log_struct(feedback.model_dump(), severity="INFO")
    else:
        logger.info(f"Feedback received: {feedback.model_dump()}")
    return {"status": "success"}


# --- Serve Demo Web UI ---

static_dir = os.path.join(AGENT_DIR, "app", "static")
os.makedirs(static_dir, exist_ok=True)


@app.get("/")
@app.get("/chat")
def read_root():
    return FileResponse(os.path.join(static_dir, "index.html"))


app.mount("/static", StaticFiles(directory=static_dir), name="static")


# Main execution
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
