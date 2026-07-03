import os
import sqlite3
import json
from datetime import datetime, timedelta
from typing import List, Dict, Any, Tuple, Optional

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "family_receipts.db")

def get_connection():
    """Returns a connection to the SQLite database."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Initializes tables and seeds mock deals if not present."""
    with get_connection() as conn:
        cursor = conn.cursor()
        
        # Create receipts table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS receipts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                store TEXT NOT NULL,
                date TEXT NOT NULL,
                total REAL NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Create items table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                receipt_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                price REAL NOT NULL,
                FOREIGN KEY (receipt_id) REFERENCES receipts(id)
            )
        """)
        
        # Create mock_deals table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS mock_deals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_name TEXT NOT NULL,
                store TEXT NOT NULL,
                deal_price REAL NOT NULL,
                details TEXT NOT NULL
            )
        """)
        
        # Create agent_runs table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS agent_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT NOT NULL,
                household_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                intent TEXT NOT NULL,
                route_taken TEXT NOT NULL,
                tools_called TEXT NOT NULL,
                mcp_success INTEGER NOT NULL,
                pii_redacted_count INTEGER NOT NULL,
                errors TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Seed mock deals if table is empty
        cursor.execute("SELECT COUNT(*) FROM mock_deals")
        if cursor.fetchone()[0] == 0:
            mock_deals = [
                ("Milk", "Costco", 2.89, "Costco has organic whole milk for $2.89 (regularly $3.89)"),
                ("Eggs", "Target", 3.29, "Target Circle deal: 12-count Grade A Eggs for $3.29"),
                ("Bread", "Safeway", 1.99, "Safeway Club Card: Fresh sourdough for $1.99"),
                ("Avocados", "Trader Joe's", 0.99, "Trader Joe's organic avocados at $0.99 each"),
                ("Apples", "Whole Foods", 1.49, "Whole Foods Prime Member special: Honeycrisp apples for $1.49/lb"),
                ("Coffee", "Starbucks", 7.99, "Starbucks ground coffee 12oz bag on sale for $7.99 at Safeway"),
                ("Paper Towels", "Costco", 18.99, "Costco member coupon: 12-roll Bounty paper towels for $18.99"),
                ("Hose Nozzle", "Walmart", 14.99, "Walmart has the FANHAO Garden Hose Nozzle on sale for $14.99 (regularly $24.99)")
            ]
            cursor.executemany(
                "INSERT INTO mock_deals (item_name, store, deal_price, details) VALUES (?, ?, ?, ?)",
                mock_deals
            )
            
        # Always ensure Hose Nozzle is seeded in existing databases
        cursor.execute("SELECT COUNT(*) FROM mock_deals WHERE item_name = 'Hose Nozzle'")
        if cursor.fetchone()[0] == 0:
            cursor.execute(
                "INSERT INTO mock_deals (item_name, store, deal_price, details) VALUES (?, ?, ?, ?)",
                ("Hose Nozzle", "Walmart", 14.99, "Walmart has the FANHAO Garden Hose Nozzle on sale for $14.99 (regularly $24.99)")
            )
        conn.commit()

def check_duplicate_receipt(store: str, date: str, total: float) -> bool:
    """Checks if a receipt with same store, date, and total already exists."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id FROM receipts WHERE LOWER(store) = LOWER(?) AND date = ? AND ABS(total - ?) < 0.01",
            (store.strip(), date.strip(), total)
        )
        return cursor.fetchone() is not None

def check_duplicate_items(store: str, date_str: str, items: List[Tuple[str, float]]) -> List[str]:
    """
    Checks if any of the items in the receipt were purchased recently (within 1 day).
    Returns list of duplicate item messages.
    """
    duplicates = []
    
    # Parse incoming date
    try:
        current_date = datetime.strptime(date_str.strip(), "%Y-%m-%d")
    except ValueError:
        # Fallback if format is not YYYY-MM-DD
        return []
        
    start_date = (current_date - timedelta(days=1)).strftime("%Y-%m-%d")
    end_date = (current_date + timedelta(days=1)).strftime("%Y-%m-%d")
    
    with get_connection() as conn:
        cursor = conn.cursor()
        for item_name, item_price in items:
            # Query items purchased at same store within 1 day window
            cursor.execute("""
                SELECT i.name, r.date, r.id FROM items i
                JOIN receipts r ON i.receipt_id = r.id
                WHERE LOWER(r.store) = LOWER(?) 
                  AND r.date BETWEEN ? AND ? 
                  AND LOWER(i.name) LIKE ?
            """, (store.strip(), start_date, end_date, f"%{item_name.lower().strip()}%"))
            
            matches = cursor.fetchall()
            if matches:
                for match in matches:
                    duplicates.append(
                        f"Item '{item_name}' seems to match previous purchase '{match['name']}' at {store} on {match['date']} (Receipt #{match['id']})."
                    )
    return duplicates

def save_receipt_and_items(store: str, date: str, total: float, items: List[Tuple[str, float]]) -> int:
    """Saves a receipt and its associated items to SQLite. Returns receipt ID."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO receipts (store, date, total) VALUES (?, ?, ?)",
            (store.strip(), date.strip(), total)
        )
        receipt_id = cursor.lastrowid
        
        for name, price in items:
            cursor.execute(
                "INSERT INTO items (receipt_id, name, price) VALUES (?, ?, ?)",
                (receipt_id, name.strip(), price)
            )
        conn.commit()
        return receipt_id

def get_deals_for_items(items: List[Tuple[str, float]]) -> List[Dict[str, Any]]:
    """Checks for deals or better prices in the database for the given items."""
    deals_alerts = []
    with get_connection() as conn:
        cursor = conn.cursor()
        for name, price in items:
            # Match deal by item name substring
            cursor.execute(
                "SELECT item_name, store, deal_price, details FROM mock_deals WHERE LOWER(?) LIKE '%' || LOWER(item_name) || '%'",
                (name.lower().strip(),)
            )
            matches = cursor.fetchall()
            for match in matches:
                # Alert if match deal price is less than current purchase price
                if match['deal_price'] < price:
                    deals_alerts.append({
                        "item_name": name,
                        "current_price": price,
                        "deal_price": match['deal_price'],
                        "store": match['store'],
                        "details": match['details']
                    })
    return deals_alerts

def query_purchase_history(search_term: str) -> Dict[str, Any]:
    """
    Searches the database for past purchases of items matching the search term.
    
    Args:
        search_term: The search term to find in item names or store names.

    Returns:
        A dict containing 'results', which is a list of matching purchase records.
    """
    import re
    cleaned = re.sub(r'[^\w\s]', ' ', search_term)
    tokens = [t.lower().strip() for t in cleaned.split() if len(t.strip()) >= 2]
    
    if not tokens:
        tokens = [search_term.lower().strip()]
        
    query_parts = []
    params = []
    for token in tokens:
        query_parts.append("(LOWER(i.name) LIKE ? OR LOWER(r.store) LIKE ?)")
        params.extend([f"%{token}%", f"%{token}%"])
        
    where_clause = " OR ".join(query_parts)
    
    sql = f"""
        SELECT r.store, r.date, i.name, i.price
        FROM items i
        JOIN receipts r ON i.receipt_id = r.id
        WHERE {where_clause}
        ORDER BY r.date DESC
    """
    
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(sql, params)
        
        results = []
        for row in cursor.fetchall():
            results.append({
                "store": row["store"],
                "date": row["date"],
                "item_name": row["name"],
                "price": row["price"]
            })
            
        # Fallback to appending the latest 50 purchases if we have few targeted matches
        if len(results) < 10:
            cursor.execute("""
                SELECT r.store, r.date, i.name, i.price
                FROM items i
                JOIN receipts r ON i.receipt_id = r.id
                ORDER BY r.date DESC
                LIMIT 50
            """)
            existing = {(row["store"].lower(), row["date"], row["item_name"].lower()) for row in results}
            for row in cursor.fetchall():
                key = (row["store"].lower(), row["date"], row["name"].lower())
                if key not in existing:
                    results.append({
                        "store": row["store"],
                        "date": row["date"],
                        "item_name": row["name"],
                        "price": row["price"]
                    })
        return {"results": results}


def log_agent_run(
    request_id: str,
    household_id: str,
    user_id: str,
    intent: str,
    route_taken: str,
    tools_called: List[str],
    mcp_success: bool,
    pii_redacted_count: int,
    errors: Optional[str] = None
) -> None:
    """Logs the execution metrics of an agent workflow run."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO agent_runs (
                request_id, household_id, user_id, intent, route_taken, 
                tools_called, mcp_success, pii_redacted_count, errors
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            request_id, household_id, user_id, intent, route_taken,
            json.dumps(tools_called), 1 if mcp_success else 0, pii_redacted_count, errors
        ))
        conn.commit()


def get_agent_runs(limit: int = 20) -> List[Dict[str, Any]]:
    """Retrieves the history of agent runs from the database."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, request_id, household_id, user_id, intent, route_taken,
                   tools_called, mcp_success, pii_redacted_count, errors, timestamp
            FROM agent_runs
            ORDER BY id DESC
            LIMIT ?
        """, (limit,))
        results = []
        for r in cursor.fetchall():
            results.append({
                "id": r["id"],
                "request_id": r["request_id"],
                "household_id": r["household_id"],
                "user_id": r["user_id"],
                "intent": r["intent"],
                "route_taken": r["route_taken"],
                "tools_called": json.loads(r["tools_called"]),
                "mcp_success": bool(r["mcp_success"]),
                "pii_redacted_count": r["pii_redacted_count"],
                "errors": r["errors"],
                "timestamp": r["timestamp"]
            })
        return results


def get_all_purchases() -> List[Dict[str, Any]]:
    """Retrieves all past purchases from the database."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT r.store, r.date, i.name, i.price
            FROM items i
            JOIN receipts r ON i.receipt_id = r.id
            ORDER BY r.date DESC
        """)
        results = []
        for row in cursor.fetchall():
            results.append({
                "store": row["store"],
                "date": row["date"],
                "item_name": row["name"],
                "price": row["price"]
            })
        return results

