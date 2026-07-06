import os
import sqlite3
import json
from mcp.server.fastmcp import FastMCP

# Create an MCP server instance
mcp = FastMCP("family-retail-mcp-server")

# Resolve DB path relative to this file
DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "app",
    "family_receipts.db"
)

def get_connection():
    """Returns a connection to the SQLite database."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

@mcp.tool()
def lookup_price(product_name: str) -> str:
    """
    Looks up existing deals in the database for a specific product name.
    
    NOTE: In production, you can replace this SQLite mock query with a call to a 
    real shopping API (e.g. Google Shopping API or SerpApi) to retrieve live, 
    real-world deals.
    
    Args:
        product_name: The name of the product to search.
    """
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT item_name, store, deal_price, details FROM mock_deals WHERE LOWER(?) LIKE '%' || LOWER(item_name) || '%'",
            (product_name.lower().strip(),)
        )
        rows = cursor.fetchall()
        results = []
        for r in rows:
            results.append({
                "item_name": r["item_name"],
                "store": r["store"],
                "deal_price": r["deal_price"],
                "details": r["details"]
            })
        return json.dumps(results)

@mcp.tool()
def check_price_drop(product_name: str, paid_price: float) -> str:
    """
    Checks if a lower price deal exists compared to the paid price.
    
    NOTE: In production, you can replace this SQLite mock query with a call to a 
    real shopping API (e.g. Google Shopping API or SerpApi) to retrieve live, 
    real-world deals.
    
    Args:
        product_name: The name of the product.
        paid_price: The price paid for the product.
    """
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT item_name, store, deal_price, details FROM mock_deals WHERE LOWER(?) LIKE '%' || LOWER(item_name) || '%'",
            (product_name.lower().strip(),)
        )
        rows = cursor.fetchall()
        results = []
        for r in rows:
            if r["deal_price"] < paid_price:
                results.append({
                    "item_name": product_name,
                    "store": r["store"],
                    "deal_price": r["deal_price"],
                    "details": r["details"]
                })
        return json.dumps(results)

@mcp.tool()
def get_store_return_policy(store_name: str) -> str:
    """
    Returns the return policy details for a given store name.
    
    Args:
        store_name: The name of the store.
    """
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT policy_text FROM store_policies WHERE LOWER(?) LIKE '%' || LOWER(store_name) || '%'",
            (store_name.lower().strip(),)
        )
        row = cursor.fetchone()
        if row:
            return f"{store_name} return policy: {row['policy_text']}"
            
    return f"Return policy for {store_name}: Standard 30-day return policy applies."

if __name__ == "__main__":
    mcp.run()
