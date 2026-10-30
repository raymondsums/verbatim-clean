"""The theme setting the page and the panel share."""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401  keep tests off the real vault and .env

import server  # noqa: E402


def test_theme_round_trip(monkeypatch):
    monkeypatch.setattr(server, "UI_PATH", os.path.join(tempfile.mkdtemp(), "ui.json"))
    c = server.app.test_client()
    assert c.get("/api/ui/theme").get_json() == {"theme": "system"}
    assert c.post("/api/ui/theme", json={"theme": "dark"}).status_code == 200
    assert c.get("/api/ui/theme").get_json() == {"theme": "dark"}
    assert c.post("/api/ui/theme", json={"theme": "purple"}).status_code == 400
    assert c.get("/api/ui/theme").get_json() == {"theme": "dark"}
