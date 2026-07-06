/* LotVue360 dealer dashboard — Quick Start + Tools helper.
   Non-invasive: a floating "Tools" button that opens onboarding + per-vehicle
   access to the GLO3D-upgrade features (window sticker, buyers guide, showroom,
   viewer link). Reads /api/vehicles with the dealer's session cookie. */
(function () {
  var P = '#2E75B6', INK = '#2C3E50', MUT = '#7A8CA0';
  var css = document.createElement('style');
  css.textContent = `
  .lvd-fab{position:fixed;right:20px;bottom:20px;background:${P};color:#fff;border:none;border-radius:26px;padding:13px 20px;font:700 14px -apple-system,Segoe UI,sans-serif;cursor:pointer;box-shadow:0 6px 18px rgba(46,117,182,.4);z-index:9000}
  .lvd-panel{position:fixed;right:20px;bottom:74px;width:380px;max-width:94vw;max-height:78vh;overflow:auto;background:#fff;border-radius:16px;box-shadow:0 16px 48px rgba(44,62,80,.3);z-index:9001;display:none;color:${INK}}
  .lvd-panel.open{display:block}
  .lvd-h{background:${P};color:#fff;padding:15px 18px;font:800 16px sans-serif;border-radius:16px 16px 0 0;display:flex;justify-content:space-between;align-items:center}
  .lvd-h button{background:none;border:none;color:#fff;font-size:22px;cursor:pointer}
  .lvd-body{padding:16px 18px}
  .lvd-sec{font:700 12px sans-serif;color:${MUT};text-transform:uppercase;letter-spacing:.5px;margin:14px 0 8px}
  .lvd-step{display:flex;gap:9px;align-items:flex-start;margin-bottom:9px;font-size:13.5px;line-height:1.45}
  .lvd-num{flex:none;width:22px;height:22px;border-radius:50%;background:#EAF2FB;color:${P};font:700 12px sans-serif;display:flex;align-items:center;justify-content:center}
  .lvd-sel{width:100%;padding:10px;border:1.5px solid #DDE5EE;border-radius:9px;font-size:14px;margin-bottom:10px}
  .lvd-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}
  .lvd-btn{display:block;text-align:center;padding:11px 8px;border:1px solid #DDE5EE;border-radius:9px;background:#fff;color:${INK};text-decoration:none;font:600 13px sans-serif;cursor:pointer}
  .lvd-btn:hover{border-color:${P};color:${P}}
  .lvd-btn.pri{background:${P};color:#fff;border-color:${P}}
  .lvd-note{font-size:12px;color:${MUT};margin-top:10px}
  .lvd-link{color:${P};font-weight:600;word-break:break-all}
  .lvd-inv{max-height:250px;overflow:auto;margin-top:8px;border:1px solid #EDF1F6;border-radius:10px;padding:6px;display:none;background:#FBFCFE}
  .lvd-inv.on{display:block}
  .lvd-row{display:flex;align-items:center;gap:8px;padding:7px 8px;border:1px solid #DDE5EE;border-radius:8px;margin-bottom:6px;background:#fff}
  .lvd-row-main{flex:1;min-width:0}
  .lvd-row-t{font:600 12.5px sans-serif;color:${INK};white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .lvd-row-m{font-size:11.5px;color:${MUT}}
  .lvd-imp{flex:none;background:${P};color:#fff;border:none;border-radius:7px;padding:7px 10px;font:700 11.5px sans-serif;cursor:pointer}
  .lvd-imp:disabled{opacity:.55;cursor:default}
  .lvd-row-s{font-size:11.5px;margin-top:3px;color:${INK};word-break:break-word}
  `;
  document.head.appendChild(css);

  var fab = document.createElement('button');
  fab.className = 'lvd-fab'; fab.innerHTML = '🛠 Tools & Quick Start';
  document.body.appendChild(fab);

  var panel = document.createElement('div'); panel.className = 'lvd-panel';
  panel.innerHTML =
    '<div class="lvd-h"><span>Dealer Tools</span><button id="lvdx">×</button></div>' +
    '<div class="lvd-body">' +
    '<div class="lvd-sec">Quick start — first 360 in minutes</div>' +
    '<div class="lvd-step"><span class="lvd-num">1</span><span>On your phone, open <b>lotvue.abuz8ai.com/mobile</b> and sign in.</span></div>' +
    '<div class="lvd-step"><span class="lvd-num">2</span><span>Scan the VIN, then walk around the car taking the guided shots.</span></div>' +
    '<div class="lvd-step"><span class="lvd-num">3</span><span>Tap <b>Build my 360</b> — it appears here automatically.</span></div>' +
    '<div class="lvd-step"><span class="lvd-num">4</span><span>Use the tools below to make a sticker, share, or syndicate it.</span></div>' +
    '<div class="lvd-sec">Per-vehicle tools</div>' +
    '<select class="lvd-sel" id="lvdveh"><option>Loading inventory…</option></select>' +
    '<div class="lvd-grid">' +
    '<a class="lvd-btn pri" id="lvd-view" target="_blank">Open 360 Viewer</a>' +
    '<a class="lvd-btn" id="lvd-manage" target="_blank">Manage / Delete Photos</a>' +
    '<a class="lvd-btn" id="lvd-sticker" target="_blank">Window Sticker</a>' +
    '<a class="lvd-btn" id="lvd-guide" target="_blank">FTC Buyers Guide</a>' +
    '<a class="lvd-btn" id="lvd-commercial" target="_blank">AI Commercial</a>' +
    '</div>' +
    '<div class="lvd-sec">Import from dealer site</div>' +
    '<select class="lvd-sel" id="lvd-dlr" style="margin-bottom:8px">' +
    '<option value="drivesmartwc">Drive Smart Auto Sales (drivesmartwc.com)</option>' +
    '<option value="unlimitedag">Unlimited Auto Group (unlimitedag.com)</option>' +
    '</select>' +
    '<button class="lvd-btn pri" id="lvd-loadinv" style="width:100%">Load inventory</button>' +
    '<div class="lvd-note" id="lvd-invmsg" style="margin-top:6px"></div>' +
    '<div class="lvd-inv" id="lvd-invlist"></div>' +
    '<div class="lvd-sec">Your storefront & syndication</div>' +
    '<div class="lvd-grid">' +
    '<a class="lvd-btn" id="lvd-showroom" target="_blank">My Showroom</a>' +
    '<a class="lvd-btn" href="/api/feeds/google-vehicle-listings.xml" target="_blank">Google Feed</a>' +
    '<a class="lvd-btn" href="/api/marketplace/facebook/catalog.csv" target="_blank">Facebook Catalog</a>' +
    '<a class="lvd-btn" id="lvd-intel" target="_blank">Vehicle Intel</a>' +
    '</div>' +
    '<div class="lvd-note" id="lvd-sr-link"></div>' +
    '</div>';
  document.body.appendChild(panel);

  fab.onclick = function () { panel.classList.toggle('open'); };
  document.getElementById('lvdx').onclick = function () { panel.classList.remove('open'); };

  var dealer = 'default';
  try {
    fetch('/api/tenant/me', { credentials: 'same-origin' }).then(function (r) { return r.json(); })
      .then(function (d) { if (d && (d.name || d.dealer)) { dealer = d.name || d.dealer; wireShowroom(); } }).catch(function () {});
  } catch (e) {}

  function wireShowroom() {
    var url = location.origin + '/showroom/' + encodeURIComponent(dealer);
    document.getElementById('lvd-showroom').href = url;
    document.getElementById('lvd-sr-link').innerHTML = 'Public storefront: <a class="lvd-link" href="' + url + '" target="_blank">' + url + '</a>';
  }
  wireShowroom();

  function wire(vin) {
    document.getElementById('lvd-view').href = '/view/' + vin;
    document.getElementById('lvd-manage').href = '/manage/' + vin;
    document.getElementById('lvd-sticker').href = '/api/vehicle/' + vin + '/window-sticker.pdf';
    document.getElementById('lvd-guide').href = '/api/vehicle/' + vin + '/buyers-guide.pdf';
    document.getElementById('lvd-commercial').href = '/ai-commercial?vin=' + vin;
    document.getElementById('lvd-intel').href = '/api/intel/' + vin + '/aggregate';
  }

  /* ── Import from dealer site: inventory pick-list + one-click Import + 3D ── */
  function invMsg(t) { document.getElementById('lvd-invmsg').textContent = t; }

  function importVin(dealerKey, vin, btn, statusEl) {
    btn.disabled = true; btn.textContent = 'Importing…';
    statusEl.textContent = 'Downloading photos, classifying exterior/interior, queuing 3D…';
    fetch('/api/import/' + encodeURIComponent(dealerKey) + '/' + encodeURIComponent(vin),
      { method: 'POST', credentials: 'same-origin' })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        var j = res.j || {};
        if (!res.ok || j.error) {
          statusEl.textContent = '⚠ ' + (j.error || j.detail || 'Import failed');
          btn.disabled = false; btn.textContent = 'Import + 3D'; return;
        }
        var c = j.classification || {};
        var parts = [(j.photos_saved || 0) + ' photos'];
        if (c.exterior != null) parts.push(c.exterior + ' ext · ' + (c.interior || 0) + ' int · ' + (c.detail || 0) + ' detail');
        parts.push(j.build_queued ? '3D build queued ✓' : ('no build — ' + (j.build_skip_reason || 'too few exterior shots')));
        statusEl.textContent = '';
        statusEl.appendChild(document.createTextNode(parts.join(' · ') + ' — '));
        var a = document.createElement('a');
        a.className = 'lvd-link'; a.target = '_blank';
        a.href = '/view/' + encodeURIComponent(j.vin || vin);
        a.textContent = 'Open /view/' + (j.vin || vin);
        statusEl.appendChild(a);
        btn.textContent = 'Imported ✓';
      })
      .catch(function (e) {
        statusEl.textContent = '⚠ ' + e;
        btn.disabled = false; btn.textContent = 'Import + 3D';
      });
  }

  document.getElementById('lvd-loadinv').onclick = function () {
    var dealerKey = document.getElementById('lvd-dlr').value;
    var list = document.getElementById('lvd-invlist');
    var loadBtn = document.getElementById('lvd-loadinv');
    loadBtn.disabled = true; invMsg('Loading live inventory from ' + dealerKey + '…');
    list.classList.remove('on'); list.innerHTML = '';
    fetch('/api/import/' + encodeURIComponent(dealerKey) + '/inventory', { credentials: 'same-origin' })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        loadBtn.disabled = false;
        var j = res.j || {};
        if (!res.ok || j.error) { invMsg('⚠ ' + (j.error || j.detail || 'Inventory load failed')); return; }
        var inv = j.inventory || [];
        invMsg(inv.length + ' vehicles live on ' + dealerKey + ' — pick one to import.');
        if (!inv.length) return;
        inv.forEach(function (v) {
          if (!v.vin) return;
          var row = document.createElement('div'); row.className = 'lvd-row';
          var main = document.createElement('div'); main.className = 'lvd-row-main';
          var t = document.createElement('div'); t.className = 'lvd-row-t';
          t.textContent = v.title || v.vin;
          var m = document.createElement('div'); m.className = 'lvd-row-m';
          m.textContent = (v.price ? '$' + v.price : 'price n/a') +
            ' · ' + (v.photo_count != null ? v.photo_count + ' photos' : 'photos n/a') +
            ' · ' + v.vin.slice(-8);
          var s = document.createElement('div'); s.className = 'lvd-row-s';
          main.appendChild(t); main.appendChild(m); main.appendChild(s);
          var btn = document.createElement('button');
          btn.className = 'lvd-imp'; btn.textContent = 'Import + 3D';
          btn.onclick = function () { importVin(dealerKey, v.vin, btn, s); };
          row.appendChild(main); row.appendChild(btn);
          list.appendChild(row);
        });
        list.classList.add('on');
      })
      .catch(function (e) { loadBtn.disabled = false; invMsg('⚠ ' + e); });
  };

  fetch('/api/vehicles', { credentials: 'same-origin' }).then(function (r) { return r.json(); })
    .then(function (d) {
      var list = Array.isArray(d) ? d : (d.vehicles || []);
      var sel = document.getElementById('lvdveh'); sel.innerHTML = '';
      if (!list.length) { var o = document.createElement('option'); o.textContent = 'No vehicles yet — capture one on /mobile'; sel.appendChild(o); return; }
      list.forEach(function (v) {
        var vin = v.vin || v.VIN; if (!vin) return;
        var o = document.createElement('option'); o.value = vin;
        o.textContent = ((v.year || '') + ' ' + (v.make || '') + ' ' + (v.model || '')).trim() + ' · ' + vin.slice(-6);
        sel.appendChild(o);
      });
      sel.onchange = function () { wire(sel.value); };
      if (sel.value) wire(sel.value);
    }).catch(function () {});
})();
