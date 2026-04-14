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
      resource_templates: (s.resource_templates || []),
      prompts:   (s.prompts   || []),
    }));
    state.clients = clients.map(c => ({
      ...c,
      desc: c.description,
      key:  c.api_key,
      perms: {}
    }));
    state.logs = logs.map(l => ({
      time:   new Date(l.timestamp).toLocaleTimeString(),
      method: l.method,
      client: l.client_name,
      tool:   l.tool,
      status: l.status
    }));
    
    // Load permissions for all clients
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
    // convert { serverId: [tool,...] } → { serverId: Set }
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
  const resourceCount = state.servers.reduce((sum, s) => sum + (s.resources ? s.resources.length : 0), 0);
  const promptCount = state.servers.reduce((sum, s) => sum + (s.prompts ? s.prompts.length : 0), 0);
  txt.textContent = `v1.0.0 • ${toolCount} tools • ${resourceCount} resources • ${promptCount} prompts`;
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

function renderStats() {
  loadStats();
}

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
  el.innerHTML = `<table><thead><tr><th>Client</th><th>Servers</th><th>Tool permissions</th><th>Key</th></tr></thead><tbody>
    ${state.clients.map(c => {
      const srvCount = c.perms ? Object.keys(c.perms).length : 0;
      const toolCount = c.perms ? Object.values(c.perms).reduce((a,s) => a+s.size,0) : 0;
      return `<tr>
        <td class="primary">${c.name}</td>
        <td><span class="badge badge-blue">${srvCount} server${srvCount!==1?'s':''}</span></td>
        <td><span class="badge badge-amber">${toolCount} tool${toolCount!==1?'s':''}</span></td>
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
      const caps = s.capabilities || {};
      const toolCount = (s.tools||[]).length;
      const resCount  = (s.resources||[]).length;
      const tplCount  = (s.resource_templates||[]).length;
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
          <span class="badge badge-blue" style="margin-right:4px" title="Resource templates"># ${tplCount}</span>
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
          <span class="badge badge-amber">${toolCount} tool${toolCount!==1?'s':''}</span>
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

  // Load from API
  await loadClientPerms(clientId);

  const client = state.clients.find(c => c.id === clientId);
  wrap.innerHTML = '<div class="perm-matrix">' +
    state.servers.map(s => {
      const hasServer = !!(state.pendingPerms[clientId] && state.pendingPerms[clientId][s.id]);
      return `<div class="perm-server-row${hasServer?' open':''}" id="psr-${clientId}-${s.id}">
        <div class="perm-server-header" onclick="toggleServerRow('${clientId}','${s.id}')">
          <div class="perm-server-name">
            <span style="font-family:var(--mono);color:var(--accent)">${s.name}</span>
            <span style="color:var(--text3);font-size:12px">${s.desc||''}</span>
            <span class="badge badge-gray">${s.tools.length} tools</span>
          </div>
          <div class="perm-server-access">
            <label class="toggle" onclick="event.stopPropagation()">
              <input type="checkbox" ${hasServer?'checked':''} onchange="toggleServerAccess('${clientId}','${s.id}',this.checked)">
              <span class="toggle-slider"></span>
            </label>
            <span class="perm-chevron">▶</span>
          </div>
        </div>
        <div class="perm-tools-panel">
          <div style="font-size:11px;color:var(--text3);margin-bottom:8px;font-family:var(--mono)">Select which tools this client can call:</div>
          <div class="perm-tools-grid">
            ${s.tools.map(t => {
              const allowed = state.pendingPerms[clientId] && state.pendingPerms[clientId][s.id]?.has(t);
              return `<div class="perm-tool-item${allowed?' allowed':''}" id="pt-${clientId}-${s.id}-${t.replace(/[^a-z0-9]/gi,'_')}" onclick="toggleTool('${clientId}','${s.id}','${t}')">
                <div class="perm-tool-checkbox">${allowed?'✓':''}</div>
                <div class="perm-tool-name">${t.split('__')[1]||t}</div>
              </div>`;
            }).join('')}
          </div>
          <div style="margin-top:10px;display:flex;gap:8px;">
            <button class="btn btn-ghost btn-sm" onclick="selectAllTools('${clientId}','${s.id}')">Select all</button>
            <button class="btn btn-ghost btn-sm" onclick="clearAllTools('${clientId}','${s.id}')">Clear all</button>
          </div>
        </div>
      </div>`;
    }).join('') + '</div>';
}

function toggleServerRow(cid, sid) {
  const row = document.getElementById(`psr-${cid}-${sid}`);
  row.classList.toggle('open');
}

function toggleServerAccess(cid, sid, checked) {
  if (!state.pendingPerms[cid]) state.pendingPerms[cid] = {};
  if (checked) {
    if (!state.pendingPerms[cid][sid]) state.pendingPerms[cid][sid] = new Set();
    document.getElementById(`psr-${cid}-${sid}`).classList.add('open');
  } else {
    delete state.pendingPerms[cid][sid];
    // uncheck all tools visually
    const server = state.servers.find(s=>s.id===sid);
    server.tools.forEach(t => {
      const el = document.getElementById(`pt-${cid}-${sid}-${t.replace(/[^a-z0-9]/gi,'_')}`);
      if (el) { el.classList.remove('allowed'); el.querySelector('.perm-tool-checkbox').textContent=''; }
    });
  }
}

function toggleTool(cid, sid, tool) {
  if (!state.pendingPerms[cid]) state.pendingPerms[cid] = {};
  if (!state.pendingPerms[cid][sid]) state.pendingPerms[cid][sid] = new Set();
  const set = state.pendingPerms[cid][sid];
  const el = document.getElementById(`pt-${cid}-${sid}-${tool.replace(/[^a-z0-9]/gi,'_')}`);
  if (set.has(tool)) { set.delete(tool); el.classList.remove('allowed'); el.querySelector('.perm-tool-checkbox').textContent=''; }
  else { set.add(tool); el.classList.add('allowed'); el.querySelector('.perm-tool-checkbox').textContent='✓'; }
  // ensure server toggle is on
  const toggle = document.querySelector(`#psr-${cid}-${sid} input[type=checkbox]`);
  if (toggle && set.size > 0) toggle.checked = true;
}

function selectAllTools(cid, sid) {
  const server = state.servers.find(s=>s.id===sid);
  if (!state.pendingPerms[cid]) state.pendingPerms[cid] = {};
  state.pendingPerms[cid][sid] = new Set(server.tools);
  server.tools.forEach(t => {
    const el = document.getElementById(`pt-${cid}-${sid}-${t.replace(/[^a-z0-9]/gi,'_')}`);
    if (el) { el.classList.add('allowed'); el.querySelector('.perm-tool-checkbox').textContent='✓'; }
  });
  const toggle = document.querySelector(`#psr-${cid}-${sid} input[type=checkbox]`);
  if (toggle) toggle.checked = true;
}

function clearAllTools(cid, sid) {
  const server = state.servers.find(s=>s.id===sid);
  if (state.pendingPerms[cid]) delete state.pendingPerms[cid][sid];
  server.tools.forEach(t => {
    const el = document.getElementById(`pt-${cid}-${sid}-${t.replace(/[^a-z0-9]/gi,'_')}`);
    if (el) { el.classList.remove('allowed'); el.querySelector('.perm-tool-checkbox').textContent=''; }
  });
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

function renderLogs() {
  const el = document.getElementById('logs-wrap');
  if (!state.logs.length) { el.innerHTML = '<div class="empty-state"><div class="empty-icon">≡</div>No activity yet</div>'; return; }
  el.innerHTML = state.logs.map(l => `
    <div class="log-entry">
      <div class="log-time">${l.time}</div>
      <div class="log-method">${l.method}</div>
      <div class="log-desc"><span style="color:var(--text)">${l.client}</span> — ${l.tool}</div>
      <div class="log-status ${l.status===200?'badge-green':l.status===403?'badge-red':'badge-amber'}" style="font-family:var(--mono);font-size:11px;color:${l.status===200?'var(--accent)':l.status===403?'var(--red)':'var(--amber)'}">${l.status}</div>
    </div>`).join('');
}

// ── Server modal ──
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

      // populate tabs
      document.getElementById('tc').textContent = preview.tool_count;
      document.getElementById('rc').textContent = preview.resource_count;
      document.getElementById('rtc').textContent = preview.resource_template_count;
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
            <div class="preview-item-name">${r.title || r.name || r.uri}</div>
            <div class="preview-item-desc">${r.description || 'Resource exposed by upstream server'}</div>
            <div class="preview-item-meta">${r.uri}</div>
          </div>`).join('')
        : '<div style="padding:12px;color:var(--text3);font-size:12px">No resources reported</div>';

      document.getElementById('srv-tab-templates').innerHTML = preview.resource_templates.length
        ? preview.resource_templates.map(t => `<div class="preview-item">
            <div class="preview-item-name">${t.title || t.name || t.uriTemplate}</div>
            <div class="preview-item-desc">${t.description || 'Template for generating resource URIs'}</div>
            <div class="preview-item-meta">${t.uriTemplate}</div>
          </div>`).join('')
        : '<div style="padding:12px;color:var(--text3);font-size:12px">No resource templates reported</div>';

      document.getElementById('srv-tab-prompts').innerHTML = preview.prompts.length
        ? preview.prompts.map(p => `<div class="preview-item">
            <div class="preview-item-name">${p.title || p.name}</div>
            <div class="preview-item-desc">${p.description||'—'}</div>
            <div class="preview-item-meta">${p.arguments?.length ? `${p.arguments.length} argument(s)` : 'No arguments'}</div>
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
  } catch(e) {
    showToast('Error: ' + e.message);
  } finally {
    btn.disabled = false;
  }
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

// ── Client modal ──
function openAddClientModal() {
  ['cli-name','cli-desc','cli-key'].forEach(id => document.getElementById(id).value='');
  openModal('modal-client');
}

function generateKey() {
  const chars = 'abcdefghijklmnopqrstuvwxyz0123456789';
  const seg = n => Array.from({length:n},()=>chars[Math.floor(Math.random()*chars.length)]).join('');
  document.getElementById('cli-key').value = `key-${seg(6)}-${seg(6)}`;
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

// ── Key modal ──
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

// ── Modal helpers ──
function openModal(id) { document.getElementById(id).classList.add('open'); }
function closeModal(id) { document.getElementById(id).classList.remove('open'); }
document.querySelectorAll('.modal-overlay').forEach(el => {
  el.addEventListener('click', e => { if (e.target===el) el.classList.remove('open'); });
});

// ── Misc ──
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

// ── Stats ────────────────────────────────────────────────────────
async function loadStats() {
  try {
    const s = await GET('/admin/stats');
    document.getElementById('stat-servers').textContent = s.servers;
    document.getElementById('stat-clients').textContent = s.clients;
    document.getElementById('stat-tools').textContent   = s.tools;
    document.getElementById('stat-perms').textContent   = s.permissions;
    // update stat bar labels if resource/prompt counts exist
    const toolCard = document.getElementById('stat-tools').closest('.stat-card');
    if (toolCard) {
      const extra = document.getElementById('stat-extra');
      if (!extra && (s.resources > 0 || s.prompts > 0)) {
        toolCard.innerHTML += `<div style="font-family:var(--mono);font-size:10px;color:var(--text3);margin-top:4px">${s.resources} resources · ${s.prompts} prompts</div>`;
        toolCard.id = 'stat-extra';
      }
    }
  } catch(_) {}
}

// ── Init ──
loadAll();
