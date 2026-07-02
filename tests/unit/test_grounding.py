import pytest
from app.interfaces import ReceiptData, ReceiptItem
from app.agent import validate_receipt_extraction, invalid_receipt_responder
from google.adk.events.event import Event

class MockCtx:
    def __init__(self, sanitized_text: str):
        self.state = {
            "sanitized_text": sanitized_text,
            "current_run": {
                "pii_redacted_count": 0,
                "intent": "receipt",
                "route_taken": "receipt"
            }
        }

def test_validation_passes_fully_grounded():
    # Input has coffee and bread
    sanitized = "Safeway Store\nCoffee: $9.99\nBread: $2.49\nTotal: $12.48"
    ctx = MockCtx(sanitized)
    
    receipt = ReceiptData(
        store="Safeway",
        date="2026-07-02",
        items=[
            ReceiptItem(name="Coffee", price=9.99, quantity=1, confidence=1.0, raw_line="Coffee: $9.99"),
            ReceiptItem(name="Bread", price=2.49, quantity=1, confidence=1.0, raw_line="Bread: $2.49")
        ],
        prices=[9.99, 2.49],
        total=12.48,
        extraction_confidence=1.0
    )
    
    event = validate_receipt_extraction(receipt, ctx=ctx)
    assert event.actions.route == "valid"
    assert event.output == receipt

def test_validation_rejects_hallucinations():
    # Input has coffee, but receipt extraction invents sunscreen and lotion
    sanitized = "Safeway Store\nCoffee: $9.99\nTotal: $9.99"
    ctx = MockCtx(sanitized)
    
    receipt = ReceiptData(
        store="Safeway",
        date="2026-07-02",
        items=[
            ReceiptItem(name="Coffee", price=9.99),
            ReceiptItem(name="Sunscreen", price=13.12),
            ReceiptItem(name="Lotion", price=8.50)
        ],
        prices=[9.99, 13.12, 8.50],
        total=31.61,
        extraction_confidence=0.9
    )
    
    event = validate_receipt_extraction(receipt, ctx=ctx)
    assert event.actions.route == "invalid"
    assert "could not reliably read this receipt" in event.output

def test_validation_rejects_empty_items():
    sanitized = "Just random chat text that does not look like a receipt."
    ctx = MockCtx(sanitized)
    
    receipt = ReceiptData(
        store="unknown",
        date="2026-07-02",
        items=[],
        prices=[],
        total=0.0,
        extraction_confidence=0.2
    )
    
    event = validate_receipt_extraction(receipt, ctx=ctx)
    assert event.actions.route == "invalid"
    assert "could not reliably read this receipt" in event.output

def test_validation_rejects_low_confidence():
    sanitized = "Safeway Store\nCoffee: $9.99\nTotal: $9.99"
    ctx = MockCtx(sanitized)
    
    receipt = ReceiptData(
        store="Safeway",
        date="2026-07-02",
        items=[ReceiptItem(name="Coffee", price=9.99)],
        prices=[9.99],
        total=9.99,
        extraction_confidence=0.3 # Low confidence
    )
    
    event = validate_receipt_extraction(receipt, ctx=ctx)
    assert event.actions.route == "invalid"
    assert "could not reliably read this receipt" in event.output

def test_approximate_matching_passes():
    # Check if slight item formatting/case differences pass
    sanitized = "Whole Foods\nORGANIC HONEYCRISP APPLES - $4.99\nTotal: $4.99"
    ctx = MockCtx(sanitized)
    
    receipt = ReceiptData(
        store="Whole Foods",
        date="2026-07-02",
        items=[ReceiptItem(name="Honeycrisp Apples", price=4.99)],
        prices=[4.99],
        total=4.99,
        extraction_confidence=0.95
    )
    
    event = validate_receipt_extraction(receipt, ctx=ctx)
    assert event.actions.route == "valid"

def test_invalid_receipt_responder():
    msg = "I could not reliably read this receipt."
    out = invalid_receipt_responder(msg)
    assert out == msg

def test_online_order_summary_one_item_accepted():
    sanitized = "Amazon Order Summary\nOrder placed July 1, 2026\nOrder # 112-2923048-7481024\nGrand Total: $22.25\nFANHAO Garden Hose Nozzle: $20.23\nSold by: Fanhao Shop"
    ctx = MockCtx(sanitized)
    receipt = ReceiptData(
        store="Amazon",
        date="2026-07-01",
        receipt_type="online_order_summary",
        order_id="112-2923048-7481024",
        items=[ReceiptItem(name="FANHAO Garden Hose Nozzle", price=20.23, seller="Fanhao Shop")],
        prices=[20.23],
        subtotal=20.23,
        shipping=0.00,
        tax=2.02,
        grand_total=22.25,
        total=22.25,
        extraction_confidence=1.0
    )
    event = validate_receipt_extraction(receipt, ctx=ctx)
    assert event.actions.route == "valid"

def test_long_wrapped_item_name_accepted():
    sanitized = "Amazon Order Summary\nArriving today\nFANHAO Garden Hose Nozzle, 100% Heavy Duty\nMetal Spray Nozzle with Thumb Control, High Pressure\nWater Nozzle with 8 Adjustable Spray Patterns for Watering\nPlants, Washing Cars and Showering Pets\nSold by: Fanhao Shop\n$20.23"
    ctx = MockCtx(sanitized)
    receipt = ReceiptData(
        store="Amazon",
        date="2026-07-01",
        receipt_type="online_order_summary",
        items=[ReceiptItem(name="FANHAO Garden Hose Nozzle, 100% Heavy Duty Metal Spray Nozzle", price=20.23)],
        prices=[20.23],
        total=20.23,
        grand_total=20.23,
        extraction_confidence=0.9
    )
    event = validate_receipt_extraction(receipt, ctx=ctx)
    assert event.actions.route == "valid"

def test_multiple_items_accepted():
    sanitized = "Target Store\nShirts: $10.00\nSun Bum sunscreen: $13.12\nTotal: $23.12"
    ctx = MockCtx(sanitized)
    receipt = ReceiptData(
        store="Target",
        date="2026-07-02",
        receipt_type="retail_receipt",
        items=[
            ReceiptItem(name="Shirts", price=10.00),
            ReceiptItem(name="Sun Bum sunscreen", price=13.12)
        ],
        prices=[10.00, 13.12],
        subtotal=23.12,
        tax=0.0,
        grand_total=23.12,
        total=23.12,
        extraction_confidence=1.0
    )
    event = validate_receipt_extraction(receipt, ctx=ctx)
    assert event.actions.route == "valid"

def test_hallucination_prevention_rejects_unrelated_items():
    sanitized = "Amazon Order Summary\nFANHAO Garden Hose Nozzle: $20.23\nGrand Total: $22.25"
    ctx = MockCtx(sanitized)
    # Extracted data includes unrelated Target/Costco items not in input
    receipt = ReceiptData(
        store="Amazon",
        date="2026-07-01",
        items=[
            ReceiptItem(name="FANHAO Garden Hose Nozzle", price=20.23),
            ReceiptItem(name="VICK Cough Drops", price=5.99), # Hallucinated
            ReceiptItem(name="Up&Up Diapers", price=12.49) # Hallucinated
        ],
        prices=[20.23, 5.99, 12.49],
        total=38.71,
        extraction_confidence=0.9
    )
    event = validate_receipt_extraction(receipt, ctx=ctx)
    assert event.actions.route == "invalid"

def test_sku_joined_line_accepted():
    sanitized = "Target Store\n037119242Sun Bum T+ $13.12\nTotal $13.12"
    ctx = MockCtx(sanitized)
    receipt = ReceiptData(
        store="Target",
        date="2026-06-10",
        items=[ReceiptItem(name="Sun Bum", price=13.12, raw_line="037119242Sun Bum T+ $13.12")],
        prices=[13.12],
        total=13.12,
        extraction_confidence=0.95
    )
    event = validate_receipt_extraction(receipt, ctx=ctx)
    assert event.actions.route == "valid"

def test_regular_price_and_sale_price_accepted():
    sanitized = "Target Store\n037119242Sun Bum T+ $13.12\nRegular Price $17.49\nTotal $13.12"
    ctx = MockCtx(sanitized)
    receipt = ReceiptData(
        store="Target",
        date="2026-06-10",
        items=[ReceiptItem(name="Sun Bum", price=13.12)],
        prices=[13.12, 17.49],
        total=13.12,
        extraction_confidence=0.9
    )
    event = validate_receipt_extraction(receipt, ctx=ctx)
    assert event.actions.route == "valid"

def test_repeated_item_name_accepted():
    sanitized = "Target Store\n081228577Mondo Llama T $1.50\n081222893Mondo Llama T $1.50\nTotal $3.00"
    ctx = MockCtx(sanitized)
    receipt = ReceiptData(
        store="Target",
        date="2026-06-10",
        items=[
            ReceiptItem(name="Mondo Llama", price=1.50),
            ReceiptItem(name="Mondo Llama", price=1.50)
        ],
        prices=[1.50, 1.50],
        total=3.00,
        extraction_confidence=0.9
    )
    event = validate_receipt_extraction(receipt, ctx=ctx)
    assert event.actions.route == "valid"

def test_extra_prices_array_values_accepted():
    sanitized = "Target Store\nShirts: $10.00\nTotal $10.00"
    ctx = MockCtx(sanitized)
    receipt = ReceiptData(
        store="Target",
        date="2026-06-10",
        items=[ReceiptItem(name="Shirts", price=10.00)],
        prices=[10.00, 20.00, 10.00, 1.00],
        total=10.00,
        extraction_confidence=0.9
    )
    event = validate_receipt_extraction(receipt, ctx=ctx)
    assert event.actions.route == "valid"
