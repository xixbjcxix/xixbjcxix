"""Login window: a local page (opened in your browser) to connect your Kalshi API key.

Everything stays on this computer: the page is served on 127.0.0.1 only, the
private key is written to data/kalshi-key.pem, and nothing is sent anywhere
except the signed test request to Kalshi.
"""
from __future__ import annotations

import asyncio
import os
import secrets
import webbrowser

from aiohttp import web
from cryptography.hazmat.primitives import serialization

from .config import BASE_URLS, DATA_DIR, load_settings, save_settings
from .kalshi import KalshiClient, load_keys, save_keys

KEY_FILE = "kalshi-key.pem"

PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>adaptbot login</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--fg:#1d2330;--mut:#6b7280;--bd:#d9dde3;--acc:#3b5bdb;--ok:#137a3a;--bad:#b42318;--in:#f3f4f6}
@media (prefers-color-scheme:dark){:root{--bg:#111418;--card:#1a1e24;--fg:#e6e8eb;--mut:#9aa3ad;--bd:#2f3640;--acc:#7c94ff;--ok:#4cc37a;--bad:#ff6b6b;--in:#22272e}}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif}
main{max-width:620px;margin:32px auto;padding:0 16px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:20px}
h1{font-size:20px;margin:0 0 4px} p{color:var(--mut);margin:4px 0 16px}
label{display:block;font-weight:600;margin:14px 0 6px}
input,textarea,select{width:100%;box-sizing:border-box;background:var(--in);color:var(--fg);border:1px solid var(--bd);border-radius:8px;padding:9px;font:inherit}
textarea{height:130px;font-family:ui-monospace,monospace;font-size:12px}
.row{display:flex;gap:10px;flex-wrap:wrap;margin-top:18px}
button{background:var(--acc);color:#fff;border:0;border-radius:8px;padding:10px 16px;font-weight:600;cursor:pointer}
button.sec{background:transparent;color:var(--acc);border:1px solid var(--acc)}
#msg{margin-top:16px;white-space:pre-wrap} .ok{color:var(--ok)} .bad{color:var(--bad)}
.hint{font-size:13px;color:var(--mut);font-weight:400}
</style></head><body><main><div class="card">
<h1>Connect your Kalshi account</h1>
<p>Use the API key you already have (the one kbot used) or create one on kalshi.com &rarr; Account &amp; security &rarr; API keys.
Everything stays on this computer.</p>
<div id="kbot"></div>
<label>Account</label>
<select id="env"><option value="prod">kalshi.com (real money)</option><option value="demo">Demo (practice)</option></select>
<label>API key ID</label><input id="kid" placeholder="e.g. 1a2b3c4d-..." autocomplete="off" value="__KID__">
<label>Private key <span class="hint">- choose the .pem/.key file you downloaded, or paste its contents</span></label>
<input type="file" id="file" accept=".pem,.key,.txt">
<textarea id="pem" placeholder="-----BEGIN RSA PRIVATE KEY-----&#10;...&#10;-----END RSA PRIVATE KEY-----"></textarea>
<div class="row"><button onclick="go('test')">Test connection</button><button class="sec" onclick="go('save')">Save and close</button></div>
<div id="msg">__STATUS__</div>
</div></main><script>
const T="__TOKEN__";
document.getElementById('env').value="__ENV__";
document.getElementById('file').onchange=async e=>{const f=e.target.files[0]; if(f) document.getElementById('pem').value=await f.text()};
async function go(action, extra){
 const m=document.getElementById('msg'); m.className=''; m.textContent='working...';
 const body=Object.assign({action,env:env.value,key_id:kid.value.trim(),pem:pem.value.trim()},extra||{});
 const r=await fetch('/api?t='+T,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
 const d=await r.json(); m.className=d.ok?'ok':'bad'; m.textContent=d.message;
 if(d.ok&&action=='save') setTimeout(()=>window.close(),1500);
}
const kb=__KBOT__;
if(kb){document.getElementById('kbot').innerHTML=`<div class="row" style="margin:0 0 6px"><button class="sec" onclick="go('kbot')">Use my kbot key (${kb})</button></div>`}
</script></body></html>"""


def _load_pem(text: str):
    return serialization.load_pem_private_key(text.encode(), password=None)


def find_kbot_env() -> str | None:
    """Look for an old kbot install (its .env holds KALSHI_PROD_KEY_ID / _PATH)."""
    here = os.path.abspath(".")
    cands = [os.path.join(here, ".env"), os.path.join(here, "..", "kbot", ".env"),
             os.path.join(here, "..", ".env"), os.path.join(os.path.expanduser("~"), "kbot", ".env")]
    for c in cands:
        if os.path.exists(c):
            return os.path.abspath(c)
    return None


def read_kbot_key(env_path: str, which: str = "PROD") -> tuple[str, str] | None:
    vals = {}
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip().strip('"').strip("'")
    kid, kp = vals.get(f"KALSHI_{which}_KEY_ID"), vals.get(f"KALSHI_{which}_KEY_PATH")
    if not kid or not kp:
        return None
    if not os.path.isabs(kp):
        kp = os.path.join(os.path.dirname(env_path), kp)
    return (kid, kp) if os.path.exists(kp) else None


async def test_key(env: str, key_id: str, key_path: str) -> str:
    async with KalshiClient(BASE_URLS[env], key_id, key_path) as c:
        bal = await c.balances()
    parts = [f"shard {k}: ${v:,.2f}" for k, v in sorted(bal.items())]
    return "Connected. Cash " + ", ".join(parts) + "  (crypto markets use shard 2)"


def write_key(pem: str) -> str:
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.abspath(os.path.join(DATA_DIR, KEY_FILE))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(pem.strip() + "\n")
    return path


async def run_login(port: int = 8788, open_browser: bool = True) -> bool:
    token = secrets.token_urlsafe(16)
    done = asyncio.Event()
    result = {"saved": False}
    s = load_settings()
    existing = load_keys()
    kbot_env = find_kbot_env()
    kbot_key = read_kbot_key(kbot_env) if kbot_env else None

    async def page(req):
        if req.query.get("t") != token:
            return web.Response(status=403, text="open the link printed in the bot window")
        status = f"Currently saved key: {existing[0][:8]}..." if existing else ""
        html = (PAGE.replace("__TOKEN__", token).replace("__ENV__", s.kalshi_env)
                .replace("__KID__", "").replace("__STATUS__", status)
                .replace("__KBOT__", f'"{kbot_key[0][:8]}..."' if kbot_key else "null"))
        return web.Response(text=html, content_type="text/html")

    async def api(req):
        if req.query.get("t") != token:
            return web.json_response({"ok": False, "message": "bad token"}, status=403)
        d = await req.json()
        env = d.get("env") if d.get("env") in BASE_URLS else "prod"
        try:
            if d["action"] == "kbot":
                if not kbot_key:
                    return web.json_response({"ok": False, "message": "no kbot key found"})
                msg = await test_key(env, *kbot_key)
                save_keys(*kbot_key)
                s.kalshi_env = env
                save_settings(s)
                result["saved"] = True
                done.set()
                return web.json_response({"ok": True, "message": msg + "\nSaved - using your kbot key. "
                                          "You can close this window."})
            kid, pem = d.get("key_id", ""), d.get("pem", "")
            if not kid:
                return web.json_response({"ok": False, "message": "Enter the API key ID."})
            if pem:
                try:
                    _load_pem(pem)
                except Exception:  # noqa: BLE001
                    return web.json_response({"ok": False, "message": "That private key could not be read. "
                                              "Choose the original .pem/.key file from Kalshi."})
                tmp = write_key(pem)
            elif existing and existing[0] == kid:
                tmp = existing[1]
            else:
                return web.json_response({"ok": False, "message": "Choose or paste the private key."})
            msg = await test_key(env, kid, tmp)
            if d["action"] == "save":
                save_keys(kid, tmp)
                s.kalshi_env = env
                save_settings(s)
                result["saved"] = True
                done.set()
                msg += "\nSaved. You can close this window."
            return web.json_response({"ok": True, "message": msg})
        except Exception as e:  # noqa: BLE001
            text = str(e)
            if "401" in text or "403" in text:
                text = ("Kalshi rejected the key (wrong key ID, wrong private key, or a demo key used on "
                        "kalshi.com). " + text[:160])
            return web.json_response({"ok": False, "message": "Connection failed: " + text[:300]})

    app = web.Application()
    app.add_routes([web.get("/", page), web.post("/api", api)])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    url = f"http://127.0.0.1:{port}/?t={token}"
    print(f"\nLogin window: {url}\n(opening it in your browser - press Ctrl-C here to cancel)")
    if open_browser:
        webbrowser.open(url)
    try:
        await done.wait()
        await asyncio.sleep(2)          # let the page show the result
    finally:
        await runner.cleanup()
    print("Kalshi key saved. Next: Check (2).")
    return result["saved"]
