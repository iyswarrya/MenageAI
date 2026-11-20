import json
import os
import sqlite3
from datetime import datetime, timedelta
from typing import Any

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "family_receipts.db")


def get_connection():
    """Returns a connection to the SQLite database with foreign keys enforced."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    """Initializes tables and seeds mock deals if not present."""
    with get_connection() as conn:
        cursor = conn.cursor()

        # --- SEC-01 identity / household / session tables ---
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                display_name TEXT,
                whatsapp_e164 TEXT UNIQUE,
                auth_subject TEXT UNIQUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_users_whatsapp ON users(whatsapp_e164)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_users_auth_subject ON users(auth_subject)"
        )

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS households (
                household_id TEXT PRIMARY KEY,
                name TEXT NOT NULL DEFAULT 'Family',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS household_members (
                household_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'member',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (household_id, user_id),
                FOREIGN KEY (household_id) REFERENCES households(household_id),
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
        """)
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_members_user ON household_members(user_id)"
        )

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS conversation_sessions (
                session_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                household_id TEXT NOT NULL,
                channel TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id),
                FOREIGN KEY (household_id) REFERENCES households(household_id)
            )
        """)
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_sessions_user "
            "ON conversation_sessions(user_id)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_sessions_household "
            "ON conversation_sessions(household_id)"
        )

        # Create receipts table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS receipts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                store TEXT NOT NULL,
                date TEXT NOT NULL,
                total REAL NOT NULL,
                household_id TEXT NOT NULL DEFAULT 'default',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Migration: Ensure receipts table has household_id column
        try:
            cursor.execute(
                "ALTER TABLE receipts ADD COLUMN household_id TEXT NOT NULL DEFAULT 'default'"
            )
        except sqlite3.OperationalError:
            pass

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

        # Create store_policies table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS store_policies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                store_name TEXT UNIQUE NOT NULL,
                policy_text TEXT NOT NULL
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

        # Seed store policies if table is empty
        cursor.execute("SELECT COUNT(*) FROM store_policies")
        if cursor.fetchone()[0] == 0:
            store_policies = [
                (
                    "Costco",
                    "Return most items within 90 days or anytime for 100% satisfaction.",
                ),
                (
                    "Target",
                    "Most unopened items in new condition can be returned within 90 days.",
                ),
                ("Safeway", "Refund or exchange within 15 days with receipt."),
            ]
            cursor.executemany(
                "INSERT OR IGNORE INTO store_policies (store_name, policy_text) VALUES (?, ?)",
                store_policies,
            )

        # Seed mock deals if table is empty
        cursor.execute("SELECT COUNT(*) FROM mock_deals")
        if cursor.fetchone()[0] == 0:
            mock_deals = [
                (
                    "Milk",
                    "Costco",
                    2.89,
                    "Costco has organic whole milk for $2.89 (regularly $3.89)",
                ),
                (
                    "Eggs",
                    "Target",
                    3.29,
                    "Target Circle deal: 12-count Grade A Eggs for $3.29",
                ),
                (
                    "Bread",
                    "Safeway",
                    1.99,
                    "Safeway Club Card: Fresh sourdough for $1.99",
                ),
                (
                    "Avocados",
                    "Trader Joe's",
                    0.99,
                    "Trader Joe's organic avocados at $0.99 each",
                ),
                (
                    "Apples",
                    "Whole Foods",
                    1.49,
                    "Whole Foods Prime Member special: Honeycrisp apples for $1.49/lb",
                ),
                (
                    "Coffee",
                    "Starbucks",
                    7.99,
                    "Starbucks ground coffee 12oz bag on sale for $7.99 at Safeway",
                ),
                (
                    "Paper Towels",
                    "Costco",
                    18.99,
                    "Costco member coupon: 12-roll Bounty paper towels for $18.99",
                ),
                (
                    "Hose Nozzle",
                    "Walmart",
                    14.99,
                    "Walmart has the FANHAO Garden Hose Nozzle on sale for $14.99 (regularly $24.99)",
                ),
            ]
            cursor.executemany(
                "INSERT INTO mock_deals (item_name, store, deal_price, details) VALUES (?, ?, ?, ?)",
                mock_deals,
            )

        # Always ensure Hose Nozzle is seeded in existing databases
        cursor.execute(
            "SELECT COUNT(*) FROM mock_deals WHERE item_name = 'Hose Nozzle'"
        )
        if cursor.fetchone()[0] == 0:
            cursor.execute(
                "INSERT INTO mock_deals (item_name, store, deal_price, details) VALUES (?, ?, ?, ?)",
                (
                    "Hose Nozzle",
                    "Walmart",
                    14.99,
                    "Walmart has the FANHAO Garden Hose Nozzle on sale for $14.99 (regularly $24.99)",
                ),
            )
        conn.commit()


def check_duplicate_receipt(
    store: str, date: str, total: float, household_id: str = "default"
) -> bool:
    """Checks if a receipt with same store, date, and total already exists."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id FROM receipts WHERE LOWER(store) = LOWER(?) AND date = ? AND ABS(total - ?) < 0.01 AND household_id = ?",
            (store.strip(), date.strip(), total, household_id),
        )
        return cursor.fetchone() is not None


def check_duplicate_items(
    store: str,
    date_str: str,
    items: list[tuple[str, float]],
    household_id: str = "default",
) -> list[str]:
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
        for item_name, _item_price in items:
            # Query items purchased at same store within 1 day window
            cursor.execute(
                """
                SELECT i.name, r.date, r.id FROM items i
                JOIN receipts r ON i.receipt_id = r.id
                WHERE LOWER(r.store) = LOWER(?)
                  AND r.date BETWEEN ? AND ?
                  AND LOWER(i.name) LIKE ?
                  AND r.household_id = ?
            """,
                (
                    store.strip(),
                    start_date,
                    end_date,
                    f"%{item_name.lower().strip()}%",
                    household_id,
                ),
            )

            matches = cursor.fetchall()
            if matches:
                for match in matches:
                    duplicates.append(
                        f"Item '{item_name}' seems to match previous purchase '{match['name']}' at {store} on {match['date']} (Receipt #{match['id']})."
                    )
    return duplicates


def save_receipt_and_items(
    store: str,
    date: str,
    total: float,
    items: list[tuple[str, float]],
    household_id: str = "default",
) -> int:
    """Saves a receipt and its associated items to SQLite. Returns receipt ID."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO receipts (store, date, total, household_id) VALUES (?, ?, ?, ?)",
            (store.strip(), date.strip(), total, household_id),
        )
        receipt_id = cursor.lastrowid

        for name, price in items:
            cursor.execute(
                "INSERT INTO items (receipt_id, name, price) VALUES (?, ?, ?)",
                (receipt_id, name.strip(), price),
            )
        conn.commit()
        return receipt_id


def get_deals_for_items(items: list[tuple[str, float]]) -> list[dict[str, Any]]:
    """Checks for deals or better prices in the database for the given items."""
    deals_alerts = []
    with get_connection() as conn:
        cursor = conn.cursor()
        for name, price in items:
            # Match deal by item name substring
            cursor.execute(
                "SELECT item_name, store, deal_price, details FROM mock_deals WHERE LOWER(?) LIKE '%' || LOWER(item_name) || '%'",
                (name.lower().strip(),),
            )
            matches = cursor.fetchall()
            for match in matches:
                # Alert if match deal price is less than current purchase price
                if match["deal_price"] < price:
                    deals_alerts.append(
                        {
                            "item_name": name,
                            "current_price": price,
                            "deal_price": match["deal_price"],
                            "store": match["store"],
                            "details": match["details"],
                        }
                    )
    return deals_alerts


def query_purchase_history(
    search_term: str, household_id: str = "default"
) -> dict[str, Any]:
    """
    Searches the database for past purchases of items matching the search term.

    Args:
        search_term: The search term to find in item names or store names.
        household_id: The context household/session ID.

    Returns:
        A dict containing 'results', which is a list of matching purchase records.
    """
    import re

    cleaned = re.sub(r"[^\w\s]", " ", search_term)
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
        WHERE ({where_clause}) AND r.household_id = ?
        ORDER BY r.date DESC
    """
    params.append(household_id)

    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(sql, params)

        results = []
        for row in cursor.fetchall():
            results.append(
                {
                    "store": row["store"],
                    "date": row["date"],
                    "item_name": row["name"],
                    "price": row["price"],
                }
            )

        # Fallback to appending the latest 50 purchases if we have few targeted matches
        if len(results) < 10:
            cursor.execute(
                """
                SELECT r.store, r.date, i.name, i.price
                FROM items i
                JOIN receipts r ON i.receipt_id = r.id
                WHERE r.household_id = ?
                ORDER BY r.date DESC
                LIMIT 50
            """,
                (household_id,),
            )
            existing = {
                (row["store"].lower(), row["date"], row["item_name"].lower())
                for row in results
            }
            for row in cursor.fetchall():
                key = (row["store"].lower(), row["date"], row["name"].lower())
                if key not in existing:
                    results.append(
                        {
                            "store": row["store"],
                            "date": row["date"],
                            "item_name": row["name"],
                            "price": row["price"],
                        }
                    )
        return {"results": results}


def log_agent_run(
    request_id: str,
    household_id: str,
    user_id: str,
    intent: str,
    route_taken: str,
    tools_called: list[str],
    mcp_success: bool,
    pii_redacted_count: int,
    errors: str | None = None,
) -> None:
    """Logs the execution metrics of an agent workflow run."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO agent_runs (
                request_id, household_id, user_id, intent, route_taken,
                tools_called, mcp_success, pii_redacted_count, errors
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                request_id,
                household_id,
                user_id,
                intent,
                route_taken,
                json.dumps(tools_called),
                1 if mcp_success else 0,
                pii_redacted_count,
                errors,
            ),
        )
        conn.commit()


def get_agent_runs(
    limit: int = 20, household_id: str = "default"
) -> list[dict[str, Any]]:
    """Retrieves the history of agent runs from the database."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, request_id, household_id, user_id, intent, route_taken,
                   tools_called, mcp_success, pii_redacted_count, errors, timestamp
            FROM agent_runs
            WHERE household_id = ?
            ORDER BY id DESC
            LIMIT ?
        """,
            (household_id, limit),
        )
        results = []
        for r in cursor.fetchall():
            results.append(
                {
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
                    "timestamp": r["timestamp"],
                }
            )
        return results


def get_all_purchases(household_id: str = "default") -> list[dict[str, Any]]:
    """Retrieves all past purchases from the database for a specific household/session."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT r.store, r.date, i.name, i.price
            FROM items i
            JOIN receipts r ON i.receipt_id = r.id
            WHERE r.household_id = ?
            ORDER BY r.date DESC
        """,
            (household_id,),
        )
        results = []
        for row in cursor.fetchall():
            results.append(
                {
                    "store": row["store"],
                    "date": row["date"],
                    "item_name": row["name"],
                    "price": row["price"],
                }
            )
        return results


# --- SEC-01 identity / membership / session helpers ---


class SessionBindingConflictError(ValueError):
    """Raised when a session_id is already bound to a different user or household."""


def create_user(
    user_id: str,
    *,
    display_name: str | None = None,
    whatsapp_e164: str | None = None,
    auth_subject: str | None = None,
) -> str:
    """Creates a user row. Returns user_id."""
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO users (user_id, display_name, whatsapp_e164, auth_subject)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, display_name, whatsapp_e164, auth_subject),
        )
        conn.commit()
    return user_id


def create_household(household_id: str, *, name: str = "Family") -> str:
    """Creates a household row. Returns household_id."""
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO households (household_id, name) VALUES (?, ?)",
            (household_id, name),
        )
        conn.commit()
    return household_id


def add_household_member(
    household_id: str, user_id: str, *, role: str = "member"
) -> None:
    """Adds a user to a household. Requires both entities to exist (FK enforced)."""
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO household_members (household_id, user_id, role)
            VALUES (?, ?, ?)
            """,
            (household_id, user_id, role),
        )
        conn.commit()


def user_belongs_to_household(user_id: str, household_id: str) -> bool:
    """Returns True if user_id is a member of household_id."""
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM household_members
            WHERE user_id = ? AND household_id = ?
            LIMIT 1
            """,
            (user_id, household_id),
        ).fetchone()
        return row is not None


def list_user_households(user_id: str) -> list[str]:
    """Returns household_ids the user belongs to, ordered by join time."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT household_id FROM household_members
            WHERE user_id = ?
            ORDER BY created_at ASC
            """,
            (user_id,),
        ).fetchall()
        return [r["household_id"] for r in rows]


def get_user_by_whatsapp(whatsapp_e164: str) -> dict[str, Any] | None:
    """Looks up a user by WhatsApp address (e.g. whatsapp:+15551234567)."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE whatsapp_e164 = ?",
            (whatsapp_e164,),
        ).fetchone()
        return dict(row) if row else None


def get_user_by_auth_subject(auth_subject: str) -> dict[str, Any] | None:
    """Looks up a user by OIDC/Firebase auth subject."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE auth_subject = ?",
            (auth_subject,),
        ).fetchone()
        return dict(row) if row else None


def get_user(user_id: str) -> dict[str, Any] | None:
    """Looks up a user by internal user_id."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None


def get_conversation_session(session_id: str) -> dict[str, Any] | None:
    """Returns the ownership binding for a conversation session, if any."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM conversation_sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return dict(row) if row else None


def create_conversation_session(
    session_id: str,
    user_id: str,
    household_id: str,
    *,
    channel: str,
) -> dict[str, Any]:
    """
    Binds a session_id to exactly one (user_id, household_id).

    Re-binding the same triple is idempotent (updates last_seen_at).
    Reusing a session_id for a different user or household raises
    SessionBindingConflictError.
    """
    existing = get_conversation_session(session_id)
    if existing:
        if existing["user_id"] != user_id or existing["household_id"] != household_id:
            raise SessionBindingConflictError(
                "session_id is already bound to a different user or household"
            )
        with get_connection() as conn:
            conn.execute(
                """
                UPDATE conversation_sessions
                SET last_seen_at = CURRENT_TIMESTAMP
                WHERE session_id = ?
                """,
                (session_id,),
            )
            conn.commit()
        return get_conversation_session(session_id)  # type: ignore[return-value]

    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO conversation_sessions
                (session_id, user_id, household_id, channel)
            VALUES (?, ?, ?, ?)
            """,
            (session_id, user_id, household_id, channel),
        )
        conn.commit()
    binding = get_conversation_session(session_id)
    assert binding is not None
    return binding


def session_owned_by_user(session_id: str, user_id: str) -> bool:
    """Returns True if session_id exists and is owned by user_id."""
    binding = get_conversation_session(session_id)
    return binding is not None and binding["user_id"] == user_id
