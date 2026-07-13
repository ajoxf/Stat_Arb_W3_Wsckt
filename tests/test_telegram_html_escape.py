"""
Regression tests for the Telegram HTML-escaping in TelegramNotifier._R.

_R builds every "bold label + inline-code value" row used across /dashboard,
/status, /positions and the trade notifications. Both fields carry dynamic
content — engine error strings, block reasons, exit reasons, _outcome_tag prose,
even static labels like "Level P&L" — that can contain '<', '>' or '&'.

Unescaped, a single stray '<' (classically a Python exception message such as
"'<' not supported between instances of 'NoneType' and 'float'" landing in
status['error']) makes Telegram reject the ENTIRE message with HTTP 400
"Unsupported start tag", and it only survives via the plain-text fallback in
send_telegram_message. Escaping at the _R choke point fixes it for all callers.
"""
import re

from core.telegram_bot import TelegramNotifier

# Tags Telegram's HTML parser actually accepts (used by the validator below).
_ALLOWED = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
            "a", "code", "pre", "blockquote", "tg-spoiler", "span", "tg-emoji"}


def _telegram_html_ok(text: str) -> bool:
    """Approximate Telegram's HTML validation: every '<...>' must open/close a
    tag whose name is in the allow-list. A stray '<' followed by a space/digit
    (an empty or unknown tag name) is exactly what triggers the 400 we fixed."""
    for m in re.finditer(r"<(/?)([^>]*)>", text):
        name = m.group(2).strip().split()[0].lower() if m.group(2).strip() else ""
        if name not in _ALLOWED:
            return False
    # No bare '<' left that didn't form a <...> tag at all (e.g. "a < b").
    stripped = re.sub(r"<(/?)([^>]*)>", "", text)
    return "<" not in stripped


def test_R_escapes_stray_lt_in_value():
    # The classic crash: a TypeError message with a literal '<'.
    err = "'<' not supported between instances of 'NoneType' and 'float'"
    row = TelegramNotifier._R("Error", err)
    assert "&lt;" in row              # the '<' was escaped
    assert "<code>'&lt;'" in row      # still wrapped in the code tag
    assert _telegram_html_ok(row)     # Telegram would accept it now


def test_R_escapes_ampersand_in_label():
    # "Level P&L" and _outcome_tag's "P&L" prose carry a raw '&'.
    row = TelegramNotifier._R("Level P&L", "BE $0.00 · TP +$8.94")
    assert "P&amp;L" in row
    assert _telegram_html_ok(row)


def test_R_escapes_all_three_metacharacters():
    row = TelegramNotifier._R("x", "a < b > c & d")
    assert "&lt;" in row and "&gt;" in row and "&amp;" in row
    # No raw metacharacter survives inside the value position.
    inner = row.split("<code>", 1)[1].rsplit("</code>", 1)[0]
    assert "<" not in inner and ">" not in inner
    assert _telegram_html_ok(row)


def test_R_plain_values_unchanged_and_valid():
    row = TelegramNotifier._R("Z-Score", "-2.7325")
    assert row == "<b>Z-Score</b>  <code>-2.7325</code>"
    assert _telegram_html_ok(row)


def test_validator_catches_the_original_bug():
    # Sanity-check the validator itself: the pre-fix output WOULD be rejected.
    broken = "<b>Error</b>  <code>'<' not supported</code>"
    assert not _telegram_html_ok(broken)
