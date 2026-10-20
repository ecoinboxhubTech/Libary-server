"""Card rules for the NITT Pay sandbox. Pure functions: nothing here stores, logs or returns a card number."""
from datetime import datetime

OTP_CODE = "123456"
MAX_CARD_ATTEMPTS = 5
MAX_OTP_ATTEMPTS = 3

# number -> (outcome, message). Any other card that passes validation succeeds.
TEST_CARDS = {
    "4242424242424242": ("success", None),
    "4000000000003220": ("otp", None),
    "4000000000009995": ("fail", "Insufficient funds"),
    "4000000000000002": ("fail", "Declined by bank"),
    "4000000000000069": ("fail", "Expired card"),
    "4000000000000119": ("fail", "Processing error"),
}


def luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
    return total % 10 == 0


def parse_expiry(exp: str) -> tuple[int, int] | None:
    """MM/YY, MM/YYYY, MM-YY or MMYY -> (year, month); None if it is not a date."""
    cleaned = "".join(ch for ch in exp if ch.isdigit())
    if len(cleaned) not in (4, 6):
        return None
    month, year = int(cleaned[:2]), int(cleaned[2:])
    if year < 100:
        year += 2000
    return (year, month) if 1 <= month <= 12 else None


def validate(number: str, exp: str, cvv: str, now: datetime) -> tuple[str | None, str | None]:
    """In the order the brief fixes: Luhn, expiry in the future, 3-4 digit code. Returns (digits, problem)."""
    digits = "".join(ch for ch in number if ch not in " -")
    if not digits.isdigit() or not 12 <= len(digits) <= 19 or not luhn_ok(digits):
        return None, "Invalid card number"
    parsed = parse_expiry(exp)
    if not parsed or parsed < (now.year, now.month):  # a card is good through the end of its expiry month
        return None, "Card has expired or the expiry date is invalid"
    if not (cvv.isdigit() and len(cvv) in (3, 4)):
        return None, "Invalid security code"
    return digits, None


def outcome(digits: str) -> tuple[str, str | None]:
    return TEST_CARDS.get(digits, ("success", None))
