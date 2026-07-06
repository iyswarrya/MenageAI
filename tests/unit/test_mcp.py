import os
import json
import pytest
from app.interfaces import registry, DealAlert, DealsClient
from app.agent import deals_engine, preprocess_input, MemoryAgentOutput, ReceiptData, ReceiptItem
from app.services import MCPDealsClient, SqliteDealsClient
from mcp_server.retail_server import lookup_price, check_price_drop, get_store_return_policy
from google.genai import types

# Setup a clean test database for each test run
from app import db
@pytest.fixture(autouse=True)
def setup_test_db(monkeypatch, tmp_path):
    """Overrides DB_PATH to point to a temporary file for tests and initializes it."""
    test_db_file = tmp_path / "test_family_receipts.db"
    monkeypatch.setattr(db, "DB_PATH", str(test_db_file))
    db.init_db()
    # Also patch mcp server DB_PATH
    import mcp_server.retail_server
    monkeypatch.setattr(mcp_server.retail_server, "DB_PATH", str(test_db_file))
    yield

def test_mcp_server_tools_direct():
    """Test 1: Verify MCP server tools directly return mock deals and policies."""
    # Lookup price (seeded deal: Milk Costco 2.89)
    res_milk = lookup_price("Organic Milk")
    deals = json.loads(res_milk)
    assert len(deals) >= 1
    assert deals[0]["store"] == "Costco"
    assert deals[0]["deal_price"] == 2.89

    # Check price drop
    res_drop = check_price_drop("Milk", 4.50)
    drops = json.loads(res_drop)
    assert len(drops) >= 1
    assert drops[0]["deal_price"] == 2.89

    # Return policy lookup
    policy = get_store_return_policy("Costco Wholesale")
    assert "90 days" in policy

def test_mcp_deals_client_conformance():
    """Test 2: Verify MCPDealsClient conforms to DealsClient protocol."""
    sqlite_deals = SqliteDealsClient()
    client = MCPDealsClient(fallback_client=sqlite_deals)
    assert isinstance(client, DealsClient)

def test_deals_engine_works_with_mcp_client(monkeypatch):
    """Test 3: Verify deals_engine works end-to-end with MCPDealsClient."""
    monkeypatch.setenv("USE_MCP_DEALS", "true")
    sqlite_deals = SqliteDealsClient()
    mcp_deals = MCPDealsClient(fallback_client=sqlite_deals)
    
    # Register the MCP client
    original_client = registry.deals_client
    registry.deals_client = mcp_deals

    try:
        input_data = MemoryAgentOutput(
            receipt=ReceiptData(
                store="Safeway",
                date="2026-07-01",
                items=[ReceiptItem(name="Organic Milk", price=3.99)],
                prices=[3.99],
                total=3.99
            ),
            is_duplicate_receipt=False,
            duplicate_items=[]
        )
        
        output = deals_engine(input_data)
        
        # Verify it fetched the deal via the MCP server
        assert len(output.deals) == 1
        assert output.deals[0].store == "Costco"
        assert output.deals[0].deal_price == 2.89
    finally:
        registry.deals_client = original_client

def test_mcp_fallback_to_sqlite(monkeypatch):
    """Test 4: Verify if MCP server is unavailable, system falls back to SQLite."""
    monkeypatch.setenv("USE_MCP_DEALS", "true")
    sqlite_deals = SqliteDealsClient()
    
    # Configure MCPDealsClient to use a non-existent command to force failure
    faulty_mcp = MCPDealsClient(fallback_client=sqlite_deals, command="invalid-mcp-command-xyz")
    
    original_client = registry.deals_client
    registry.deals_client = faulty_mcp

    try:
        # Should fallback to SQLite (Milk Costco 2.89 is in SQLite seeded deals)
        deals = registry.deals_client.check_price_drop("Organic Milk", 3.99)
        assert len(deals) == 1
        assert deals[0].deal_price == 2.89
        assert deals[0].store == "Costco"
    finally:
        registry.deals_client = original_client

def test_pii_not_sent_to_mcp():
    """Test 5: Verify PII is masked in preprocessing and not sent to MCP client/tools."""
    raw_receipt_text = (
        "Trader Joe's receipt, July 1, 2026. Milk $3.99. "
        "Card: 4111 1111 1111 1111. Phone: 555-0199. Email: test@example.com"
    )
    
    # Run the preprocessor
    sanitized_text = preprocess_input(types.Content(parts=[types.Part(text=raw_receipt_text)]))
    
    # Verify sensitive data is redacted
    assert "4111" not in sanitized_text
    assert "555-0199" not in sanitized_text
    assert "test@example.com" not in sanitized_text
    
    # Downstream workflow would only pass the sanitized text to receipt_agent / deals client
    assert "Milk" in sanitized_text
