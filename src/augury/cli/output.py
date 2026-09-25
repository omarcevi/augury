from augury.core.text import strip_control_chars


def safe(value: object) -> str:
    """Fetched text (feed titles, error messages) for the terminal: no escape sequences."""
    return strip_control_chars(str(value))
