"""
crm_api.py — LotVue Pro Dealer CRM Center (P4)
One pane: lead inbox w/ stages, vehicle-linked context, appointments,
follow-ups, AI-BDC thread hand-off. Reads the live vehicles.db / lotvue.db.
Auth: NOT in auth_gate public lists -> dealer sign-in required (correct).
"""
import sqlite3, json
from pathlib import Path
from datetime import datetime
from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse

BASE = Path(__file__).parent
STAGES = ["new", "contacted", "appointment", "negotiating", "sold", "lost"]

def _db(name="vehicles.db"):
    c = sqlite3.connect(BASE / name)
    c.row_factory = sqlite3.Row
    return c

def _rows(c, q, args=()):
    try:
        return [dict(r) for r in c.execute(q, args)]
    except sqlite3.OperationalError:
        return []

def register_crm(app):
    @app.get("/api/crm/summary")
    async def crm_summary():
        v, l = _db(), _db("lotvue.db")
        out = {
            "vehicles": _rows(v, "select count(*) n, sum(case when status='live' then 1 else 0 end) live from vehicles")[0],
            "leads_total": (_rows(v, "select count(*) n from leads") or [{"n": 0}])[0]["n"],
            "leads_new": (_rows(v, "select count(*) n from leads where coalesce(status,'new')='new'") or [{"n": 0}])[0]["n"],
            "by_stage": {r["s"]: r["n"] for r in _rows(v, "select coalesce(status,'new') s, count(*) n from leads group by 1")},
            "appointments_upcoming": (_rows(l, "select count(*) n from appointments where scheduled_at >= datetime('now')") or [{"n": 0}])[0]["n"],
            "followups_pending": (_rows(l, "select count(*) n from follow_ups where status='pending'") or [{"n": 0}])[0]["n"],
        }
        v.close(); l.close()
        return out

    @app.get("/api/crm/leads")
    async def crm_leads(stage: str = "", limit: int = 100):
        v = _db()
        q = ("select l.*, ve.year, ve.make, ve.model, ve.price from leads l "
             "left join vehicles ve on ve.vin = l.vin ")
        args = ()
        if stage:
            q += "where coalesce(l.status,'new') = ? "
            args = (stage,)
        q += "order by l.created_at desc limit ?"
        rows = _rows(v, q, args + (limit,))
        v.close()
        return {"stages": STAGES, "leads": rows}

    @app.post("/api/crm/leads/{lead_id}/stage")
    async def crm_lead_stage(lead_id: int, request: Request):
        body = await request.json()
        stage = body.get("stage", "")
        if stage not in STAGES:
            return JSONResponse({"error": f"stage must be one of {STAGES}"}, status_code=400)
        v = _db()
        v.execute("update leads set status=? where id=?", (stage, lead_id))
        v.commit(); v.close()
        return {"ok": True, "lead_id": lead_id, "stage": stage}

    @app.get("/api/crm/appointments")
    async def crm_appointments():
        l = _db("lotvue.db")
        rows = _rows(l, "select * from appointments order by scheduled_at asc limit 200")
        l.close()
        return {"appointments": rows}

    @app.post("/api/crm/appointments")
    async def crm_appointment_create(request: Request):
        b = await request.json()
        l = _db("lotvue.db")
        l.execute("insert into appointments (lead_id, vin, appointment_type, scheduled_at, status, notes, created_at) "
                  "values (?,?,?,?,?,?,datetime('now'))",
                  (b.get("lead_id"), b.get("vin", ""), b.get("type", "test_drive"),
                   b.get("scheduled_at", ""), "scheduled", b.get("notes", "")))
        l.commit(); l.close()
        return {"ok": True}

    @app.get("/crm", response_class=HTMLResponse)
    async def crm_page():
        return CRM_HTML

    print("[CRM] registered: /crm center + /api/crm/{summary,leads,appointments} (dealer-gated)")


CRM_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>CRM — LotVue Pro</title>
<style>
 body{margin:0;background:#0a1226;color:#dce6f5;font-family:-apple-system,'Segoe UI',Roboto,sans-serif}
 .top{display:flex;gap:14px;align-items:center;padding:14px 20px;border-bottom:1px solid rgba(150,180,235,.15)}
 .top h1{font-size:17px;margin:0;letter-spacing:.5px} .top a{color:#8fb0e8;font-size:13px;text-decoration:none;margin-left:auto}
 .stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;padding:16px 20px}
 .st{background:rgba(255,255,255,.05);border:1px solid rgba(150,180,235,.14);border-radius:10px;padding:12px 14px}
 .st b{font-size:22px;display:block;color:#fff} .st span{font-size:11px;color:#8fa5cc;text-transform:uppercase;letter-spacing:.8px}
 .board{display:grid;grid-template-columns:repeat(6,1fr);gap:8px;padding:0 20px 24px;min-height:50vh}
 .col{background:rgba(255,255,255,.035);border:1px solid rgba(150,180,235,.1);border-radius:10px;padding:8px}
 .col h3{font-size:11px;text-transform:uppercase;letter-spacing:1px;color:#7d95c1;margin:4px 6px 8px}
 .card{background:rgba(20,34,68,.85);border:1px solid rgba(150,180,235,.16);border-radius:8px;padding:9px 10px;margin-bottom:7px;font-size:12.5px;cursor:grab}
 .card b{display:block;color:#e8eefb} .card i{color:#8fa5cc;font-style:normal;font-size:11.5px}
 .card a{color:#8fb0e8;font-size:11px;text-decoration:none}
 .col.drag{outline:2px dashed rgba(120,165,255,.5)}
 @media(max-width:900px){.board{grid-template-columns:repeat(2,1fr)}}
</style></head><body>
<div class="top"><h1>🏁 LotVue CRM</h1><a href="/">← Dashboard</a></div>
<div class="stats" id="stats"></div>
<div class="board" id="board"></div>
<script>
const STAGES=["new","contacted","appointment","negotiating","sold","lost"];
async function load(){
 const s=await (await fetch('/api/crm/summary')).json();
 document.getElementById('stats').innerHTML=
  `<div class="st"><b>${s.leads_total}</b><span>Total leads</span></div>
   <div class="st"><b>${s.leads_new}</b><span>New / unworked</span></div>
   <div class="st"><b>${s.appointments_upcoming}</b><span>Upcoming appts</span></div>
   <div class="st"><b>${s.followups_pending}</b><span>Follow-ups due</span></div>
   <div class="st"><b>${(s.vehicles&&s.vehicles.n)||0}</b><span>Vehicles</span></div>`;
 const d=await (await fetch('/api/crm/leads')).json();
 const byStage={}; STAGES.forEach(x=>byStage[x]=[]);
 d.leads.forEach(l=>{(byStage[l.status||'new']=byStage[l.status||'new']||[]).push(l)});
 document.getElementById('board').innerHTML=STAGES.map(st=>
  `<div class="col" data-st="${st}" ondragover="event.preventDefault();this.classList.add('drag')"
        ondragleave="this.classList.remove('drag')" ondrop="drop(event,'${st}')">
   <h3>${st} (${(byStage[st]||[]).length})</h3>` +
   (byStage[st]||[]).map(l=>
    `<div class="card" draggable="true" ondragstart="event.dataTransfer.setData('id',${l.id})">
      <b>${(l.name||'Anonymous')}</b>
      <i>${[l.year,l.make,l.model].filter(Boolean).join(' ')||l.vin||''}</i><br>
      <i>${(l.phone||l.email||'').slice(0,26)}</i><br>
      ${l.vin?`<a href="/vdp/${l.vin}" target="_blank">VDP ↗</a> `:''}
      ${l.wants_test_drive?'🚗':''}${l.wants_video?'🎬':''}
     </div>`).join('') + `</div>`).join('');
}
async function drop(ev,st){ev.preventDefault();
 const id=ev.dataTransfer.getData('id');
 await fetch(`/api/crm/leads/${id}/stage`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({stage:st})});
 load();}
load();setInterval(load,30000);
</script></body></html>"""
