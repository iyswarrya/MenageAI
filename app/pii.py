import re

def mask_pii(text: str) -> str:
    """
    Masks Personally Identifiable Information (PII) such as credit cards,
    emails, and phone numbers from the input text.
    """
    if not text:
        return text

    # Mask credit card numbers (13 to 16 digits, allowing optional spaces/dashes between digits)
    # Matches: 4111-1111-1111-1111, 1234567890123456, etc.
    cc_pattern = r'\b(?:\d[ -]*?){13,16}\b'
    text = re.sub(cc_pattern, "[REDACTED CARD]", text)

    # Mask email addresses
    email_pattern = r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b'
    text = re.sub(email_pattern, "[REDACTED EMAIL]", text)

    # Mask US-like phone numbers
    # Matches: 555-0199, (123) 456-7890, 123-456-7890, +1 123.456.7890, 1234567890
    phone_pattern = r'\b(?:\+?1[-. ]?)?(?:\(?\d{3}\)?[-. ]?)?\d{3}[-. ]?\d{4}\b'
    text = re.sub(phone_pattern, "[REDACTED PHONE]", text)

    return text
