"""Local dashboard at http://127.0.0.1:<port> (read-only plus a kill switch)."""
from __future__ import annotations

import json
import os

from aiohttp import web

from .config import DATA_DIR

PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>adaptbot</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--fg:#1d2330;--mut:#6b7280;--bd:#e3e6eb;--pos:#137a3a;--neg:#b42318;--acc:#3b5bdb}
@media (prefers-color-scheme:dark){:root{--bg:#111418;--card:#1a1e24;--fg:#e6e8eb;--mut:#9aa3ad;--bd:#2a3038;--pos:#4cc37a;--neg:#ff6b6b;--acc:#7c94ff}}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.4 system-ui,sans-serif}
main{max-width:1100px;margin:auto;padding:16px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:12px 14px;margin-bottom:12px}
h2{font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:var(--mut);margin:0 0 8px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.k b{display:block;font-size:20px} .k span{color:var(--mut);font-size:12px}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums} td,th{padding:4px 6px;border-bottom:1px solid var(--bd);text-align:right}
td:first-child,th:first-child{text-align:left} .pos{color:var(--pos)} .neg{color:var(--neg)}
.wrap{overflow-x:auto} .halt{background:var(--neg);color:#fff;padding:8px 12px;border-radius:8px;margin-bottom:12px}
button{background:var(--neg);color:#fff;border:0;border-radius:8px;padding:8px 14px;font-weight:600;cursor:pointer}
</style></head><body><main>
<div id="halt"></div>
<div class="card"><h2>Status</h2><div class="kpis" id="kpis"></div>
<p id="sel" style="color:var(--mut);margin:8px 0 0"></p></div>
<div class="card"><h2>Markets</h2><div class="wrap"><table id="mk"></table></div></div>
<div class="card"><h2>Profiles (shadow, head to head)</h2><div class="wrap"><table id="pf"></table></div></div>
<div class="card"><h2>Recent trades</h2><div class="wrap"><table id="tr"></table></div></div>
<div class="card"><h2>Settled markets</h2><div class="wrap"><table id="st"></table></div></div>
<div class="card"><button onclick="kill()">KILL: stop entries and sell everything</button></div>
</main><script>
const f=(x,d=2)=>x==null?'-':(+x).toFixed(d), cls=x=>x>0?'pos':x<0?'neg':'';
const row=(c,h)=>'<tr>'+c.map(x=>`<${h?'th':'td'}>${x}</${h?'th':'td'}>`).join('')+'</tr>';
async function kill(){if(confirm('Stop new entries and sell all positions?'))await fetch('/api/kill',{method:'POST'})}
async function load(){
 let s; try{s=await (await fetch('/api/state')).json()}catch(e){return}
 document.getElementById('halt').innerHTML=s.halted?`<div class="halt">HALTED: ${s.halted}</div>`:'';
 const k=[[s.live?'LIVE (real money)':'PAPER','mode'],[`<span class="${cls(s.day_pnl)}">${f(s.day_pnl)}</span>`,'day P&L (account)'],
  [`<span class="${cls(s.realized)}">${f(s.realized)}</span>`,'realized (bot)'],['$'+f(s.exposure),'exposure'],
  [s.profile||'sitting out','live profile'],[`a=${f(s.calibrator.a,2)} b=${f(s.calibrator.b,2)}`,'calibration']];
 document.getElementById('kpis').innerHTML=k.map(([v,l])=>`<div class="k"><b>${v}</b><span>${l}</span></div>`).join('');
 document.getElementById('sel').textContent=s.selector;
 document.getElementById('mk').innerHTML=row(['ticker','strike','left s','yes bid','yes ask','position'],1)+
  s.markets.map(m=>row([m.ticker,f(m.strike),m.remaining,f(m.yes_bid),f(m.yes_ask),m.position+(m.profile?' ('+m.profile+')':'')])).join('');
 document.getElementById('pf').innerHTML=row(['profile','markets','traded','won','lost','total $','score/mkt','last results'],1)+
  Object.entries(s.profiles).sort((a,b)=>b[1].score-a[1].score).map(([n,p])=>row([n+(n==s.profile?' *':''),p.n,p.traded,p.wins,p.losses,
  `<span class="${cls(p.total)}">${f(p.total)}</span>`,f(p.score,3),p.recent.slice(-8).join(' ')])).join('');
 document.getElementById('tr').innerHTML=row(['time','ticker','action','qty','price','why','profile','pnl'],1)+
  s.trades.slice().reverse().map(t=>row([new Date(t.ts*1000).toLocaleTimeString(),t.ticker,t.action+' '+t.side,t.qty,f(t.price),t.reason,t.profile,
  t.action=='sell'?`<span class="${cls(t.pnl)}">${f(t.pnl)}</span>`:''])).join('');
 document.getElementById('st').innerHTML=row(['ticker','result','live pnl','profile'],1)+
  s.settled.slice().reverse().map(m=>row([m.ticker,m.result,m.traded?`<span class="${cls(m.pnl)}">${f(m.pnl)}</span>`:'no trade',m.profile||''])).join('');
}
load();setInterval(load,2000);
</script></body></html>"""


async def start_dashboard(engine, port: int) -> web.AppRunner:
    async def index(_):
        return web.Response(text=PAGE, content_type="text/html")

    async def state(_):
        return web.Response(text=json.dumps(engine.snapshot(), default=str), content_type="application/json")

    async def kill(_):
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(os.path.join(DATA_DIR, "KILL"), "w", encoding="utf-8") as fh:
            fh.write("dashboard")
        engine.risk.halt("kill switch (dashboard)", flatten=True)
        return web.json_response({"ok": True})

    app = web.Application()
    app.add_routes([web.get("/", index), web.get("/api/state", state), web.post("/api/kill", kill)])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    return runner
