# ruff: noqa
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

import datetime
from typing import List, Literal, Dict, Any, Optional
from pydantic import BaseModel, Field

from google.adk.agents import LlmAgent
from google.adk.apps import App
from google.adk.models import Gemini
from google.adk.workflow import Workflow, node, START
from google.adk.events.event import Event
from google.adk.plugins import BasePlugin
from google.genai import types

from app import db
from app.interfaces import registry, ReceiptItem, ReceiptData
from app.services import (
    GeminiReceiptParser,
    SqlitePurchaseMemoryRepository,
    SqliteDealsClient,
    RegexSecurityRedactor,
    StructuredRunLogger,
    MCPDealsClient
)

# Register default SQLite/Regex/Gemini implementations
registry.receipt_parser = GeminiReceiptParser()
registry.memory_repo = SqlitePurchaseMemoryRepository()

sqlite_deals = SqliteDealsClient()
registry.deals_client = MCPDealsClient(fallback_client=sqlite_deals)

registry.redactor = RegexSecurityRedactor()
registry.logger = StructuredRunLogger()

# Initialize database tables on startup
db.init_db()


# --- Custom Plugin for Observability & Run Logging ---

class AgentRunLoggerPlugin(BasePlugin):
    async def before_run_callback(self, *, invocation_context) -> Optional[types.Content]:
        session_state = invocation_context.session.state
        session_state["current_run"] = {
            "request_id": invocation_context.invocation_id or "local",
            "household_id": invocation_context.session.id or "default",
            "user_id": invocation_context.user_id or "default_user",
            "intent": "unknown",
            "route_taken": "unknown",
            "tools_called": [],
            "mcp_success": True,
            "pii_redacted_count": 0,
            "errors": None
        }
        return None

    async def after_tool_callback(self, *, tool, tool_args, tool_context, result) -> Optional[dict]:
        state = tool_context.state
        if "current_run" in state:
            state["current_run"]["tools_called"].append(tool.name)
        return None

    async def after_run_callback(self, *, invocation_context) -> None:
        session_state = invocation_context.session.state
        run_data = session_state.get("current_run")
        if run_data:
            mcp_success = True
            if hasattr(registry.deals_client, "last_run_mcp_success"):
                mcp_success = getattr(registry.deals_client, "last_run_mcp_success", True)
                
            errors = None
            for event in invocation_context._get_events(current_invocation=True):
                if hasattr(event, "error") and event.error:
                    errors = str(event.error)
                    break
                    
            db.log_agent_run(
                request_id=run_data["request_id"],
                household_id=run_data["household_id"],
                user_id=run_data["user_id"],
                intent=run_data["intent"],
                route_taken=run_data["route_taken"],
                tools_called=run_data["tools_called"],
                mcp_success=mcp_success,
                pii_redacted_count=run_data["pii_redacted_count"],
                errors=errors
            )


# --- Schemas ---

class Intent(BaseModel):
    intent: Literal["receipt", "query"] = Field(
        description="Whether the user is uploading/logging a new receipt (receipt) or asking a question about history/memory (query)."
    )
    original_query: str = Field(description="Verbatim copy of the user input query text.")

class MemoryAgentOutput(BaseModel):
    receipt: ReceiptData
    is_duplicate_receipt: bool
    duplicate_items: List[str]

class DealAlert(BaseModel):
    item_name: str
    current_price: float
    deal_price: float
    store: str
    details: str

class DealsAgentOutput(BaseModel):
    receipt: ReceiptData
    is_duplicate_receipt: bool
    duplicate_items: List[str]
    deals: List[DealAlert]


# --- Undecorated Raw Python Functions for Nodes (Testable) ---

def preprocess_input(node_input: types.Content, ctx=None) -> str:
    """Sanitizes user input by masking PII before running the agent."""
    text = ""
    image_parts = []
    if node_input and hasattr(node_input, "parts") and node_input.parts:
        for part in node_input.parts:
            if part.text:
                text += part.text
            elif part.inline_data or part.file_data:
                image_parts.append(part)
    elif isinstance(node_input, str):
        text = node_input
    
    registry.logger.log_input("preprocess_input", text)
    res = registry.redactor.mask_pii(text)
    
    if ctx and hasattr(ctx, "state"):
        ctx.state["sanitized_text"] = res.sanitized_text
        ctx.state["image_parts"] = image_parts
        if "current_run" in ctx.state:
            ctx.state["current_run"]["pii_redacted_count"] = res.redacted_items_count
        
    registry.logger.log_output("preprocess_input", res.sanitized_text)
    return res.sanitized_text


# Intent Classifier (LlmAgent)
intent_classifier = LlmAgent(
    name="intent_classifier",
    model=Gemini(
        model="gemini-2.5-flash",
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    instruction="""You are a family receipt assistant router.
Determine whether the user is providing/logging a new receipt (intent='receipt') or asking a question/seeking information from history (intent='query').
Provide the intent and copy the original user input exactly into original_query.""",
    output_schema=Intent,
    output_key="intent_classification"
)


def router(node_input: Intent, ctx=None) -> Event:
    """Routes the sanitised query based on classification intent."""
    registry.logger.log_input("router", node_input)
    if ctx and hasattr(ctx, "state") and "current_run" in ctx.state:
        ctx.state["current_run"]["intent"] = node_input.intent
        ctx.state["current_run"]["route_taken"] = node_input.intent
        
    evt = Event(
        output=node_input.original_query,
        route=node_input.intent
    )
    registry.logger.log_output("router", f"Routed to: {node_input.intent}")
    return evt


def receipt_agent(node_input: str, ctx=None) -> ReceiptData:
    """Parses receipt data using the registered parser."""
    registry.logger.log_input("receipt_agent", node_input)
    image_parts = []
    if ctx and hasattr(ctx, "state"):
        image_parts = ctx.state.get("image_parts", [])
    receipt_data = registry.receipt_parser.parse(node_input, image_parts=image_parts)
    registry.logger.log_output("receipt_agent", receipt_data)
    return receipt_data


def memory_agent(node_input: ReceiptData, ctx=None) -> MemoryAgentOutput:
    """Stores the receipt and items in SQLite and checks for duplicate purchases."""
    registry.logger.log_input("memory_agent", node_input)
    
    household_id = "default"
    if ctx and hasattr(ctx, "state") and "current_run" in ctx.state:
        household_id = ctx.state["current_run"].get("household_id", "default")
        
    items = [ReceiptItem(name=item.name, price=item.price) for item in node_input.items]
    is_duplicate_receipt = db.check_duplicate_receipt(node_input.store, node_input.date, node_input.total, household_id)
    
    dup_alerts = registry.memory_repo.find_duplicates(
        items=items,
        store=node_input.store,
        date=node_input.date,
        household_id=household_id
    )
    duplicate_items_warnings = [alert.message for alert in dup_alerts]
    
    if not is_duplicate_receipt:
        registry.memory_repo.save_receipt(node_input, household_id)
        
    res = MemoryAgentOutput(
        receipt=node_input,
        is_duplicate_receipt=is_duplicate_receipt,
        duplicate_items=duplicate_items_warnings
    )
    registry.logger.log_output("memory_agent", res)
    return res


def deals_agent(node_input: MemoryAgentOutput) -> DealsAgentOutput:
    """Checks the database mock deals to find better prices or price drops."""
    registry.logger.log_input("deals_agent", node_input)
    
    deals_alerts = []
    for item in node_input.receipt.items:
        alerts = registry.deals_client.check_price_drop(item.name, item.price)
        for alert in alerts:
            deals_alerts.append(
                DealAlert(
                    item_name=alert.item_name,
                    current_price=alert.current_price,
                    deal_price=alert.deal_price,
                    store=alert.store,
                    details=alert.details
                )
            )
        
    res = DealsAgentOutput(
        receipt=node_input.receipt,
        is_duplicate_receipt=node_input.is_duplicate_receipt,
        duplicate_items=node_input.duplicate_items,
        deals=deals_alerts
    )
    registry.logger.log_output("deals_agent", res)
    return res


def normalize_text(text: str) -> str:
    """Normalizes text by lowercasing, removing spaces, punctuation, SKU numbers, and tax markers."""
    import re
    if not text:
        return ""
    # Lowercase
    t = text.lower()
    # Ignore SKU prefixes (sequences of digits, e.g., 013001149 or 037119242)
    t = re.sub(r'\b\d{8,15}\b', '', t)
    t = re.sub(r'\d{8,15}', '', t)
    # Ignore tax markers like T and T+ (especially if they are near the price or end of line)
    t = re.sub(r'\b(t\+?)\b', '', t)
    # Remove punctuation
    t = re.sub(r'[^\w\s\.]', '', t)
    # Remove spaces
    t = "".join(t.split())
    return t

def validate_receipt_extraction(node_input: ReceiptData, ctx=None) -> Event:
    """Compares extracted items against original sanitized input to ensure grounding."""
    import json
    import datetime
    registry.logger.log_input("validate_receipt_extraction", node_input)
    
    sanitized_text = ""
    if ctx and hasattr(ctx, "state") and "sanitized_text" in ctx.state:
        sanitized_text = ctx.state["sanitized_text"]
        
    # Use the actual document transcription if available, especially when image/file parts are uploaded
    if node_input.source_text_excerpt:
        is_placeholder = (
            not sanitized_text.strip() or 
            "analyze this" in sanitized_text.lower() or 
            "provided image" in sanitized_text.lower()
        )
        has_image = ctx and hasattr(ctx, "state") and ctx.state.get("image_parts")
        if is_placeholder or has_image:
            sanitized_text = node_input.source_text_excerpt
        
    source_lower = sanitized_text.lower().strip()
    norm_source = normalize_text(sanitized_text)
    has_no_input_text = len(norm_source) == 0
    
    total_items = len(node_input.items)
    unmatched_items = []
    matched_items_count = 0
    
    # Item name evidence matching (at least one item name matching)
    at_least_one_item_name_evidence = False
    for item in node_input.items:
        norm_name = normalize_text(item.name)
        norm_raw = normalize_text(item.raw_line) if item.raw_line else ""
        
        # Check name evidence
        name_found = False
        if norm_raw and norm_raw in norm_source:
            name_found = True
        elif norm_name and norm_name in norm_source:
            name_found = True
            
        if name_found:
            at_least_one_item_name_evidence = True
            
    # Price evidence matching (at least one item price matching)
    at_least_one_item_price_evidence = False
    for item in node_input.items:
        price_str = f"{item.price:.2f}"
        price_int_str = str(int(item.price))
        if price_str in norm_source or price_int_str in norm_source:
            at_least_one_item_price_evidence = True
            break
            
    # Validation helper for individual items
    def check_item_evidence(item_name: str, item_price: float, raw_line: str, source_txt: str) -> bool:
        norm_name = normalize_text(item_name)
        norm_raw = normalize_text(raw_line) if raw_line else ""
        
        price_str = f"{item_price:.2f}"
        price_int_str = str(int(item_price))
        
        # 1. Match item evidence using raw_line first
        # 2. Check if name/raw_line and price exist in the same or nearest following line
        lines = source_txt.splitlines()
        for idx, line in enumerate(lines):
            norm_line = normalize_text(line)
            matches_item = False
            if norm_raw and norm_raw in norm_line:
                matches_item = True
            elif norm_name and norm_name in norm_line:
                matches_item = True
                
            if matches_item:
                # Check price on same line
                if price_str in norm_line or price_int_str in norm_line:
                    return True
                # Or check nearest following line (e.g. within next 2 lines)
                for offset in range(1, 3):
                    if idx + offset < len(lines):
                        next_line = lines[idx + offset]
                        norm_next = normalize_text(next_line)
                        if price_str in norm_next or price_int_str in norm_next:
                            if "regularprice" not in norm_next:
                                return True
                                
        # Fallback to global substring search in normalized text if line-by-line fails
        norm_global = normalize_text(source_txt)
        name_in_source = (norm_raw in norm_global) if norm_raw else (norm_name in norm_global)
        price_in_source = (price_str in norm_global) or (price_int_str in norm_global)
        return name_in_source and price_in_source

    for item in node_input.items:
        if check_item_evidence(item.name, item.price, item.raw_line, sanitized_text):
            matched_items_count += 1
        else:
            unmatched_items.append(item.name)
            
    unmatched_count = len(unmatched_items)
    
    # Store evidence check
    store_evidence = False
    store_lower = node_input.store.lower().strip()
    if store_lower in ["unknown", ""]:
        store_evidence = True
    else:
        store_norm = normalize_text(store_lower)
        if store_norm in norm_source:
            store_evidence = True
        else:
            store_words = [w for w in store_lower.split() if len(w) > 2]
            store_evidence = any(normalize_text(w) in norm_source for w in store_words)
            
    # Date evidence check
    date_evidence = False
    today_str = datetime.date.today().strftime("%Y-%m-%d")
    if not node_input.date or node_input.date == today_str:
        date_evidence = True
    else:
        parts = node_input.date.split("-")
        if len(parts) == 3:
            year, month, day = parts
            short_year = year[2:]
            fmt1 = f"{month}{day}{year}"
            fmt2 = f"{month}{day}{short_year}"
            fmt3 = f"{year}{month}{day}"
            fmt4 = f"{day}{month}{year}"
            fmt5 = f"{day}{month}{short_year}"
            
            # Month name verification (e.g. "July 1, 2026" -> contains "jul" or "july" and "1" and "2026")
            month_names = {
                "01": ["jan", "january"], "02": ["feb", "february"], "03": ["mar", "march"],
                "04": ["apr", "april"], "05": ["may"], "06": ["jun", "june"],
                "07": ["jul", "july"], "08": ["aug", "august"], "09": ["sep", "september"],
                "10": ["oct", "october"], "11": ["nov", "november"], "12": ["dec", "december"]
            }
            month_words = month_names.get(month, [])
            has_month_word = any(w in source_lower for w in month_words)
            has_day = str(int(day)) in source_lower
            has_year = year in source_lower or short_year in source_lower
            
            if (fmt1 in norm_source or 
                fmt2 in norm_source or 
                fmt3 in norm_source or 
                fmt4 in norm_source or
                fmt5 in norm_source or
                (month in norm_source and day in norm_source) or
                (has_month_word and has_day and has_year)):
                date_evidence = True
        else:
            date_evidence = True
            
    store_or_date_evidence = store_evidence or date_evidence
    
    # Total/subtotal evidence check (if extracted)
    total_subtotal_evidence = False
    has_total_subtotal_extracted = False
    prices_to_check = []
    if node_input.total > 0:
        prices_to_check.append(node_input.total)
        has_total_subtotal_extracted = True
    if node_input.subtotal and node_input.subtotal > 0:
        prices_to_check.append(node_input.subtotal)
        has_total_subtotal_extracted = True
        
    if not has_total_subtotal_extracted:
        total_subtotal_evidence = True
    else:
        for p in prices_to_check:
            p_str = f"{p:.2f}"
            p_int = str(int(p))
            if p_str in norm_source or p_int in norm_source:
                total_subtotal_evidence = True
                break
                
    # Threshold check: Accept if >= 70% of items have evidence
    pass_percentage = False
    if total_items > 0:
        pass_percentage = (matched_items_count / total_items) >= 0.7
        
    # Decide rejection
    is_rejected = False
    rejection_reasons = []
    
    if total_items == 0:
        is_rejected = True
        rejection_reasons.append("No items extracted.")
    elif has_no_input_text:
        if node_input.extraction_confidence < 0.7:
            is_rejected = True
            rejection_reasons.append("Pure image without text has low confidence.")
    else:
        if not store_or_date_evidence:
            is_rejected = True
            rejection_reasons.append("No store/source or date evidence.")
        if not at_least_one_item_name_evidence:
            is_rejected = True
            rejection_reasons.append("No item name evidence.")
        if not at_least_one_item_price_evidence:
            is_rejected = True
            rejection_reasons.append("No item price evidence.")
        if not total_subtotal_evidence:
            is_rejected = True
            rejection_reasons.append("No matching total or subtotal evidence.")
        if not pass_percentage:
            is_rejected = True
            rejection_reasons.append(f"Too few items supported (only {matched_items_count}/{total_items} matched).")
        if node_input.extraction_confidence < 0.4:
            is_rejected = True
            rejection_reasons.append("Overall extraction confidence too low.")
            
    status = "invalid" if is_rejected else "valid"
    warnings = list(node_input.warnings)
    if is_rejected:
        warnings.append(f"Extraction rejected: {', '.join(rejection_reasons)}")
        
    # Structured logging
    log_data = {
        "sanitized_input_length": len(sanitized_text),
        "extracted_item_count": total_items,
        "validation_status": status,
        "rejected_items": unmatched_items,
        "warnings": warnings
    }
    registry.logger.log_output("validate_receipt_extraction", json.dumps(log_data))
    
    if is_rejected:
        safe_msg = "I could not reliably read this receipt. Please upload a clearer image or paste the receipt text."
        return Event(output=safe_msg, route="invalid")
    else:
        return Event(output=node_input, route="valid")

def invalid_receipt_responder(node_input: str) -> str:
    """Returns the safe response directly when validation fails."""
    registry.logger.log_input("invalid_receipt_responder", node_input)
    registry.logger.log_output("invalid_receipt_responder", node_input)
    return node_input


# Response Agent (LlmAgent)
response_agent = LlmAgent(
    name="response_agent",
    model=Gemini(
        model="gemini-2.5-flash",
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    instruction="""You are a friendly family coordinator.
Formulate a concise WhatsApp-style receipt summary based on the input details (purchased items, totals, duplicate warnings, and price drop deals).

CRITICAL RULE: Rely ONLY on the structured input details (items, store, prices, warnings, deals) passed directly to this node for the current turn. Do NOT include, mention, or carry over any items, stores, dates, or prices from previous receipts or messages in the conversation history. Treat each receipt summary as completely isolated.

Rules:
1. Keep it short, casual, and highly scannable (WhatsApp style).
2. Use bold text for store name, total, and item names.
3. Use emojis:
   🧾 for receipt metadata
   🛒 for items list
   ⚠️ for duplicate warnings (Highlight duplicate receipt warnings at the top!)
   💰 for price drops / deal recommendations
4. Show the total clearly.
5. If deals are found, tell the family about where they could have saved money (e.g. "We bought Coffee for $9.99, but Safeway has it for $7.99!").
6. Keep the language warm and family-friendly."""
)


# Node 5: Query Agent Tool
def query_purchase_history(search_term: str, tool_context=None) -> Dict[str, Any]:
    """
    Searches the database for past purchases of items matching the search term.
    
    Args:
        search_term: The search term to find in item names or store names.
        tool_context: ADK ToolContext injected automatically.

    Returns:
        A dict containing 'results', which is a list of matching purchase records.
    """
    registry.logger.log_tool_call("query_agent", "query_purchase_history", {"search_term": search_term})
    
    household_id = "default"
    if tool_context and hasattr(tool_context, "state") and "current_run" in tool_context.state:
        household_id = tool_context.state["current_run"].get("household_id", "default")
        
    results = registry.memory_repo.query_purchase_history(search_term, household_id=household_id)
    
    formatted = {
        "results": [
            {
                "store": r.store,
                "date": r.date,
                "item_name": r.item_name,
                "price": r.price
            } for r in results
        ]
    }
    registry.logger.log_output("query_agent_tool", formatted)
    return formatted


# Query Agent (Answering memory questions using SQL search tool)
query_agent = LlmAgent(
    name="query_agent",
    model=Gemini(
        model="gemini-2.5-flash",
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    instruction="""You are a family memory query assistant.
The family is asking a question about their purchase history.
Use the query_purchase_history tool to search the database.
Formulate a very friendly, concise WhatsApp-style answer based ONLY on the database search results returned.
If you find matching items, list their purchase dates, store, and price in a nice list with emojis.
If nothing is found, gently let them know. Never make up purchases. Keep it brief.""",
    tools=[query_purchase_history]
)


# --- Wrap Functions as ADK Workflow Nodes ---

preprocess_input_node = node(name="preprocess_input")(preprocess_input)
router_node = node(name="router")(router)
receipt_agent_node = node(name="receipt_agent")(receipt_agent)
validate_receipt_extraction_node = node(name="validate_receipt_extraction")(validate_receipt_extraction)
invalid_receipt_responder_node = node(name="invalid_receipt_responder")(invalid_receipt_responder)
memory_agent_node = node(name="memory_agent")(memory_agent)
deals_agent_node = node(name="deals_agent")(deals_agent)


# --- Workflow Graph Definition ---

root_agent = Workflow(
    name="family_receipt_agent",
    edges=[
        (START, preprocess_input_node),
        (preprocess_input_node, intent_classifier),
        (intent_classifier, router_node),
        
        # Route to receipt analysis workflow branch or history query branch
        (router_node, {
            "receipt": receipt_agent_node,
            "query": query_agent
        }),
        
        # Receipt analysis path
        (receipt_agent_node, validate_receipt_extraction_node),
        (validate_receipt_extraction_node, {
            "valid": memory_agent_node,
            "invalid": invalid_receipt_responder_node
        }),
        (memory_agent_node, deals_agent_node),
        (deals_agent_node, response_agent),
    ]
)

app = App(
    root_agent=root_agent,
    name="app",
    plugins=[AgentRunLoggerPlugin(name="agent_run_logger")]
)
