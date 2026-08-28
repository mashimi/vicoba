let pendingIntent = null;
let allMembersCache = [];
let groupSettings = {};
let currentUser = null;

// ── App Startup ──
document.addEventListener('DOMContentLoaded', () => {
  fetchCurrentUser();
  checkHealth();
  fetchSettings();
  loadDashboard();
});

// ── Current User & Role Visibility ──
async function fetchCurrentUser() {
  try {
    const res = await fetch('/api/me');
    if (res.status === 401) { location.href = '/login'; return; }
    const data = await res.json();
    if (!data.ok) { location.href = '/login'; return; }
    currentUser = data.user;
    const chip = document.getElementById('userChip');
    if (chip) {
      chip.textContent = currentUser.name + ' · ' + currentUser.role_label;
      chip.style.display = '';
    }
    applyRoleUI();
  } catch(e) { location.href = '/login'; }
}

function applyRoleUI() {
  if (!currentUser) return;
  const isAdmin = currentUser.role === 'mwenyekiti';
  const canInput = currentUser.role !== 'katibu';

  document.getElementById('settingsBtn').style.display = isAdmin ? '' : 'none';
  document.getElementById('adminUsersBtn').style.display = isAdmin ? '' : 'none';

  if (!canInput) {
    const inputTab = document.getElementById('tabBtn-input');
    const inputSec = document.getElementById('tab-input');
    const regBtn = document.getElementById('registerMemberBtn');
    if (inputTab) inputTab.style.display = 'none';
    if (inputSec) inputSec.style.display = 'none';
    if (regBtn) regBtn.style.display = 'none';
    showTab('dash');
  }
}

// ── Chairperson: User Management ──
async function openUsersModal() {
  openModal('👥 Watumiaji & Madaraka', '<div id="usrBody" class="loading"></div> Kupakia...');
  await loadUsersPanel();
}

async function loadUsersPanel() {
  const box = document.getElementById('usrBody');
  if (!box) return;
  let html = '';
  try {
    const res = await fetch('/api/admin/users');
    const data = await res.json();
    if (data.ok && data.users) {
      html += '<table style="width:100%;font-size:13px;border-collapse:collapse">';
      html += '<tr><th style="text-align:left;padding:6px">Jina</th><th style="text-align:left;padding:6px">Jukumu</th><th style="text-align:left;padding:6px">Simu</th></tr>';
      const roleNames = { mwenyekiti: 'Mwenyekiti', mhazinaji: 'Mhazinaji', katibu: 'Katibu' };
      for (const u of data.users) {
        html += '<tr><td style="padding:6px">' + esc(u.name) + '</td>';
        html += '<td style="padding:6px">' + (roleNames[u.role] || u.role) + '</td>';
        html += '<td style="padding:6px" class="monospace">' + esc(u.phone || '—') + '</td></tr>';
      }
      html += '</table>';
    }
  } catch(e) { html += '<i>Imeshindikana kupakia watumiaji.</i>'; }
  html += '<hr style="margin:16px 0;border:none;border-top:1px solid rgba(255,255,255,0.1)">';
  html += '<form onsubmit="submitUser(event)">';
  html += '<div class="form-group"><label>Jina Kamili *</label><input id="usr_name" required autofocus></div>';
  html += '<div class="form-group"><label>Jukumu</label><select id="usr_role">';
  html += '<option value="katibu">Katibu (Msajili)</option>';
  html += '<option value="mhazinaji">Mhazinaji (Treasurer)</option>';
  html += '<option value="mwenyekiti">Mwenyekiti (Chair)</option></select></div>';
  html += '<div class="form-group"><label>Namba ya Simu (WhatsApp, hiari)</label><input id="usr_phone" placeholder="mfano: 0712345678"></div>';
  html += '<button type="submit" class="btn-confirm" style="width:100%;padding:12px;margin-top:8px">➕ Ongeza Mtumiaji</button>';
  html += '</form><div id="usrResult" style="margin-top:12px"></div>';
  box.innerHTML = html;
}

async function submitUser(e) {
  e.preventDefault();
  const fd = new FormData();
  fd.append('name', document.getElementById('usr_name').value.trim());
  fd.append('role', document.getElementById('usr_role').value);
  fd.append('phone', document.getElementById('usr_phone').value.trim());
  try {
    const res = await fetch('/api/admin/users', { method: 'POST', body: fd });
    const data = await res.json();
    if (data.ok) {
      await loadUsersPanel();
      const note = document.getElementById('usrResult');
      if (note) {
        note.innerHTML = '<div style="padding:12px;background:rgba(16,185,129,0.2);border-radius:10px;border:1px solid rgba(16,185,129,0.4)">'
          + '<b>' + esc(data.message) + '</b><br>'
          + 'PIN ya muda: <b style="font-size:20px;letter-spacing:2px;font-family:monospace">' + esc(data.temp_pin) + '</b><br>'
          + '<span style="color:#fca5a5">' + esc(data.warning) + '</span></div>';
      }
    } else {
      const box = document.getElementById('usrResult');
      if (box) box.innerHTML = '<div style="padding:12px;background:rgba(244,63,94,0.2);border-radius:10px;border:1px solid rgba(244,63,94,0.4)">' + esc(data.error || 'Hitilafu') + '</div>';
    }
  } catch(err) { alert('Hitilafu: ' + err.message); }
}

// ── Fetch Settings ──
async function fetchSettings() {
  try {
    const res = await fetch('/api/settings');
    const data = await res.json();
    if (data.ok) groupSettings = data.settings || {};
  } catch(e) { console.error(e); }
}

// ── Health Check ──
async function checkHealth() {
  try {
    const res = await fetch('/api/health');
    const data = await res.json();
    const pill = document.getElementById('healthPill');
    if (data.ok && data.invariant_ok) {
      pill.textContent = 'Mingatio: Sawa ✓';
      pill.style.background = 'rgba(16, 185, 129, 0.2)';
      pill.style.borderColor = 'rgba(16, 185, 129, 0.4)';
    } else {
      pill.textContent = 'Mingatio: Hitilafu!';
      pill.style.background = 'rgba(244, 63, 94, 0.2)';
      pill.style.borderColor = 'rgba(244, 63, 94, 0.4)';
      pill.style.color = '#fca5a5';
    }
  } catch(e) {
    document.getElementById('healthPill').textContent = 'Kizingiti';
  }
}

// ── Tab Navigation ──
function showTab(name) {
  document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
  document.querySelectorAll('.tabs-wrapper button').forEach(b => b.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  document.getElementById('tabBtn-' + name).classList.add('active');
  if (name === 'dash') loadDashboard();
  if (name === 'members') loadMembers();
}

function fillTrigger(text) {
  showTab('input');
  const input = document.getElementById('cmdInput');
  input.value = text;
  input.focus();
}

// ── Parse Phase ──
async function handleParse(e) {
  e.preventDefault();
  const text = document.getElementById('cmdInput').value.trim();
  if (!text) return;
  const btn = document.getElementById('parseBtn');
  btn.disabled = true;
  btn.innerHTML = '<span class="loading"></span>';
  document.getElementById('previewArea').innerHTML = '';
  document.getElementById('messageArea').innerHTML = '';

  try {
    const fd = new FormData();
    fd.append('text', text);
    const res = await fetch('/parse', {method:'POST', body:fd});
    const data = await res.json();
    if (data.ok && data.intent) {
      pendingIntent = data.intent;
      showPreview(data.intent, text);
    } else {
      showMsg(data.error || 'Sielewi maagizo.', 'error');
      pendingIntent = null;
    }
  } catch(err) {
    showMsg('Hitilafu ya mtandao: ' + err.message, 'error');
  }
  btn.disabled = false;
  btn.innerHTML = '→';
}

// ── Render Preview ──
function showPreview(intent, raw) {
  if (intent.action === 'unknown') {
    showMsg('Sielewi maagizo: "' + raw + '". Jaribu mfano: "Amina amelipa hisa 5000"', 'error');
    return;
  }
  if (['member_statement','who_unpaid','group_position'].includes(intent.action)) {
    executeQuery(intent);
    return;
  }
  let html = '<div class="preview-card"><h3>✨ Thibitisha Uingizaji Data</h3>';
  const labels = {
    register: 'Msajili Mwanachama',
    contribute: 'Mchango wa Wanachama',
    fee: 'Ada ya Kikundi',
    fine: 'Faini',
    loan: 'Tozo la Mkopo',
    repay: 'Rejesho la Mkopo',
    payout: 'Tozo kwenye Mfuko',
    expense: 'Matumizi ya Kikundi',
    exit: 'Kutoka kwa Mwanachama'
  };
  html += '<div class="detail"><span>Aina ya Kitendo</span><span class="val">' + (labels[intent.action]||intent.action) + '</span></div>';
  if (intent.member) html += '<div class="detail"><span>Mwanachama</span><span class="val">' + esc(intent.member) + '</span></div>';
  if (intent.mpesa_ref) html += '<div class="detail"><span>M-Pesa Reference</span><span class="val badge-mpesa">' + esc(intent.mpesa_ref) + '</span></div>';

  if (intent.amounts) {
    for (const [k,v] of Object.entries(intent.amounts)) {
      if (k === 'phone') {
        html += '<div class="detail"><span>Simu</span><span class="val">' + esc(v) + '</span></div>';
        continue;
      }
      html += '<div class="detail"><span>' + k.charAt(0).toUpperCase() + k.slice(1) + '</span><span class="val">' + Number(v).toLocaleString() + ' TSH</span></div>';
    }
    html += '<div class="detail" style="border-top:2px solid var(--accent-emerald);padding-top:12px"><span><strong>Jumla</strong></span><span class="val" style="color:#34d399;font-size:16px"><strong>' + Number(intent.amount).toLocaleString() + ' TSH</strong></span></div>';
  } else if (intent.amount > 0) {
    html += '<div class="detail"><span>Kiasi</span><span class="val">' + Number(intent.amount).toLocaleString() + ' TSH</span></div>';
  }
  if (intent.guarantors && intent.guarantors.length) {
    html += '<div class="detail"><span>Wadhamini</span><span class="val">' + intent.guarantors.map(esc).join(', ') + '</span></div>';
  }

  html += '<div class="buttons">';
  html += '<button class="btn-cancel" onclick="cancelPreview()">Futa</button>';
  html += '<button class="btn-confirm" onclick="handleCommit()">✓ Thibitisha Kitendo</button>';
  html += '</div></div>';
  document.getElementById('previewArea').innerHTML = html;
}

function cancelPreview() {
  pendingIntent = null;
  document.getElementById('previewArea').innerHTML = '';
  document.getElementById('cmdInput').focus();
}

// ── Commit Phase & Instant WhatsApp Share ──
async function handleCommit() {
  if (!pendingIntent) return;
  const area = document.getElementById('previewArea');
  area.innerHTML = '<div class="message"><span class="loading"></span> Inatekeleza mahesabu kwenye ledger...</div>';

  try {
    const fd = new FormData();
    fd.append('data', JSON.stringify(pendingIntent));
    const res = await fetch('/commit', {method:'POST', body:fd});
    const data = await res.json();
    if (data.ok) {
      let msgHtml = '<div class="message success">';
      msgHtml += esc(data.message);
      
      const r = data;
      if (['contribute', 'repay', 'loan', 'fee', 'fine'].includes(r.action)) {
        const phone = r.member_phone || '';
        const memberName = r.member_name || pendingIntent.member || '';
        const mpesaRef = r.mpesa_ref || pendingIntent.mpesa_ref || '';
        const text = encodeURIComponent(
          `💰 RISITI YA VICOBA - ${groupSettings.group_name || 'Kikundi'}\n\n` +
          `Habari ${memberName},\n` +
          `${data.message}\n` +
          (mpesaRef ? `M-Pesa Ref: ${mpesaRef}\n` : '') +
          `Tarehe: ${new Date().toLocaleDateString()}\n\n` +
          `Ahsante kwa uwajibikaji!`
        );
        const waUrl = phone ? `https://wa.me/255${phone.replace(/^0/,'')}?text=${text}` : `https://api.whatsapp.com/send?text=${text}`;
        msgHtml += `<br><br><a href="${waUrl}" target="_blank" class="btn-sm btn-wa-sm" style="display:inline-block;padding:8px 16px;border-radius:10px;text-decoration:none;font-size:13px">📲 Tuma Risiti WhatsApp</a>`;
      }
      msgHtml += '</div>';

      document.getElementById('messageArea').innerHTML = msgHtml;
      document.getElementById('cmdInput').value = '';
      checkHealth();
    } else {
      showMsg(data.error || 'Hitilafu wakati wa kutekeleza', 'error');
    }
  } catch(err) {
    showMsg('Hitilafu ya mtandao: ' + err.message, 'error');
  }
  pendingIntent = null;
  area.innerHTML = '';
}

// ── Read Queries ──
async function executeQuery(intent) {
  const area = document.getElementById('messageArea');
  area.innerHTML = '<div class="message"><span class="loading"></span> Inatafuta taarifa kwenye database...</div>';
  try {
    let url;
    if (intent.action === 'member_statement' && intent.member) url = '/api/statement/' + encodeURIComponent(intent.member);
    else if (intent.action === 'group_position') url = '/api/group';
    else if (intent.action === 'who_unpaid') url = '/api/unpaid';
    else { area.innerHTML = ''; return; }

    const res = await fetch(url);
    const data = await res.json();
    if (!data.ok) { showMsg(data.error, 'error'); return; }
    if (intent.action === 'member_statement') renderStatementModal(data);
    else if (intent.action === 'group_position') { showTab('dash'); }
    else if (intent.action === 'who_unpaid') renderUnpaid(data);
  } catch(err) { showMsg('Hitilafu: ' + err.message, 'error'); }
}

function renderUnpaid(data) {
  let html = '<div class="message success">';
  html += '<strong>Hali ya Mchango Leo (' + data.date + '):</strong> ' + data.paid_today + '/' + data.total_active + ' wamelipa.<br><br>';
  if (data.missing.length === 0) html += '✅ Wanachama wote active wamelipa leo!';
  else {
    html += '<strong>Hawajalipa bado:</strong><br>';
    const till = groupSettings.mpesa_till || '---';
    const accName = groupSettings.mpesa_name || 'VICOBA';
    data.missing.forEach(m => {
      const phone = m.phone || '';
      const text = encodeURIComponent(
        `📢 KIKUMBUSHO CHA MCHANGO - VICOBA\n\n` +
        `Habari ${m.name} (${m.member_no}),\n` +
        `Unakumbushwa kutoa mchango wako wa leo wa VICOBA.\n\n` +
        `Lipa kupitia M-Pesa:\n` +
        `• Till / Namba: ${till}\n` +
        `• Jina la Akaunti: ${accName}\n\n` +
        `Ahsante!`
      );
      const waUrl = phone ? `https://wa.me/255${phone.replace(/^0/,'')}?text=${text}` : `https://api.whatsapp.com/send?text=${text}`;
      html += `<div style="display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid rgba(255,255,255,0.08)">` +
              `<span>• <strong>${esc(m.name)}</strong> (${m.member_no})</span>` +
              `<a href="${waUrl}" target="_blank" class="btn-sm btn-wa-sm" style="text-decoration:none">📲 Kikumbusha</a></div>`;
    });
  }
  html += '</div>';
  document.getElementById('messageArea').innerHTML = html;
}

// ── Dashboard Loading ──
async function loadDashboard() {
  try {
    const res = await fetch('/api/group');
    const data = await res.json();
    if (!data.ok) return;
    const g = data;
    document.getElementById('statsGrid').innerHTML =
      statCard('Fedha Tasani (Cash)', g.cash, 'positive') +
      statCard('Jumla ya Hisa', g.total_hisa, 'positive') +
      statCard('Mikopo Hai', g.total_outstanding_loans, 'negative') +
      statCard('Wanachama', g.active_members + '/' + g.total_members) +
      statCard('Mfuko wa Jamii', g.jamii) +
      statCard('Mfuko wa Bima', g.bima) +
      statCard('Mapato ya Kikundi', g.total_income, 'positive') +
      statCard('Matumizi', g.total_expenses, g.total_expenses > 0 ? 'negative' : '');

    let rows = '';
    (g.members || []).forEach(m => {
      rows += '<tr>';
      rows += '<td><a href="javascript:void(0)" onclick="viewMemberStatement(\'' + esc(m.name) + '\')" style="color:#67e8f9;text-decoration:none;font-weight:700">' + esc(m.name) + '</a></td>';
      rows += '<td class="right monospace">' + m.hisa.toLocaleString() + '</td>';
      rows += '<td class="right monospace">' + m.akiba.toLocaleString() + '</td>';
      rows += '<td class="right monospace" style="color:' + (m.deni_lichangiwa > 0 ? '#f87171' : '#34d399') + '">' + m.deni_lichangiwa.toLocaleString() + '</td>';
      rows += '</tr>';
    });
    document.getElementById('memberTable').innerHTML = rows || '<tr><td colspan="4" class="empty">Hakuna wanachama waliosajiliwa bado.</td></tr>';
  } catch(e) { console.error(e); }
}

function statCard(label, value, cls) {
  const display = typeof value === 'number' ? value.toLocaleString() : value;
  return '<div class="stat-card"><div class="label">' + label + '</div><div class="value ' + (cls||'') + '">' + display + (typeof value === 'number' ? ' <span style="font-size:12px;font-weight:500;color:var(--text-muted)">TSH</span>' : '') + '</div></div>';
}

function showGawio() {
  fetch('/api/gawio').then(r=>r.json()).then(data => {
    if (!data.ok) return;
    let html = '<div class="table-wrap"><div class="table-header"><span>Makadirio ya Gawio (Profit Distribution Preview)</span></div>';
    html += '<p style="padding:14px 20px;font-size:13.5px;color:var(--text-muted);border-bottom:1px solid rgba(255,255,255,0.08)">Mapato yanayogawika: <strong style="color:#34d399">' + data.distributable.toLocaleString() + ' TSH</strong> · Kiwango: <strong>' + data.per_hisa_rate + ' TSH</strong> kwa kila hisa 1</p>';
    html += '<table><thead><tr><th>Mwanachama</th><th class="right">Hisa</th><th class="right">Gawio Linalotazamiwa</th></tr></thead><tbody>';
    (data.members||[]).forEach(m => {
      html += '<tr><td>' + esc(m.name) + ' (' + m.member_no + ')</td><td class="right monospace">' + m.hisa.toLocaleString() + '</td><td class="right monospace" style="color:#34d399;font-weight:700">' + m.gawio.toLocaleString() + ' TSH</td></tr>';
    });
    html += '</tbody></table></div>';
    document.getElementById('gawioArea').innerHTML = html;
  });
}

// ── Members List ──
async function loadMembers() {
  try {
    const res = await fetch('/api/members');
    const data = await res.json();
    if (!data.ok) return;
    allMembersCache = data.members || [];
    renderMembersTable(allMembersCache);
  } catch(e) { console.error(e); }
}

function renderMembersTable(members) {
  let rows = '';
  members.forEach(m => {
    const isAct = m.status === 'active';
    const badgeClass = isAct ? 'badge-active' : 'badge-exited';
    rows += '<tr>';
    rows += '<td><strong style="font-size:12px;font-family:monospace">' + esc(m.member_no) + '</strong></td>';
    rows += '<td>' + esc(m.name) + (m.phone ? '<br><span style="font-size:11.5px;color:var(--text-muted);font-family:monospace">' + esc(m.phone) + '</span>' : '') + '</td>';
    rows += '<td><span class="badge ' + badgeClass + '">' + m.status + '</span></td>';
    rows += '<td class="right">';
    rows += '<button class="btn-sm" onclick="viewMemberStatement(\'' + esc(m.name) + '\')">Taarifa</button> ';
    if (isAct) {
      rows += '<button class="btn-sm btn-danger-sm" onclick="previewExit(\'' + esc(m.name) + '\')">Kutoka</button>';
    }
    rows += '</td></tr>';
  });
  document.getElementById('fullMemberTable').innerHTML = rows || '<tr><td colspan="4" class="empty">Hakuna mwanachama anayefanana na utafutaji wako.</td></tr>';
}

function filterMembersTable() {
  const query = document.getElementById('memberSearchInput').value.toLowerCase().trim();
  if (!query) {
    renderMembersTable(allMembersCache);
    return;
  }
  const filtered = allMembersCache.filter(m =>
    (m.name && m.name.toLowerCase().includes(query)) ||
    (m.member_no && m.member_no.toLowerCase().includes(query))
  );
  renderMembersTable(filtered);
}

// ── Statement & Exit Modals ──
async function viewMemberStatement(memberName) {
  openModal('Taarifa ya Mwanachama: ' + memberName, '<div class="loading"></div> Inapakia...');
  try {
    const res = await fetch('/api/statement/' + encodeURIComponent(memberName));
    const data = await res.json();
    if (!data.ok) {
      document.getElementById('modalBody').innerHTML = '<div class="message error">' + esc(data.error) + '</div>';
      return;
    }
    renderStatementModal(data);
  } catch(e) {
    document.getElementById('modalBody').innerHTML = '<div class="message error">Hitilafu: ' + e.message + '</div>';
  }
}

function renderStatementModal(s) {
  let html = '<div style="margin-bottom:16px">';
  html += '<p style="font-size:14px;color:var(--text-muted);margin-bottom:12px">Namba: <strong style="color:#ffffff">' + s.member_no + '</strong> · Hali: <strong style="color:#34d399">' + s.status + '</strong></p>';
  html += '<div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:16px">';
  html += '<div style="background:rgba(6,24,18,0.7);padding:14px;border-radius:12px;border:1px solid rgba(255,255,255,0.08)">Hisa<br><strong style="font-size:16px;color:#34d399;font-family:monospace">' + s.hisa.toLocaleString() + ' TSH</strong></div>';
  html += '<div style="background:rgba(6,24,18,0.7);padding:14px;border-radius:12px;border:1px solid rgba(255,255,255,0.08)">Akiba<br><strong style="font-size:16px;color:#34d399;font-family:monospace">' + s.akiba.toLocaleString() + ' TSH</strong></div>';
  html += '<div style="background:rgba(6,24,18,0.7);padding:14px;border-radius:12px;border:1px solid rgba(255,255,255,0.08);grid-column:span 2">Deni la Mkopo<br><strong style="font-size:16px;font-family:monospace;color:' + (s.deni_lichangiwa > 0 ? '#f87171' : 'var(--text-main)') + '">' + s.deni_lichangiwa.toLocaleString() + ' TSH</strong></div>';
  html += '</div>';

  if (s.loans && s.loans.length) {
    html += '<h4 style="font-size:13px;margin-bottom:8px;color:#a7f3d0">Historia ya Mikopo</h4>';
    s.loans.forEach(l => {
      html += '<div style="font-size:12.5px;padding:10px;background:rgba(255,255,255,0.04);border-radius:8px;margin-bottom:8px;border:1px solid rgba(255,255,255,0.06)">';
      html += 'Mkopo: <strong style="font-family:monospace">' + l.principal.toLocaleString() + '</strong> (Riba ' + (l.rate*100) + '%) = ' + l.total_due.toLocaleString() + ' TSH<br>';
      html += 'Kulipwa: ' + l.amount_paid.toLocaleString() + ' TSH · Hali: <strong>' + l.status + '</strong>';
      html += '</div>';
    });
  }

  if (s.transactions && s.transactions.length) {
    html += '<h4 style="font-size:13px;margin-top:14px;margin-bottom:8px;color:#a7f3d0">Miamala ya Hivi Karibuni</h4>';
    html += '<div style="max-height:200px;overflow-y:auto"><table style="font-size:12px"><thead><tr><th>Tarehe</th><th>Maelezo</th></tr></thead><tbody>';
    s.transactions.forEach(t => {
      html += '<tr><td>' + t.tx_date + '</td><td>' + esc(t.description) + '</td></tr>';
    });
    html += '</tbody></table></div>';
  }

  html += '</div>';
  openModal('Taarifa: ' + s.name + ' (' + s.member_no + ')', html);
}

async function previewExit(memberName) {
  openModal('Kutoka Mwanachama: ' + memberName, '<div class="loading"></div> Inapima mahesabu...');
  try {
    const res = await fetch('/api/exit/' + encodeURIComponent(memberName));
    const data = await res.json();
    if (!data.ok) {
      document.getElementById('modalBody').innerHTML = '<div class="message error">' + esc(data.error) + '</div>';
      return;
    }
    const e = data;
    let html = '<div style="font-size:14px;line-height:1.6">';
    html += '<p>Mwanachama: <strong>' + esc(e.name) + ' (' + e.member_no + ')</strong></p>';
    html += '<p>Hisa: <strong class="monospace">' + e.hisa.toLocaleString() + ' TSH</strong></p>';
    html += '<p>Akiba: <strong class="monospace">' + e.akiba.toLocaleString() + ' TSH</strong></p>';
    html += '<p style="border-top:1px solid rgba(255,255,255,0.1);padding-top:10px;margin-top:10px">Jumla inayorejeshwa: <strong style="color:#34d399;font-size:16px" class="monospace">' + e.payable.toLocaleString() + ' TSH</strong></p>';
    html += '<p style="font-size:12.5px;color:var(--text-muted);margin-top:10px;background:rgba(6,24,18,0.7);padding:10px;border-radius:10px;border:1px solid rgba(255,255,255,0.06)">' + esc(e.note) + '</p>';

    if (e.can_exit) {
      html += '<div style="margin-top:18px;display:flex;gap:12px">';
      html += '<button class="btn-cancel" style="flex:1;padding:12px;border-radius:10px" onclick="closeModal()">Ghairi</button>';
      html += '<button class="btn-confirm" style="flex:1;padding:12px;border-radius:10px;background:var(--accent-rose)" onclick="confirmExitFromModal(\'' + esc(e.name) + '\')">Thibitisha Kutoka</button>';
      html += '</div>';
    }
    html += '</div>';
    openModal('Hali ya Kutoka: ' + e.name, html);
  } catch(err) {
    document.getElementById('modalBody').innerHTML = '<div class="message error">Hitilafu: ' + err.message + '</div>';
  }
}

function confirmExitFromModal(name) {
  closeModal();
  fillTrigger('ondoa mwanachama ' + name);
}

// ── Meeting Sheet & WhatsApp Group Report ──
async function loadMeeting() {
  try {
    const res = await fetch('/api/meeting');
    const data = await res.json();
    if (!data.ok) return;
    let html = '<div class="table-wrap"><div class="table-header"><span>Ripoti ya Kutaniko — ' + data.date + '</span>';
    
    const waText = encodeURIComponent(
      `📊 RIPOTI YA KUTANIKO - ${groupSettings.group_name || 'VICOBA'}\n` +
      `Tarehe: ${data.date}\n\n` +
      `• Cash In: ${data.day_cash_in.toLocaleString()} TSH\n` +
      `• Cash Out: ${data.day_cash_out.toLocaleString()} TSH\n` +
      `• Baki Tasani: ${data.cash_balance.toLocaleString()} TSH\n\n` +
      `Miamala ya Leo: ${data.entries.length}\n` +
      `Ahsanteni wanachama!`
    );
    html += `<a href="https://api.whatsapp.com/send?text=${waText}" target="_blank" class="btn-sm btn-wa-sm" style="text-decoration:none">📲 WhatsApp Group</a></div>`;

    html += '<div style="padding:14px 20px;display:flex;justify-content:space-between;font-size:13.5px;background:rgba(6,24,18,0.5);border-bottom:1px solid rgba(255,255,255,0.08)">';
    html += '<span>Kuingia: <strong style="color:#34d399" class="monospace">' + data.day_cash_in.toLocaleString() + ' TSH</strong></span>';
    html += '<span>Kutoka: <strong style="color:#f87171" class="monospace">' + data.day_cash_out.toLocaleString() + ' TSH</strong></span>';
    html += '<span>Baki Tasani: <strong class="monospace">' + data.cash_balance.toLocaleString() + ' TSH</strong></span></div>';

    if (data.entries.length === 0) {
      html += '<p style="padding:24px;text-align:center;color:var(--text-muted)">Hakuna shughuli za kifedha zilizofanyika leo bado.</p>';
    } else {
      html += '<table><thead><tr><th>#</th><th>Aina</th><th>Maelezo</th><th class="right">Ingia (D)</th><th class="right">Toka (C)</th></tr></thead><tbody>';
      data.entries.forEach(e => {
        html += '<tr><td>' + e.journal_id + '</td><td><span class="badge" style="background:rgba(255,255,255,0.1);color:#ffffff">' + e.kind + '</span></td><td>' + esc(e.description) + '</td>';
        html += '<td class="right monospace">' + (e.debit ? e.debit.toLocaleString() : '—') + '</td><td class="right monospace">' + (e.credit ? e.credit.toLocaleString() : '—') + '</td></tr>';
      });
      html += '</tbody></table>';
    }
    html += '</div>';
    document.getElementById('meetingArea').innerHTML = html;
  } catch(e) { console.error(e); }
}

async function loadUnpaid() {
  try {
    const res = await fetch('/api/unpaid');
    const data = await res.json();
    renderUnpaid(data);
  } catch(e) { showMsg('Hitilafu: ' + e.message, 'error'); }
}

// ── Settings Modal ──
async function openSettingsModal() {
  await fetchSettings();
  let html = '<form onsubmit="saveSettings(event)">';
  html += '<div class="form-group"><label>Jina la Kikundi</label><input type="text" id="set_group_name" value="' + esc(groupSettings.group_name||'') + '"></div>';
  html += '<div class="form-group"><label>M-Pesa Till / Paybill / Namba ya Simu</label><input type="text" id="set_mpesa_till" placeholder="mfano: 554433 au 0712345678" value="' + esc(groupSettings.mpesa_till||'') + '"></div>';
  html += '<div class="form-group"><label>M-Pesa Registered Name (Jina la Akaunti)</label><input type="text" id="set_mpesa_name" placeholder="mfano: VICOBA KIKUNDI" value="' + esc(groupSettings.mpesa_name||'') + '"></div>';
  html += '<div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">';
  html += '<div class="form-group"><label>Riba ya Mkopo (%)</label><input type="number" id="set_interest_rate_pct" value="' + esc(groupSettings.interest_rate_pct||'10') + '"></div>';
  html += '<div class="form-group"><label>Muda wa Mkopo (Wiki)</label><input type="number" id="set_loan_weeks" value="' + esc(groupSettings.loan_weeks||'12') + '"></div>';
  html += '</div>';
  html += '<div class="form-group"><label>Cheo cha Kukopa (Multiple ya Hisa, mfano: 3x)</label><input type="number" id="set_eligibility_multiple" value="' + esc(groupSettings.eligibility_multiple||'3') + '"></div>';
  
  html += '<hr style="margin:16px 0;border:none;border-top:1px solid rgba(255,255,255,0.1)">';
  html += '<h4 style="font-size:13.5px;margin-bottom:12px;color:#34d399">🤖 Injini ya AI ya Local (100% Offline)</h4>';
  html += '<div class="form-group"><label>Local LLM Server Endpoint URL</label><input type="text" id="set_local_llm_url" placeholder="http://localhost:11434/v1/chat/completions au http://localhost:8080/v1/chat/completions" value="' + esc(groupSettings.local_llm_url||'http://localhost:11434/v1/chat/completions') + '"></div>';
  html += '<div class="form-group"><label>Local Model Name (Jina la Model)</label><input type="text" id="set_local_llm_model" placeholder="cactus, qwen2.5, gemma2, llama3" value="' + esc(groupSettings.local_llm_model||'cactus') + '"></div>';
  
  html += '<button type="submit" class="btn-confirm" style="width:100%;padding:14px;margin-top:12px">Hifadhi Mipangilio ✓</button>';
  html += '</form>';
  openModal('⚙️ Mipangilio ya Kikundi & Local AI', html);
}

async function saveSettings(e) {
  e.preventDefault();
  const payload = {
    group_name: document.getElementById('set_group_name').value.trim(),
    mpesa_till: document.getElementById('set_mpesa_till').value.trim(),
    mpesa_name: document.getElementById('set_mpesa_name').value.trim(),
    interest_rate_pct: document.getElementById('set_interest_rate_pct').value.trim(),
    loan_weeks: document.getElementById('set_loan_weeks').value.trim(),
    eligibility_multiple: document.getElementById('set_eligibility_multiple').value.trim(),
    local_llm_url: document.getElementById('set_local_llm_url').value.trim(),
    local_llm_model: document.getElementById('set_local_llm_model').value.trim(),
  };
  try {
    const fd = new FormData();
    fd.append('data', JSON.stringify(payload));
    const res = await fetch('/api/settings', {method:'POST', body:fd});
    const data = await res.json();
    if (data.ok) {
      groupSettings = {...groupSettings, ...payload};
      closeModal();
      showMsg(data.message || 'Mipangilio imehifadhiwa!', 'success');
    } else {
      alert(data.error || 'Hitilafu');
    }
  } catch(err) { alert('Hitilafu: ' + err.message); }
}

// ── Register Member Modal ──
function openRegisterModal() {
  let html = '<form onsubmit="submitRegisterMember(event)">';
  html += '<div class="form-group"><label>Jina Kamili la Mwanachama *</label><input type="text" id="reg_name" placeholder="mfano: Juma Ally" required autofocus></div>';
  html += '<div class="form-group"><label>Namba ya Simu</label><input type="text" id="reg_phone" placeholder="mfano: 0712345678"></div>';
  html += '<div class="form-group"><label>Namba ya Mwanachama (Optional)</label><input type="text" id="reg_no" placeholder="Acha wazi kutoa BSDA-001 kiotomatiki"></div>';
  html += '<button type="submit" class="btn-confirm" style="width:100%;padding:14px;margin-top:12px">✓ Msajili Mwanachama</button>';
  html += '</form>';
  openModal('➕ Sajili Mwanachama Mpya', html);
}

async function submitRegisterMember(e) {
  e.preventDefault();
  const name = document.getElementById('reg_name').value.trim();
  const phone = document.getElementById('reg_phone').value.trim();
  const memberNo = document.getElementById('reg_no').value.trim();
  if (!name) return;

  const intent = {
    action: 'register',
    member: name,
    member_no: memberNo || null,
    amounts: phone ? { phone: phone } : null,
    description: `Msajili ${name}`
  };

  try {
    const fd = new FormData();
    fd.append('data', JSON.stringify(intent));
    const res = await fetch('/commit', { method: 'POST', body: fd });
    const data = await res.json();
    if (data.ok) {
      closeModal();
      loadMembers();
      loadDashboard();
      showMsg(data.message, 'success');
    } else {
      alert(data.error || 'Hitilafu ya usajili');
    }
  } catch(err) {
    alert('Hitilafu: ' + err.message);
  }
}

// ── General Modal Helpers ──
function openModal(title, content) {
  document.getElementById('modalTitle').textContent = title;
  document.getElementById('modalBody').innerHTML = content;
  document.getElementById('appModal').classList.add('active');
}

function closeModal() {
  document.getElementById('appModal').classList.remove('active');
}

function showMsg(text, cls) {
  document.getElementById('messageArea').innerHTML = '<div class="message ' + cls + '">' + esc(text) + '</div>';
}

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s || '';
  return d.innerHTML;
}

function exportCSV() {
  window.open('/api/export/meeting.csv', '_blank');
}
