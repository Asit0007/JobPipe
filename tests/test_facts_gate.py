import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import jobpipe.config as config
from jobpipe.tailor import _menu


def test_unverified_facts_are_blocked():
    menu, meta = _menu()
    assert meta["blocked"], "seed facts ship unverified so the gate is exercised"
    for fid in meta["blocked"]:
        assert fid not in menu, f"{fid} leaked into the model's menu"


def test_only_verified_facts_reach_the_model(monkeypatch):
    fake = {
        "roles": [{"company": "X", "facts": [
            {"id": "A1", "verified": True,  "text": "real thing"},
            {"id": "A2", "verified": False, "text": "unconfirmed thing"},
        ]}],
        "projects": [],
    }
    monkeypatch.setattr(config, "facts", lambda: fake)
    import jobpipe.tailor as tailor
    monkeypatch.setattr(tailor, "facts", lambda: fake)
    menu, meta = tailor._menu()
    assert "A1" in menu and "real thing" in menu
    assert "A2" not in menu and "unconfirmed thing" not in menu
    assert list(meta["allowed"]) == ["A1"]
