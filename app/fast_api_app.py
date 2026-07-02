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
from fastapi import FastAPI, Response
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
        if event.content and event.content.parts:
            for part in event.content.parts:
                if part.text:
                    response_text += part.text
    return {"response": response_text, "session_id": session.id}


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


# --- Serve Demo Web UI ---

static_dir = os.path.join(AGENT_DIR, "app", "static")
os.makedirs(static_dir, exist_ok=True)

# Mount index.html at root "/"
@app.get("/")
def read_root():
    return FileResponse(os.path.join(static_dir, "index.html"))

# Mount all other static files
app.mount("/static", StaticFiles(directory=static_dir), name="static")


# Main execution
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
