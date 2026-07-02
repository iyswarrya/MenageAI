from typing import Protocol, List, Dict, Any, Optional, runtime_checkable
from pydantic import BaseModel, Field

# --- Shared Schemas / Data Types ---

class ReceiptItem(BaseModel):
    name: str = Field(description="Name of the item purchased (e.g. Milk, Eggs)")
    price: float = Field(description="Price of the item purchased")
    quantity: int = Field(default=1, description="Quantity of the item purchased")
    confidence: float = Field(default=1.0, description="Extraction confidence score between 0.0 and 1.0")
    raw_line: str = Field(default="", description="The raw line from the receipt matching this item")
    seller: Optional[str] = Field(default=None, description="The merchant or third-party seller name (e.g. Fanhao Shop)")

class ReceiptData(BaseModel):
    store: str = Field(description="Name of the store where the receipt was issued")
    date: str = Field(description="Date of the receipt in YYYY-MM-DD format")
    items: List[ReceiptItem] = Field(description="List of individual items on the receipt")
    prices: List[float] = Field(description="List of all prices listed on the receipt")
    total: float = Field(description="Total price paid including tax")
    extraction_confidence: float = Field(default=1.0, description="Overall extraction confidence score between 0.0 and 1.0")
    warnings: List[str] = Field(default_factory=list, description="Warnings generated during extraction")
    source_text_excerpt: Optional[str] = Field(default=None, description="Excerpt of the source text containing these items")
    receipt_type: str = Field(default="retail_receipt", description="Type of the receipt (retail_receipt, online_order_summary, invoice, unknown)")
    subtotal: Optional[float] = Field(default=None, description="Subtotal before tax/shipping")
    tax: Optional[float] = Field(default=None, description="Tax amount paid")
    shipping: Optional[float] = Field(default=None, description="Shipping and handling cost")
    grand_total: Optional[float] = Field(default=None, description="Grand total price paid including tax and shipping")
    order_id: Optional[str] = Field(default=None, description="The order identifier or confirmation code (e.g. 112-2923048-7481024)")

class SavedReceiptResult(BaseModel):
    receipt_id: int
    success: bool

class DuplicateAlert(BaseModel):
    message: str

class PurchaseMatch(BaseModel):
    store: str
    date: str
    item_name: str
    price: float

class DealAlert(BaseModel):
    item_name: str
    current_price: float
    deal_price: float
    store: str
    details: str

class RedactionResult(BaseModel):
    sanitized_text: str
    redacted_items_count: int


# --- Protocols / Interfaces ---

@runtime_checkable
class ReceiptParser(Protocol):
    def parse(self, input_text: str) -> ReceiptData:
        """Parses receipt text into structured ReceiptData."""
        ...

@runtime_checkable
class PurchaseMemoryRepository(Protocol):
    def save_receipt(self, receipt: ReceiptData) -> SavedReceiptResult:
        """Saves receipt data and its items to purchase history storage."""
        ...
        
    def find_duplicates(self, items: List[ReceiptItem], household_id: str) -> List[DuplicateAlert]:
        """Checks for duplicate item purchases within the household context."""
        ...
        
    def query_purchase_history(self, query: str, household_id: str) -> List[PurchaseMatch]:
        """Queries historical purchases matching the search term."""
        ...

@runtime_checkable
class DealsClient(Protocol):
    def lookup_price(self, product_name: str) -> List[DealAlert]:
        """Looks up existing deals for a specific product name."""
        ...
        
    def check_price_drop(self, product_name: str, paid_price: float) -> List[DealAlert]:
        """Checks if a lower price deal exists compared to the paid price."""
        ...

@runtime_checkable
class SecurityRedactor(Protocol):
    def mask_pii(self, text: str) -> RedactionResult:
        """Masks PII like credit cards, emails, and phone numbers in the text."""
        ...

@runtime_checkable
class AgentRunLogger(Protocol):
    def log_input(self, step: str, input_data: Any) -> None:
        """Logs input payload for a workflow step."""
        ...
        
    def log_tool_call(self, step: str, tool_name: str, args: Any) -> None:
        """Logs tool execution within a workflow step."""
        ...
        
    def log_output(self, step: str, output_data: Any) -> None:
        """Logs output payload of a workflow step."""
        ...
        
    def log_error(self, step: str, error_message: str) -> None:
        """Logs an execution error in the workflow."""
        ...


# --- Central Service Registry ---

class ServiceRegistry:
    def __init__(self):
        self.receipt_parser: Optional[ReceiptParser] = None
        self.memory_repo: Optional[PurchaseMemoryRepository] = None
        self.deals_client: Optional[DealsClient] = None
        self.redactor: Optional[SecurityRedactor] = None
        self.logger: Optional[AgentRunLogger] = None

# Global instance resolved by nodes at runtime
registry = ServiceRegistry()
