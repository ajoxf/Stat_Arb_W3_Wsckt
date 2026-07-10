"""
Tests for the Telegram command-menu registration (setMyCommands).

The bot's handlers existed but were invisible in the Telegram chat "Menu"
button because the command list was never published to the Bot API. These
tests pin: (1) registration posts every menu command, (2) a failed
registration leaves _menu_token unset so the poll loop retries, and
(3) every command advertised in the menu actually reaches a handler
(no "Unknown command" reply) — so menu and handler table can't drift apart.
"""
from types import SimpleNamespace

from core.telegram_bot import TelegramNotifier, BOT_COMMAND_MENU


def _ready_bot():
    bot = TelegramNotifier()
    bot._enabled = True
    bot._token = "TESTTOKEN"
    bot._chat_id = "42"
    return bot


def test_menu_registration_posts_all_commands(monkeypatch):
    calls = {}

    def fake_post(url, json=None, timeout=None):
        calls["url"] = url
        calls["payload"] = json
        return SimpleNamespace(status_code=200, json=lambda: {"ok": True}, text="")

    monkeypatch.setattr("core.telegram_bot.requests.post", fake_post)
    bot = _ready_bot()

    assert bot._register_command_menu() is True
    assert calls["url"].endswith("/setMyCommands")
    posted = {c["command"] for c in calls["payload"]["commands"]}
    assert posted == {c for c, _ in BOT_COMMAND_MENU}
    # The whole point of the change: the algo on/off switch is in the menu.
    assert {"pause", "resume"} <= posted
    assert bot._menu_token == "TESTTOKEN"


def test_menu_registration_failure_leaves_token_unset(monkeypatch):
    def fake_post(url, json=None, timeout=None):
        return SimpleNamespace(status_code=400, json=lambda: {"ok": False},
                               text="bad request")

    monkeypatch.setattr("core.telegram_bot.requests.post", fake_post)
    bot = _ready_bot()

    assert bot._register_command_menu() is False
    assert bot._menu_token is None  # poll loop will retry next cycle


def test_menu_command_names_are_api_legal():
    # Bot API: 1-32 chars, lowercase a-z 0-9 underscore; description 1-256.
    for cmd, desc in BOT_COMMAND_MENU:
        assert 1 <= len(cmd) <= 32
        assert all(ch.islower() or ch.isdigit() or ch == "_" for ch in cmd)
        assert 1 <= len(desc) <= 256


def test_restart_command_invokes_callback(monkeypatch):
    bot = _ready_bot()
    sent, called = [], []
    monkeypatch.setattr(bot, "_send", lambda t: sent.append(t))
    bot.restart_cb = lambda: (called.append(True), {"ok": True})[1]
    bot._handle_update({"message": {"chat": {"id": 42}, "text": "/restart"}})
    assert called == [True]                          # restart callback fired
    assert any("RESTART" in t for t in sent)         # user got a confirmation first


def test_restart_without_callback_is_safe(monkeypatch):
    # No callback wired -> replies 'Not configured' and does NOT exec anything.
    bot = _ready_bot()
    sent = []
    monkeypatch.setattr(bot, "_send", lambda t: sent.append(t))
    bot.restart_cb = None
    bot._handle_update({"message": {"chat": {"id": 42}, "text": "/restart"}})
    assert any("Not configured" in t for t in sent)


def test_every_menu_command_reaches_a_handler(monkeypatch):
    """Feed each advertised command through _handle_update: none may fall
    through to the 'Unknown command' branch. Handlers may reply 'Not
    configured.' or an error (callbacks are unset here) — that still proves
    the command is routed."""
    bot = _ready_bot()
    sent = []
    monkeypatch.setattr(bot, "_send", lambda text: sent.append(text))

    for cmd, _ in BOT_COMMAND_MENU:
        bot._handle_update({"message": {"chat": {"id": 42}, "text": f"/{cmd}"}})

    assert not any("Unknown command" in t for t in sent)
