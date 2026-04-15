// ── Inject CSS Fixes dynamically for overflows and section titles ───────
const style = document.createElement('style');
style.innerHTML = `
  .perm-tool-item {
    overflow: hidden;
    white-space: nowrap;
    text-overflow: ellipsis;
    display: flex;
    align-items: center;
    max-width: 100%;
    cursor: pointer;
  }
  .perm-tool-name {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    flex: 1;
  }
  .perm-section-title {
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--text3);
    margin: 12px 0 6px 0;
    font-family: var(--mono);
    border-bottom: 1px solid var(--border);
    padding-bottom: 2px;
  }
`;
document.head.appendChild(style);

// ── Config ──────────────────────────────────────────────────────
const API_BASE   = '';          // same origin
const ADMIN_KEY  = 'admin-secret-change-me';  // match ADMIN_KEY env var

const headers = (extra = {}) => ({
  'Content-Type': 'application/json',
  'X-Admin-Key': ADMIN_KEY,
  ...extra
});

// ── State ──
const state = {
  servers: [],
  clients: [],
  logs: [],
  pendingPerms: {}
};

// ── API helpers ─────────────────────────────────────────────────
async function api(method, path, body) {
  const opts = { method, headers: headers() };
  if (body !== undefined) opts.body = JSON.stringify(body);
  const res = await fetch(API_BASE + path, opts);
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || res.statusText);
  }
  if (res.status === 204) return null;
  return res.json();
}

const GET    = (path)        => api('GET',    path);
const POST   = (path, body)  => api('POST',   path, body);
const PUT    = (path, body)  => api('PUT',    path, body);
const PATCH  = (path, body)  => api('PATCH',  path, body);
const DEL    = (path)        => api('DELETE', path);

// ── Load all data from backend ───────────────────────────────────
async function loadAll() {
  try {
    const [servers, clients, logs] = await Promise.all([
      GET('/admin/servers'),
      GET('/admin/clients'),
      GET('/admin/logs?limit=50'),
    ]);
    state.servers = servers.map(s => ({
      ...s,
      desc:      s.description,
      tools:     (s.tools     || []).map(t => t.name), 
      resources: (s.resources || []),                  
      prompts:   (s.prompts   || []),                  
    }));
    state.clients = clients.map(c => ({
      ...c,
      desc: c.description,
      key:  c.api_key,
      perms: {}
    }));
    state.logs = logs.map(l => ({
      time:   new Date(l.timestamp).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit' }),
      method: l.method,
      client: l.client_name,
      tool:   l.tool,
      status: l.status
    }));
    
    for (const client of state.clients) {
      try {
        const data = await GET(`/admin/clients/${client.id}/permissions`);
        const perms = {};
        for (const [sid, tools] of Object.entries(data.permissions || {})) {
          perms[sid] = new Set(tools);
        }
        client.perms = perms;
      } catch(e) {
        console.warn(`Could not load permissions for client ${client.id}`);
      }
    }
    
    renderAll();
    updateGatewayStatus();
  } catch(e) {
    showToast('Backend error: ' + e.message);
    updateGatewayStatus();
  }
}

async function loadClientPerms(clientId) {
  try {
    const data = await GET(`/admin/clients/${clientId}/permissions`);
    const perms = {};
    for (const [sid, tools] of Object.entries(data.permissions || {})) {
      perms[sid] = new Set(tools);
    }
    state.pendingPerms[clientId] = perms;
    return perms;
  } catch(e) {
    showToast('Could not load permissions: ' + e.message);
    return {};
  }
}

// ── Navigation ──────────────────────────────────────────────────
const pageMeta = {
  dashboard:   { title: 'Dashboard',        crumb: 'gateway / overview',    action: '+ Add server',  fn: 'openAddServerModal' },
  servers:     { title: 'MCP Servers',      crumb: 'gateway / servers',     action: '+ Register server', fn: 'openAddServerModal' },
  clients:     { title: 'Clients',          crumb: 'gateway / clients',     action: '+ Add client',  fn: 'openAddClientModal' },
  permissions: { title: 'Permissions',      crumb: 'gateway / permissions', action: null },
  logs:        { title: 'Activity Log',     crumb: 'gateway / logs',        action: null },
};

function showPage(name) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  document.getElementById('page-' + name).classList.add('active');
  document.querySelector(`[data-page="${name}"]`).classList.add('active');
  const meta = pageMeta[name];
  document.getElementById('topbar-title').textContent = meta.title;
  document.getElementById('topbar-crumb').textContent = meta.crumb;
  const btn = document.getElementById('topbar-action-btn');
  if (meta.action) { btn.textContent = meta.action; btn.style.display = ''; }
  else btn.style.display = 'none';
  renderAll();
}

function updateGatewayStatus() {
  const txt = document.getElementById('footer-status');
  if (!txt) return;
  const toolCount = state.servers.reduce((sum, s) => sum + (s.tools ? s.tools.length : 0), 0);
  txt.textContent = `v1.0.0 • ${toolCount} items registered`;
}

function topbarAction() {
  const active = document.querySelector('.page.active');
  if (active.id === 'page-dashboard' || active.id === 'page-servers') openAddServerModal();
  else if (active.id === 'page-clients') openAddClientModal();
}

// ── Render all ──
function renderAll() {
  renderStats();
  renderDashServers();
  renderDashClients();
  renderServersTable();
  renderClientsTable();
  renderPermClientSelect();
  renderLogs();
}

function renderStats() { loadStats(); }

function serverStatusBadge(s) {
  return s.status === 'online'
    ? '<span class="badge badge-green">● online</span>'
    : '<span class="badge badge-red">● offline</span>';
}

function renderDashServers() {
  const el = document.getElementById('dash-servers-table');
  if (!state.servers.length) { el.innerHTML = '<div class="empty-state"><div class="empty-icon">◈</div>No servers registered yet</div>'; return; }
  el.innerHTML = `<table><thead><tr><th>Name</th><th>URL</th><th>Tools</th><th>Status</th></tr></thead><tbody>
    ${state.servers.map(s => `<tr>
      <td class="primary">${s.name}</td>
      <td><span style="font-family:var(--mono);font-size:12px;color:var(--text3)">${s.url}/mcp</span></td>
      <td><span class="badge badge-blue">${s.tools.length} tools</span></td>
      <td>${serverStatusBadge(s)}</td>
    </tr>`).join('')}
  </tbody></table>`;
}

function renderDashClients() {
  const el = document.getElementById('dash-clients-table');
  if (!state.clients.length) { el.innerHTML = '<div class="empty-state"><div class="empty-icon">◉</div>No clients registered yet</div>'; return; }
  el.innerHTML = `<table><thead><tr><th>Client</th><th>Servers</th><th>Capabilities</th><th>Key</th></tr></thead><tbody>
    ${state.clients.map(c => {
      const srvCount = c.perms ? Object.keys(c.perms).length : 0;
      const toolCount = c.perms ? Object.values(c.perms).reduce((a,s) => a+s.size,0) : 0;
      return `<tr>
        <td class="primary">${c.name}</td>
        <td><span class="badge badge-blue">${srvCount} server${srvCount!==1?'s':''}</span></td>
        <td><span class="badge badge-amber">${toolCount} item${toolCount!==1?'s':''}</span></td>
        <td><button class="btn btn-ghost btn-sm" onclick="showKey('${c.id}')">⊙ view key</button></td>
      </tr>`;
    }).join('')}
  </tbody></table>`;
}

function renderServersTable() {
  const el = document.getElementById('servers-table');
  if (!state.servers.length) { el.innerHTML = '<div class="empty-state"><div class="empty-icon">◈</div>No servers yet — register your first MCP server</div>'; return; }
  el.innerHTML = `<table><thead><tr><th>Name</th><th>Endpoint</th><th>Server info</th><th>Discovered</th><th>Status</th><th></th></tr></thead><tbody>
    ${state.servers.map(s => {
      const si = s.server_info || {};
      const toolCount = (s.tools||[]).length;
      const resCount  = (s.resources||[]).length;
      const prmCount  = (s.prompts||[]).length;
      return `<tr>
        <td class="primary"><span style="font-family:var(--mono)">${s.name}</span></td>
        <td><span style="font-family:var(--mono);font-size:11px;color:var(--text3)">${s.url}/mcp</span></td>
        <td>
          ${si.name ? `<span style="font-size:12px;color:var(--text2)">${si.name} ${si.version||''}</span>` : '<span style="color:var(--text3);font-size:12px">—</span>'}
          ${s.protocol_version ? `<div style="font-family:var(--mono);font-size:11px;color:var(--text3)">MCP ${s.protocol_version}</div>` : ''}
        </td>
        <td>
          <span class="badge badge-blue" style="margin-right:4px" title="Tools">⚙ ${toolCount}</span>
          <span class="badge badge-amber" style="margin-right:4px" title="Resources">◎ ${resCount}</span>
          <span class="badge badge-gray" title="Prompts">✦ ${prmCount}</span>
        </td>
        <td>${serverStatusBadge(s)}</td>
        <td style="display:flex;gap:6px">
          <button class="btn btn-ghost btn-sm" onclick="refreshServer('${s.id}')" title="Re-discover tools">↻</button>
          <button class="btn btn-danger btn-sm" onclick="deleteServer('${s.id}')">✕</button>
        </td>
      </tr>`;
    }).join('')}
  </tbody></table>`;
}

function renderClientsTable() {
  const el = document.getElementById('clients-table');
  if (!state.clients.length) { el.innerHTML = '<div class="empty-state"><div class="empty-icon">◉</div>No clients yet — add your first client</div>'; return; }
  el.innerHTML = `<table><thead><tr><th>Name</th><th>Description</th><th>Permissions</th><th>API key</th><th></th></tr></thead><tbody>
    ${state.clients.map(c => {
      const toolCount = c.perms ? Object.values(c.perms).reduce((a,s)=>a+s.size,0) : 0;
      const srvCount  = c.perms ? Object.keys(c.perms).length : 0;
      return `<tr>
        <td class="primary">${c.name}</td>
        <td style="color:var(--text3)">${c.desc||'—'}</td>
        <td>
          <span class="badge badge-blue" style="margin-right:4px">${srvCount} server${srvCount!==1?'s':''}</span>
          <span class="badge badge-amber">${toolCount} item${toolCount!==1?'s':''}</span>
        </td>
        <td><button class="btn btn-ghost btn-sm" onclick="showKey('${c.id}')">⊙ view key</button></td>
        <td style="display:flex;gap:6px">
          <button class="btn btn-ghost btn-sm" onclick="editPermsFor('${c.id}')">◫ edit perms</button>
          <button class="btn btn-danger btn-sm" onclick="deleteClient('${c.id}')">✕</button>
        </td>
      </tr>`;
    }).join('')}
  </tbody></table>`;
}

function renderPermClientSelect() {
  const sel = document.getElementById('perm-client-select');
  const cur = sel.value;
  sel.innerHTML = '<option value="">— choose a client —</option>' +
    state.clients.map(c => `<option value="${c.id}">${c.name}</option>`).join('');
  if (cur) sel.value = cur;
}

// ── PERMISSIONS MATRIX LOGIC ──────────────────────────────────────

async function renderPermMatrix() {
  const clientId = document.getElementById('perm-client-select').value;
  const wrap = document.getElementById('perm-matrix-wrap');
  const saveBtn = document.getElementById('save-perm-btn');
  
  if (!clientId) {
    wrap.innerHTML = '<div class="empty-state" style="background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius);"><div class="empty-icon">◫</div>Select a client to edit their permissions</div>';
    saveBtn.style.display = 'none';
    return;
  }
  
  saveBtn.style.display = '';
  wrap.innerHTML = '<div class="empty-state" style="background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius);">Loading...</div>';

  await loadClientPerms(clientId);

  wrap.innerHTML = '<div class="perm-matrix">' +
    state.servers.map(s => {
      const hasAccess = !!(state.pendingPerms[clientId] && state.pendingPerms[clientId][s.id]);
      
      const renderSection = (title, items, icon) => {
        if (!items || items.length === 0) return '';
        return `
          <div class="perm-section-title">${icon} ${title}</div>
          <div class="perm-tools-grid">
            ${items.map(item => {
              const name = item.name || item.uri || item; 
              const idName = String(name).replace(/[^a-z0-9]/gi,'_');
              const allowed = state.pendingPerms[clientId]?.[s.id]?.has(name);
              
              return `
                <div class="perm-tool-item${allowed?' allowed':''}" 
                     id="pt-${clientId}-${s.id}-${idName}" 
                     title="${name}"
                     onclick="toggleCapability('${clientId}','${s.id}','${name}')">
                  <div class="perm-tool-checkbox">${allowed?'✓':''}</div>
                  <div class="perm-tool-name">${name.includes('__') ? name.split('__')[1] : name}</div>
                </div>`;
            }).join('')}
          </div>`;
      };

      return `
      <div class="perm-server-row${hasAccess?' open':''}" id="psr-${clientId}-${s.id}">
        <div class="perm-server-header" onclick="toggleServerRow('${clientId}','${s.id}')">
          <div class="perm-server-name">
            <span style="font-family:var(--mono);color:var(--accent)">${s.name}</span>
            <span style="color:var(--text3);font-size:12px">${s.desc||''}</span>
            <span class="badge badge-gray">
              ${(s.tools||[]).length}T · ${(s.resources||[]).length}R · ${(s.prompts||[]).length}P
            </span>
          </div>
          <div class="perm-server-access">
            <label class="toggle" onclick="event.stopPropagation()">
              <input type="checkbox" ${hasAccess?'checked':''} 
                     onchange="toggleServerAccess('${clientId}','${s.id}',this.checked)">
              <span class="toggle-slider"></span>
            </label>
            <span class="perm-chevron">▶</span>
          </div>
        </div>
        <div class="perm-tools-panel">
          ${renderSection('Tools', s.tools, '⚙')}
          ${renderSection('Resources', s.resources, '◎')}
          ${renderSection('Prompts', s.prompts, '✦')}
          
          <div style="margin-top:10px;display:flex;gap:8px;">
            <button class="btn btn-ghost btn-sm" onclick="selectAllCaps('${clientId}','${s.id}')">Select all</button>
            <button class="btn btn-ghost btn-sm" onclick="clearAllCaps('${clientId}','${s.id}')">Clear all</button>
          </div>
        </div>
      </div>`;
    }).join('') + '</div>';
}

function toggleServerRow(cid, sid) {
  const row = document.getElementById(`psr-${cid}-${sid}`);
  if(row) row.classList.toggle('open');
}

function toggleServerAccess(cid, sid, checked) {
  if (!state.pendingPerms[cid]) state.pendingPerms[cid] = {};
  if (checked) {
    if (!state.pendingPerms[cid][sid]) state.pendingPerms[cid][sid] = new Set();
    document.getElementById(`psr-${cid}-${sid}`).classList.add('open');
  } else {
    delete state.pendingPerms[cid][sid];
    const s = state.servers.find(srv => srv.id === sid);
    if(!s) return;
    const allNames = [
      ...(s.tools || []), 
      ...(s.resources || []).map(r => r.uri || r.name), 
      ...(s.prompts || []).map(p => p.name)
    ];
    allNames.forEach(name => {
      const idName = String(name).replace(/[^a-z0-9]/gi,'_');
      const el = document.getElementById(`pt-${cid}-${sid}-${idName}`);
      if (el) { 
        el.classList.remove('allowed'); 
        el.querySelector('.perm-tool-checkbox').textContent = ''; 
      }
    });
  }
}

function toggleCapability(cid, sid, name) {
  if (!state.pendingPerms[cid]) state.pendingPerms[cid] = {};
  if (!state.pendingPerms[cid][sid]) state.pendingPerms[cid][sid] = new Set();
  
  const set = state.pendingPerms[cid][sid];
  const idName = String(name).replace(/[^a-z0-9]/gi,'_');
  const el = document.getElementById(`pt-${cid}-${sid}-${idName}`);
  
  if (set.has(name)) {
    set.delete(name);
    if(el) {
      el.classList.remove('allowed');
      el.querySelector('.perm-tool-checkbox').textContent = '';
    }
  } else {
    set.add(name);
    if(el) {
      el.classList.add('allowed');
      el.querySelector('.perm-tool-checkbox').textContent = '✓';
    }
  }
  
  const toggle = document.querySelector(`#psr-${cid}-${sid} input[type=checkbox]`);
  if (toggle) toggle.checked = (set.size > 0);
}

function selectAllCaps(cid, sid) {
  const s = state.servers.find(srv => srv.id === sid);
  if (!s) return;
  if (!state.pendingPerms[cid]) state.pendingPerms[cid] = {};
  
  const allNames = [
    ...(s.tools || []), 
    ...(s.resources || []).map(r => r.uri || r.name), 
    ...(s.prompts || []).map(p => p.name)
  ];
  
  state.pendingPerms[cid][sid] = new Set(allNames);
  
  allNames.forEach(name => {
    const idName = String(name).replace(/[^a-z0-9]/gi,'_');
    const el = document.getElementById(`pt-${cid}-${sid}-${idName}`);
    if (el) { 
      el.classList.add('allowed'); 
      el.querySelector('.perm-tool-checkbox').textContent = '✓'; 
    }
  });
  
  const toggle = document.querySelector(`#psr-${cid}-${sid} input[type=checkbox]`);
  if (toggle) toggle.checked = true;
}

function clearAllCaps(cid, sid) {
  if (state.pendingPerms[cid]) delete state.pendingPerms[cid][sid];
  
  const s = state.servers.find(srv => srv.id === sid);
  if (!s) return;
  
  const allNames = [
    ...(s.tools || []), 
    ...(s.resources || []).map(r => r.uri || r.name), 
    ...(s.prompts || []).map(p => p.name)
  ];
  
  allNames.forEach(name => {
    const idName = String(name).replace(/[^a-z0-9]/gi,'_');
    const el = document.getElementById(`pt-${cid}-${sid}-${idName}`);
    if (el) { 
      el.classList.remove('allowed'); 
      el.querySelector('.perm-tool-checkbox').textContent = ''; 
    }
  });
  
  const toggle = document.querySelector(`#psr-${cid}-${sid} input[type=checkbox]`);
  if (toggle) toggle.checked = false;
}

async function savePermissions() {
  const cid = document.getElementById('perm-client-select').value;
  if (!cid) return;
  const pending = state.pendingPerms[cid] || {};
  const permissions = {};
  Object.entries(pending).forEach(([sid, tools]) => {
    if (tools.size > 0) permissions[sid] = [...tools];
  });
  try {
    await PUT(`/admin/clients/${cid}/permissions`, { permissions });
    const client = state.clients.find(c=>c.id===cid);
    showToast('Permissions saved for ' + (client?.name || cid));
    await loadStats();
  } catch(e) { showToast('Error saving: ' + e.message); }
}

// ── LOGS (Updated Tabular Format) ──────────────────────────────────
function renderLogs() {
  const el = document.getElementById('logs-wrap');
  if (!state.logs.length) { 
    el.innerHTML = '<div class="empty-state"><div class="empty-icon">≡</div>No activity yet</div>'; 
    return; 
  }

  el.innerHTML = `
    <table style="width: 100%; border-collapse: collapse;">
      <thead>
        <tr style="text-align: left; border-bottom: 1px solid var(--border); color: var(--text3);">
          <th style="padding: 12px; width: 180px;">Timestamp</th>
          <th style="padding: 12px;">Method Called</th>
          <th style="padding: 12px;">Client Name</th>
          <th style="padding: 12px;">Target / Resource</th>
          <th style="padding: 12px; text-align: right;">Status</th>
        </tr>
      </thead>
      <tbody>
        ${state.logs.map(l => {
          let statusBadge = '';
          if (l.status >= 200 && l.status < 300) {
            statusBadge = '<span class="badge badge-green" style="font-family:var(--mono); font-size: 11px;">' + l.status + ' OK</span>';
          } else if (l.status >= 400 && l.status < 500) {
            statusBadge = '<span class="badge badge-amber" style="font-family:var(--mono); font-size: 11px;">' + l.status + ' ERR</span>';
          } else {
            statusBadge = '<span class="badge badge-red" style="font-family:var(--mono); font-size: 11px;">' + l.status + ' FAIL</span>';
          }

          const methodFmt = '<span style="font-family:var(--mono); color:var(--accent);">' + l.method + '</span>';
          
          const toolContent = l.tool !== '—' 
            ? '<span style="font-family:var(--mono); background:var(--bg2); padding:3px 6px; border-radius:4px; border:1px solid var(--border); font-size:11px; display:inline-block; max-width: 250px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; vertical-align: bottom;">' + l.tool + '</span>' 
            : '<span style="color:var(--text3)">—</span>';

          return `
            <tr style="border-bottom: 1px solid var(--border);">
              <td style="padding: 12px; color:var(--text3); font-size:12px; white-space: nowrap;">${l.time}</td>
              <td style="padding: 12px;">${methodFmt}</td>
              <td class="primary" style="padding: 12px; font-weight:500;">${l.client}</td>
              <td style="padding: 12px;">${toolContent}</td>
              <td style="padding: 12px; text-align: right;">${statusBadge}</td>
            </tr>
          `;
        }).join('')}
      </tbody>
    </table>
  `;
}

// ── Modals & Actions ──────────────────────────────────────────────
let _srvPreviewed = false;

function openAddServerModal() {
  ['srv-name','srv-url','srv-desc','srv-key'].forEach(id => document.getElementById(id).value='');
  document.getElementById('srv-step1').style.display = '';
  document.getElementById('srv-preview').style.display = 'none';
  document.getElementById('srv-main-btn').textContent = 'Test connection →';
  document.getElementById('srv-back-btn').style.display = 'none';
  _srvPreviewed = false;
  openModal('modal-server');
}

function srvBack() {
  document.getElementById('srv-step1').style.display = '';
  document.getElementById('srv-preview').style.display = 'none';
  document.getElementById('srv-main-btn').textContent = 'Test connection →';
  document.getElementById('srv-back-btn').style.display = 'none';
  _srvPreviewed = false;
}

async function srvMainAction() {
  if (!_srvPreviewed) { await previewServer(); }
  else                { await saveServer(); }
}

async function previewServer() {
  const name = document.getElementById('srv-name').value.trim();
  const url  = document.getElementById('srv-url').value.trim();
  const key  = document.getElementById('srv-key').value.trim();
  if (!name || !url) { alert('Name and URL are required.'); return; }

  const btn = document.getElementById('srv-main-btn');
  btn.textContent = 'Connecting…'; btn.disabled = true;

  try {
    const preview = await POST('/admin/servers/preview', {
      name, url, description: '', upstream_key: key
    });

    document.getElementById('srv-step1').style.display = 'none';
    document.getElementById('srv-preview').style.display = '';
    document.getElementById('srv-back-btn').style.display = '';

    const statusEl = document.getElementById('srv-preview-status');
    if (preview.reachable) {
      const si = preview.server_info || {};
      statusEl.style.background = 'var(--accent-dim)';
      statusEl.style.border = '1px solid rgba(0,229,160,0.25)';
      statusEl.innerHTML = `<span style="color:var(--accent);font-weight:500">● Connected</span>
        &nbsp;·&nbsp; <span style="color:var(--text2)">${si.name||name} ${si.version||''}</span>
        &nbsp;·&nbsp; <span style="color:var(--text3);font-family:var(--mono);font-size:11px">MCP ${preview.protocol_version}</span>`;
      btn.textContent = 'Register server';
      _srvPreviewed = true;

      document.getElementById('tc').textContent = preview.tool_count;
      document.getElementById('rc').textContent = preview.resource_count;
      document.getElementById('pc').textContent = preview.prompt_count;
      document.getElementById('srv-preview-tabs').style.display = '';

      document.getElementById('srv-tab-tools').innerHTML = preview.tools.length
        ? preview.tools.map(t => `<div class="preview-item">
            <div class="preview-item-name">${t.name}</div>
            <div class="preview-item-desc">${t.description||'—'}</div>
          </div>`).join('')
        : '<div style="padding:12px;color:var(--text3);font-size:12px">No tools reported</div>';

      document.getElementById('srv-tab-resources').innerHTML = preview.resources.length
        ? preview.resources.map(r => `<div class="preview-item">
            <div class="preview-item-name">${r.name||r.uri}</div>
            <div class="preview-item-desc">${r.uri}</div>
          </div>`).join('')
        : '<div style="padding:12px;color:var(--text3);font-size:12px">No resources reported</div>';

      document.getElementById('srv-tab-prompts').innerHTML = preview.prompts.length
        ? preview.prompts.map(p => `<div class="preview-item">
            <div class="preview-item-name">${p.name}</div>
            <div class="preview-item-desc">${p.description||'—'}</div>
          </div>`).join('')
        : '<div style="padding:12px;color:var(--text3);font-size:12px">No prompts reported</div>';

    } else {
      statusEl.style.background = 'var(--red-dim)';
      statusEl.style.border = '1px solid rgba(255,94,94,0.2)';
      statusEl.innerHTML = `<span style="color:var(--red);font-weight:500">✕ Unreachable</span>
        &nbsp;—&nbsp; <span style="color:var(--text3)">${preview.error}</span>
        <div style="margin-top:6px;font-size:11px;color:var(--text3)">You can still register this server and refresh it once it comes online.</div>`;
      document.getElementById('srv-preview-tabs').style.display = 'none';
      btn.textContent = 'Register anyway';
      _srvPreviewed = true;
    }
  } catch(e) {
    showToast('Preview failed: ' + e.message);
  } finally {
    btn.disabled = false;
  }
}

function showSrvTab(tab) {
  document.querySelectorAll('.srv-tab').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
  document.querySelectorAll('.srv-tab-pane').forEach(p => p.style.display = 'none');
  document.getElementById('srv-tab-' + tab).style.display = '';
}

async function saveServer() {
  const name = document.getElementById('srv-name').value.trim();
  const url  = document.getElementById('srv-url').value.trim();
  const desc = document.getElementById('srv-desc').value.trim();
  const key  = document.getElementById('srv-key').value.trim();
  const btn  = document.getElementById('srv-main-btn');
  btn.textContent = 'Registering…'; btn.disabled = true;
  try {
    await POST('/admin/servers', { name, url, description: desc, upstream_key: key });
    closeModal('modal-server');
    showToast('Server "' + name + '" registered');
    await loadAll();
  } catch(e) { showToast('Error: ' + e.message); } 
  finally { btn.disabled = false; }
}

async function refreshServer(id) {
  const s = state.servers.find(s=>s.id===id);
  showToast(`Refreshing "${s.name}"…`);
  try {
    await POST(`/admin/servers/${id}/refresh`, {});
    showToast(`"${s.name}" refreshed`);
    await loadAll();
  } catch(e) { showToast('Refresh failed: ' + e.message); }
}

async function deleteServer(id) {
  const s = state.servers.find(s=>s.id===id);
  if (!confirm(`Remove server "${s.name}"?`)) return;
  try {
    await DEL(`/admin/servers/${id}`);
    showToast('Server removed');
    await loadAll();
  } catch(e) { showToast('Error: ' + e.message); }
}

function openAddClientModal() {
  ['cli-name','cli-desc','cli-key'].forEach(id => document.getElementById(id).value='');
  openModal('modal-client');
}

/**
 * Cryptographically Secure API Key Generation
 * Replaces insecure Math.random() with Web Crypto API.
 */
function generateKey() {
  const chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
  const randomValues = new Uint32Array(32); // 32 characters long
  window.crypto.getRandomValues(randomValues);
  
  let key = '';
  for (let i = 0; i < randomValues.length; i++) {
    key += chars[randomValues[i] % chars.length];
  }
  
  // Format as sk-mcp-[32-char crypto random string]
  document.getElementById('cli-key').value = 'sk-mcp-' + key;
}

async function saveClient() {
  const name = document.getElementById('cli-name').value.trim();
  const desc = document.getElementById('cli-desc').value.trim();
  const key  = document.getElementById('cli-key').value.trim();
  if (!name || !key) { alert('Name and API key are required.'); return; }
  try {
    await POST('/admin/clients', { name, description: desc, api_key: key });
    closeModal('modal-client');
    showToast('Client "' + name + '" added');
    await loadAll();
  } catch(e) { showToast('Error: ' + e.message); }
}

async function deleteClient(id) {
  const c = state.clients.find(c=>c.id===id);
  if (!confirm(`Remove client "${c.name}"?`)) return;
  try {
    await DEL(`/admin/clients/${id}`);
    showToast('Client removed');
    await loadAll();
  } catch(e) { showToast('Error: ' + e.message); }
}

async function editPermsFor(cid) {
  showPage('permissions');
  const sel = document.getElementById('perm-client-select');
  sel.value = cid;
  await renderPermMatrix();
}

function showKey(cid) {
  const c = state.clients.find(c=>c.id===cid);
  document.getElementById('key-modal-client').textContent = c.name;
  document.getElementById('key-modal-value').textContent = c.key;
  openModal('modal-key');
}

function copyKey() {
  const val = document.getElementById('key-modal-value').textContent;
  navigator.clipboard.writeText(val).catch(()=>{});
  showToast('Key copied to clipboard');
}

function openModal(id) { document.getElementById(id).classList.add('open'); }
function closeModal(id) { document.getElementById(id).classList.remove('open'); }
document.querySelectorAll('.modal-overlay').forEach(el => {
  el.addEventListener('click', e => { if (e.target===el) el.classList.remove('open'); });
});

async function clearLogs() {
  await DEL('/admin/logs').catch(()=>{});
  state.logs = [];
  renderLogs();
}

async function exportConfig() {
  try {
    const cfg = await GET('/admin/export');
    const blob = new Blob([JSON.stringify(cfg,null,2)], {type:'application/json'});
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'mcp-gateway-config.json';
    a.click();
    showToast('Config exported');
  } catch(e) { showToast('Export failed: ' + e.message); }
}

function showToast(msg) {
  const t = document.createElement('div');
  t.textContent = msg;
  Object.assign(t.style, {
    position:'fixed', bottom:'24px', right:'24px', background:'var(--accent)',
    color:'#000', padding:'10px 18px', borderRadius:'8px', fontSize:'13px',
    fontFamily:'var(--sans)', fontWeight:'500', zIndex:'999',
    animation:'slideUp 0.2s ease', boxShadow:'0 4px 16px rgba(0,229,160,0.3)'
  });
  document.body.appendChild(t);
  setTimeout(()=>t.remove(), 2800);
}

async function loadStats() {
  try {
    const s = await GET('/admin/stats');
    document.getElementById('stat-servers').textContent = s.servers;
    document.getElementById('stat-clients').textContent = s.clients;
    document.getElementById('stat-tools').textContent   = s.tools;
    document.getElementById('stat-perms').textContent   = s.permissions;
  } catch(_) {}
}

loadAll();