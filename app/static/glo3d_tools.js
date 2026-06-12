/* LotVue360 — buyer-facing conversion tools injected into the 360 viewer.
   AI chat bubble + lead-magnet dock (trade-in, price advisor, history,
   test drive, request video). Reads VIN/title from #lv-glo3d-root data attrs. */
(function () {
  var root = document.getElementById('lv-glo3d-root');
  if (!root) return;
  var VIN = root.getAttribute('data-vin');
  var TITLE = root.getAttribute('data-title') || 'this vehicle';
  var P = '#2E75B6', INK = '#2C3E50', MUT = '#7A8CA0';

  var css = document.createElement('style');
  css.textContent = `
  .lv-dock{position:fixed;left:16px;bottom:16px;display:flex;flex-wrap:wrap;gap:8px;z-index:9000;max-width:46vw}
  .lv-pill{background:#fff;color:${INK};border:1px solid #DDE5EE;border-radius:22px;padding:9px 14px;font:600 13px/1 -apple-system,Segoe UI,sans-serif;cursor:pointer;box-shadow:0 3px 10px rgba(44,62,80,.15);display:flex;align-items:center;gap:6px}
  .lv-pill:hover{border-color:${P};color:${P}}
  .lv-pill .i{font-size:15px}
  .lv-adv{background:${P};color:#fff;border:none}
  .lv-fab{position:fixed;right:18px;bottom:18px;width:60px;height:60px;border-radius:50%;background:${P};color:#fff;border:none;cursor:pointer;box-shadow:0 6px 18px rgba(46,117,182,.45);font-size:26px;z-index:9001}
  .lv-fab:hover{filter:brightness(1.07)}
  .lv-chat{position:fixed;right:18px;bottom:90px;width:360px;max-width:92vw;height:480px;max-height:72vh;background:#fff;border-radius:16px;box-shadow:0 12px 40px rgba(44,62,80,.3);display:none;flex-direction:column;overflow:hidden;z-index:9001}
  .lv-chat.open{display:flex}
  .lv-chd{background:${P};color:#fff;padding:13px 16px;font:700 15px -apple-system,Segoe UI,sans-serif;display:flex;justify-content:space-between;align-items:center}
  .lv-chd small{display:block;font-weight:500;opacity:.85;font-size:11px}
  .lv-chd button{background:transparent;border:none;color:#fff;font-size:20px;cursor:pointer}
  .lv-msgs{flex:1;overflow-y:auto;padding:14px;background:#F8FAFC;display:flex;flex-direction:column;gap:9px}
  .lv-b{max-width:82%;padding:9px 12px;border-radius:13px;font:14px/1.4 -apple-system,Segoe UI,sans-serif}
  .lv-bot{background:#fff;border:1px solid #E3EAF2;color:${INK};align-self:flex-start;border-bottom-left-radius:3px}
  .lv-me{background:${P};color:#fff;align-self:flex-end;border-bottom-right-radius:3px}
  .lv-typing{color:${MUT};font-style:italic;font-size:13px}
  .lv-inp{display:flex;border-top:1px solid #E3EAF2;background:#fff}
  .lv-inp input{flex:1;border:none;padding:13px;font-size:14px;outline:none}
  .lv-inp button{border:none;background:${P};color:#fff;padding:0 18px;cursor:pointer;font-weight:700}
  .lv-modal{position:fixed;inset:0;background:rgba(20,30,45,.55);display:none;align-items:center;justify-content:center;z-index:9002;padding:14px}
  .lv-modal.open{display:flex}
  .lv-card{background:#fff;border-radius:16px;width:420px;max-width:96vw;max-height:90vh;overflow:auto;padding:22px;color:${INK}}
  .lv-card h3{font:800 19px -apple-system,Segoe UI,sans-serif;margin-bottom:4px}
  .lv-card p.sub{color:${MUT};font-size:13px;margin-bottom:14px}
  .lv-card label{display:block;font:600 12px -apple-system,Segoe UI,sans-serif;color:${MUT};margin:9px 0 3px}
  .lv-card input,.lv-card select{width:100%;padding:10px;border:1.5px solid #DDE5EE;border-radius:9px;font-size:14px}
  .lv-row{display:flex;gap:9px}.lv-row>div{flex:1}
  .lv-go{width:100%;margin-top:15px;padding:12px;border:none;border-radius:10px;background:${P};color:#fff;font:700 15px sans-serif;cursor:pointer}
  .lv-x{float:right;border:none;background:none;font-size:22px;color:${MUT};cursor:pointer;margin-top:-6px}
  .lv-result{background:#F0F6FC;border:1px solid #CFE0F0;border-radius:10px;padding:14px;margin-top:12px;text-align:center}
  .lv-result .big{font:800 26px sans-serif;color:${P}}
  .lv-badge{position:fixed;left:16px;bottom:64px;background:#fff;border:1px solid #DDE5EE;border-radius:10px;padding:7px 12px;font:600 13px sans-serif;box-shadow:0 3px 10px rgba(44,62,80,.15);z-index:9000;display:none}
  @media(max-width:560px){.lv-dock{max-width:62vw}.lv-badge{bottom:58px}}
  `;
  document.head.appendChild(css);

  function el(tag, cls, html) { var e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; }
  function api(path, body) {
    return fetch(path, { method: body ? 'POST' : 'GET', headers: { 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined }).then(function (r) { return r.json(); });
  }

  // ── price-advisor badge ────────────────────────────────────────────────────
  var badge = el('div', 'lv-badge'); document.body.appendChild(badge);
  api('/api/leadmagnet/' + VIN + '/price-advisor').then(function (d) {
    if (d && d.rating) { badge.innerHTML = '<span style="color:' + (d.rating_color || P) + '">●</span> ' + d.rating; badge.style.display = 'block'; }
  }).catch(function () {});

  // ── tools dock ──────────────────────────────────────────────────────────────
  var dock = el('div', 'lv-dock');
  var tools = [
    ['💰', 'Value My Trade', openTrade],
    ['📊', 'Price Check', showPrice],
    ['📋', 'Vehicle History', function () { contactModal('Get the Vehicle History', 'We\'ll email you the full CARFAX/AutoCheck report.', '/api/leadmagnet/request-history'); }],
    ['🚗', 'Book Test Drive', openTestDrive],
    ['🎬', 'Request Video', function () { contactModal('Request a Walk-Around Video', 'A specialist records a personal video of ' + TITLE + '.', '/api/leadmagnet/request-video'); }]
  ];
  tools.forEach(function (t, i) {
    var b = el('button', 'lv-pill' + (i === 0 ? ' lv-adv' : ''), '<span class="i">' + t[0] + '</span>' + t[1]);
    b.onclick = t[2]; dock.appendChild(b);
  });
  document.body.appendChild(dock);

  // ── modal plumbing ────────────────────────────────────────────────────────
  var modal = el('div', 'lv-modal'); var card = el('div', 'lv-card'); modal.appendChild(card);
  document.body.appendChild(modal);
  modal.addEventListener('click', function (e) { if (e.target === modal) modal.classList.remove('open'); });
  function show(htmlStr) { card.innerHTML = htmlStr; modal.classList.add('open'); }
  function close() { modal.classList.remove('open'); }
  window._lvClose = close;

  function contactFields() {
    return '<label>Full Name</label><input id="lvn" placeholder="John Smith">' +
           '<label>Email</label><input id="lve" type="email" placeholder="you@email.com">' +
           '<label>Phone</label><input id="lvp" placeholder="(555) 000-0000">';
  }
  function getContact() { return { name: v('lvn'), email: v('lve'), phone: v('lvp') }; }
  function v(id) { var e = document.getElementById(id); return e ? e.value.trim() : ''; }

  function contactModal(title, sub, endpoint) {
    show('<button class="lv-x" onclick="_lvClose()">×</button><h3>' + title + '</h3><p class="sub">' + sub + '</p>' +
      contactFields() + '<button class="lv-go" id="lvgo">Send</button>');
    document.getElementById('lvgo').onclick = function () {
      var c = getContact(); if (!c.email && !c.phone) { alert('Add an email or phone so we can reach you.'); return; }
      api(endpoint, { vin: VIN, name: c.name, email: c.email, phone: c.phone }).then(function (d) {
        show('<h3>✓ Done</h3><p class="sub">' + (d.message || 'We\'ll be in touch shortly.') + '</p><button class="lv-go" onclick="_lvClose()">Close</button>');
      });
    };
  }

  function showPrice() {
    show('<button class="lv-x" onclick="_lvClose()">×</button><h3>Price Check</h3><p class="sub">How ' + TITLE + ' is priced vs. the market.</p><div id="lvpr">Checking…</div>');
    api('/api/leadmagnet/' + VIN + '/price-advisor').then(function (d) {
      var m = d.market_estimate || {};
      document.getElementById('lvpr').innerHTML =
        '<div class="lv-result"><div class="big" style="color:' + (d.rating_color || P) + '">' + (d.rating || '—') + '</div>' +
        (d.asking_price ? '<div>Asking: <b>$' + d.asking_price.toLocaleString() + '</b></div>' : '') +
        (m.average ? '<div style="color:' + MUT + '">Market estimate: $' + (m.low || 0).toLocaleString() + ' – $' + (m.high || 0).toLocaleString() + '</div>' : '') +
        '<div style="font-size:11px;color:' + MUT + ';margin-top:8px">' + (d.disclaimer || '') + '</div></div>';
    });
  }

  function openTrade() {
    show('<button class="lv-x" onclick="_lvClose()">×</button><h3>Value My Trade</h3><p class="sub">Instant estimate for your current vehicle.</p>' +
      '<div class="lv-row"><div><label>Year</label><input id="ty" placeholder="2018"></div><div><label>Make</label><input id="tm" placeholder="Honda"></div></div>' +
      '<div class="lv-row"><div><label>Model</label><input id="tmo" placeholder="Accord"></div><div><label>Mileage</label><input id="tmi" placeholder="60000"></div></div>' +
      '<label>Condition</label><select id="tc"><option value="excellent">Excellent</option><option value="good" selected>Good</option><option value="fair">Fair</option><option value="poor">Poor</option></select>' +
      '<hr style="border:none;border-top:1px solid #eee;margin:14px 0">' + contactFields() +
      '<button class="lv-go" id="tgo">Get My Estimate</button><div id="tres"></div>');
    document.getElementById('tgo').onclick = function () {
      var c = getContact(); if (!c.email && !c.phone) { alert('Add an email or phone for your estimate.'); return; }
      api('/api/leadmagnet/tradein', { name: c.name, email: c.email, phone: c.phone, vehicle_of_interest: VIN, year: v('ty'), make: v('tm'), model: v('tmo'), mileage: v('tmi'), condition: v('tc') }).then(function (d) {
        var e = d.estimate || {};
        document.getElementById('tres').innerHTML = '<div class="lv-result"><div style="color:' + MUT + '">Estimated trade value</div><div class="big">$' + (e.low || 0).toLocaleString() + ' – $' + (e.high || 0).toLocaleString() + '</div><div style="font-size:11px;color:' + MUT + '">' + (d.disclaimer || '') + '</div></div>';
      });
    };
  }

  function openTestDrive() {
    show('<button class="lv-x" onclick="_lvClose()">×</button><h3>Book a Test Drive</h3><p class="sub">Pick a time for ' + TITLE + '.</p><label>Time</label><select id="tds"><option>Loading…</option></select>' + contactFields() + '<button class="lv-go" id="tdgo">Request Test Drive</button>');
    api('/api/leadmagnet/test-drive-slots').then(function (d) {
      var sel = document.getElementById('tds'); sel.innerHTML = '';
      (d.slots || []).forEach(function (s) { var o = document.createElement('option'); var lbl = (s.label || s.datetime || s); o.value = (s.datetime || s); o.textContent = lbl; sel.appendChild(o); });
      if (!sel.options.length) { var o = document.createElement('option'); o.textContent = 'Any time — we\'ll confirm'; o.value = 'flexible'; sel.appendChild(o); }
    });
    document.getElementById('tdgo').onclick = function () {
      var c = getContact(); if (!c.email && !c.phone) { alert('Add an email or phone so we can confirm.'); return; }
      api('/api/leadmagnet/book-test-drive', { vin: VIN, name: c.name, email: c.email, phone: c.phone, slot: v('tds') }).then(function (d) {
        show('<h3>✓ Requested</h3><p class="sub">' + (d.message || '') + '</p><button class="lv-go" onclick="_lvClose()">Close</button>');
      });
    };
  }

  // ── AI chat ─────────────────────────────────────────────────────────────────
  var fab = el('button', 'lv-fab', '💬'); document.body.appendChild(fab);
  var chat = el('div', 'lv-chat');
  chat.innerHTML = '<div class="lv-chd"><div>Ask about this vehicle<small>Instant answers · 24/7</small></div><button id="lvchx">×</button></div><div class="lv-msgs" id="lvmsgs"></div><div class="lv-inp"><input id="lvci" placeholder="Type a question…"><button id="lvcs">Send</button></div>';
  document.body.appendChild(chat);
  var history = [];
  var msgs = function () { return document.getElementById('lvmsgs'); };
  function addMsg(text, who) { var b = el('div', 'lv-b ' + (who === 'me' ? 'lv-me' : 'lv-bot'), text); msgs().appendChild(b); msgs().scrollTop = msgs().scrollHeight; return b; }
  fab.onclick = function () {
    chat.classList.toggle('open');
    if (chat.classList.contains('open') && !history.length) addMsg('Hi! Ask me anything about the ' + TITLE + ' — price, mileage, features, history, or book a test drive.', 'bot');
  };
  document.getElementById('lvchx').onclick = function () { chat.classList.remove('open'); };
  function send() {
    var inp = document.getElementById('lvci'); var t = inp.value.trim(); if (!t) return;
    inp.value = ''; addMsg(t, 'me'); history.push({ role: 'user', content: t });
    var typing = addMsg('typing…', 'bot'); typing.className = 'lv-b lv-bot lv-typing';
    api('/api/chat/' + VIN, { message: t, history: history.slice(-8) }).then(function (d) {
      typing.remove(); var r = (d && d.reply) || 'Let me have a specialist follow up with you.';
      addMsg(r, 'bot'); history.push({ role: 'assistant', content: r });
    }).catch(function () { typing.remove(); addMsg('Connection hiccup — please try again.', 'bot'); });
  }
  document.getElementById('lvcs').onclick = send;
  document.getElementById('lvci').addEventListener('keydown', function (e) { if (e.key === 'Enter') send(); });
})();
