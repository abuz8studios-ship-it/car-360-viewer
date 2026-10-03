"""
crm_api.py — LotVue Pro Dealer CRM Center (P4)
One pane: lead inbox w/ stages, vehicle-linked context, appointments,
follow-ups, AI-BDC thread hand-off. Reads the live vehicles.db / lotvue.db.
Auth: NOT in auth_gate public lists -> dealer sign-in required (correct).
"""
import os
import sqlite3, json
from datetime import datetime
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from app_paths import DB_PATH

STAGES = ["new", "contacted", "appointment", "negotiating", "sold", "lost"]

async def _read_limited_body(request: Request, limit_bytes: int) -> bytes:
    chunks = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit_bytes:
            raise HTTPException(status_code=413, detail="Request body exceeds the size limit")
        chunks.append(chunk)
    return b"".join(chunks)

def _db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c

def _rows(c, q, args=()):
    try:
        return [dict(r) for r in c.execute(q, args)]
    except sqlite3.OperationalError:
        return []

def _actor_email(request: Request) -> str:
    token = request.headers.get("authorization", "")
    token = token[7:].strip() if token.lower().startswith("bearer ") else request.cookies.get("lotvue_token", "")
    from a2_extensions import JWT_ALG, _jwt_secret, jwt, JWTError
    try:
        payload = jwt.decode(token, _jwt_secret(), algorithms=[JWT_ALG])
    except JWTError as exc:
        from fastapi import HTTPException
        raise HTTPException(401, "Sign in required") from exc
    email = str(payload.get("sub", "")).strip().lower()
    if payload.get("type") != "access" or not email:
        from fastapi import HTTPException
        raise HTTPException(401, "Sign in required")
    conn = _db()
    exists = conn.execute("SELECT 1 FROM users WHERE LOWER(email)=?", (email,)).fetchone()
    conn.close()
    if not exists:
        from fastapi import HTTPException
        raise HTTPException(401, "User no longer exists")
    return email


def _scope(email: str, alias: str = "l") -> tuple[str, tuple]:
    owner_email = os.environ.get("LOTVUE_OWNER_EMAIL", "").strip().lower()
    if owner_email and email == owner_email:
        return "1=1", ()
    return (
        f"EXISTS (SELECT 1 FROM vehicles v WHERE v.vin={alias}.vin "
        "AND LOWER(v.user_email)=?)",
        (email,),
    )


def _owned_lead(conn, email: str, lead_id: int):
    from fastapi import HTTPException
    clause, args = _scope(email, "l")
    lead = conn.execute(
        f"SELECT l.* FROM leads l WHERE l.id=? AND {clause}", (lead_id, *args)
    ).fetchone()
    if not lead:
        raise HTTPException(404, "Lead not found")
    return lead


def _update_lead_stage(request: Request, lead_id: int, stage: str) -> None:
    email = _actor_email(request)
    conn = _db()
    try:
        _owned_lead(conn, email, lead_id)
        conn.execute("UPDATE leads SET status=? WHERE id=?", (stage, lead_id))
        conn.commit()
    finally:
        conn.close()


def _create_appointment(request: Request, body: dict):
    from ai_studio.lead_engine import book_appointment

    email = _actor_email(request)
    lead_id = int(body["lead_id"])
    conn = _db()
    try:
        _owned_lead(conn, email, lead_id)
    finally:
        conn.close()
    return book_appointment(
        lead_id,
        str(body.get("scheduled_at", ""))[:64],
        str(body.get("type", "test_drive")),
        str(body.get("notes", ""))[:500],
        user_email=email,
    )


def register_crm(app):
    @app.get("/api/crm/summary")
    def crm_summary(request: Request):
        email = _actor_email(request)
        clause, args = _scope(email, "l")
        v = _db()
        try:
            vehicles = _rows(v, "SELECT COUNT(*) n, "
                "SUM(CASE WHEN workflow_status='published' THEN 1 ELSE 0 END) live "
                "FROM vehicles WHERE LOWER(user_email)=?", (email,))
            if os.getenv("LOTVUE_OWNER_EMAIL", "").strip().lower() == email:
                vehicles = _rows(v, "SELECT COUNT(*) n, "
                    "SUM(CASE WHEN workflow_status='published' THEN 1 ELSE 0 END) live FROM vehicles")
            leads_total = (_rows(v, f"SELECT COUNT(*) n FROM leads l WHERE {clause}", args) or [{"n": 0}])[0]["n"]
            leads_new = (_rows(v, f"SELECT COUNT(*) n FROM leads l WHERE {clause} "
                "AND COALESCE(l.status,'new')='new'", args) or [{"n": 0}])[0]["n"]
            by_stage = {r["s"]: r["n"] for r in _rows(v,
                f"SELECT COALESCE(l.status,'new') s, COUNT(*) n FROM leads l WHERE {clause} GROUP BY 1", args)}
            appointments = (_rows(v,
                "SELECT COUNT(*) n FROM appointments a JOIN leads l ON l.id=a.lead_id "
                f"WHERE a.scheduled_at >= datetime('now') AND {clause}", args) or [{"n": 0}])[0]["n"]
            followups = (_rows(v,
                "SELECT COUNT(*) n FROM follow_ups f JOIN leads l ON l.id=f.lead_id "
                f"WHERE f.status='pending' AND {clause}", args) or [{"n": 0}])[0]["n"]
        finally:
            v.close()
        return {
            "vehicles": (vehicles or [{"n": 0, "live": 0}])[0],
            "leads_total": leads_total,
            "leads_new": leads_new,
            "by_stage": by_stage,
            "appointments_upcoming": appointments,
            "followups_pending": followups,
        }

    @app.get("/api/crm/leads")
    def crm_leads(request: Request, stage: str = "", limit: int = 100):
        if stage and stage not in STAGES:
            return JSONResponse({"error": "Invalid stage"}, status_code=400)
        limit = max(1, min(limit, 200))
        email = _actor_email(request)
        clause, scope_args = _scope(email, "l")
        v = _db()
        q = ("SELECT l.*, ve.year, ve.make, ve.model, ve.price FROM leads l "
             "LEFT JOIN vehicles ve ON ve.vin=l.vin WHERE " + clause)
        args = list(scope_args)
        if stage:
            q += " AND COALESCE(l.status,'new')=?"
            args.append(stage)
        q += " ORDER BY l.created_at DESC LIMIT ?"
        args.append(limit)
        rows = _rows(v, q, tuple(args))
        v.close()
        return {"stages": STAGES, "leads": rows}

    @app.post("/api/crm/leads/{lead_id}/stage")
    async def crm_lead_stage(lead_id: int, request: Request):
        if lead_id <= 0:
            return JSONResponse({"error": "Invalid lead id"}, status_code=400)
        raw = await _read_limited_body(request, 2048)
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            return JSONResponse({"error": "Invalid JSON"}, status_code=400)
        stage = body.get("stage", "") if isinstance(body, dict) else ""
        if stage not in STAGES:
            return JSONResponse({"error": f"stage must be one of {STAGES}"}, status_code=400)
        await run_in_threadpool(_update_lead_stage, request, lead_id, stage)
        return {"ok": True, "lead_id": lead_id, "stage": stage}

    @app.get("/api/crm/appointments")
    def crm_appointments(request: Request):
        email = _actor_email(request)
        clause, args = _scope(email, "l")
        conn = _db()
        rows = _rows(conn,
            "SELECT a.* FROM appointments a JOIN leads l ON l.id=a.lead_id "
            f"WHERE {clause} ORDER BY a.scheduled_at ASC LIMIT 200", args)
        conn.close()
        return {"appointments": rows}

    @app.post("/api/crm/appointments")
    async def crm_appointment_create(request: Request):
        raw = await _read_limited_body(request, 4096)
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            return JSONResponse({"error": "Invalid JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "Expected a JSON object"}, status_code=400)
        try:
            lead_id = int(body.get("lead_id", 0))
        except (TypeError, ValueError):
            return JSONResponse({"error": "Invalid lead_id"}, status_code=400)
        if lead_id <= 0:
            return JSONResponse({"error": "Invalid lead_id"}, status_code=400)
        body["lead_id"] = lead_id
        result = await run_in_threadpool(_create_appointment, request, body)
        if "error" in result:
            return JSONResponse({"error": result["error"]}, status_code=400)
        return {"ok": True, "appointment": result}

    @app.get("/crm", response_class=HTMLResponse)
    def crm_page():
        return CRM_HTML

    print("[CRM] registered: tenant-scoped /crm center + lead/appointment APIs")


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
const esc=v=>String(v??'').replace(/[&<>\"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[c]));
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
      <b>${esc(l.name||'Anonymous')}</b>
      <i>${esc([l.year,l.make,l.model].filter(Boolean).join(' ')||l.vin||'')}</i><br>
      <i>${esc((l.phone||l.email||'').slice(0,26))}</i><br>
      ${l.vin?`<a href="/vdp/${esc(encodeURIComponent(l.vin))}" target="_blank" rel="noopener">VDP ↗</a> `:''}
      ${l.wants_test_drive?'🚗':''}${l.wants_video?'🎬':''}
     </div>`).join('') + `</div>`).join('');
}
async function drop(ev,st){ev.preventDefault();
 const id=ev.dataTransfer.getData('id');
 await fetch(`/api/crm/leads/${id}/stage`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({stage:st})});
 load();}
load();setInterval(load,30000);
</script></body></html>"""
