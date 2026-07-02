import os
import json
import pytest
from fastapi.testclient import TestClient
from app.fast_api_app import app
from app import db
from app.interfaces import registry

# Clean DB setup for tests
@pytest.fixture(autouse=True)
def clean_db(monkeypatch, tmp_path):
    """Overrides DB_PATH to point to a temporary file for tests and initializes it."""
    test_db_file = tmp_path / "test_demo_runs.db"
    monkeypatch.setattr(db, "DB_PATH", str(test_db_file))
    db.init_db()
    # Patch mcp server DB_PATH too
    import mcp_server.retail_server
    monkeypatch.setattr(mcp_server.retail_server, "DB_PATH", str(test_db_file))
    yield

def test_health_and_ready_endpoints(clean_db):
    """Verify health and ready checks return 200 and correct status."""
    with TestClient(app) as client:
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["database"] is True
        assert data["mcp"] is True

        response_ready = client.get("/ready")
        assert response_ready.status_code == 200
        assert response_ready.json()["status"] == "ready"

def test_agent_message_receipt_flow(clean_db, monkeypatch):
    """Verify posting a receipt message sanitizes, runs graph, saves items, and logs run."""
    monkeypatch.setenv("USE_MCP_DEALS", "true")
    
    with TestClient(app) as client:
        payload = {
            "message": "Safeway receipt, July 2, 2026. Coffee $9.99. Total $9.99",
            "user_id": "test_user_demo",
            "session_id": "session_demo_1"
        }
        response = client.post("/agent/message", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert "response" in data
        assert data["session_id"] == "session_demo_1"

        # Verify receipt was saved to database
        purchases = client.get("/purchases").json()
        assert len(purchases) >= 1
        assert purchases[0]["store"] == "Safeway"
        assert purchases[0]["item_name"] == "Coffee"
        assert purchases[0]["price"] == 9.99

        # Verify agent run was logged to agent_runs
        runs = client.get("/agent/runs").json()
        assert len(runs) >= 1
        assert runs[0]["intent"] == "receipt"
        assert runs[0]["route_taken"] == "receipt"
        assert runs[0]["pii_redacted_count"] == 0
        assert runs[0]["mcp_success"] is True

def test_agent_message_query_flow(clean_db):
    """Verify memory queries bypass receipt ingestion and search history."""
    # Pre-seed database with a purchase
    db.save_receipt_and_items("Trader Joe's", "2026-07-01", 5.99, [("Avocados", 5.99)])
    
    with TestClient(app) as client:
        payload = {
            "message": "When did we buy avocados?",
            "user_id": "test_user_demo",
            "session_id": "session_demo_2"
        }
        response = client.post("/agent/message", json=payload)
        assert response.status_code == 200
        response_text = response.json()["response"]
        assert "Trader" in response_text or "trader" in response_text.lower()

        # Verify run was logged and tools_called includes query_purchase_history
        runs = client.get("/agent/runs").json()
        assert len(runs) >= 1
        assert runs[0]["intent"] == "query"
        assert runs[0]["route_taken"] == "query"
        assert "query_purchase_history" in runs[0]["tools_called"]

def test_mcp_fallback_behavior_in_api(clean_db, monkeypatch):
    """Verify that if MCP server fails during API run, it falls back and logs mcp_success=False."""
    monkeypatch.setenv("USE_MCP_DEALS", "true")

    with TestClient(app) as client:
        # Force MCP fallback by wrapping fallback client with non-existent server command
        from app.services import MCPDealsClient
        original_deals = registry.deals_client
        registry.deals_client = MCPDealsClient(fallback_client=original_deals.fallback_client, command="invalid-mcp-cmd")

        try:
            payload = {
                "message": "Whole Foods receipt, July 2, 2026. Apples $3.99. Total $3.99",
                "user_id": "test_user_demo",
                "session_id": "session_demo_3"
            }
            response = client.post("/agent/message", json=payload)
            assert response.status_code == 200

            # Verify the run was logged successfully and mcp_success is False (fallback occurred)
            runs = client.get("/agent/runs").json()
            assert len(runs) >= 1
            assert runs[0]["mcp_success"] is False
        finally:
            registry.deals_client = original_deals
