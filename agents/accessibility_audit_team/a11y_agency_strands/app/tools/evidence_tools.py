def log_keyboard_test(journey: str, outcome: str) -> dict:
    return {"journey": journey, "mode": "keyboard", "outcome": outcome}


def log_screen_reader_test(journey: str, outcome: str) -> dict:
    return {"journey": journey, "mode": "screen_reader", "outcome": outcome}


def log_mobile_accessibility_test(journey: str, outcome: str) -> dict:
    return {"journey": journey, "mode": "mobile", "outcome": outcome}
