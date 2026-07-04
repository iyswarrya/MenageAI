import os
import sqlite3
import pytest
from app import db

@pytest.fixture(autouse=True)
def setup_test_db(monkeypatch, tmp_path):
    """Overrides DB_PATH to point to a temporary file for tests and initializes it."""
    test_db_file = tmp_path / "test_family_receipts.db"
    monkeypatch.setattr(db, "DB_PATH", str(test_db_file))
    db.init_db()
    yield

def test_init_db():
    """Verifies that tables are created and mock deals are seeded."""
    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [row[0] for row in cursor.fetchall()]
        assert "receipts" in tables
        assert "items" in tables
        assert "mock_deals" in tables

        cursor.execute("SELECT COUNT(*) FROM mock_deals")
        assert cursor.fetchone()[0] > 0

def test_save_receipt_and_items():
    """Verifies saving a receipt inserts rows correctly."""
    items = [("Milk", 3.49), ("Eggs", 2.99)]
    receipt_id = db.save_receipt_and_items("Costco", "2026-07-01", 6.48, items)
    assert receipt_id > 0

    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT store, date, total FROM receipts WHERE id = ?", (receipt_id,))
        receipt = cursor.fetchone()
        assert receipt["store"] == "Costco"
        assert receipt["date"] == "2026-07-01"
        assert receipt["total"] == 6.48

        cursor.execute("SELECT name, price FROM items WHERE receipt_id = ?", (receipt_id,))
        saved_items = [(row["name"], row["price"]) for row in cursor.fetchall()]
        assert len(saved_items) == 2
        assert ("Milk", 3.49) in saved_items

def test_check_duplicate_receipt():
    """Verifies receipt duplicate detection."""
    items = [("Milk", 3.49)]
    db.save_receipt_and_items("Costco", "2026-07-01", 3.49, items)

    # Exact match should be duplicate
    assert db.check_duplicate_receipt("Costco", "2026-07-01", 3.49) is True
    # Different date is not duplicate
    assert db.check_duplicate_receipt("Costco", "2026-07-02", 3.49) is False
    # Different total is not duplicate
    assert db.check_duplicate_receipt("Costco", "2026-07-01", 3.99) is False

def test_check_duplicate_items():
    """Verifies item duplicate detection (within 1 day window)."""
    items = [("Organic Milk", 3.49)]
    db.save_receipt_and_items("Safeway", "2026-07-01", 3.49, items)

    # Check same item on same day
    dups_same_day = db.check_duplicate_items("Safeway", "2026-07-01", [("Organic Milk", 3.49)])
    assert len(dups_same_day) == 1
    assert "Organic Milk" in dups_same_day[0]

    # Check same item within 1 day (e.g. June 30)
    dups_prev_day = db.check_duplicate_items("Safeway", "2026-06-30", [("Milk", 3.49)])
    assert len(dups_prev_day) == 1

    # Check different store - should not trigger duplicate
    dups_diff_store = db.check_duplicate_items("Costco", "2026-07-01", [("Organic Milk", 3.49)])
    assert len(dups_diff_store) == 0

def test_get_deals_for_items():
    """Verifies retrieval of matching, lower-priced mock deals."""
    # Seed a custom mock deal directly to control the prices
    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM mock_deals")
        cursor.execute(
            "INSERT INTO mock_deals (item_name, store, deal_price, details) VALUES (?, ?, ?, ?)",
            ("Coffee", "Safeway", 5.99, "Custom Coffee Deal")
        )
        conn.commit()

    # Query with a price higher than the deal -> should trigger alert
    alerts = db.get_deals_for_items([("Starbucks Coffee bag", 8.99)])
    assert len(alerts) == 1
    assert alerts[0]["deal_price"] == 5.99
    assert alerts[0]["current_price"] == 8.99

    # Query with a price lower than or equal to the deal -> should not trigger alert
    alerts_no_deal = db.get_deals_for_items([("Starbucks Coffee bag", 4.99)])
    assert len(alerts_no_deal) == 0

def test_query_purchase_history():
    """Verifies querying purchase history returns matching records using token splitting and fallback."""
    db.save_receipt_and_items("Trader Joe's", "2026-07-01", 10.99, [("Olive Oil", 8.99), ("Salt", 2.00)])
    db.save_receipt_and_items("Target", "2026-06-10", 10.00, [("Shirts T", 10.00)])

    # 1. Search for item name token (multi-word, hyphenated query)
    results_tshirts = db.query_purchase_history("T-shirts")["results"]
    assert len(results_tshirts) >= 1
    # Check that "Shirts T" matches due to the "shirts" token
    matching_shirts = [r for r in results_tshirts if r["item_name"] == "Shirts T"]
    assert len(matching_shirts) == 1

    # 2. Search for store name (partial)
    results_store = db.query_purchase_history("Trader")["results"]
    assert len(results_store) >= 2  # Olive Oil and Salt (and potentially fallback matches)
    assert any(r["store"] == "Trader Joe's" for r in results_store)

def test_household_isolation():
    """Verifies that queries, duplicate checks, and retrieves are completely isolated by household_id."""
    items1 = [("Coffee", 9.99)]
    db.save_receipt_and_items("Safeway", "2026-07-01", 9.99, items1, household_id="family_1")

    items2 = [("Coffee", 9.99)]
    db.save_receipt_and_items("Safeway", "2026-07-01", 9.99, items2, household_id="family_2")

    # Duplicate check on family_1: should find duplicate
    assert db.check_duplicate_receipt("Safeway", "2026-07-01", 9.99, household_id="family_1") is True
    # Duplicate check on default family: should not find duplicate
    assert db.check_duplicate_receipt("Safeway", "2026-07-01", 9.99, household_id="default") is False

    # Item duplicate check on family_1: should find duplicate
    dup_family_1 = db.check_duplicate_items("Safeway", "2026-07-01", [("Coffee", 9.99)], household_id="family_1")
    assert len(dup_family_1) == 1
    # Item duplicate check on default family: should not find duplicate
    dup_default = db.check_duplicate_items("Safeway", "2026-07-01", [("Coffee", 9.99)], household_id="default")
    assert len(dup_default) == 0

    # Query purchase history on family_1: should find Coffee
    results_family_1 = db.query_purchase_history("Coffee", household_id="family_1")["results"]
    assert any(r["item_name"] == "Coffee" for r in results_family_1)
    # Query purchase history on default: should not find Coffee
    results_default = db.query_purchase_history("Coffee", household_id="default")["results"]
    assert not any(r["item_name"] == "Coffee" for r in results_default)

    # Get all purchases: family_1 should have 1 item
    assert len(db.get_all_purchases(household_id="family_1")) == 1
    # Get all purchases: default should have 0 items
    assert len(db.get_all_purchases(household_id="default")) == 0
