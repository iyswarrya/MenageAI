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
import os
import logging
from collections.abc import AsyncIterator
from dotenv import load_dotenv
from fastapi import FastAPI, Response, UploadFile, File, Form, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import google.auth
from a2a.server.tasks import InMemoryTaskStore
from google.adk.cli.fast_api import get_fast_api_app
from google.adk.runners import Runner
from google.genai import types

from app.app_utils import services
from app.app_utils.a2a import attach_a2a_routes
from app.app_utils.telemetry import setup_telemetry
from app.app_utils.typing import Feedback

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
    from app.agent import root_agent

    runner = Runner(
        app=adk_app,
        session_service=services.get_session_service(),
        artifact_service=services.get_artifact_service(),
        auto_create_session=True,
    )
    app.state.runner = runner
    app.state.agent_app_name = adk_app.name
    await attach_a2a_routes(
        app,
        agent=root_agent,
        runner=runner,
        task_store=InMemoryTaskStore(),
        rpc_path=f"/a2a/{adk_app.name}",
    )
    yield


app: FastAPI = get_fast_api_app(
    agents_dir=AGENT_DIR,
    web=True,
    artifact_service_uri=services.ARTIFACT_SERVICE_URI,
    allow_origins=allow_origins,
    session_service_uri=services.SESSION_SERVICE_URI,
    otel_to_cloud=False,
    lifespan=lifespan,
)
app.title = "family-receipt-agent"
app.description = "API for interacting with the Agent family-receipt-agent"

# Remove default health route to allow custom health route to take precedence
app.routes[:] = [r for r in app.routes if getattr(r, "path", None) != "/health"]


# --- Schemas ---

class MessageRequest(BaseModel):
    message: str
    user_id: str = "default_user"
    session_id: str = "default_session"


# --- Custom Routes ---

@app.post("/agent/message")
async def agent_message(req: MessageRequest):
    """Executes the agent workflow with user input."""
    runner = app.state.runner
    content = types.Content(role="user", parts=[types.Part.from_text(text=req.message)])
    
    # Ensure session exists or create a new one
    session = await runner.session_service.get_session(
        app_name=app.state.agent_app_name, 
        session_id=req.session_id,
        user_id=req.user_id
    )
    if not session:
        session = await runner.session_service.create_session(
            user_id=req.user_id, 
            app_name=app.state.agent_app_name, 
            session_id=req.session_id
        )
        
    response_text = ""
    async for event in runner.run_async(
        new_message=content,
        user_id=req.user_id,
        session_id=session.id
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
    return {"response": response_text, "session_id": session.id}


@app.post("/agent/upload")
async def agent_upload(
    file: UploadFile = File(...),
    user_id: str = Form("default_user"),
    session_id: str = Form("default_session")
):
    """Executes the agent workflow with an uploaded receipt file."""
    runner = app.state.runner
    file_bytes = await file.read()
    content_type = file.content_type or "image/jpeg"
    
    parts = [
        types.Part.from_bytes(data=file_bytes, mime_type=content_type),
        types.Part.from_text(text="Analyze this receipt image.")
    ]
    content = types.Content(role="user", parts=parts)
    
    session = await runner.session_service.get_session(
        app_name=app.state.agent_app_name, 
        session_id=session_id,
        user_id=user_id
    )
    if not session:
        session = await runner.session_service.create_session(
            user_id=user_id, 
            app_name=app.state.agent_app_name, 
            session_id=session_id
        )
        
    response_text = ""
    async for event in runner.run_async(
        new_message=content,
        user_id=user_id,
        session_id=session.id
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
    return {"response": response_text, "session_id": session.id}


def twilio_send_message(account_sid: str, auth_token: str, from_num: str, to_num: str, body: str):
    import requests
    sys_logger = logging.getLogger("twilio_integration")
    sys_logger.info(f"Starting Twilio WhatsApp broadcast to {to_num}...")
    try:
        resp = requests.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json",
            auth=(account_sid, auth_token),
            data={
                "From": from_num,
                "To": to_num,
                "Body": body
            },
            timeout=15
        )
        sys_logger.info(f"Twilio WhatsApp broadcast to {to_num} status code: {resp.status_code}")
        if resp.status_code not in (200, 201):
            sys_logger.error(f"Twilio error response body: {resp.text}")
    except Exception as e:
        sys_logger.error(f"Failed to broadcast Twilio WhatsApp to {to_num}: {e}")


async def process_webhook_async(From: str, To: str, Body: str | None, MediaUrl0: str | None):
    """Background task to run the ADK agent workflow and send the responses back via Twilio REST API."""
    import requests
    sys_logger = logging.getLogger("twilio_integration")
    runner = app.state.runner
    agent_app_name = app.state.agent_app_name
    
    session_id = "twilio_session_family"
    user_id = f"twilio_user_{From.replace('whatsapp:', '')}"
    
    try:
        session = await runner.session_service.get_session(
            app_name=agent_app_name, 
            session_id=session_id,
            user_id=user_id
        )
        if not session:
            session = await runner.session_service.create_session(
                user_id=user_id, 
                app_name=agent_app_name, 
                session_id=session_id
            )
            
        parts = []
        text_message = Body.strip() if Body else ""
        is_receipt_upload = False
        
        if MediaUrl0:
            is_receipt_upload = True
            try:
                account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
                auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
                auth = (account_sid, auth_token) if account_sid and auth_token else None
                
                resp = requests.get(MediaUrl0, auth=auth, timeout=15)
                sys_logger.info(f"Twilio media download status code: {resp.status_code}")
                
                if resp.status_code == 200:
                    content_type = resp.headers.get("content-type", "image/jpeg")
                    parts.append(
                        types.Part.from_bytes(data=resp.content, mime_type=content_type)
                    )
                    if not text_message:
                        text_message = "Analyze this receipt image."
                else:
                    sys_logger.warning(f"Failed to download Twilio media: HTTP {resp.status_code}")
            except Exception as e:
                sys_logger.error(f"Error downloading Twilio media: {e}")
            
        if text_message:
            parts.append(types.Part.from_text(text=text_message))
            
        if not parts:
            sys_logger.warning("No parts to process in background webhook.")
            return
            
        content = types.Content(role="user", parts=parts)
        response_text = ""
        intent_detected = "query"
        
        async for event in runner.run_async(
            new_message=content,
            user_id=user_id,
            session_id=session.id
        ):
            node_name = event.node_info.name if event.node_info else ""
            if node_name == "intent_classifier" and event.output:
                try:
                    if hasattr(event.output, "intent"):
                        intent_detected = event.output.intent
                    elif isinstance(event.output, str):
                        import json
                        intent_detected = json.loads(event.output).get("intent", "query")
                except Exception:
                    pass
            elif node_name in ("response_agent", "query_agent"):
                if event.content and event.content.parts:
                    for part in event.content.parts:
                        if part.text:
                            response_text += part.text
            elif node_name == "invalid_receipt_responder":
                if event.output:
                    response_text += str(event.output)
                    
        if not response_text:
            response_text = "Sorry, I encountered an issue processing your request."
            
        account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
        auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
        twilio_number = To  # Use the actual incoming receiver address (To) as outbound From channel
        
        if account_sid and auth_token and twilio_number:
            # 1. Send the response back directly to the sender
            twilio_send_message(account_sid, auth_token, twilio_number, From, response_text)
            
            # 2. Proactive Broadcast:
            has_validation_failed = "could not reliably read" in response_text.lower() or "invalid receipt" in response_text.lower()
            if (intent_detected == "receipt" or is_receipt_upload) and not has_validation_failed:
                family_numbers_str = os.environ.get("FAMILY_WHATSAPP_NUMBERS", "")
                if family_numbers_str:
                    family_members = [n.strip() for n in family_numbers_str.split(",") if n.strip()]
                    for member in family_members:
                        if member == From:
                            # Skip sending duplicate to the sender
                            continue
                        twilio_send_message(account_sid, auth_token, twilio_number, member, response_text)
    except Exception as e:
        sys_logger.error(f"Error in process_webhook_async: {e}", exc_info=True)


@app.post("/twilio/webhook")
async def twilio_webhook(
    background_tasks: BackgroundTasks,
    Body: str = Form(None),
    From: str = Form(...),
    To: str = Form(...),
    MediaUrl0: str = Form(None)
):
    """Receives WhatsApp incoming messages directly from Twilio,
    spawns a background task to process it asynchronously,
    and returns an empty TwiML response immediately to prevent timeout.
    """
    sys_logger = logging.getLogger("twilio_integration")
    sys_logger.info(f"Received Twilio Webhook from {From} to {To}. Offloading to background task...")
    
    background_tasks.add_task(
        process_webhook_async,
        From,
        To,
        Body,
        MediaUrl0
    )
    
    # Return empty Response immediately to satisfy Twilio's 15s timeout window
    twiml_response = """<?xml version="1.0" encoding="UTF-8"?>
    <Response></Response>"""
    return Response(content=twiml_response, media_type="application/xml")


@app.get("/purchases")
def get_purchases():
    """Returns all past purchases from the SQLite database."""
    from app import db
    return db.get_all_purchases()


@app.get("/agent/runs")
def get_runs(limit: int = 20):
    """Returns logged agent execution history runs."""
    from app import db
    return db.get_agent_runs(limit=limit)


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
            media_type="application/json"
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


# --- Serve Demo Web UI (Commented Out) ---
# 
# static_dir = os.path.join(AGENT_DIR, "app", "static")
# os.makedirs(static_dir, exist_ok=True)
# 
# # Mount index.html at root "/" and "/chat"
# @app.get("/")
# @app.get("/chat")
# def read_root():
#     return FileResponse(os.path.join(static_dir, "index.html"))
# 
# # Mount all other static files
# app.mount("/static", StaticFiles(directory=static_dir), name="static")


# Main execution
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
