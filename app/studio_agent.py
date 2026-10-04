"""Dealer Studio control plane: readiness snapshot, vehicle plans, and safe job bridges.

The orchestrator is deliberately deterministic. It recommends and runs local
media work only after an authenticated dealer action; it never publishes feeds,
sends messages, changes pricing, or contacts external providers automatically.
"""
from __future__ import annotations

import heapq
import importlib.util
import os
import re
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from ipaddress import ip_address
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse


STUDIO_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Dealer Studio — LotVue360</title>
  <style>
    :root{color-scheme:light;--ink:#142236;--muted:#66758a;--line:#dce4ee;--blue:#2868a8;--soft:#f4f7fb;--green:#16704a;--amber:#9b5b00;--red:#a73535}
    *{box-sizing:border-box}body{margin:0;background:var(--soft);color:var(--ink);font:15px/1.5 Inter,system-ui,-apple-system,Segoe UI,sans-serif}
    header{background:#101b2a;color:#fff;padding:18px max(20px,calc((100vw - 1160px)/2));display:flex;align-items:center;justify-content:space-between;gap:18px;flex-wrap:wrap}
    header strong{font-size:19px;letter-spacing:.2px}nav{display:flex;gap:16px;flex-wrap:wrap}nav a{color:#c8d8ec;text-decoration:none;font-size:14px}nav a:hover{color:#fff}
    main{max-width:1160px;margin:auto;padding:26px 20px 60px}.hero{display:flex;justify-content:space-between;align-items:flex-end;gap:20px;flex-wrap:wrap;margin-bottom:22px}
    h1{font-size:30px;margin:0 0 5px;letter-spacing:-.6px}.sub{color:var(--muted);margin:0}.badge{display:inline-flex;align-items:center;gap:7px;border:1px solid var(--line);background:white;padding:7px 11px;border-radius:999px;font-size:12px;color:var(--muted)}
    .grid{display:grid;grid-template-columns:repeat(12,minmax(0,1fr));gap:16px}.card{background:#fff;border:1px solid var(--line);border-radius:15px;padding:18px;box-shadow:0 4px 15px rgba(28,48,74,.035)}
    .span8{grid-column:span 8}.span4{grid-column:span 4}.span12{grid-column:span 12}h2{font-size:17px;margin:0 0 13px}h3{font-size:15px;margin:0 0 5px}
    label{display:block;color:var(--muted);font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.55px;margin:0 0 6px}select,button{font:inherit}select{width:100%;padding:10px 12px;border:1px solid var(--line);border-radius:9px;background:#fff;color:var(--ink)}
    .row{display:grid;grid-template-columns:1fr 1fr;gap:12px}.actions{display:flex;gap:9px;flex-wrap:wrap;margin-top:14px}.btn{display:inline-flex;align-items:center;justify-content:center;gap:7px;border:1px solid var(--blue);background:var(--blue);color:#fff;text-decoration:none;padding:9px 13px;border-radius:9px;font-weight:700;font-size:13px;cursor:pointer}.btn.secondary{color:var(--blue);background:#fff}.btn:disabled{opacity:.55;cursor:wait}
    .small{font-size:12px;color:var(--muted)}.notice{border-radius:10px;padding:11px 12px;font-size:13px;margin-top:12px;background:#edf4fc;color:#21496f}.notice.warn{background:#fff5e6;color:#80500b}.notice.bad{background:#fff0f0;color:#873030}
    .cap-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}.cap{border:1px solid var(--line);border-radius:11px;padding:12px;background:#fff}.cap-top{display:flex;justify-content:space-between;gap:8px;align-items:center}.state{font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:.45px;border-radius:999px;padding:4px 7px;white-space:nowrap}.state.ready{background:#e8f5ee;color:var(--green)}.state.degraded,.state.setup_required,.state.owner_action_required,.state.not_configured{background:#fff3dd;color:var(--amber)}.state.error{background:#ffeaea;color:var(--red)}.state.configured{background:#edf4fc;color:#345b80}
    .metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:9px}.metric{background:#f6f8fb;border-radius:9px;padding:10px}.metric b{display:block;font-size:17px}.metric span{font-size:11px;color:var(--muted)}
    .vehicle-head{display:flex;justify-content:space-between;align-items:flex-start;gap:14px;flex-wrap:wrap}.vehicle-vin{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;color:var(--muted)}.job{border-top:1px solid var(--line);padding:9px 0;display:flex;justify-content:space-between;gap:12px;font-size:13px}.job:first-child{border-top:0}.job b{font-weight:700}.scene-preview{display:block;max-width:100%;max-height:500px;object-fit:contain;background:#f6f8fb;border:1px solid var(--line);border-radius:10px;margin-top:10px}.script-scenes{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:10px;margin-top:12px}.script-scene{border:1px solid var(--line);border-radius:10px;padding:12px;background:#fff}.script-scene p{font-size:13px;margin:7px 0}.script-scene .small{display:block;margin-top:7px}.script-voiceover{display:block;width:100%;margin-top:10px;padding:10px;border:1px solid var(--line);border-radius:9px;font:inherit;line-height:1.5;resize:vertical}.hidden{display:none!important}
    @media(max-width:800px){.span8,.span4{grid-column:span 12}.cap-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.metrics{grid-template-columns:repeat(2,1fr)}}@media(max-width:500px){main{padding:20px 13px 42px}.row{grid-template-columns:1fr}.cap-grid{grid-template-columns:1fr}h1{font-size:25px}}
  </style>
</head>
<body>
<header><strong>LotVue360 <span style="color:#82b9ef">Dealer Studio</span></strong>
  <nav><a href="/">Dashboard</a><a href="/capture">Capture</a><a href="/crm">CRM</a><a href="/3d-studio">3D Studio</a><a href="/settings">Settings</a><a href="/health">System</a></nav>
</header>
<main>
  <div class="hero"><div><h1>One studio. One vehicle workflow.</h1><p class="sub">A control room for capture, 360 rendering, Photo Studio, buyer review, CRM, and readiness.</p></div><span class="badge" id="probeMode">Local configuration snapshot · no external services contacted</span></div>
  <div class="grid">
    <section class="card span8" aria-labelledby="vehicle-title">
      <div class="vehicle-head"><div><h2 id="vehicle-title">Vehicle workflow</h2><p class="small">Choose inventory you own. This assistant only queues local media work after you confirm.</p></div><a class="btn secondary" href="/capture">＋ Capture vehicle</a></div>
      <div class="row" style="margin-top:14px"><div><label for="vehicleSelect">Vehicle</label><select id="vehicleSelect"><option value="">Loading your inventory…</option></select></div><div><label for="backgroundSelect">Shared backdrop</label><select id="backgroundSelect"><option value="studio_white">Studio White</option></select></div></div>
      <div id="vehicleSummary" class="notice">Select a vehicle to see the next safe action.</div>
      <div class="actions"><button class="btn" id="buildBtn" disabled>Build / refresh 360</button><button class="btn secondary" id="photoBtn" disabled>Prepare Photo Studio images</button><a class="btn secondary hidden" id="viewBtn" target="_blank" rel="noopener">Review buyer page</a></div>
      <div class="small" style="margin-top:10px">Background removal is optional. When it is not installed, the 360 renderer keeps photographed backgrounds and marks the result as degraded rather than claiming a cut-out.</div>
      <div class="scene-tools" style="margin-top:16px;padding-top:14px;border-top:1px solid var(--line)">
        <h3>Render one stored photo</h3>
        <div class="row" style="margin-top:10px"><div><label for="sceneImage">Source photo</label><select id="sceneImage" disabled><option value="">Choose a vehicle first…</option></select></div><div><label for="sceneSelect">Scene</label><select id="sceneSelect"><option value="clean-studio">Clean Studio</option></select></div></div>
        <div class="row" style="margin-top:10px"><div><label for="sceneEngine">Renderer</label><select id="sceneEngine"><option value="local">Local deterministic composite</option><option value="comfyui">ComfyUI AI workflow</option></select></div><div class="actions" style="align-items:end;margin-top:20px"><button class="btn secondary" id="sceneBtn" disabled>Render selected scene</button></div></div>
        <p class="small" id="sceneHelp">Local rendering requires a transparent vehicle cut-out. ComfyUI requires an owner-configured workflow and may change vehicle details.</p>
        <div class="small notice warn">Generated files are served from the existing public vehicle-media path. AI output is not held in a private review queue; review the result before sharing it.</div>
        <div id="sceneOutput" class="notice hidden" aria-live="polite"></div>
      </div>
    </section>
    <section class="card span4"><h2>Recommended next steps</h2><div id="actionList" class="small">Choose a vehicle to generate a deterministic workflow plan.</div><div class="actions"><a class="btn secondary" href="/api/vehicles" target="_blank" rel="noopener">Inventory API</a><a class="btn secondary" href="/api/backgrounds" target="_blank" rel="noopener">Background catalog</a></div></section>
    <section class="card span12" aria-labelledby="video-script-title"><h2 id="video-script-title">Draft a vehicle video script</h2><p class="small">Uses the existing local text-template route only. This creates a script draft, not video or audio; verify vehicle facts, dealer name, and price before use. Nothing is published or sent.</p><div class="row"><div><label for="videoAdType">Draft style</label><select id="videoAdType"><option value="listing">Full listing · 30 seconds</option><option value="social">Short social · 15 seconds</option></select></div><div class="actions" style="align-items:end;margin-top:20px"><button class="btn secondary" id="videoScriptBtn" disabled>Draft voiceover script</button></div></div><div id="videoScriptStatus" class="notice hidden" role="status" aria-live="polite"></div><div id="videoScriptOutput" class="hidden"></div></section>
    <section class="card span12" id="system-health"><div class="vehicle-head"><div><h2>System & engine readiness</h2><p class="small">Configuration-only snapshot. “Configured” is not a live integration check, production uptime claim, or visual-quality sign-off.</p></div><button class="btn secondary" id="refreshHealth">Refresh</button></div><div id="capabilities" class="cap-grid" style="margin-top:13px"><div class="small">Loading local readiness…</div></div><div id="resourceCaps" class="metrics" style="margin-top:14px"></div><p class="small" style="margin:12px 0 0">Pricing and billing remain closed pending owner approval. No feed, email, payment, social post, or external AI request is initiated by this page.</p></section>
    <section class="card span12"><h2>Connected workspaces</h2><div class="actions" style="margin-top:0"><a class="btn secondary" href="/crm">CRM & lead inbox</a><a class="btn secondary" href="/3d-studio">Optional 3D workflow</a><a class="btn secondary" id="interiorLink" href="/capture">Interior panorama studio</a><a class="btn secondary" href="/settings">Dealer profile & integrations</a><a class="btn secondary" href="/health">System details</a></div><p class="small" style="margin:10px 0 0">External feed delivery, AI video, Gaussian reconstruction, outbound email, and Stripe each retain separate setup or owner gates; readiness is shown above.</p></section>
  </div>
</main>
<script>
const $=id=>document.getElementById(id);let activeVin='';let polling=null;let sceneInputRequest=0;
function node(tag,cls,text){const e=document.createElement(tag);if(cls)e.className=cls;if(text!==undefined)e.textContent=text;return e;}
async function getJson(url,options={}){const response=await fetch(url,{credentials:'same-origin',...options});let data={};try{data=await response.json();}catch(_){}if(!response.ok)throw new Error(data.detail||('Request failed ('+response.status+')'));return data;}
function renderCapabilityCard(item){const card=node('article','cap');const top=node('div','cap-top');top.append(node('h3','',item.label));const state=node('span','state '+item.status,item.status.replaceAll('_',' '));top.append(state);card.append(top);card.append(node('p','small',item.detail));return card;}
async function loadHealth(){const box=$('capabilities');box.replaceChildren(node('div','small','Checking local configuration…'));try{const data=await getJson('/api/system/health');box.replaceChildren();Object.values(data.subsystems).forEach(item=>box.append(renderCapabilityCard(item)));const m=data.limits||{};$('resourceCaps').replaceChildren();[[m.spin_frames,'360 frames / spin'],[m.viewer_frames,'viewer frame cap'],[m.photo_batch_mb+' MiB','photo batch cap'],[m.background_workers+' / '+m.background_jobs,'workers / admitted jobs'],[m.comfyui_scene_generation_jobs,'ComfyUI scenes / process']].forEach(([v,l])=>{const d=node('div','metric');d.append(node('b','',String(v)));d.append(node('span','',l));$('resourceCaps').append(d);});$('probeMode').textContent=data.external_services_contacted?'Probe performed':'Local configuration snapshot · no external services contacted';}catch(e){box.replaceChildren(node('div','notice bad',e.message));}}
async function loadBackgrounds(){try{const data=await getJson('/api/backgrounds');const select=$('backgroundSelect');select.replaceChildren();data.filter(x=>x.slug!=='none').forEach(bg=>{const opt=node('option','',bg.name);opt.value=bg.slug;select.append(opt);});}catch(_){}}
async function loadSceneCatalog(){const select=$('sceneSelect');try{const data=await getJson('/api/backgrounds/scenes');select.replaceChildren();(data.scenes||[]).forEach(scene=>{const opt=node('option','',scene.name);opt.value=scene.id;select.append(opt);});if(!select.options.length){const empty=node('option','','No scenes available');empty.value='';empty.disabled=true;select.replaceChildren(empty);}syncSceneButton();}catch(e){const empty=node('option','',e.message);empty.value='';empty.disabled=true;select.replaceChildren(empty);syncSceneButton();}}
function syncSceneButton(){const btn=$('sceneBtn');if(btn)btn.disabled=!activeVin||!$('sceneImage').value||!$('sceneSelect').value||btn.dataset.busy==='true';}
function syncVideoScriptButton(){const btn=$('videoScriptBtn');if(btn)btn.disabled=!activeVin||btn.dataset.busy==='true';}
function updateSceneHelp(){const help=$('sceneHelp');if(!help)return;if($('sceneEngine').value==='comfyui'){help.textContent='ComfyUI AI mode requires the owner-configured API workflow; results may alter the vehicle and are written to public vehicle storage for manual review.';}else{help.textContent='Local mode is deterministic and requires a transparent vehicle cut-out. Opaque originals are rejected; use a transparent PNG or the existing Photo Studio pipeline.';}}
async function loadSceneInputs(vin){const requestId=++sceneInputRequest;const select=$('sceneImage');select.disabled=true;select.replaceChildren(node('option','','Loading vehicle photos…'));try{const data=await getJson('/api/studio/vehicles/'+encodeURIComponent(vin)+'/scene-inputs');if(requestId!==sceneInputRequest||vin!==activeVin)return;select.replaceChildren(node('option','','Choose a source photo…'));(data.images||[]).forEach(image=>{const opt=node('option','',image.label);opt.value=image.path;select.append(opt);});if(!(data.images||[]).length)select.replaceChildren(node('option','','No stored photos found'));select.disabled=!(data.images||[]).length;}catch(e){if(requestId!==sceneInputRequest||vin!==activeVin)return;select.replaceChildren(node('option','',e.message));select.disabled=true;}syncSceneButton();}
async function loadVehicles(){
const select=$('vehicleSelect');try{const rows=await getJson('/api/vehicles');select.replaceChildren(node('option','','Select a vehicle…'));rows.forEach(v=>{const label=[v.year,v.make,v.model].filter(Boolean).join(' ')||'Vehicle';const opt=node('option','',label+' · '+v.vin);opt.value=v.vin;select.append(opt);});if(!rows.length)select.replaceChildren(node('option','','No vehicles yet — capture one to begin'));if(location.hash.startsWith('#vin=')){const wanted=decodeURIComponent(location.hash.slice(5));select.value=wanted;}if(select.value)await selectVehicle();}catch(e){select.replaceChildren(node('option','',e.message));}}
function appendAction(container,label,description){const row=node('div','job');row.append(node('span','',label));row.append(node('b','',description));container.append(row);}
async function loadVehicleJobs(vin){const data=await getJson('/api/studio/vehicles/'+encodeURIComponent(vin)+'/jobs');return data.jobs||[];}
async function selectVehicle(){const previousVin=activeVin;activeVin=$('vehicleSelect').value;if(previousVin!==activeVin){$('videoScriptOutput').replaceChildren();$('videoScriptOutput').classList.add('hidden');$('videoScriptStatus').className='notice hidden';$('videoScriptStatus').textContent='';}syncVideoScriptButton();const build=$('buildBtn'),photo=$('photoBtn'),view=$('viewBtn');build.disabled=!activeVin;photo.disabled=!activeVin;view.classList.toggle('hidden',!activeVin);if(!activeVin){sceneInputRequest++;$('sceneImage').disabled=true;$('sceneImage').replaceChildren(node('option','','Choose a vehicle first…'));syncSceneButton();$('vehicleSummary').textContent='Select a vehicle to see the next safe action.';$('actionList').textContent='Choose a vehicle to generate a workflow plan.';return;}view.href='/view/'+encodeURIComponent(activeVin);$('interiorLink').href='/interior-studio/'+encodeURIComponent(activeVin);loadSceneInputs(activeVin);try{const plan=await getJson('/api/studio/vehicles/'+encodeURIComponent(activeVin)+'/plan');$('vehicleSummary').className='notice'+(plan.warnings.length?' warn':'');$('vehicleSummary').textContent=plan.stage+' · '+plan.assets.exterior_frames+' source frames · '+plan.assets.spin_frames+' rendered 360 frames · '+plan.assets.studio_photos+' Photo Studio images';const list=$('actionList');list.replaceChildren();plan.actions.forEach(a=>appendAction(list,a.label,a.state));plan.warnings.forEach(w=>list.append(node('div','notice warn',w)));const jobs=await loadVehicleJobs(activeVin);jobs.forEach(j=>appendAction(list,j.kind.replaceAll('_',' '),j.status+' · '+(j.step||'')+' · '+(j.message||'')));}catch(e){$('vehicleSummary').className='notice bad';$('vehicleSummary').textContent=e.message;}}
$('vehicleSelect').addEventListener('change',selectVehicle);
$('refreshHealth').addEventListener('click',loadHealth);
$('sceneImage').addEventListener('change',syncSceneButton);
$('sceneSelect').addEventListener('change',syncSceneButton);
$('sceneEngine').addEventListener('change',updateSceneHelp);
$('videoScriptBtn').addEventListener('click',async()=>{if(!activeVin)return;const vin=activeVin,btn=$('videoScriptBtn'),status=$('videoScriptStatus'),output=$('videoScriptOutput');btn.dataset.busy='true';syncVideoScriptButton();status.className='notice';status.textContent='Drafting a text-only script from stored vehicle details…';output.replaceChildren();output.classList.add('hidden');try{const result=await getJson('/api/vehicle/'+encodeURIComponent(vin)+'/video-ad',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({type:$('videoAdType').value})});if(vin!==activeVin)return;const script=result.script||{};status.className='notice warn';status.textContent='Draft ready. Verify every vehicle fact, the dealer name, and price before using it; this does not render or publish media.';const meta=node('div','small',(script.vehicle_title||'Vehicle')+' · '+(script.ad_type||'listing')+' · '+(script.total_duration||0)+' seconds · text template');const grid=node('div','script-scenes');(Array.isArray(script.scenes)?script.scenes:[]).forEach(scene=>{const card=node('article','script-scene');card.append(node('h3','',(scene.shot||'Scene').replaceAll('_',' ')));card.append(node('p','',scene.voiceover||''));if(scene.text_overlay)card.append(node('span','small','On-screen text: '+scene.text_overlay));if(scene.duration)card.append(node('span','small','Shot duration: '+scene.duration+' s'));grid.append(card);});const voiceLabel=node('h3','','Full voiceover');voiceLabel.style.marginTop='14px';const voice=node('textarea','script-voiceover');voice.readOnly=true;voice.rows=4;voice.value=script.full_voiceover||'';voice.setAttribute('aria-label','Full voiceover text');const copy=node('button','btn secondary','Copy full voiceover');copy.type='button';copy.addEventListener('click',async()=>{try{await navigator.clipboard.writeText(voice.value);status.className='notice';status.textContent='Copied the full voiceover text. Review it before use.';}catch(_){status.className='notice warn';status.textContent='Clipboard unavailable; select and copy the voiceover text below.';}});output.replaceChildren(meta,grid,voiceLabel,voice,copy);output.classList.remove('hidden');}catch(e){if(vin!==activeVin)return;status.className='notice bad';status.textContent=e.message;}finally{delete btn.dataset.busy;syncVideoScriptButton();}});
$('sceneBtn').addEventListener('click',async()=>{if(!activeVin||!$('sceneImage').value)return;const btn=$('sceneBtn'),box=$('sceneOutput');btn.dataset.busy='true';syncSceneButton();box.className='notice';box.textContent='Rendering the selected photo…';try{const result=await getJson('/api/photo/composite',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({image_path:$('sceneImage').value,scene:$('sceneSelect').value,engine:$('sceneEngine').value})});box.className='notice'+(result.ai_generated?' warn':'');box.replaceChildren(node('div','',result.ai_generated?'AI-generated result ready. Review vehicle identity and details before sharing.':'Local composite ready.'));const preview=node('img','scene-preview');preview.src=result.output;preview.alt=result.ai_generated?'AI-generated vehicle scene for manual review':'Locally composited vehicle scene';preview.loading='lazy';preview.decoding='async';const link=node('a','btn secondary','Open rendered image');link.href=result.output;link.target='_blank';link.rel='noopener';box.append(preview,link);}catch(e){box.className='notice bad';box.textContent=e.message;}finally{delete btn.dataset.busy;syncSceneButton();}});
$('buildBtn').addEventListener('click',async()=>{if(!activeVin)return;const btn=$('buildBtn');btn.disabled=true;try{const fd=new FormData();fd.append('background_slug',$('backgroundSelect').value);fd.append('turntable_style','studio_shadow');await getJson('/api/vehicle/'+encodeURIComponent(activeVin)+'/background',{method:'POST',body:fd});const result=await getJson('/api/studio/vehicles/'+encodeURIComponent(activeVin)+'/run',{method:'POST'});$('vehicleSummary').className='notice';$('vehicleSummary').textContent=result.message||'360 build started.';if(polling)clearInterval(polling);polling=setInterval(async()=>{try{const s=await getJson('/api/finalize-status/'+encodeURIComponent(activeVin));$('vehicleSummary').textContent=s.status+' · '+s.spin_frames+' frames · '+(s.message||'');if(s.status==='completed'||s.status==='degraded'||s.status==='error'){clearInterval(polling);polling=null;btn.disabled=false;await selectVehicle();}}catch(e){clearInterval(polling);polling=null;btn.disabled=false;$('vehicleSummary').className='notice bad';$('vehicleSummary').textContent=e.message;}},1800);}catch(e){btn.disabled=false;$('vehicleSummary').className='notice bad';$('vehicleSummary').textContent=e.message;}});
$('photoBtn').addEventListener('click',async()=>{if(!activeVin)return;const btn=$('photoBtn');btn.disabled=true;try{const result=await getJson('/api/vehicle/'+encodeURIComponent(activeVin)+'/process-photos',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({background:$('backgroundSelect').value,upscale:false})});$('vehicleSummary').className='notice';$('vehicleSummary').textContent=result.message||'Photo Studio job started. Check the job summary below.';setTimeout(selectVehicle,1300);}catch(e){$('vehicleSummary').className='notice bad';$('vehicleSummary').textContent=e.message;}finally{btn.disabled=false;}});
updateSceneHelp();loadHealth();loadBackgrounds();loadSceneCatalog();loadVehicles();
</script>
</body></html>"""


_SCENE_INPUT_DIRECTORIES = ("exterior", "raw", "source", "studio")
_SCENE_INPUT_LIMIT = 72
_SCENE_INPUT_SCAN_LIMIT = 4096
_SCENE_INPUT_VALIDATION_BYTES = 200 * 1024 * 1024
_SCENE_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".heif"}


def _list_vehicle_scene_inputs(main_module, vin: str) -> list[dict]:
    """Enumerate a bounded list of verified image references for an owned vehicle."""
    from PIL import Image

    directories: dict[str, Path] = {}

    def candidates():
        scanned = 0
        for directory_name in _SCENE_INPUT_DIRECTORIES:
            try:
                directory = main_module._ensure_safe_vehicle_subdir(vin, directory_name)
            except HTTPException as exc:
                if exc.status_code == 404:
                    continue
                raise
            directories[directory_name] = directory
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        if scanned >= _SCENE_INPUT_SCAN_LIMIT:
                            return
                        scanned += 1
                        try:
                            if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                                continue
                        except OSError:
                            continue
                        filename = entry.name
                        if filename.startswith("."):
                            continue
                        candidate = Path(filename)
                        if (
                            candidate.suffix.lower() not in _SCENE_IMAGE_SUFFIXES
                            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", candidate.stem)
                        ):
                            continue
                        yield directory_name, filename
            except OSError:
                continue

    selected = heapq.nsmallest(_SCENE_INPUT_SCAN_LIMIT, candidates())
    images = []
    validated_bytes = 0
    max_file_bytes = int(main_module.MAX_PHOTO_MB) * 1024 * 1024
    for directory_name, filename in selected:
        directory = directories.get(directory_name)
        if directory is None:
            continue
        candidate = directory / filename
        if candidate.is_symlink():
            continue
        try:
            resolved = candidate.resolve(strict=True)
            file_size = resolved.stat().st_size
        except (OSError, RuntimeError, ValueError):
            continue
        if (
            resolved.parent != directory
            or not resolved.is_file()
            or file_size <= 0
            or file_size > max_file_bytes
            or validated_bytes + file_size > _SCENE_INPUT_VALIDATION_BYTES
        ):
            continue
        validated_bytes += file_size
        try:
            with Image.open(resolved) as image:
                width, height = image.size
                if (
                    width <= 0
                    or height <= 0
                    or width * height > int(main_module.MAX_UPLOAD_IMAGE_PIXELS)
                ):
                    continue
                image.verify()
        except Exception:
            continue
        image_ref = f"/storage/{vin}/{directory_name}/{filename}"
        images.append({
            "path": image_ref,
            "directory": directory_name,
            "filename": filename,
            "label": f"{directory_name.title()} · {filename}",
        })
        if len(images) >= _SCENE_INPUT_LIMIT:
            break
    return images


def _binary_available(value: str) -> bool:
    if not value:
        return False
    candidate = Path(value).expanduser()
    return candidate.is_file() or shutil.which(value) is not None


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        return False


def _public_https_url_configured(value: str) -> bool:
    """Validate a public feed base URL without performing DNS or HTTP probes."""
    try:
        parts = urlsplit(value.strip())
        port = parts.port
        host = (parts.hostname or "").lower().rstrip(".")
        if (parts.scheme != "https" or not host or parts.username is not None
                or parts.password is not None or parts.query or parts.fragment
                or port not in (None, 443)):
            return False
        if host in {"localhost", "localhost.localdomain"} or host.endswith((".local", ".internal")):
            return False
        try:
            if not ip_address(host).is_global:
                return False
        except ValueError:
            pass
        return True
    except ValueError:
        return False


def _google_feed_configuration_ready() -> bool:
    """Mirror the feed route's required static settings without exposing them."""
    token = os.getenv("LOTVUE_GOOGLE_FEED_TOKEN", "").strip()
    email = os.getenv("LOTVUE_GOOGLE_FEED_USER_EMAIL", "").strip()
    address = all(os.getenv(key, "").strip() for key in (
        "LOTVUE_DEALER_ADDR1", "LOTVUE_DEALER_CITY", "LOTVUE_DEALER_REGION",
        "LOTVUE_DEALER_POSTAL",
    ))
    return bool(
        32 <= len(token) <= 512
        and re.fullmatch(r"[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s]{2,63}", email)
        and os.getenv("LOTVUE_GOOGLE_STORE_CODE", "").strip()
        and os.getenv("LOTVUE_DEALER_NAME", "").strip()
        and address
        and _public_https_url_configured(os.getenv("LOTVUE_PUBLIC_URL", ""))
    )


def build_system_health(main_module) -> dict:
    """Return safe configuration/readiness data; this performs no network probes."""
    render_requested = os.environ.get("LOTVUE_RENDER_MODE", "studio").lower()
    ffmpeg_ready = _binary_available(str(main_module.FFMPEG))
    ffprobe_ready = _binary_available(str(main_module.FFPROBE))
    rembg_ready = bool(main_module.REMBG_AVAILABLE)
    try:
        from rembg_runtime import configured_model as _configured_rembg_model
        from rembg_runtime import package_available as _rembg_package_available
        rembg_package_installed = _rembg_package_available()
        rembg_model_selected = bool(_configured_rembg_model())
        rembg_license_acknowledged = os.getenv("LOTVUE_REMBG_LICENSE_ACCEPTED", "").strip() == "1"
    except Exception:
        rembg_package_installed = rembg_model_selected = rembg_license_acknowledged = False
    comfy_configured = bool(os.getenv("COMFYUI_URL", "").strip())
    try:
        from ai_studio.background_studio import comfyui_scene_workflow_configured
        comfy_scene_workflow_ready = comfyui_scene_workflow_configured()
    except Exception:
        comfy_scene_workflow_ready = False
    stripe_key_ready = bool(getattr(main_module, "STRIPE_KEY", ""))
    stripe_webhook_ready = bool(getattr(main_module, "STRIPE_WEBHOOK_SECRET", ""))
    feed_ready = _google_feed_configuration_ready()
    smtp_ready = bool(
        os.getenv("LOTVUE_SMTP_HOST", "").strip()
        and os.getenv("LOTVUE_NOTIFY_EMAIL", "").strip()
    )
    cuda_ready = bool(main_module.GPU_INFO.get("cuda"))
    torch_ready = _module_available("torch")
    colmap_ready = _binary_available(os.getenv("COLMAP_PATH", "colmap"))
    background_count = len(main_module.list_background_assets())
    render_config_valid = render_requested in {"natural", "studio"}
    spin_ready = bool(main_module.cv2 is not None and background_count > 0 and render_config_valid)
    try:
        with sqlite3.connect(str(main_module.DB_PATH), timeout=1) as connection:
            connection.execute("SELECT 1 FROM vehicles LIMIT 0").fetchall()
            connection.execute("SELECT 1 FROM users LIMIT 0").fetchall()
        database_ready = True
    except sqlite3.Error:
        database_ready = False
    storage_dir = Path(main_module.STORAGE_DIR)
    storage_ready = storage_dir.is_dir() and os.access(storage_dir, os.W_OK)

    subsystems = {
        "local_storage": {
            "label": "Local storage & database",
            "status": "ready" if storage_ready and database_ready else "degraded",
            "detail": "Storage is writable and the local SQLite connection responds." if storage_ready and database_ready else "Check local storage permissions or SQLite availability; no path is disclosed.",
        },
        "media_capture": {
            "label": "Capture & quality gate",
            "status": "ready" if ffmpeg_ready and ffprobe_ready else "degraded",
            "detail": "Local upload limits and capture validation are enabled; video extraction requires FFmpeg and FFprobe.",
        },
        "spin_renderer": {
            "label": "36-frame 360 renderer",
            "status": "ready" if spin_ready else "degraded",
            "detail": f"OpenCV interpolation and {background_count} shared packaged backdrops; studio cut-out availability is reported separately." if render_config_valid else "The configured render mode is invalid; use natural or studio.",
        },
        "background_removal": {
            "label": "Background removal",
            "status": "configured" if rembg_ready else "setup_required",
            "detail": (
                "An operator-selected model is enabled; its license and inference quality are not verified by this app."
                if rembg_ready else
                "The rembg runtime is installed, but no model is selected. Verify model commercial rights, set LOTVUE_REMBG_MODEL, and explicitly acknowledge review before use."
                if rembg_package_installed and not rembg_model_selected else
                "A model name is selected but the operator has not acknowledged review of its commercial terms; inference remains disabled."
                if rembg_package_installed and rembg_model_selected and not rembg_license_acknowledged else
                "rembg is not installed; 360 rendering retains photographed backgrounds and strict Photo Studio cut-out is unavailable."
            ),
        },
        "photo_studio": {
            "label": "Photo Studio",
            "status": "configured" if rembg_ready else "degraded",
            "detail": (
                "Shared processor: selected-model cut-out, scene composite, enhancement, heuristic plate-blur attempt, optional upscale. Model license, inference quality, and plate blur are not verified."
                if rembg_ready else
                "Shared processor keeps photographed backgrounds until an operator verifies and explicitly enables a model; scene composition and enhancement remain available. Plate blur is heuristic and not guaranteed."
            ),
        },
        "comfyui_upscale": {
            "label": "ComfyUI 4x upscale",
            "status": "not_configured" if not comfy_configured else "setup_required",
            "detail": "Configuration presence only; endpoint/model reachability is not probed. A requested upscale falls back to labeled Lanczos 2x when unavailable.",
        },
        "comfyui_scene_generation": {
            "label": "ComfyUI scene generation",
            "status": "configured" if comfy_scene_workflow_ready else "setup_required",
            "detail": (
                "The owner-supplied API workflow file passes structural checks; endpoint, custom nodes, model weights, and generated-image quality are not verified."
                if comfy_scene_workflow_ready else
                "Optional: provide a ComfyUI API-format scene workflow with the documented LotVue tokens; no workflow or model is bundled."
            ),
        },
        "ai_video": {
            "label": "AI video commercials",
            "status": "configured" if cuda_ready and comfy_configured else "setup_required",
            "detail": "CUDA and an explicit ComfyUI URL are configured, but WAN model files and live generation remain unverified." if cuda_ready and comfy_configured else "Optional WAN/ComfyUI generation needs a configured service, model assets, and compatible runtime; none are probed here.",
        },
        "gaussian_3d": {
            "label": "Optional Gaussian 3D",
            "status": "configured" if cuda_ready and torch_ready and colmap_ready else "setup_required",
            "detail": "Requires CUDA/PyTorch plus a COLMAP executable and separately provisioned reconstruction assets; none are live-probed.",
        },
        "buyer_viewer": {
            "label": "Buyer viewer & VDP",
            "status": "ready",
            "detail": "Public viewer routes are available for active inventory; each spin is capped at 36 generated frames and public enumeration at 120.",
        },
        "crm_leads": {
            "label": "CRM & lead records",
            "status": "ready",
            "detail": "Local tenant-owned CRM/lead routes are registered. Outbound email is a separate optional integration.",
        },
        "lead_email": {
            "label": "Lead email delivery",
            "status": "configured" if smtp_ready else "not_configured",
            "detail": "SMTP host/address settings are present; no connection or delivery was tested." if smtp_ready else "SMTP is not configured; lead records can still be stored locally.",
        },
        "google_feed": {
            "label": "Google vehicle feed",
            "status": "configured" if feed_ready else "not_configured",
            "detail": "Local feed settings pass static checks; live Google validation and ingestion have not been tested." if feed_ready else "Feed settings are incomplete or invalid; no external feed is published from this environment.",
        },
        "social_listing": {
            "label": "Social / listing connectors",
            "status": "configured" if os.getenv("POSTIZ_URL", "").strip() and os.getenv("POSTIZ_API_KEY", "").strip() else "not_configured",
            "detail": "Connector configuration presence only; no post or remote probe was performed. Assisted listing exports remain separate.",
        },
        "dealer_import": {
            "label": "Inventory import",
            "status": "ready",
            "detail": "Tenant-scoped file import routes are registered. Live DMS credentials, source access, and imported-data accuracy are unverified.",
        },
        "billing": {
            "label": "Stripe & published pricing",
            "status": "owner_action_required",
            "detail": "Pricing remains owner-unapproved and payments are fail-closed. Key presence does not establish live billing readiness.",
        },
    }

    return {
        "status": "degraded" if any(item["status"] not in {"ready"} for item in subsystems.values()) else "ready",
        "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "probe_mode": "local_configuration_only",
        "external_services_contacted": False,
        "requested_render_mode": render_requested,
        "effective_render_mode": (
            "invalid_config" if not render_config_valid else
            "natural_fallback" if render_requested == "studio" and not rembg_ready else
            render_requested
        ),
        "subsystems": subsystems,
        "limits": {
            "spin_frames": int(main_module.NUM_EXTERIOR_FRAMES),
            "viewer_frames": 120,
            "photo_mb": int(main_module.MAX_PHOTO_MB),
            "photo_batch_mb": int(main_module.MAX_PHOTO_BATCH_MB),
            "video_mb": int(main_module.MAX_VIDEO_MB),
            "image_pixels": int(main_module.MAX_UPLOAD_IMAGE_PIXELS),
            "background_workers": int(main_module._BACKGROUND_WORKERS),
            "background_jobs": int(main_module._BACKGROUND_MAX_JOBS),
            "comfyui_scene_generation_jobs": int(getattr(main_module, "_COMFYUI_SCENE_MAX_JOBS", 1)),
            "vehicle_media_entries": 300,
        },
        "runtime": {
            "heic_available": bool(main_module.HEIC_SUPPORTED),
            "rembg_available": rembg_ready,
            "comfyui_scene_workflow_configured": comfy_scene_workflow_ready,
            "cuda_available": cuda_ready,
            "torch_available": torch_ready,
            "ffmpeg_available": ffmpeg_ready,
            "ffprobe_available": ffprobe_ready,
            "packaged_backgrounds": background_count,
            "local_database_available": database_ready,
            "storage_writable": storage_ready,
            "render_mode_valid": render_config_valid,
            "pricing_approved": bool(main_module._pricing_is_approved()),
            "stripe_credentials_present": stripe_key_ready and stripe_webhook_ready,
        },
        "unverified": [
            "Production uptime and deployment behavior",
            "Live billing, webhook delivery, and real payment receipts",
            "Live SMTP, Google, DMS, social, ComfyUI, WAN, and NHTSA availability",
            "Selected rembg model license, weights provenance, and inference quality",
            "Real-vehicle visual quality and plate-blur recall",
        ],
    }


def _vehicle_plan(main_module, vin: str) -> dict:
    vehicle = main_module.db_get_vehicle(vin)
    if not vehicle or vehicle.get("status", "active") != "active":
        raise HTTPException(status_code=404, detail="Vehicle not found")
    try:
        exterior = main_module._ensure_safe_vehicle_subdir(vin, "exterior")
        exterior_count = main_module._bounded_frame_count(exterior, "frame_", 72)
    except HTTPException:
        exterior_count = 0
    try:
        spin = main_module._ensure_safe_vehicle_subdir(vin, "spin")
        spin_count = main_module._bounded_frame_count(spin, "spin_", 120)
    except HTTPException:
        spin_count = 0
    try:
        studio = main_module._ensure_safe_vehicle_subdir(vin, "studio")
        studio_count = main_module._bounded_frame_count(studio, "studio_", 72)
    except HTTPException:
        studio_count = 0
    jobs = main_module._read_pipeline_jobs(vin)
    job_by_kind = {item["kind"]: item for item in jobs}
    spin_job = job_by_kind.get("spin", {})
    warnings = []
    if not main_module.REMBG_AVAILABLE:
        warnings.append("No explicitly approved background-removal model is enabled; spin output will retain photographed backgrounds.")
    if spin_job.get("status") == "error":
        warnings.append("The latest 360 build failed. Existing viewer files, if any, were preserved.")

    actions = []
    if exterior_count < 4:
        stage = "capture_required"
        actions.append({"id": "capture", "label": "Capture at least four exterior photos", "state": "available"})
    elif spin_job.get("status") in {"queued", "running"}:
        stage = "processing"
        actions.append({"id": "wait", "label": "360 media build", "state": spin_job.get("status", "running")})
    elif spin_count == 0 or spin_job.get("status") == "error":
        stage = "spin_required" if spin_count == 0 else "retry_available"
        actions.append({"id": "build_spin", "label": "Build or retry the 360 viewer", "state": "available"})
    else:
        stage = "review"
        actions.append({"id": "review", "label": "Review the buyer-facing vehicle page", "state": "available"})
    if exterior_count >= 1:
        actions.append({
            "id": "photo_studio", "label": "Prepare Photo Studio derivatives",
            "state": "available_with_cutout" if main_module.REMBG_AVAILABLE else "available_degraded_without_cutout",
        })
    if not _google_feed_configuration_ready():
        actions.append({"id": "feed_setup", "label": "Complete external inventory feed setup", "state": "requires_owner_configuration"})
    return {
        "vin": vin,
        "stage": stage,
        "workflow_status": str(vehicle.get("workflow_status", "intake") or "intake"),
        "assets": {
            "exterior_frames": exterior_count,
            "spin_frames": spin_count,
            "studio_photos": studio_count,
        },
        "jobs": jobs,
        "actions": actions,
        "warnings": warnings,
        "review_url": f"/view/{vin}" if spin_count else None,
    }


def register_studio_agent(app: FastAPI) -> None:
    """Register the Dealer Studio API, safe system page, and deterministic plan routes."""

    @app.get("/studio", response_class=HTMLResponse)
    def dealer_studio_page(request: Request):
        from main import _authenticated_user_email
        _authenticated_user_email(request)
        return HTMLResponse(STUDIO_HTML)

    @app.get("/health")
    def system_health_alias(request: Request):
        from main import _authenticated_user_email
        _authenticated_user_email(request)
        return RedirectResponse(url="/studio#system-health", status_code=307)

    @app.get("/api/system/health")
    def system_health_api(request: Request):
        from main import _authenticated_user_email
        import main as main_module
        _authenticated_user_email(request)
        return build_system_health(main_module)

    @app.get("/api/studio/vehicles/{vin}/plan")
    def vehicle_workflow_plan(vin: str, request: Request):
        import main as main_module
        normalized = main_module.validate_vehicle_id(vin)
        main_module._require_vehicle_owner(request, normalized)
        return _vehicle_plan(main_module, normalized)

    @app.get("/api/studio/vehicles/{vin}/jobs")
    def vehicle_pipeline_jobs(vin: str, request: Request):
        import main as main_module
        normalized = main_module.validate_vehicle_id(vin)
        main_module._require_vehicle_owner(request, normalized)
        vehicle = main_module.db_get_vehicle(normalized)
        if not vehicle or vehicle.get("status", "active") != "active":
            raise HTTPException(status_code=404, detail="Vehicle not found")
        return {"vin": normalized, "jobs": main_module._read_pipeline_jobs(normalized)}

    @app.get("/api/studio/vehicles/{vin}/scene-inputs")
    def vehicle_scene_inputs(vin: str, request: Request):
        import main as main_module
        normalized = main_module.validate_vehicle_id(vin)
        main_module._require_vehicle_owner(request, normalized)
        vehicle = main_module.db_get_vehicle(normalized)
        if not vehicle or vehicle.get("status", "active") != "active":
            raise HTTPException(status_code=404, detail="Vehicle not found")
        images = _list_vehicle_scene_inputs(main_module, normalized)
        return {
            "vin": normalized,
            "images": images,
            "count": len(images),
            "limit": _SCENE_INPUT_LIMIT,
            "scan_limit": _SCENE_INPUT_SCAN_LIMIT,
            "validation_bytes_limit": _SCENE_INPUT_VALIDATION_BYTES,
        }

    @app.post("/api/studio/vehicles/{vin}/run")
    def run_vehicle_studio(vin: str, request: Request):
        import main as main_module
        normalized = main_module.validate_vehicle_id(vin)
        main_module._require_vehicle_owner(request, normalized)
        vehicle = main_module.db_get_vehicle(normalized)
        if not vehicle or vehicle.get("status", "active") != "active":
            raise HTTPException(status_code=404, detail="Vehicle not found")
        exterior = main_module._ensure_safe_vehicle_subdir(normalized, "exterior")
        count = main_module._bounded_frame_count(exterior, "frame_", 72)
        if count < 4:
            raise HTTPException(status_code=400, detail="At least four exterior photos are required")
        manifest = None
        manifest_path = main_module._safe_vehicle_directory(normalized) / "manifest.json"
        if manifest_path.is_file() and not manifest_path.is_symlink():
            try:
                if manifest_path.stat().st_size <= 1_000_000:
                    import json
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeError):
                manifest = None
        result = main_module._queue_vehicle_spin_job(normalized, manifest)
        if result == "busy":
            raise HTTPException(status_code=429, detail="Background processing capacity is full",
                                headers={"Retry-After": "30"})
        return {"status": result, "vin": normalized,
                "message": "360 media build queued. Poll /api/finalize-status/{vin}.",
                "status_url": f"/api/finalize-status/{normalized}"}
