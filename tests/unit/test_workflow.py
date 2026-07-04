import os
import pytest
from dotenv import load_dotenv
load_dotenv()

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from app.agent import root_agent
from app import db
from app import pii

# Setup a clean test database for each test run
@pytest.fixture(autouse=True)
def setup_test_db(monkeypatch, tmp_path):
    """Overrides DB_PATH to point to a temporary file for tests and initializes it."""
    test_db_file = tmp_path / "test_family_receipts.db"
    monkeypatch.setattr(db, "DB_PATH", str(test_db_file))
    db.init_db()
    yield

@pytest.mark.asyncio
async def test_pii_masking_regex():
    """Verify that pii masking utility replaces card, email, phone numbers."""
    raw_text = "My card is 4111 1111 1111 1111, contact test@example.com or call 555-0199."
    masked = pii.mask_pii(raw_text)
    assert "4111" not in masked
    assert "test@example.com" not in masked
    assert "555-0199" not in masked
    assert "[REDACTED CARD]" in masked
    assert "[REDACTED EMAIL]" in masked
    assert "[REDACTED PHONE]" in masked

@pytest.mark.asyncio
async def test_new_receipt_ingestion_and_deals():
    """Test 1 & 4: New receipt ingestion and mock deal alert."""
    session_service = InMemorySessionService()
    session = await session_service.create_session(user_id="test_user", app_name="app")
    runner = Runner(agent=root_agent, session_service=session_service, app_name="app")

    # Send receipt with higher price than the mock deal (Apple deal price is 1.49)
    message = types.Content(
        role="user",
        parts=[types.Part.from_text(text="Whole Foods receipt, July 1, 2026. Apples $3.99. Total $3.99")]
    )

    response_text = ""
    async for event in runner.run_async(
        new_message=message,
        user_id="test_user",
        session_id=session.id
    ):
        if event.content and event.content.parts:
            for part in event.content.parts:
                if part.text:
                    response_text += part.text

    # Verify receipt details
    assert "Whole Foods" in response_text or "whole foods" in response_text.lower()
    assert "Apples" in response_text or "apples" in response_text.lower()
    assert "$3.99" in response_text

    # Verify deal warning (Apples deal)
    assert "$1.49" in response_text
    assert any(w in response_text.lower() for w in ["deal", "save", "special", "savings"])

    # Verify it was saved in the database
    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT store, total FROM receipts")
        row = cursor.fetchone()
        assert row is not None
        assert "Whole Foods" in row["store"]
        assert abs(row["total"] - 3.99) < 0.01

@pytest.mark.asyncio
async def test_duplicate_receipt_detection():
    """Test 2: Duplicate receipt detection."""
    # Pre-save a receipt to database
    db.save_receipt_and_items("Safeway", "2026-07-01", 6.48, [("Milk", 3.49), ("Bread", 2.99)])

    session_service = InMemorySessionService()
    session = await session_service.create_session(user_id="test_user", app_name="app")
    runner = Runner(agent=root_agent, session_service=session_service, app_name="app")

    # Send duplicate receipt
    message = types.Content(
        role="user",
        parts=[types.Part.from_text(text="Safeway receipt, July 1, 2026. Milk $3.49, Bread $2.99. Total $6.48")]
    )

    response_text = ""
    async for event in runner.run_async(
        new_message=message,
        user_id="test_user",
        session_id=session.id
    ):
        if event.content and event.content.parts:
            for part in event.content.parts:
                if part.text:
                    response_text += part.text

    # Verify duplicate warning was included in the WhatsApp response
    assert "duplicate" in response_text.lower() or "⚠️" in response_text

@pytest.mark.asyncio
async def test_memory_question_answering():
    """Test 3: Memory question answering."""
    # Pre-save a unique item in database
    db.save_receipt_and_items("Trader Joe's", "2026-07-01", 12.99, [("Batteries", 12.99)])

    session_service = InMemorySessionService()
    session = await session_service.create_session(user_id="test_user", app_name="app")
    runner = Runner(agent=root_agent, session_service=session_service, app_name="app")

    # Ask memory question
    message = types.Content(
        role="user",
        parts=[types.Part.from_text(text="When did we buy batteries?")]
    )

    response_text = ""
    async for event in runner.run_async(
        new_message=message,
        user_id="test_user",
        session_id=session.id
    ):
        if event.content and event.content.parts:
            for part in event.content.parts:
                if part.text:
                    response_text += part.text

    # Verify response answers the question using database info
    assert "Trader Joe's" in response_text or "trader joe" in response_text.lower()
    assert "Batteries" in response_text or "batteries" in response_text.lower()
    assert "2026" in response_text
    assert "July" in response_text or "07-01" in response_text or "7-01" in response_text

@pytest.mark.asyncio
async def test_pii_masking_integration():
    """Test 5: PII masking integration in receipt processing."""
    session_service = InMemorySessionService()
    session = await session_service.create_session(user_id="test_user", app_name="app")
    runner = Runner(agent=root_agent, session_service=session_service, app_name="app")

    # Send receipt containing sensitive card, phone, and email information
    message = types.Content(
        role="user",
        parts=[types.Part.from_text(
            text="Safeway receipt, July 1, 2026. Milk $2.99. Total $2.99. Card: 4111 1111 1111 1111. Phone: 555-0199. Email: test@example.com"
        )]
    )

    response_text = ""
    async for event in runner.run_async(
        new_message=message,
        user_id="test_user",
        session_id=session.id
    ):
        if event.content and event.content.parts:
            for part in event.content.parts:
                if part.text:
                    response_text += part.text

    # Ensure sensitive info was redacted and does NOT appear in response
    assert "4111" not in response_text
    assert "555-0199" not in response_text
    assert "test@example.com" not in response_text

    # Ensure the receipt was still ingested successfully
    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT store, total FROM receipts")
        row = cursor.fetchone()
        assert row is not None
        assert "Safeway" in row["store"]


@pytest.mark.asyncio
async def test_invalid_receipt_flow():
    """Test invalid receipt flow."""
    session_service = InMemorySessionService()
    session = await session_service.create_session(user_id="test_user", app_name="app")
    runner = Runner(agent=root_agent, session_service=session_service, app_name="app")

    message = types.Content(
        role="user",
        parts=[
            types.Part.from_text(text="Analyze this receipt image."),
            types.Part.from_text(text="Fake receipt text with zero matches")
        ]
    )

    print("STARTING INVALID RECEIPT TEST RUN")
    response_text = ""
    async for event in runner.run_async(
        new_message=message,
        user_id="test_user",
        session_id=session.id
    ):
        node_name = event.node_info.name if event.node_info else "unknown"
        print(f"EVENT: node={node_name}, output={getattr(event, 'output', None)}")
        if node_name == "invalid_receipt_responder":
            if event.output:
                response_text += str(event.output)

    print(f"FINISHED RUN, RESPONSE: {response_text}")
    assert "could not reliably read" in response_text
