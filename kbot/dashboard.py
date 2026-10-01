"""Local dashboard (stdlib only): active markets, inventory, open orders, live PnL,
risk-limit status and a kill-switch button. Binds to 127.0.0.1 by default."""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
from typing import Optional

log = logging.getLogger("kbot.dashboard")

# editable from the dashboard: key -> (type, min, max)
EDITABLE = {
    "strategy.target_pair_cost": (float, 0.50, 0.99),
    "strategy.clip_shares": (float, 1, 5000),
    "strategy.cutoff_s.900": (float, 5, 600),
    "strategy.residual.enabled": (bool, None, None),
    "strategy.residual.max_usd": (float, 0, 10000),
    "risk.max_order_usd": (float, 1, 100000),
    "risk.max_market_usd": (float, 1, 1000000),
    "risk.max_residual_usd": (float, 0, 100000),
    "risk.max_residual_shares": (float, 0, 100000),
    "risk.max_total_usd": (float, 1, 1000000),
    "risk.max_daily_loss_usd": (float, 1, 1000000),
    "markets.assets": (list, None, None),
}


def _get(obj, key: str):
    for part in key.split("."):
        obj = obj[int(part)] if isinstance(obj, dict) else getattr(obj, part)
    return obj


def _set(obj, key: str, val) -> None:
    parts = key.split(".")
    for part in parts[:-1]:
        obj = getattr(obj, part)
    last = parts[-1]
    if isinstance(obj, dict):
        obj[int(last)] = val
    else:
        setattr(obj, last, val)


def get_settings(cfg) -> dict:
    out = {k: _get(cfg, k) for k in EDITABLE}
    out["_available_assets"] = sorted(cfg.kalshi.series.keys())
    return out


def validate_settings(data: dict, known_assets: Optional[list] = None) -> dict:
    clean = {}
    for k, v in data.items():
        if k not in EDITABLE:
            raise ValueError(f"not editable: {k}")
        typ, lo, hi = EDITABLE[k]
        if typ is bool:
            clean[k] = bool(v)
        elif typ is list:
            vals = [str(x).strip().lower() for x in (v if isinstance(v, list) else str(v).split(","))]
            vals = [x for x in vals if x]
            allowed = set(known_assets) if known_assets else None
            if not vals or (allowed is not None and any(x not in allowed for x in vals)):
                raise ValueError("assets must be one of: " + ", ".join(sorted(allowed)) if allowed
                                 else "assets must be a non-empty list")
            clean[k] = vals
        else:
            try:
                x = float(v)
            except (TypeError, ValueError):
                raise ValueError(f"{k} must be a number")
            if x != x or x < lo or x > hi:
                raise ValueError(f"{k} must be between {lo} and {hi}")
            clean[k] = x
    return clean


def apply_settings(core, data: dict, overlay_path: Optional[str] = None) -> dict:
    """Validate, apply to the running config (strategy/risk read it live) and persist the overlay."""
    import os
    import yaml
    from .config import SETTINGS_OVERLAY
    clean = validate_settings(data, known_assets=list(core.cfg.kalshi.series.keys()))
    for k, v in clean.items():
        _set(core.cfg, k, v)
    path = overlay_path or SETTINGS_OVERLAY
    cur = {}
    if os.path.exists(path):
        cur = yaml.safe_load(open(path)) or {}
    for k, v in clean.items():
        d = cur
        parts = k.split(".")
        for part in parts[:-1]:
            d = d.setdefault(part, {})
        d[int(parts[-1]) if parts[-1].isdigit() else parts[-1]] = v
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as fh:
        yaml.safe_dump(cur, fh, sort_keys=True)
    core.store.risk_event(core.run_id, core.now_ms, "settings_changed", json.dumps(clean))
    return clean

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kalshi bot</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--fg:#1d2330;--mut:#6b7280;--line:#e5e7eb;--good:#0f7b4f;--bad:#b42318;--warn:#b54708;--acc:#2f5bea}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#171a21;--fg:#e6e8ee;--mut:#9aa3b2;--line:#262b36;--good:#3ccf8e;--bad:#ff6b5e;--warn:#f5a524;--acc:#7b9cff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
header{display:flex;align-items:center;gap:12px;padding:14px 20px;border-bottom:1px solid var(--line);background:var(--card);position:sticky;top:0;flex-wrap:wrap}
h1{font-size:16px;margin:0;font-weight:650}.pill{padding:3px 10px;border-radius:999px;font-size:12px;font-weight:600;border:1px solid var(--line)}
.ok{color:var(--good)}.bad{color:var(--bad)}.warn{color:var(--warn)}.mut{color:var(--mut)}
main{padding:16px 20px;display:grid;gap:16px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px}.kpi b{display:block;font-size:20px;font-variant-numeric:tabular-nums}
.kpi span{font-size:12px;color:var(--mut)}
section{background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:auto}
section h2{font-size:13px;margin:0;padding:10px 12px;border-bottom:1px solid var(--line);color:var(--mut);font-weight:600;text-transform:uppercase;letter-spacing:.04em}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}th,td{padding:7px 10px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
th{font-size:12px;color:var(--mut);font-weight:600}td:first-child,th:first-child{text-align:left}
button{margin-left:auto;background:var(--bad);color:#fff;border:0;border-radius:8px;padding:8px 14px;font-weight:650;cursor:pointer}
button:disabled{opacity:.5;cursor:not-allowed}
.empty{padding:14px 12px;color:var(--mut)}
.settings{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:10px 18px;padding:12px}
.settings label{display:flex;flex-direction:column;gap:4px;font-size:12px;color:var(--mut)}
.settings input{font:inherit;color:var(--fg);background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:6px 8px}
.settings .row{display:flex;gap:10px;align-items:center;grid-column:1/-1}
.settings .save{background:var(--acc);margin-left:0}
#saved{font-size:12px}
</style></head><body>
<header><h1>Kalshi bot</h1><span id="mode" class="pill mut">…</span><span id="data" class="pill">…</span>
<span id="clock" class="mut"></span><button id="kill">Kill switch</button></header>
<main>
<div id="status" class="kpi bad" style="display:none"></div>
<div class="kpis">
 <div class="kpi"><span>PnL today (incl. MtM)</span><b id="k_today">–</b></div>
 <div class="kpi"><span>Realized (settled)</span><b id="k_real">–</b></div>
 <div class="kpi"><span>Settled markets</span><b id="k_settled">–</b></div>
 <div class="kpi"><span>Open orders</span><b id="k_orders">–</b></div>
 <div class="kpi"><span>Risk status</span><b id="k_risk">–</b></div>
</div>
<section><h2>Active markets</h2><div id="markets"></div></section>
<section><h2 id="orders_h">Open orders</h2><div id="orders"></div></section>
<section><h2>Risk limits</h2><div id="limits"></div></section>
<section><h2>Settings</h2><form id="settings" class="settings"></form>
<div class="mut" style="padding:0 12px 12px">Saved to data/settings.yaml and applied immediately (asset changes apply on restart).</div></section>
</main>
<script>
const TOKEN="__TOKEN__";
const f=(x,d=2)=>x===null||x===undefined?"–":Number(x).toFixed(d);
const cls=x=>x>0?"ok":x<0?"bad":"";
function tbl(cols,rows){if(!rows.length)return '<div class="empty">none</div>';
 return '<table><tr>'+cols.map(c=>'<th>'+c[0]+'</th>').join('')+'</tr>'+rows.map(r=>'<tr>'+cols.map(c=>'<td>'+c[1](r)+'</td>').join('')+'</tr>').join('')+'</table>'}
async function tick(){
 let s; try{s=await (await fetch('/api/state')).json()}catch(e){document.getElementById('data').textContent='dashboard offline';return}
 const md=document.getElementById('mode');md.textContent=(s.mode==='live'?'LIVE · REAL MONEY · ':s.mode+' · ')+s.run_id;
 md.className='pill '+(s.mode==='live'?'bad':'mut');md.style.fontWeight=s.mode==='live'?'800':'600';
 document.getElementById('orders_h').textContent=s.mode==='demo'?'Open orders (real, on demo.kalshi.co)':s.mode==='live'?'Open orders (REAL MONEY, kalshi.com)':'Open orders (simulated)';
 const st=document.getElementById('status');st.style.display=s.status_message?'block':'none';st.textContent=s.status_message||'';
 const d=document.getElementById('data');
 d.textContent=s.data_ok&&!s.spot_gap?'data ok':'DATA GAP: '+(s.data_gap_reason||'spot');d.className='pill '+(s.data_ok&&!s.spot_gap?'ok':'bad');
 document.getElementById('clock').textContent=new Date(s.now_ms).toLocaleTimeString();
 const t=document.getElementById('k_today');t.textContent=f(s.pnl_today);t.className=cls(s.pnl_today);
 const r=document.getElementById('k_real');r.textContent=f(s.realized_total);r.className=cls(s.realized_total);
 document.getElementById('k_settled').textContent=s.settled_markets;
 document.getElementById('k_orders').textContent=s.orders.length;
 const k=document.getElementById('k_risk');
 if(s.killed){k.textContent='KILLED';k.className='bad';document.getElementById('kill').disabled=true}
 else if(s.halted){k.textContent='HALTED';k.className='warn'} else {k.textContent='armed';k.className='ok'}
 document.getElementById('markets').innerHTML=tbl([
  ['market',m=>m.slug],['target',m=>m.target?Number(m.target).toLocaleString():'–'],['index',m=>m.spot?Number(m.spot).toLocaleString():'–'],
  ['t-left',m=>f(m.t_left_s,0)+'s'],['mode',m=>m.mode],
  ['YES bid/ask',m=>f(m.up_bid)+' / '+f(m.up_ask)],['NO bid/ask',m=>f(m.down_bid)+' / '+f(m.down_ask)],
  ['YES',m=>f(m.up,1)],['NO',m=>f(m.down,1)],['paired',m=>f(m.paired,1)],['pair cost',m=>f(m.pair_cost,3)],
  ['residual',m=>'<span class="'+(Math.abs(m.residual)>0?'warn':'')+'">'+f(m.residual,1)+'</span>'],['resid $',m=>f(m.residual_usd)],
  ['signal',m=>m.signal?(m.signal.ok?(m.signal.dir||'none')+' z='+f(m.signal.z,2)+' p='+f(m.signal.p_model,2):m.signal.reason||'stale'):'–'],
  ['fees',m=>f(m.fees)],['MtM PnL',m=>'<span class="'+cls(m.mtm_pnl)+'">'+f(m.mtm_pnl)+'</span>'],['worst case',m=>'<span class="'+cls(m.worst_case_pnl)+'">'+f(m.worst_case_pnl)+'</span>']
 ],s.markets);
 document.getElementById('orders').innerHTML=tbl([
  ['id',o=>o.id],['market',o=>o.market],['side',o=>(o.side==='BUY'?'buy ':'sell ')+o.outcome],['price',o=>f(o.price)],['size',o=>f(o.size,1)],
  ['filled',o=>f(o.filled,1)],['queue ahead',o=>f(o.queue_ahead,0)],['tag',o=>o.tag],['status',o=>o.status]],s.orders);
 document.getElementById('limits').innerHTML=tbl([['limit',x=>x[0]],['value',x=>x[1]]],
  Object.entries(s.risk).concat([['kill reason',s.kill_reason||'–'],['halt reason',s.halt_reason||'–'],
  ['recent rejects',(s.recent_rejects||[]).map(x=>x[2]).slice(-5).join(', ')||'–']]));
}
const FIELDS=[
 ['strategy.target_pair_cost','Target pair cost incl. fees ($)','number','0.01'],
 ['strategy.clip_shares','Contracts per order','number','1'],
 ['strategy.cutoff_s.900','Stop quoting N s before close','number','1'],
 ['strategy.residual.enabled','Allow directional residual','checkbox'],
 ['strategy.residual.max_usd','Max residual kept ($)','number','0.5'],
 ['risk.max_order_usd','Max $ per order','number','1'],
 ['risk.max_market_usd','Max $ per market','number','1'],
 ['risk.max_residual_usd','Max unpaired $ per market','number','1'],
 ['risk.max_residual_shares','Max unpaired contracts per market','number','1'],
 ['risk.max_total_usd','Max $ across markets','number','5'],
 ['risk.max_daily_loss_usd','Max daily loss ($)','number','1'],
 ['markets.assets','Assets (comma separated)','text']];
async function loadSettings(){
 const v=await (await fetch('/api/settings')).json();
 const avail=v._available_assets||[];
 const form=document.getElementById('settings');
 form.innerHTML=FIELDS.map(([k,l,t,st])=>{const val=v[k];
  if(k==='markets.assets')l=l+' - available: '+avail.join(', ');
  if(t==='checkbox')return '<label><span>'+l+'</span><input type="checkbox" name="'+k+'" '+(val?'checked':'')+'></label>';
  return '<label><span>'+l+'</span><input type="'+t+'" '+(st?'step="'+st+'"':'')+' name="'+k+'" value="'+(Array.isArray(val)?val.join(','):val)+'"></label>'}).join('')
  +'<div class="row"><button class="save" type="submit">Save settings</button><span id="saved" class="mut"></span></div>';
}
document.getElementById('settings').onsubmit=async(e)=>{e.preventDefault();
 const body={};for(const [k,,t] of FIELDS){const el=e.target.elements[k];
  body[k]=t==='checkbox'?el.checked:t==='number'?Number(el.value):el.value.split(',').map(x=>x.trim().toLowerCase()).filter(Boolean)}
 const r=await fetch('/api/settings',{method:'POST',headers:{'X-Kbot-Token':TOKEN,'Content-Type':'application/json'},body:JSON.stringify(body)});
 const j=await r.json();document.getElementById('saved').textContent=r.ok?'Saved.':('Not saved: '+(j.error||r.status));
 document.getElementById('saved').className=r.ok?'ok':'bad';loadSettings()};
loadSettings();
document.getElementById('kill').onclick=async()=>{
 if(!confirm('Trip the kill switch? Cancels all orders and stops trading until restart.'))return;
 await fetch('/api/kill',{method:'POST',headers:{'X-Kbot-Token':TOKEN}});tick()};
tick();setInterval(tick,1000);
</script></body></html>"""


class DashboardServer:
    def __init__(self, core, host: str = "127.0.0.1", port: int = 8787, config_path: Optional[str] = None) -> None:
        self.core = core
        self.config_path = config_path
        self.host = host
        self.port = port
        self.token = secrets.token_hex(16)
        self._server: Optional[asyncio.base_events.Server] = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)

    async def start_any_port(self, tries: int = 20) -> str:
        """Bind the configured port, or the next free one. Returns the URL to open."""
        last = None
        for p in range(self.port, self.port + tries):
            try:
                self._server = await asyncio.start_server(self._handle, self.host, p)
                self.port = p
                return f"http://{'127.0.0.1' if self.host in ('0.0.0.0', '') else self.host}:{p}"
            except OSError as e:
                last = e
                log.warning("port %d unavailable (%s), trying %d", p, e, p + 1)
        raise RuntimeError(f"no free port for the dashboard near {self.port}: {last}")

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
            lines = head.decode(errors="replace").split("\r\n")
            method, path, _ = (lines[0].split(" ") + ["", "", ""])[:3]
            headers = {}
            for ln in lines[1:]:
                if ":" in ln:
                    k, v = ln.split(":", 1)
                    headers[k.strip().lower()] = v.strip()
            n = int(headers.get("content-length", "0") or 0)
            body = await reader.readexactly(min(n, 65536)) if n else b""
            if method == "GET" and path in ("/", "/index.html"):
                await self._send(writer, 200, "text/html; charset=utf-8", PAGE.replace("__TOKEN__", self.token))
            elif method == "GET" and path == "/api/state":
                await self._send(writer, 200, "application/json", json.dumps(self.core.snapshot(), default=str))
            elif method == "GET" and path == "/api/settings":
                await self._send(writer, 200, "application/json", json.dumps(get_settings(self.core.cfg)))
            elif method == "POST" and path == "/api/settings":
                if headers.get("x-kbot-token") != self.token:
                    await self._send(writer, 403, "application/json", '{"error":"bad token"}')
                else:
                    try:
                        apply_settings(self.core, json.loads(body or b"{}"))
                        await self._send(writer, 200, "application/json", '{"ok":true}')
                    except ValueError as e:
                        await self._send(writer, 400, "application/json", json.dumps({"error": str(e)}))
            elif method == "POST" and path == "/api/kill":
                if headers.get("x-kbot-token") != self.token:
                    await self._send(writer, 403, "application/json", '{"error":"bad token"}')
                else:
                    self.core.kill("dashboard")
                    log.warning("KILL SWITCH tripped from dashboard")
                    await self._send(writer, 200, "application/json", '{"killed":true}')
            else:
                await self._send(writer, 404, "text/plain", "not found")
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    async def _send(writer: asyncio.StreamWriter, code: int, ctype: str, body: str) -> None:
        b = body.encode()
        reason = {200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found"}.get(code, "")
        writer.write(f"HTTP/1.1 {code} {reason}\r\nContent-Type: {ctype}\r\nContent-Length: {len(b)}\r\n"
                     f"Cache-Control: no-store\r\nConnection: close\r\n\r\n".encode() + b)
        await writer.drain()
