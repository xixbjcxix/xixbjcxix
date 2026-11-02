import asyncio
import json

import aiohttp
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

import adaptbot.login as login


def pem():
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                           serialization.NoEncryption()).decode()


def test_read_kbot_key(tmp_path):
    (tmp_path / "keys").mkdir()
    (tmp_path / "keys" / "kalshi-prod.pem").write_text(pem())
    env = tmp_path / ".env"
    env.write_text("# c\nKALSHI_PROD_KEY_ID=abc-123\nKALSHI_PROD_KEY_PATH=keys/kalshi-prod.pem\n")
    kid, path = login.read_kbot_key(str(env))
    assert kid == "abc-123" and path.endswith("kalshi-prod.pem")
    env.write_text("KALSHI_PROD_KEY_ID=\n")
    assert login.read_kbot_key(str(env)) is None


def test_login_window_saves_key(tmp_path, monkeypatch):
    monkeypatch.setattr(login, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr("adaptbot.kalshi.DATA_DIR", str(tmp_path))
    monkeypatch.setattr("adaptbot.config.DATA_DIR", str(tmp_path))
    monkeypatch.setattr(login, "find_kbot_env", lambda: None)
    monkeypatch.setattr("adaptbot.config.settings_path", lambda: str(tmp_path / "settings.yaml"))

    async def fake_test(env, kid, path):
        return f"Connected. Cash shard 2: $60.00 ({kid})"
    monkeypatch.setattr(login, "test_key", fake_test)
    printed = []
    monkeypatch.setattr("builtins.print", lambda *a, **k: printed.append(" ".join(map(str, a))))

    async def main():
        task = asyncio.create_task(login.run_login(port=8799, open_browser=False))
        for _ in range(50):
            await asyncio.sleep(0.05)
            if any("Login window" in p for p in printed):
                break
        import re
        url = re.search(r"http://\S+", next(p for p in printed if "Login window" in p)).group(0)
        token = url.split("t=")[1]
        async with aiohttp.ClientSession() as s:
            async with s.get("http://127.0.0.1:8799/") as r:
                assert r.status == 403                      # no token, no page
            async with s.get(url) as r:
                assert r.status == 200 and "Connect your Kalshi account" in await r.text()
            bad = {"action": "test", "env": "prod", "key_id": "k", "pem": "not a key"}
            async with s.post(f"http://127.0.0.1:8799/api?t={token}", json=bad) as r:
                assert (await r.json())["ok"] is False
            good = {"action": "save", "env": "prod", "key_id": "kid-1", "pem": pem()}
            async with s.post(f"http://127.0.0.1:8799/api?t={token}", json=good) as r:
                d = await r.json()
                assert d["ok"], d
        return await asyncio.wait_for(task, 5)

    assert asyncio.run(main()) is True
    from adaptbot.kalshi import load_keys
    kid, path = load_keys()
    assert kid == "kid-1" and path.endswith("kalshi-key.pem")
