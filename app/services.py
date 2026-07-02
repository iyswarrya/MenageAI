import os
import datetime
import logging
import json
import asyncio
from typing import List, Dict, Any, Optional, Tuple
from google import genai
from google.genai import types
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from app.interfaces import (
    ReceiptItem,
    ReceiptData,
    SavedReceiptResult,
    DuplicateAlert,
    PurchaseMatch,
    DealAlert,
    RedactionResult,
    DealsClient
)
from app import db
from app import pii

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("family_receipt_agent")


# --- Gemini Receipt Parser Implementation ---

class GeminiReceiptParser:
    def __init__(self, model_name: str = "gemini-2.5-flash"):
        self.model_name = model_name

    def parse(self, input_text: str) -> ReceiptData:
        """Parses receipt text or image using Gemini structured outputs."""
        use_vertex = os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "false").lower() == "true"
        project = os.environ.get("GOOGLE_CLOUD_PROJECT")
        location = os.environ.get("GOOGLE_CLOUD_LOCATION", "global")
        
        if use_vertex:
            client = genai.Client(vertexai=True, project=project, location=location)
        else:
            client = genai.Client()
            
        today_str = datetime.date.today().strftime("%Y-%m-%d")
        
        # Check if the input is a JSON string containing image_url
        content_input = input_text
        if isinstance(input_text, str) and input_text.strip().startswith("{"):
            try:
                img_data = json.loads(input_text)
                if "image_url" in img_data:
                    img_url = img_data["image_url"]
                    # If it's a relative URL, resolve locally
                    if img_url.startswith("/"):
                        img_url = f"http://127.0.0.1:8000{img_url}"
                    import requests
                    img_resp = requests.get(img_url, timeout=15)
                    if img_resp.status_code == 200:
                        image_bytes = img_resp.content
                        content_type = img_resp.headers.get("content-type", "image/jpeg")
                        content_input = [
                            types.Part.from_bytes(data=image_bytes, mime_type=content_type),
                            "Analyze this receipt image and extract the details."
                        ]
            except Exception as e:
                logger.warning(f"Failed to resolve image_url in receipt parser: {e}")
        
        response = client.models.generate_content(
            model=self.model_name,
            contents=content_input,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=ReceiptData,
                system_instruction=f"""You are a strict receipt analysis agent.
Analyze the input text, description, or image. Your task is to extract the details with absolute precision and grounding.

SUPPORTED RECEIPT TYPES:
You support: "retail_receipt", "online_order_summary", "invoice", "unknown".
Populate `receipt_type` with one of these values.

CRITICAL GROUNDING RULES:
1. ONLY extract items, store, date, and prices that are explicitly visible/present in the input. Never invent items, stores, dates, totals, or prices.
2. Store name: Extract only if visible in text, logo, reward program, or domain name (e.g., Target Circle or informtarget.com implies Target; Amazon.com or amazon logo implies Amazon). If missing or unclear, return "unknown" or null. Do NOT infer a store from product names alone (e.g., buying a Nike product does not mean the store is Nike).
3. Receipt date: Extract in YYYY-MM-DD format if visible. If missing or unclear, return today's date ({today_str}) and add a warning to the `warnings` list that the date was guessed/defaulted.
4. List of items:
   - Extract item lines from sections like "Arriving today", "Ordered", "Shipped", "Items", or traditional receipt listings.
   - For each item, extract its specific name, price, quantity (default to 1), confidence score (0.0 to 1.0 based on how clear it is), the `raw_line` from the input representing this item, and the `seller` (merchant or third-party seller, e.g. "Fanhao Shop") if explicitly mentioned.
   - Do NOT use generic department/section headers (like 'HEALTH AND BEAUTY', 'APPAREL', 'SPORTING GOODS') as item names unless the receipt lists them as line items with prices.
   - Do NOT invent or add common receipt items.
   - Do NOT reuse items from prior examples, mock data, or database memory.
   - Do NOT duplicate items unless they appear as separate lines in the input.
5. Overall extraction confidence: Populate `extraction_confidence` (0.0 to 1.0) indicating how clear the receipt is.
6. Warnings: Populate `warnings` with any issues encountered (e.g., "date missing", "total computed", "unclear handwritten text").
7. Excerpt: Populate `source_text_excerpt` with the complete transcribed raw text of the receipt/invoice/order summary (including the store header, date, items list, and totals).
8. Financial Fields:
   - Extract `subtotal`, `tax` (default to 0.0 if not listed), `shipping` (default to 0.0 if not listed), and `grand_total` (total price paid including tax and shipping).
   - If there is an order ID or receipt confirmation number (e.g. # 112-2923048-7481024), extract it in `order_id`.

Keep names clean, descriptive, preserve brand names when present, and ground everything strictly in the input."""
            )
        )
        return ReceiptData.model_validate_json(response.text)


# --- SQLite Purchase Memory Repository Implementation ---

class SqlitePurchaseMemoryRepository:
    def save_receipt(self, receipt: ReceiptData) -> SavedReceiptResult:
        """Saves a receipt and its items to SQLite database."""
        try:
            items_tuples = [(item.name, item.price) for item in receipt.items]
            receipt_id = db.save_receipt_and_items(
                receipt.store,
                receipt.date,
                receipt.total,
                items_tuples
            )
            return SavedReceiptResult(receipt_id=receipt_id, success=True)
        except Exception as e:
            logger.error(f"Failed to save receipt to SQLite: {e}")
            return SavedReceiptResult(receipt_id=-1, success=False)

    def find_duplicates(self, items: List[ReceiptItem], household_id: str, store: Optional[str] = None, date: Optional[str] = None) -> List[DuplicateAlert]:
        """Checks for duplicate item purchases within SQLite (within 1 day)."""
        if not store or not date:
            # Fallback if store or date is not provided
            return []
            
        items_tuples = [(item.name, item.price) for item in items]
        warnings = db.check_duplicate_items(store, date, items_tuples)
        return [DuplicateAlert(message=msg) for msg in warnings]

    def query_purchase_history(self, query: str, household_id: str) -> List[PurchaseMatch]:
        """Queries historical purchases from SQLite database matching search term."""
        db_results = db.query_purchase_history(query)
        # db_results contains {"results": [...]}
        results = db_results.get("results", [])
        
        matches = []
        for row in results:
            matches.append(
                PurchaseMatch(
                    store=row["store"],
                    date=row["date"],
                    item_name=row["item_name"],
                    price=row["price"]
                )
            )
        return matches


# --- SQLite Deals Client Implementation ---

class SqliteDealsClient:
    def lookup_price(self, product_name: str) -> List[DealAlert]:
        """Looks up existing deals for a specific product name in SQLite."""
        with db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT item_name, store, deal_price, details FROM mock_deals WHERE LOWER(?) LIKE '%' || LOWER(item_name) || '%'",
                (product_name.lower().strip(),)
            )
            results = []
            for row in cursor.fetchall():
                results.append(
                    DealAlert(
                        item_name=row["item_name"],
                        current_price=0.0,
                        deal_price=row["deal_price"],
                        store=row["store"],
                        details=row["details"]
                    )
                )
            return results

    def check_price_drop(self, product_name: str, paid_price: float) -> List[DealAlert]:
        """Checks if a lower price deal exists compared to the paid price in SQLite."""
        with db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT item_name, store, deal_price, details FROM mock_deals WHERE LOWER(?) LIKE '%' || LOWER(item_name) || '%'",
                (product_name.lower().strip(),)
            )
            results = []
            for row in cursor.fetchall():
                if row["deal_price"] < paid_price:
                    results.append(
                        DealAlert(
                            item_name=product_name,
                            current_price=paid_price,
                            deal_price=row["deal_price"],
                            store=row["store"],
                            details=row["details"]
                        )
                    )
            return results


# --- Regex Security Redactor Implementation ---

class RegexSecurityRedactor:
    def mask_pii(self, text: str) -> RedactionResult:
        """Masks PII using regex."""
        sanitized = pii.mask_pii(text)
        redacted_count = (
            sanitized.count("[REDACTED CARD]") +
            sanitized.count("[REDACTED EMAIL]") +
            sanitized.count("[REDACTED PHONE]")
        )
        return RedactionResult(sanitized_text=sanitized, redacted_items_count=redacted_count)


# --- Structured Run Logger Implementation ---

class StructuredRunLogger:
    def log_input(self, step: str, input_data: Any) -> None:
        logger.info(f"[{step}] Input: {input_data}")

    def log_tool_call(self, step: str, tool_name: str, args: Any) -> None:
        logger.info(f"[{step}] Tool Call '{tool_name}' with args: {args}")

    def log_output(self, step: str, output_data: Any) -> None:
        logger.info(f"[{step}] Output: {output_data}")

    def log_error(self, step: str, error_message: str) -> None:
        logger.error(f"[{step}] Error: {error_message}")


# --- MCP Deals Client Implementation ---

def run_async(coro):
    """Utility to run an async coroutine synchronously, even inside an existing loop."""
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
    if loop.is_running():
        import nest_asyncio
        nest_asyncio.apply()
        return loop.run_until_complete(coro)
    else:
        return loop.run_until_complete(coro)

class MCPDealsClient:
    def __init__(self, fallback_client: DealsClient, command: str = "uv", args: List[str] = None):
        self.fallback_client = fallback_client
        self.command = command
        self.last_run_mcp_success = True
        
        server_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "mcp_server",
            "retail_server.py"
        )
        self.args = args or ["run", "python", server_path]
        self.server_params = StdioServerParameters(command=self.command, args=self.args)

    async def _call_mcp_tool(self, tool_name: str, arguments: dict) -> str:
        async with stdio_client(self.server_params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                response = await session.call_tool(tool_name, arguments)
                if not response.content:
                    return ""
                return "".join(part.text for part in response.content if hasattr(part, "text") and part.text)

    def lookup_price(self, product_name: str) -> List[DealAlert]:
        if not os.environ.get("USE_MCP_DEALS", "false").lower() == "true":
            return self.fallback_client.lookup_price(product_name)
            
        try:
            self.last_run_mcp_success = True
            coro = self._call_mcp_tool("lookup_price", {"product_name": product_name})
            res_text = run_async(coro)
            data = json.loads(res_text)
            alerts = []
            for item in data:
                alerts.append(
                    DealAlert(
                        item_name=item["item_name"],
                        current_price=0.0,
                        deal_price=item["deal_price"],
                        store=item["store"],
                        details=item["details"]
                    )
                )
            return alerts
        except Exception as e:
            self.last_run_mcp_success = False
            logger.warning(f"MCP lookup_price failed, falling back: {e}")
            return self.fallback_client.lookup_price(product_name)

    def check_price_drop(self, product_name: str, paid_price: float) -> List[DealAlert]:
        if not os.environ.get("USE_MCP_DEALS", "false").lower() == "true":
            return self.fallback_client.check_price_drop(product_name, paid_price)
            
        try:
            self.last_run_mcp_success = True
            coro = self._call_mcp_tool("check_price_drop", {"product_name": product_name, "paid_price": paid_price})
            res_text = run_async(coro)
            data = json.loads(res_text)
            alerts = []
            for item in data:
                alerts.append(
                    DealAlert(
                        item_name=item["item_name"],
                        current_price=paid_price,
                        deal_price=item["deal_price"],
                        store=item["store"],
                        details=item["details"]
                    )
                )
            return alerts
        except Exception as e:
            self.last_run_mcp_success = False
            logger.warning(f"MCP check_price_drop failed, falling back: {e}")
            return self.fallback_client.check_price_drop(product_name, paid_price)


