// ── Inject CSS Fixes dynamically for overflows and section titles ───────
const style = document.createElement('style');
style.innerHTML = `
  .perm-tool-item { overflow: hidden; white-space: nowrap; text-overflow: ellipsis; display: flex; align-items: center; max-width: 100%; cursor: pointer; }
  .perm-tool-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; }
  .perm-section-title { font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--text3); margin: 12px 0 6px 0; font-family: var(--mono); border-bottom: 1px solid var(--border); padding-bottom: 2px; }
  
  .preview-item { background: var(--bg2); border: 1px solid var(--border); border-radius: 6px; padding: 10px; margin-bottom: 8px; }
  .preview-item-name { font-family: var(--mono); color: var(--accent); font-size: 13px; font-weight: 500; }
  .preview-item-desc { color: var(--text3); font-size: 12px; margin-top: 4px; }
`;
document.head.appendChild(style);

// ── Config ──────────────────────────────────────────────────────
const API_BASE = '';          
const ADMIN_KEY = 'admin-secret-change-me';

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

const GET = (path) => api('GET', path);
const POST = (path, body) => api('POST', path, body);
const PUT = (path, body) => api('PUT', path, body);
const PATCH = (path, body) => api('PATCH', path, body);
const DEL = (path) => api('DELETE', path);

// ── Load all data from backend ───────────────────────────────────
async function loadAll() {
  try {
    const [servers, clients, logs] = await Promise.all([
      GET('/admin/servers'), GET('/admin/clients'), GET('/admin/logs?limit=50'),
    ]);
    state.servers = servers.map(s => ({
      ...s, desc: s.description,
      tools: s.tools || [], 
      resources: [...(s.resources || []), ...(s.resource_templates || [])], 
      prompts: s.prompts || [],
    }));
    state.clients = clients.map(c => ({
      ...c, desc: c.description, key: c.api_key, perms: {}
    }));
    state.logs = logs.map(l => ({
      time: new Date(l.timestamp).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit' }),
      method: l.method, client: l.client_name, tool: l.tool, status: l.status
    }));

    for (const client of state.clients) {
      try {
        const data = await GET(`/admin/clients/${client.id}/permissions`);
        const perms = {};
        for (const [sid, tools] of Object.entries(data.permissions || {})) perms[sid] = new Set(tools);
        client.perms = perms;
      } catch (e) { }
    }

    renderAll();
    updateGatewayStatus();
    initPlaygroundServers(); 
  } catch (e) {
    showToast('Backend error: ' + e.message);
  }
}

async function loadClientPerms(clientId) {
  try {
    const data = await GET(`/admin/clients/${clientId}/permissions`);
    const perms = {};
    for (const [sid, tools] of Object.entries(data.permissions || {})) perms[sid] = new Set(tools);
    state.pendingPerms[clientId] = perms;
    return perms;
  } catch (e) { return {}; }
}

// ── Navigation ──────────────────────────────────────────────────
const pageMeta = {
  dashboard: { title: 'Dashboard', crumb: 'gateway / overview', action: '+ Add server', fn: 'openAddServerModal' },
  playground: { title: 'Playground', crumb: 'gateway / playground', action: null },
  servers: { title: 'MCP Servers', crumb: 'gateway / servers', action: '+ Register server', fn: 'openAddServerModal' },
  clients: { title: 'Clients', crumb: 'gateway / clients', action: '+ Add client', fn: 'openAddClientModal' },
  permissions: { title: 'Permissions', crumb: 'gateway / permissions', action: null },
  logs: { title: 'Activity Log', crumb: 'gateway / logs', action: null },
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
  if (meta.action) { btn.textContent = meta.action; btn.style.display = ''; } else btn.style.display = 'none';
  renderAll();
}

function updateGatewayStatus() {
  const txt = document.getElementById('footer-status');
  if (!txt) return;
  const itemCount = state.servers.reduce((sum, s) => sum + (s.tools ? s.tools.length : 0) + (s.resources ? s.resources.length : 0) + (s.prompts ? s.prompts.length : 0), 0);
  txt.textContent = `v1.0.0 • ${itemCount} capabilities registered`;
}

function topbarAction() {
  const active = document.querySelector('.page.active');
  if (active.id === 'page-dashboard' || active.id === 'page-servers') openAddServerModal();
  else if (active.id === 'page-clients') openAddClientModal();
}

function renderAll() {
  loadStats();
  renderDashServers(); renderDashClients();
  renderServersTable(); renderClientsTable();
  renderPermClientSelect(); renderLogs();
}

function serverStatusBadge(s) {
  return s.status === 'online' ? '<span class="badge badge-green">● online</span>' : '<span class="badge badge-red">● offline</span>';
}

function renderDashServers() {
  const el = document.getElementById('dash-servers-table');
  if (!state.servers.length) { el.innerHTML = '<div class="empty-state"><div class="empty-icon">◈</div>No servers registered yet</div>'; return; }
  el.innerHTML = `<table><thead><tr><th>Name</th><th>URL</th><th>Tools</th><th>Status</th></tr></thead><tbody>
    ${state.servers.map(s => `<tr><td class="primary">${s.name}</td><td><span style="font-family:var(--mono);font-size:12px;color:var(--text3)">${s.url}/mcp</span></td><td><span class="badge badge-blue">${(s.tools || []).length} tools</span></td><td>${serverStatusBadge(s)}</td></tr>`).join('')}
  </tbody></table>`;
}

function renderDashClients() {
  const el = document.getElementById('dash-clients-table');
  if (!state.clients.length) { el.innerHTML = '<div class="empty-state"><div class="empty-icon">◉</div>No clients registered yet</div>'; return; }
  el.innerHTML = `<table><thead><tr><th>Client</th><th>Servers</th><th>Capabilities</th><th>Key</th></tr></thead><tbody>
    ${state.clients.map(c => {
    const srvCount = c.perms ? Object.keys(c.perms).length : 0;
    const toolCount = c.perms ? Object.values(c.perms).reduce((a, s) => a + s.size, 0) : 0;
    return `<tr><td class="primary">${c.name}</td><td><span class="badge badge-blue">${srvCount} servers</span></td><td><span class="badge badge-amber">${toolCount} items</span></td><td><button class="btn btn-ghost btn-sm" onclick="showKey('${c.id}')">⊙ view key</button></td></tr>`;
  }).join('')}
  </tbody></table>`;
}

function renderServersTable() {
  const el = document.getElementById('servers-table');
  if (!state.servers.length) { el.innerHTML = '<div class="empty-state"><div class="empty-icon">◈</div>No servers yet</div>'; return; }
  el.innerHTML = `<table><thead><tr><th>Name</th><th>Endpoint</th><th>Server info</th><th>Discovered</th><th>Status</th><th></th></tr></thead><tbody>
    ${state.servers.map(s => {
    const si = s.server_info || {};
    
    // Check if this is a Virtual Swagger API
    const isVirtual = s.url.includes('/virtual/');
    const apiId = isVirtual ? s.url.split('/virtual/')[1] : '';
    const authBtn = isVirtual ? `<button class="btn btn-ghost btn-sm" onclick="openUpdateAuthModal('${apiId}')" title="Update Token">🔑</button>` : '';

    return `<tr>
        <td class="primary"><span style="font-family:var(--mono)">${s.name}</span></td>
        <td><span style="font-family:var(--mono);font-size:11px;color:var(--text3)">${s.url}/mcp</span></td>
        <td>${si.name ? `<span style="font-size:12px;color:var(--text2)">${si.name} ${si.version || ''}</span>` : '—'}</td>
        <td>
          <span class="badge badge-blue" style="margin-right:4px" title="Tools">⚙ ${(s.tools || []).length}</span>
          <span class="badge badge-amber" style="margin-right:4px" title="Resources">◎ ${(s.resources || []).length}</span>
          <span class="badge badge-gray" title="Prompts">✦ ${(s.prompts || []).length}</span>
        </td>
        <td>${serverStatusBadge(s)}</td>
        <td style="display:flex;gap:6px">
          ${authBtn}
          <button class="btn btn-ghost btn-sm" onclick="refreshServer('${s.id}')" title="Refresh">↻</button>
          <button class="btn btn-danger btn-sm" onclick="deleteServer('${s.id}')">✕</button>
        </td>
      </tr>`;
  }).join('')}
  </tbody></table>`;
}

function renderClientsTable() {
  const el = document.getElementById('clients-table');
  if (!state.clients.length) { el.innerHTML = '<div class="empty-state"><div class="empty-icon">◉</div>No clients yet</div>'; return; }
  el.innerHTML = `<table><thead><tr><th>Name</th><th>Description</th><th>Permissions</th><th>API key</th><th></th></tr></thead><tbody>
    ${state.clients.map(c => {
    const toolCount = c.perms ? Object.values(c.perms).reduce((a, s) => a + s.size, 0) : 0;
    const srvCount = c.perms ? Object.keys(c.perms).length : 0;
    return `<tr><td class="primary">${c.name}</td><td style="color:var(--text3)">${c.desc || '—'}</td><td><span class="badge badge-blue" style="margin-right:4px">${srvCount} servers</span><span class="badge badge-amber">${toolCount} items</span></td><td><button class="btn btn-ghost btn-sm" onclick="showKey('${c.id}')">⊙ view key</button></td><td style="display:flex;gap:6px"><button class="btn btn-ghost btn-sm" onclick="editPermsFor('${c.id}')">◫ perms</button><button class="btn btn-danger btn-sm" onclick="deleteClient('${c.id}')">✕</button></td></tr>`;
  }).join('')}
  </tbody></table>`;
}

function renderPermClientSelect() {
  const sel = document.getElementById('perm-client-select');
  const cur = sel.value;
  sel.innerHTML = '<option value="">— choose a client —</option>' + state.clients.map(c => `<option value="${c.id}">${c.name}</option>`).join('');
  if (cur) sel.value = cur;
}

// ── PLAYGROUND LOGIC ────────────────────────────────────────

let _pgSelectedData = null; 

function initPlaygroundServers() {
  const srvSel = document.getElementById('pg-server');
  srvSel.innerHTML = '<option value="">— Select a Server —</option>' +
    state.servers.map(s => `<option value="${s.id}">${s.name}</option>`).join('');
}

function pgUpdateTypes() {
  const sid = document.getElementById('pg-server').value;
  const typeSel = document.getElementById('pg-type');
  const itemSel = document.getElementById('pg-item');
  const cliSel = document.getElementById('pg-client');

  itemSel.innerHTML = '<option value="">—</option>'; itemSel.disabled = true;
  cliSel.innerHTML = '<option value="">— Select an authorized client —</option>'; cliSel.disabled = true;
  document.getElementById('pg-dynamic-inputs').innerHTML = '';
  document.getElementById('pg-execute-btn').disabled = true;
  document.getElementById('pg-output').textContent = 'Awaiting configuration...';
  document.getElementById('pg-status-badge').innerHTML = '';

  if (!sid) { typeSel.disabled = true; typeSel.innerHTML = '<option value="">—</option>'; return; }

  const server = state.servers.find(s => s.id === sid);
  typeSel.disabled = false;

  let html = '<option value="">— Select Type —</option>';
  if (server.tools && server.tools.length > 0) html += '<option value="tools">⚙ Tools</option>';
  if (server.resources && server.resources.length > 0) html += '<option value="resources">◎ Resources</option>';
  if (server.prompts && server.prompts.length > 0) html += '<option value="prompts">✦ Prompts</option>';

  typeSel.innerHTML = html || '<option value="">(No capabilities found)</option>';
}

function pgUpdateItems() {
  const sid = document.getElementById('pg-server').value;
  const type = document.getElementById('pg-type').value;
  const itemSel = document.getElementById('pg-item');
  const server = state.servers.find(s => s.id === sid);

  document.getElementById('pg-dynamic-inputs').innerHTML = '';
  document.getElementById('pg-execute-btn').disabled = true;
  document.getElementById('pg-client').innerHTML = '<option value="">— Select an authorized client —</option>';
  document.getElementById('pg-client').disabled = true;

  if (!type || !server) { itemSel.disabled = true; itemSel.innerHTML = '<option value="">—</option>'; return; }

  itemSel.disabled = false;
  let items = server[type] || [];

  itemSel.innerHTML = '<option value="">— Select Item —</option>' + items.map((item, idx) => {
    const name = item.name || item.uriTemplate || item.uri;
    const cleanName = name.includes('__') ? name.split('__')[1] : name;
    return `<option value="${idx}">${cleanName}</option>`;
  }).join('');
}

function pgUpdateInputs() {
  const sid = document.getElementById('pg-server').value;
  const type = document.getElementById('pg-type').value;
  const itemIdx = document.getElementById('pg-item').value;
  const inputsDiv = document.getElementById('pg-dynamic-inputs');
  const cliSel = document.getElementById('pg-client');
  const execBtn = document.getElementById('pg-execute-btn');
  const server = state.servers.find(s => s.id === sid);

  inputsDiv.innerHTML = '';
  _pgSelectedData = null;

  if (itemIdx === "") {
    cliSel.disabled = true; execBtn.disabled = true;
    cliSel.innerHTML = '<option value="">— Select an authorized client —</option>';
    return;
  }

  const item = server[type][itemIdx];
  _pgSelectedData = item;
  const fullItemName = item.name || item.uriTemplate || item.uri;

  // Build Inputs based on Type
  if (type === 'tools') {
    let schemaStr = '{}';
    let hintHtml = '<span style="color:var(--text3)">No schema constraints.</span>';
    if (item.input_schema && Object.keys(item.input_schema).length > 0) {
      hintHtml = `<span style="color:var(--accent);">Expected Schema:</span><br/>${JSON.stringify(item.input_schema, null, 2)}`;
      const mock = {};
      const req = item.input_schema.required || [];
      req.forEach(k => { mock[k] = item.input_schema.properties[k]?.type === 'number' ? 0 : ""; });
      if (Object.keys(item.input_schema.properties || {}).length > 0 && req.length === 0) {
        Object.keys(item.input_schema.properties).forEach(k => { mock[k] = ""; });
      }
      if (Object.keys(mock).length > 0) schemaStr = JSON.stringify(mock, null, 2);
    }
    inputsDiv.innerHTML = `
      <div class="form-group" style="margin:0;">
        <label class="form-label">Tool Arguments (JSON)</label>
        <textarea class="form-input" id="pg-args-json" style="font-family:var(--mono); height:120px; resize:vertical;">${schemaStr}</textarea>
        <div class="form-hint" style="max-height:80px; overflow-y:auto;"><pre style="margin:0;font-family:var(--mono);font-size:11px;">${hintHtml}</pre></div>
      </div>
    `;
  }
  else if (type === 'prompts') {
    let argsHtml = '';
    if (item.arguments && item.arguments.length > 0) {
      argsHtml = item.arguments.map(arg => `
        <div class="form-group" style="margin-bottom:8px;">
          <label class="form-label">${arg.name} ${arg.required ? '<span style="color:var(--red)">*</span>' : ''}</label>
          <input type="text" class="form-input pg-prompt-arg" data-argname="${arg.name}" placeholder="${arg.description || ''}">
        </div>
      `).join('');
    } else {
      argsHtml = '<div style="color:var(--text3); font-size:12px;">This prompt takes no arguments.</div>';
    }
    inputsDiv.innerHTML = `
      <div style="background:rgba(255,255,255,0.02); padding:12px; border-radius:6px; border:1px solid var(--border);">
        <div style="font-size:12px; font-weight:500; margin-bottom:12px; color:var(--text2)">Prompt Arguments</div>
        ${argsHtml}
      </div>
    `;
  }
  else if (type === 'resources') {
    const defaultUri = item.uriTemplate || item.uri || '';
    inputsDiv.innerHTML = `
      <div class="form-group" style="margin:0;">
        <label class="form-label">Resource URI</label>
        <input type="text" class="form-input" id="pg-res-uri" value="${defaultUri}">
        <div class="form-hint">${item.description || 'Edit the URI to resolve templates (e.g., replacing {slug} with a real value).'}</div>
      </div>
    `;
  }

  // Filter Clients based on Permission
  const allowedClients = state.clients.filter(c => c.perms && c.perms[sid] && c.perms[sid].has(fullItemName));

  if (allowedClients.length === 0) {
    cliSel.innerHTML = '<option value="">(No clients have permission!)</option>';
    cliSel.disabled = true; execBtn.disabled = true;
  } else {
    cliSel.innerHTML = allowedClients.map(c => `<option value="${c.key}">${c.name}</option>`).join('');
    cliSel.disabled = false; execBtn.disabled = false;
  }
}

async function executePlayground() {
  const type = document.getElementById('pg-type').value;
  const apiKey = document.getElementById('pg-client').value;
  const outEl = document.getElementById('pg-output');
  const badgeEl = document.getElementById('pg-status-badge');
  const btn = document.getElementById('pg-execute-btn');

  if (!apiKey || !_pgSelectedData) return;

  const fullItemName = _pgSelectedData.name || _pgSelectedData.uriTemplate || _pgSelectedData.uri;
  let payload = { jsonrpc: "2.0", id: Date.now() };

  // Build Payload based on Type
  if (type === 'tools') {
    const argsRaw = document.getElementById('pg-args-json').value.trim();
    let args = {};
    if (argsRaw) {
      try { args = JSON.parse(argsRaw); }
      catch (e) { outEl.style.color = 'var(--red)'; outEl.textContent = 'Invalid JSON:\n' + e.message; return; }
    }
    payload.method = "tools/call";
    payload.params = { name: fullItemName, arguments: args };
  }
  else if (type === 'prompts') {
    let args = {};
    document.querySelectorAll('.pg-prompt-arg').forEach(el => {
      if (el.value.trim()) args[el.dataset.argname] = el.value.trim();
    });
    payload.method = "prompts/get";
    payload.params = { name: fullItemName, arguments: args };
  }
  else if (type === 'resources') {
    payload.method = "resources/read";
    const actualUri = document.getElementById('pg-res-uri').value.trim();
    payload.params = { uri: actualUri };
  }

  outEl.style.color = '#a6accd';
  outEl.textContent = 'Executing...\n\nPayload sent to gateway:\n' + JSON.stringify(payload, null, 2) + '\n\nWaiting for upstream server...';
  badgeEl.innerHTML = '<span class="badge badge-amber" style="animation: pulse 1.5s infinite;">Running</span>';
  btn.disabled = true;

  try {
    const res = await fetch(API_BASE + '/mcp', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Api-Key': apiKey },
      body: JSON.stringify(payload)
    });

    const data = await res.json();

    if (!res.ok || data.error) {
      outEl.style.color = 'var(--red)';
      outEl.textContent = JSON.stringify(data.error || data, null, 2);
      badgeEl.innerHTML = '<span class="badge badge-red">Error</span>';
    } else {
      outEl.style.color = '#00e5a0';
      badgeEl.innerHTML = '<span class="badge badge-green">Success</span>';

      let outputStr = '';
      if (type === 'tools' && data.result?.content) {
        outputStr = data.result.content.map(c => c.text || JSON.stringify(c)).join('\n\n');
      }
      else if (type === 'prompts' && data.result?.messages) {
        outputStr = data.result.messages.map(m => `[${m.role.toUpperCase()}]\n${m.content.text || m.content.type}`).join('\n\n---\n\n');
      }
      else if (type === 'resources' && data.result?.contents) {
        outputStr = data.result.contents.map(c => `URI: ${c.uri}\nMIME: ${c.mimeType}\n\n${c.text || c.blob}`).join('\n\n');
      }
      else {
        outputStr = JSON.stringify(data.result, null, 2);
      }

      outEl.textContent = outputStr || "(Empty success response)";
    }
  } catch (e) {
    outEl.style.color = 'var(--red)';
    outEl.textContent = 'Network error: ' + e.message;
    badgeEl.innerHTML = '<span class="badge badge-red">Failed</span>';
  } finally {
    btn.disabled = false;
  }
}

// ── PERMISSIONS MATRIX LOGIC ──────────────────────────────────────

async function renderPermMatrix() {
  const clientId = document.getElementById('perm-client-select').value;
  const wrap = document.getElementById('perm-matrix-wrap');
  const saveBtn = document.getElementById('save-perm-btn');

  if (!clientId) {
    wrap.innerHTML = '<div class="empty-state" style="background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius);"><div class="empty-icon">◫</div>Select a client</div>';
    saveBtn.style.display = 'none'; return;
  }

  saveBtn.style.display = '';
  wrap.innerHTML = '<div class="empty-state">Loading...</div>';

  await loadClientPerms(clientId);

  wrap.innerHTML = '<div class="perm-matrix">' +
    state.servers.map(s => {
      const hasAccess = !!(state.pendingPerms[clientId] && state.pendingPerms[clientId][s.id]);

      const renderSection = (title, items, icon) => {
        if (!items || items.length === 0) return '';
        return `<div class="perm-section-title">${icon} ${title}</div>
          <div class="perm-tools-grid">
            ${items.map(item => {
          const name = item.name || item.uriTemplate || item.uri;
          const idName = String(name).replace(/[^a-z0-9]/gi, '_');
          const allowed = state.pendingPerms[clientId]?.[s.id]?.has(name);
          return `<div class="perm-tool-item${allowed ? ' allowed' : ''}" id="pt-${clientId}-${s.id}-${idName}" title="${name}" onclick="toggleCapability('${clientId}','${s.id}','${name}')">
                  <div class="perm-tool-checkbox">${allowed ? '✓' : ''}</div>
                  <div class="perm-tool-name">${name.includes('__') ? name.split('__')[1] : name}</div>
                </div>`;
        }).join('')}
          </div>`;
      };

      return `<div class="perm-server-row${hasAccess ? ' open' : ''}" id="psr-${clientId}-${s.id}">
        <div class="perm-server-header" onclick="toggleServerRow('${clientId}','${s.id}')">
          <div class="perm-server-name">
            <span style="font-family:var(--mono);color:var(--accent)">${s.name}</span>
            <span class="badge badge-gray">${(s.tools || []).length}T · ${(s.resources || []).length}R · ${(s.prompts || []).length}P</span>
          </div>
          <div class="perm-server-access">
            <label class="toggle" onclick="event.stopPropagation()">
              <input type="checkbox" ${hasAccess ? 'checked' : ''} onchange="toggleServerAccess('${clientId}','${s.id}',this.checked)">
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

function toggleServerRow(cid, sid) { const r = document.getElementById(`psr-${cid}-${sid}`); if (r) r.classList.toggle('open'); }
function toggleServerAccess(cid, sid, checked) {
  if (!state.pendingPerms[cid]) state.pendingPerms[cid] = {};
  if (checked) {
    if (!state.pendingPerms[cid][sid]) state.pendingPerms[cid][sid] = new Set();
    document.getElementById(`psr-${cid}-${sid}`).classList.add('open');
  } else {
    delete state.pendingPerms[cid][sid];
    const s = state.servers.find(srv => srv.id === sid);
    if (!s) return;
    [...(s.tools || []).map(t => t.name || t), ...(s.resources || []).map(r => r.uriTemplate || r.uri || r.name), ...(s.prompts || []).map(p => p.name)].forEach(name => {
      const el = document.getElementById(`pt-${cid}-${sid}-${String(name).replace(/[^a-z0-9]/gi, '_')}`);
      if (el) { el.classList.remove('allowed'); el.querySelector('.perm-tool-checkbox').textContent = ''; }
    });
  }
}
function toggleCapability(cid, sid, name) {
  if (!state.pendingPerms[cid]) state.pendingPerms[cid] = {};
  if (!state.pendingPerms[cid][sid]) state.pendingPerms[cid][sid] = new Set();
  const set = state.pendingPerms[cid][sid];
  const el = document.getElementById(`pt-${cid}-${sid}-${String(name).replace(/[^a-z0-9]/gi, '_')}`);
  if (set.has(name)) { set.delete(name); if (el) { el.classList.remove('allowed'); el.querySelector('.perm-tool-checkbox').textContent = ''; } }
  else { set.add(name); if (el) { el.classList.add('allowed'); el.querySelector('.perm-tool-checkbox').textContent = '✓'; } }
  const t = document.querySelector(`#psr-${cid}-${sid} input[type=checkbox]`); if (t) t.checked = (set.size > 0);
}
function selectAllCaps(cid, sid) {
  const s = state.servers.find(srv => srv.id === sid);
  if (!s) return;
  if (!state.pendingPerms[cid]) state.pendingPerms[cid] = {};
  const all = [...(s.tools || []).map(t => t.name || t), ...(s.resources || []).map(r => r.uriTemplate || r.uri || r.name), ...(s.prompts || []).map(p => p.name)];
  state.pendingPerms[cid][sid] = new Set(all);
  all.forEach(name => {
    const el = document.getElementById(`pt-${cid}-${sid}-${String(name).replace(/[^a-z0-9]/gi, '_')}`);
    if (el) { el.classList.add('allowed'); el.querySelector('.perm-tool-checkbox').textContent = '✓'; }
  });
  const t = document.querySelector(`#psr-${cid}-${sid} input[type=checkbox]`); if (t) t.checked = true;
}
function clearAllCaps(cid, sid) {
  if (state.pendingPerms[cid]) delete state.pendingPerms[cid][sid];
  const s = state.servers.find(srv => srv.id === sid);
  if (!s) return;
  [...(s.tools || []).map(t => t.name || t), ...(s.resources || []).map(r => r.uriTemplate || r.uri || r.name), ...(s.prompts || []).map(p => p.name)].forEach(name => {
    const el = document.getElementById(`pt-${cid}-${sid}-${String(name).replace(/[^a-z0-9]/gi, '_')}`);
    if (el) { el.classList.remove('allowed'); el.querySelector('.perm-tool-checkbox').textContent = ''; }
  });
  const t = document.querySelector(`#psr-${cid}-${sid} input[type=checkbox]`); if (t) t.checked = false;
}
async function savePermissions() {
  const cid = document.getElementById('perm-client-select').value;
  if (!cid) return;
  const p = state.pendingPerms[cid] || {}; const permissions = {};
  Object.entries(p).forEach(([sid, tools]) => { if (tools.size > 0) permissions[sid] = [...tools]; });
  try { await PUT(`/admin/clients/${cid}/permissions`, { permissions }); showToast('Permissions saved'); await loadStats(); } catch (e) { showToast('Error: ' + e.message); }
}

// ── LOGS ──────────────────────────────────
function renderLogs() {
  const el = document.getElementById('logs-wrap');
  if (!state.logs.length) { el.innerHTML = '<div class="empty-state"><div class="empty-icon">≡</div>No activity yet</div>'; return; }
  el.innerHTML = `<table style="width: 100%; border-collapse: collapse;"><thead><tr style="text-align: left; border-bottom: 1px solid var(--border); color: var(--text3);"><th style="padding: 12px; width: 180px;">Timestamp</th><th style="padding: 12px;">Method Called</th><th style="padding: 12px;">Client Name</th><th style="padding: 12px;">Target / Resource</th><th style="padding: 12px; text-align: right;">Status</th></tr></thead><tbody>
    ${state.logs.map(l => {
    let b = l.status >= 200 && l.status < 300 ? `<span class="badge badge-green" style="font-family:var(--mono); font-size: 11px;">${l.status} OK</span>` : l.status >= 400 && l.status < 500 ? `<span class="badge badge-amber" style="font-family:var(--mono); font-size: 11px;">${l.status} ERR</span>` : `<span class="badge badge-red" style="font-family:var(--mono); font-size: 11px;">${l.status} FAIL</span>`;
    const t = l.tool !== '—' ? `<span style="font-family:var(--mono); background:var(--bg2); padding:3px 6px; border-radius:4px; border:1px solid var(--border); font-size:11px; display:inline-block; max-width: 250px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; vertical-align: bottom;">${l.tool}</span>` : `<span style="color:var(--text3)">—</span>`;
    return `<tr style="border-bottom: 1px solid var(--border);"><td style="padding: 12px; color:var(--text3); font-size:12px; white-space: nowrap;">${l.time}</td><td style="padding: 12px;"><span style="font-family:var(--mono); color:var(--accent);">${l.method}</span></td><td class="primary" style="padding: 12px; font-weight:500;">${l.client}</td><td style="padding: 12px;">${t}</td><td style="padding: 12px; text-align: right;">${b}</td></tr>`;
  }).join('')}
  </tbody></table>`;
}

// ── Standard MCP Server Modals & Actions ──────────────────────────────────────────────
let _srvPreviewed = false;

function openAddServerModal() {
  ['srv-name', 'srv-url', 'srv-desc', 'srv-key'].forEach(id => document.getElementById(id).value = '');
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
  const url = document.getElementById('srv-url').value.trim();
  const key = document.getElementById('srv-key').value.trim();
  
  if (!name || !url) { alert('Name and URL required.'); return; }
  
  const btn = document.getElementById('srv-main-btn');
  btn.textContent = 'Connecting…'; btn.disabled = true;
  
  try {
    const preview = await POST('/admin/servers/preview', { name, url, description: '', upstream_key: key });
    
    document.getElementById('srv-step1').style.display = 'none';
    document.getElementById('srv-preview').style.display = '';
    document.getElementById('srv-back-btn').style.display = '';
    
    const statusEl = document.getElementById('srv-preview-status');
    
    if (preview.reachable) {
      statusEl.style.background = 'var(--accent-dim)';
      statusEl.style.border = '1px solid rgba(0,229,160,0.25)';
      statusEl.innerHTML = `<span style="color:var(--accent);font-weight:500">● Connected</span> &nbsp;·&nbsp; <span style="color:var(--text3);font-family:var(--mono);font-size:11px">MCP ${preview.protocol_version}</span>`;
      btn.textContent = 'Register server';
      _srvPreviewed = true;

      let tabsWrap = document.getElementById('srv-preview-tabs');
      if (!tabsWrap) {
        tabsWrap = document.createElement('div');
        tabsWrap.id = 'srv-preview-tabs';
        tabsWrap.innerHTML = `
          <div style="display:flex;gap:6px;margin-bottom:12px;" id="srv-tab-bar">
            <button class="btn btn-ghost btn-sm srv-tab active" onclick="showSrvTab('tools')" data-tab="tools">Tools <span id="tc" class="badge badge-blue" style="margin-left:4px"></span></button>
            <button class="btn btn-ghost btn-sm srv-tab" onclick="showSrvTab('resources')" data-tab="resources">Resources <span id="rc" class="badge badge-amber" style="margin-left:4px"></span></button>
            <button class="btn btn-ghost btn-sm srv-tab" onclick="showSrvTab('prompts')" data-tab="prompts">Prompts <span id="pc" class="badge badge-gray" style="margin-left:4px"></span></button>
          </div>
          <div id="srv-tab-tools" class="srv-tab-pane" style="max-height:220px;overflow-y:auto;"></div>
          <div id="srv-tab-resources" class="srv-tab-pane" style="display:none;max-height:220px;overflow-y:auto;"></div>
          <div id="srv-tab-prompts" class="srv-tab-pane" style="display:none;max-height:220px;overflow-y:auto;"></div>
        `;
        document.getElementById('srv-preview').appendChild(tabsWrap);
      }
      
      tabsWrap.style.display = '';
      
      document.getElementById('tc').textContent = preview.tool_count;
      document.getElementById('rc').textContent = preview.resource_count + (preview.resource_template_count || 0);
      document.getElementById('pc').textContent = preview.prompt_count;

      document.getElementById('srv-tab-tools').innerHTML = preview.tools.length
        ? preview.tools.map(t => `<div class="preview-item"><div class="preview-item-name">${t.name}</div><div class="preview-item-desc">${t.description||'—'}</div></div>`).join('')
        : '<div style="padding:12px;color:var(--text3);font-size:12px">No tools reported</div>';

      const allResources = [...(preview.resources || []), ...(preview.resource_templates || [])];
      document.getElementById('srv-tab-resources').innerHTML = allResources.length
        ? allResources.map(r => `<div class="preview-item"><div class="preview-item-name">${r.name||r.uriTemplate||r.uri}</div><div class="preview-item-desc">${r.uriTemplate||r.uri}</div></div>`).join('')
        : '<div style="padding:12px;color:var(--text3);font-size:12px">No resources reported</div>';

      document.getElementById('srv-tab-prompts').innerHTML = preview.prompts.length
        ? preview.prompts.map(p => `<div class="preview-item"><div class="preview-item-name">${p.name}</div><div class="preview-item-desc">${p.description||'—'}</div></div>`).join('')
        : '<div style="padding:12px;color:var(--text3);font-size:12px">No prompts reported</div>';

    } else {
      statusEl.style.background = 'var(--red-dim)';
      statusEl.style.border = '1px solid rgba(255,94,94,0.2)';
      statusEl.innerHTML = `<span style="color:var(--red);font-weight:500">✕ Unreachable</span><br><span style="color:var(--text3)">${preview.error}</span>`;
      btn.textContent = 'Register anyway';
      _srvPreviewed = true;
      
      const tabsWrap = document.getElementById('srv-preview-tabs');
      if (tabsWrap) tabsWrap.style.display = 'none';
    }
  } catch (e) { 
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
  const name = document.getElementById('srv-name').value.trim(); const url = document.getElementById('srv-url').value.trim(); const desc = document.getElementById('srv-desc').value.trim(); const key = document.getElementById('srv-key').value.trim();
  const btn = document.getElementById('srv-main-btn'); btn.textContent = 'Registering…'; btn.disabled = true;
  try { await POST('/admin/servers', { name, url, description: desc, upstream_key: key }); closeModal('modal-server'); showToast('Server registered'); await loadAll(); } catch (e) { showToast('Error: ' + e.message); } finally { btn.disabled = false; }
}

async function refreshServer(id) { showToast(`Refreshing...`); try { await POST(`/admin/servers/${id}/refresh`, {}); showToast(`Refreshed`); await loadAll(); } catch (e) { showToast('Failed: ' + e.message); } }
async function deleteServer(id) { if (!confirm(`Remove server?`)) return; try { await DEL(`/admin/servers/${id}`); showToast('Removed'); await loadAll(); } catch (e) { showToast('Error: ' + e.message); } }

// ── SWAGGER / REST API CONVERSION ───────────────────────────────────────────

let _swgPreviewed = false;

function toggleSwgSource() {
  const source = document.querySelector('input[name="swg-source"]:checked').value;
  if (source === 'url') {
    document.getElementById('swg-url-container').style.display = '';
    document.getElementById('swg-file-container').style.display = 'none';
  } else {
    document.getElementById('swg-url-container').style.display = 'none';
    document.getElementById('swg-file-container').style.display = '';
  }
}

function openAddSwaggerModal() {
  ['swg-name', 'swg-url', 'swg-headers'].forEach(id => document.getElementById(id).value = '');
  document.getElementById('swg-file').value = '';
  document.querySelector('input[name="swg-source"][value="url"]').checked = true;
  toggleSwgSource();
  
  document.getElementById('swg-step1').style.display = '';
  document.getElementById('swg-preview').style.display = 'none';
  document.getElementById('swg-back-btn').style.display = 'none';
  document.getElementById('swg-main-btn').textContent = 'Preview API →';
  document.getElementById('swg-main-btn').disabled = false;
  _swgPreviewed = false;
  
  openModal('modal-swagger');
}

function swgBack() {
  document.getElementById('swg-step1').style.display = '';
  document.getElementById('swg-preview').style.display = 'none';
  document.getElementById('swg-main-btn').textContent = 'Preview API →';
  document.getElementById('swg-back-btn').style.display = 'none';
  _swgPreviewed = false;
}

async function swgMainAction() {
  if (!_swgPreviewed) { await previewSwagger(); }
  else { await saveSwagger(); }
}

function showSwgTab(tab) {
  document.querySelectorAll('.swg-tab').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
  document.querySelectorAll('.swg-tab-pane').forEach(p => p.style.display = 'none');
  document.getElementById('swg-tab-' + tab).style.display = '';
}

async function previewSwagger() {
  const name = document.getElementById('swg-name').value.trim();
  const source = document.querySelector('input[name="swg-source"]:checked').value;
  
  if (!name) { alert('Server Name is required.'); return; }
  
  let payload = { api_id: name };
  
  if (source === 'url') {
     const url = document.getElementById('swg-url').value.trim();
     if(!url) { alert("Swagger URL is required."); return; }
     payload.swagger_url = url;
  } else {
     const fileInput = document.getElementById('swg-file');
     if(!fileInput.files.length) { alert("Please select a JSON file to upload."); return; }
     
     const text = await fileInput.files[0].text();
     try { payload.swagger_json = JSON.parse(text); }
     catch(e) { alert("Invalid JSON file uploaded."); return; }
  }

  const btn = document.getElementById('swg-main-btn');
  btn.textContent = 'Parsing Schema...'; btn.disabled = true;

  try {
    const previewRes = await fetch(API_BASE + '/admin/swagger/preview', {
      method: 'POST',
      headers: headers(), 
      body: JSON.stringify(payload)
    });
    
    if (!previewRes.ok) {
      const err = await previewRes.text();
      throw new Error(err);
    }

    const preview = await previewRes.json();
    
    document.getElementById('swg-step1').style.display = 'none';
    document.getElementById('swg-preview').style.display = '';
    document.getElementById('swg-back-btn').style.display = '';
    
    const statusEl = document.getElementById('swg-preview-status');
    
    if (preview.reachable) {
      statusEl.style.background = 'var(--accent-dim)';
      statusEl.style.border = '1px solid rgba(0,229,160,0.25)';
      statusEl.innerHTML = `<span style="color:var(--accent);font-weight:500">● Schema Parsed</span> &nbsp;·&nbsp; <span style="color:var(--text3);font-family:var(--mono);font-size:11px">${preview.protocol_version}</span>`;
      btn.textContent = 'Register API';
      _swgPreviewed = true;

      let tabsWrap = document.getElementById('swg-preview-tabs');
      if (!tabsWrap) {
        tabsWrap = document.createElement('div');
        tabsWrap.id = 'swg-preview-tabs';
        tabsWrap.innerHTML = `
          <div style="display:flex;gap:6px;margin-bottom:12px;" id="swg-tab-bar">
            <button class="btn btn-ghost btn-sm swg-tab active" onclick="showSwgTab('tools')" data-tab="tools">Tools <span id="swg-tc" class="badge badge-blue" style="margin-left:4px"></span></button>
          </div>
          <div id="swg-tab-tools" class="swg-tab-pane" style="max-height:220px;overflow-y:auto;"></div>
        `;
        document.getElementById('swg-preview').appendChild(tabsWrap);
      }
      
      tabsWrap.style.display = '';
      document.getElementById('swg-tc').textContent = preview.tool_count;

      document.getElementById('swg-tab-tools').innerHTML = preview.tools.length
        ? preview.tools.map(t => `<div class="preview-item"><div class="preview-item-name">${t.name}</div><div class="preview-item-desc">${t.description||'—'}</div></div>`).join('')
        : '<div style="padding:12px;color:var(--text3);font-size:12px">No tools discovered</div>';
        
    } else {
      statusEl.style.background = 'var(--red-dim)';
      statusEl.style.border = '1px solid rgba(255,94,94,0.2)';
      statusEl.innerHTML = `<span style="color:var(--red);font-weight:500">✕ Parsing Failed</span><br><span style="color:var(--text3)">${preview.error}</span>`;
      btn.textContent = 'Try Again';
      _swgPreviewed = false; // keep false so they can retry
      
      const tabsWrap = document.getElementById('swg-preview-tabs');
      if (tabsWrap) tabsWrap.style.display = 'none';
    }

  } catch (e) {
    showToast('Preview failed: ' + e.message);
  } finally {
    btn.disabled = false;
  }
}

async function saveSwagger() {
  const name = document.getElementById('swg-name').value.trim();
  const source = document.querySelector('input[name="swg-source"]:checked').value;
  const headersRaw = document.getElementById('swg-headers').value.trim();
  
  let payload = { api_id: name, headers: {} };
  
  if (headersRaw) {
    try { payload.headers = JSON.parse(headersRaw); } 
    catch(e) { alert("Invalid JSON format in the Custom Headers field."); return; }
  }

  if (source === 'url') {
     payload.swagger_url = document.getElementById('swg-url').value.trim();
  } else {
     const fileInput = document.getElementById('swg-file');
     const text = await fileInput.files[0].text();
     payload.swagger_json = JSON.parse(text);
  }

  const btn = document.getElementById('swg-main-btn');
  btn.textContent = 'Registering...'; btn.disabled = true;

  try {
    const adapterRes = await fetch(API_BASE + '/admin/swagger/generate', {
      method: 'POST',
      headers: headers(), 
      body: JSON.stringify(payload)
    });

    if (!adapterRes.ok) {
      const err = await adapterRes.text();
      throw new Error(err);
    }

    const adapterData = await adapterRes.json();
    const virtualMcpUrl = adapterData.base_url; 

    // Register into SQLite
    await POST('/admin/servers', {
      name: name,
      url: virtualMcpUrl,
      description: `Auto-converted REST API`,
      upstream_key: ''
    });

    // Force instant handshake sync
    const allServers = await GET('/admin/servers');
    const newServer = allServers.find(s => s.name === name);
    if (newServer) {
        await POST(`/admin/servers/${newServer.id}/refresh`, {});
    }

    closeModal('modal-swagger');
    showToast(`API ${name} registered successfully!`);
    await loadAll();

  } catch (e) {
    showToast('Failed to register API: ' + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Register API';
  }
}

// ── AUTO-REFRESH / HOT-SWAP TOKEN LOGIC ──────────────────────────────────────

function openUpdateAuthModal(apiId) {
  document.getElementById('auth-api-id').value = apiId;
  document.getElementById('auth-headers-json').value = '{\n  "Authorization": "Bearer CURRENT_TOKEN"\n}';
  
  ['ar-url', 'ar-payload', 'ar-key', 'ar-header', 'ar-prefix', 'ar-expiry'].forEach(id => {
      document.getElementById(id).value = '';
  });
  
  openModal('modal-update-auth');
}

async function submitUpdateAuth() {
  const apiId = document.getElementById('auth-api-id').value;
  const headersRaw = document.getElementById('auth-headers-json').value.trim();
  
  let payload = { headers: {} };
  
  if (headersRaw) {
    try { payload.headers = JSON.parse(headersRaw); } 
    catch(e) { alert("Invalid JSON format in the Static Headers field."); return; }
  }

  const arUrl = document.getElementById('ar-url').value.trim();
  if (arUrl) {
    let arPayload = {};
    try { 
      const rawPayload = document.getElementById('ar-payload').value.trim();
      if (rawPayload) arPayload = JSON.parse(rawPayload); 
    } catch(e) { 
      alert("Invalid JSON in Auto-Refresh Credentials Payload"); return; 
    }

    payload.auto_refresh = {
      endpoint: arUrl,
      payload: arPayload,
      extract_key: document.getElementById('ar-key').value.trim() || 'access_token',
      header_name: document.getElementById('ar-header').value.trim() || 'Authorization',
      header_prefix: document.getElementById('ar-prefix').value || '',
      expiry_seconds: parseInt(document.getElementById('ar-expiry').value) || 3600,
      last_fetched: 0 
    };
  } else {
    payload.auto_refresh = null; 
  }

  try {
    await PATCH(`/admin/swagger/${apiId}/headers`, payload);
    closeModal('modal-update-auth');
    showToast('Authentication updated successfully!');
  } catch (e) {
    showToast('Failed to update headers: ' + e.message);
  }
}

function openAddClientModal() { ['cli-name', 'cli-desc', 'cli-key'].forEach(id => document.getElementById(id).value = ''); openModal('modal-client'); }
function generateKey() { const uuid = window.crypto.randomUUID(); document.getElementById('cli-key').value = 'sk-mcp-' + uuid.replace(/-/g, ''); }
async function saveClient() {
  const name = document.getElementById('cli-name').value.trim(); const desc = document.getElementById('cli-desc').value.trim(); const key = document.getElementById('cli-key').value.trim();
  if (!name || !key) return;
  try { await POST('/admin/clients', { name, description: desc, api_key: key }); closeModal('modal-client'); showToast('Client added'); await loadAll(); } catch (e) { showToast('Error: ' + e.message); }
}

async function deleteClient(id) { if (!confirm(`Remove client?`)) return; try { await DEL(`/admin/clients/${id}`); showToast('Removed'); await loadAll(); } catch (e) { showToast('Error: ' + e.message); } }
async function editPermsFor(cid) { showPage('permissions'); document.getElementById('perm-client-select').value = cid; await renderPermMatrix(); }
function showKey(cid) { const c = state.clients.find(c => c.id === cid); document.getElementById('key-modal-client').textContent = c.name; document.getElementById('key-modal-value').textContent = c.key; openModal('modal-key'); }
function copyKey() { navigator.clipboard.writeText(document.getElementById('key-modal-value').textContent).catch(() => { }); showToast('Copied to clipboard'); }

function openModal(id) { document.getElementById(id).classList.add('open'); }
function closeModal(id) { document.getElementById(id).classList.remove('open'); }
document.querySelectorAll('.modal-overlay').forEach(el => { el.addEventListener('click', e => { if (e.target === el) el.classList.remove('open'); }); });

async function clearLogs() { await DEL('/admin/logs').catch(() => { }); state.logs = []; renderLogs(); }

async function exportConfig() { 
  try { 
    const cfg = await GET('/admin/export'); 
    const a = document.createElement('a'); 
    a.href = URL.createObjectURL(new Blob([JSON.stringify(cfg, null, 2)], { type: 'application/json' })); 
    a.download = 'mcp-config.json'; 
    a.click(); 
  } catch (e) { showToast('Export failed'); } 
}

function showToast(msg) { 
  const t = document.createElement('div'); 
  t.textContent = msg; 
  Object.assign(t.style, { position: 'fixed', bottom: '24px', right: '24px', background: 'var(--accent)', color: '#000', padding: '10px 18px', borderRadius: '8px', fontSize: '13px', fontFamily: 'var(--sans)', fontWeight: '500', zIndex: '999', animation: 'slideUp 0.2s ease', boxShadow: '0 4px 16px rgba(0,229,160,0.3)' }); 
  document.body.appendChild(t); 
  setTimeout(() => t.remove(), 2800); 
}

async function loadStats() { 
  try { 
    const s = await GET('/admin/stats'); 
    document.getElementById('stat-servers').textContent = s.servers; 
    document.getElementById('stat-clients').textContent = s.clients; 
    document.getElementById('stat-tools').textContent = s.tools; 
    document.getElementById('stat-perms').textContent = s.permissions; 
  } catch (_) { } 
}

// ── BACKGROUND POLLING ──────────────────────────────────────

async function silentBackgroundRefresh() {
  try {
    const logs = await GET('/admin/logs?limit=50');
    state.logs = logs.map(l => ({
      time: new Date(l.timestamp).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit' }),
      method: l.method, client: l.client_name, tool: l.tool, status: l.status
    }));
    
    if (document.getElementById('page-logs').classList.contains('active')) {
      renderLogs();
    }

    await loadStats();

    const servers = await GET('/admin/servers');
    let srvChanged = false;
    servers.forEach(newSrv => {
      const oldSrv = state.servers.find(s => s.id === newSrv.id);
      if (oldSrv && oldSrv.status !== newSrv.status) {
        oldSrv.status = newSrv.status; 
        srvChanged = true;
      }
    });

    if (srvChanged) {
      if (document.getElementById('page-dashboard').classList.contains('active')) renderDashServers();
      if (document.getElementById('page-servers').classList.contains('active')) renderServersTable();
    }

    const clients = await GET('/admin/clients');
    const updatedClients = await Promise.all(clients.map(async c => {
      try {
        const data = await GET(`/admin/clients/${c.id}/permissions`);
        const perms = {};
        for (const [sid, tools] of Object.entries(data.permissions || {})) perms[sid] = new Set(tools);
        return { ...c, desc: c.description, key: c.api_key, perms };
      } catch (e) {
        return { ...c, desc: c.description, key: c.api_key, perms: {} };
      }
    }));
    
    state.clients = updatedClients;

    if (document.getElementById('page-dashboard').classList.contains('active')) renderDashClients();
    if (document.getElementById('page-clients').classList.contains('active')) renderClientsTable();
    
  } catch (e) {}
}

setInterval(silentBackgroundRefresh, 3000);

loadAll();