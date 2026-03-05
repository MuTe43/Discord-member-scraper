const API = '';
let token = sessionStorage.getItem('sl_token') || '';
let validatedUser = null;
let userGuilds = [];
let selectedGuildId = null;
let servers = {};
let activeServer = null;
let activeMember = null;
let searchVal = '';
let sortBy = 'name';
let filterTag = 'all';

// ── Init ──────────────────────────────────────────────────────────────────────
window.onload = async () => {
    if (token) {
        document.getElementById('tokenInput').value = token;
        await validateToken(true);
    }
    await loadServers();
};

// ── Token ─────────────────────────────────────────────────────────────────────
function toggleTokenVisibility() {
    const input = document.getElementById('tokenInput');
    input.type = input.type === 'password' ? 'text' : 'password';
    document.getElementById('tokenToggle').textContent = input.type === 'password' ? '👁' : '🔒';
}

async function validateToken(silent = false) {
    token = document.getElementById('tokenInput').value.trim();
    if (!token) { showStatus('error', 'Paste a token first.'); return; }
    const btn = document.getElementById('validateBtn');
    btn.innerHTML = '<span class="spinner"></span>';
    btn.disabled = true;
    try {
        const res = await fetch(`${API}/api/validate-token`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ token })
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Invalid token');
        validatedUser = data.user;
        userGuilds = data.guilds;
        sessionStorage.setItem('sl_token', token);
        const name = data.user.global_name || data.user.username;
        const avatarHtml = data.user.avatar
            ? `<img src="https://cdn.discordapp.com/avatars/${data.user.id}/${data.user.avatar}.png?size=32" alt="">`
            : name.slice(0, 2).toUpperCase();
        document.getElementById('userChip').className = 'user-chip';
        document.getElementById('userChip').innerHTML = `<div class="user-chip-avatar">${avatarHtml}</div><div class="user-chip-name">${escHtml(name)}</div>`;
        showStatus('ok', '✓ connected');
        document.getElementById('scrapeBtn').disabled = false;
        if (!silent) toast('success', `Connected as ${name}`);
    } catch (e) {
        showStatus('error', e.message);
        document.getElementById('userChip').className = 'hidden';
        document.getElementById('scrapeBtn').disabled = true;
        validatedUser = null;
        if (!silent) toast('error', e.message);
    } finally {
        btn.innerHTML = 'Connect';
        btn.disabled = false;
    }
}

function showStatus(type, msg) {
    const el = document.getElementById('tokenStatus');
    el.className = `token-status ${type}`;
    el.textContent = msg;
}

// ── Load servers ──────────────────────────────────────────────────────────────
async function loadServers() {
    try {
        const res = await fetch(`${API}/api/servers`);
        servers = await res.json();
        render();
    } catch (e) {
        toast('error', 'Cannot reach backend. Is it running on port 8000?');
    }
}

// ── Scrape modal ──────────────────────────────────────────────────────────────
function openScrapeModal() {
    selectedGuildId = null;
    document.getElementById('manualGuildId').value = '';
    document.getElementById('scrapeProgress').classList.add('hidden');
    document.getElementById('guildPickerSection').classList.remove('hidden');
    document.getElementById('startScrapeBtn').disabled = false;
    document.getElementById('startScrapeBtn').innerHTML = 'Scrape';

    const list = document.getElementById('guildList');
    if (!userGuilds.length) {
        list.innerHTML = '<div style="text-align:center;padding:16px;color:var(--muted);font-size:12px;font-family:JetBrains Mono,monospace">No servers found.</div>';
    } else {
        list.innerHTML = userGuilds.map(g => {
            const iconUrl = g.icon ? `https://cdn.discordapp.com/icons/${g.id}/${g.icon}.png?size=64` : null;
            const iconHtml = iconUrl ? `<img src="${iconUrl}" alt="">` : `<span>${g.name.charAt(0)}</span>`;
            const scraped = servers[g.id] ? ' <span style="font-size:10px;color:var(--muted);font-weight:400">(scraped)</span>' : '';
            return `<div class="guild-item" onclick="selectGuild('${g.id}')" id="guild_${g.id}">
        <div class="guild-item-icon">${iconHtml}</div>
        <div class="guild-item-name">${escHtml(g.name)}${scraped}</div>
        <div class="guild-item-check">✓</div>
      </div>`;
        }).join('');
    }
    document.getElementById('scrapeModal').classList.remove('hidden');
}

function selectGuild(id) {
    document.querySelectorAll('.guild-item').forEach(el => el.classList.remove('selected'));
    document.getElementById(`guild_${id}`)?.classList.add('selected');
    selectedGuildId = id;
    document.getElementById('manualGuildId').value = '';
}

async function startScrape() {
    const guildId = selectedGuildId || document.getElementById('manualGuildId').value.trim();
    if (!guildId) { toast('error', 'Select a server or enter a Guild ID.'); return; }
    document.getElementById('guildPickerSection').classList.add('hidden');
    document.getElementById('scrapeProgress').classList.remove('hidden');
    document.getElementById('startScrapeBtn').disabled = true;
    const logEl = document.getElementById('scrapeProgressText');

    try {
        await new Promise((resolve, reject) => {
            fetch(`${API}/api/scrape`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ token, guild_id: guildId })
            }).then(res => {
                if (!res.ok) return res.json().then(d => reject(new Error(d.detail || 'Scrape failed')));
                const reader = res.body.getReader();
                const decoder = new TextDecoder();
                let buf = '';
                let result = null;
                function read() {
                    reader.read().then(({ done, value }) => {
                        if (done) { resolve(result); return; }
                        buf += decoder.decode(value, { stream: true });
                        const lines = buf.split('\n');
                        buf = lines.pop();
                        for (const line of lines) {
                            if (!line.startsWith('data: ')) continue;
                            try {
                                const evt = JSON.parse(line.slice(6));
                                if (evt.type === 'progress') logEl.textContent = evt.text;
                                else if (evt.type === 'done') { logEl.textContent = `✓ ${evt.scraped} members scraped`; result = evt; }
                                else if (evt.type === 'error') reject(new Error(evt.detail));
                            } catch (e) { }
                        }
                        read();
                    }).catch(reject);
                }
                read();
            }).catch(reject);
        }).then(async evt => {
            if (!evt) return;
            toast('success', `Scraped ${evt.scraped} members from ${evt.name}`);
            closeModal('scrapeModal');
            await loadServers();
            activeServer = guildId; activeMember = null; searchVal = '';
            render();
            setTimeout(() => document.getElementById('memberPanel')?.scrollIntoView({ behavior: 'smooth', block: 'nearest' }), 100);
        });
    } catch (e) {
        toast('error', e.message);
        document.getElementById('guildPickerSection').classList.remove('hidden');
        document.getElementById('scrapeProgress').classList.add('hidden');
        document.getElementById('startScrapeBtn').disabled = false;
        document.getElementById('startScrapeBtn').innerHTML = 'Scrape';
    }
}

// ── Render ────────────────────────────────────────────────────────────────────
function render() {
    renderServers();
    const panel = document.getElementById('memberPanel');
    if (activeServer && servers[activeServer]) {
        panel.classList.remove('hidden');
        renderPanel();
    } else {
        panel.classList.add('hidden');
    }
}

function renderServers() {
    const grid = document.getElementById('serversGrid');
    const ids = Object.keys(servers);
    document.getElementById('emptyState').classList.toggle('hidden', ids.length > 0);
    grid.querySelectorAll('.server-card').forEach(e => e.remove());
    ids.forEach(id => {
        const s = servers[id];
        const iconHtml = s.icon
            ? `<img src="https://cdn.discordapp.com/icons/${id}/${s.icon}.png?size=64" alt="" onerror="this.style.display='none'">`
            : `<div class="server-icon-text">${escHtml(s.name.charAt(0))}</div>`;
        const count = s.member_count ?? Object.keys(s.members || {}).length;
        const card = document.createElement('div');
        card.className = 'server-card' + (activeServer === id ? ' active' : '');
        card.innerHTML = `
      <div class="card-header">
        <div class="server-icon">${iconHtml}</div>
        <div class="card-info">
          <div class="card-name">${escHtml(s.name)}</div>
          <div class="card-meta">${count} member${count !== 1 ? 's' : ''}</div>
        </div>
        <div class="card-actions">
          <button class="icon-btn rescrape-btn" title="Re-scrape" onclick="rescrapeServer(event,'${id}')">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>
          </button>
          <button class="icon-btn danger" title="Delete" onclick="deleteServer(event,'${id}')">
            <svg width="11" height="11" viewBox="0 0 12 12"><path d="M1 1l10 10M11 1L1 11" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>
          </button>
        </div>
      </div>`;
        card.addEventListener('click', () => toggleServer(id));
        grid.appendChild(card);
    });
}

// ── helpers to build HTML chunks ─────────────────────────────────────────────
function getFilteredMembers(s) {
    const allMembers = Object.values(s.members || {});
    const q = searchVal.toLowerCase().replace(/^@/, '');
    let filtered = q
        ? allMembers.filter(m => (m.name || '').toLowerCase().includes(q) || (m.username || '').toLowerCase().includes(q))
        : allMembers;

    // Tag / role filter
    if (filterTag === 'tagged') {
        filtered = filtered.filter(m => (m.quirks || []).length > 0);
    } else if (filterTag === 'untagged') {
        filtered = filtered.filter(m => (m.quirks || []).length === 0);
    } else if (filterTag.startsWith('role:')) {
        const roleId = filterTag.slice(5);
        filtered = filtered.filter(m => (m.roles || []).includes(roleId));
    }

    // Sort
    if (sortBy === 'name') {
        filtered.sort((a, b) => (a.name || a.username || '').localeCompare(b.name || b.username || ''));
    } else if (sortBy === 'joined') {
        filtered.sort((a, b) => {
            const da = a.joined_at ? new Date(a.joined_at).getTime() : 0;
            const db = b.joined_at ? new Date(b.joined_at).getTime() : 0;
            return da - db;
        });
    } else if (sortBy === 'joined-desc') {
        filtered.sort((a, b) => {
            const da = a.joined_at ? new Date(a.joined_at).getTime() : 0;
            const db = b.joined_at ? new Date(b.joined_at).getTime() : 0;
            return db - da;
        });
    }
    return filtered;
}

function buildRoleFilterOptions(s) {
    if (!s.roles) return '';
    const roles = Object.values(s.roles).filter(r => r.name !== '@everyone').sort((a, b) => b.position - a.position);
    if (!roles.length) return '';
    return `<option disabled>──── Roles ────</option>` +
        roles.map(r => `<option value="role:${r.id}"${filterTag === 'role:' + r.id ? ' selected' : ''}>${escHtml(r.name)}</option>`).join('');
}

function buildRoleBadges(m, s) {
    if (!s.roles || !m.roles || !m.roles.length) return '';
    const resolved = m.roles.map(rid => s.roles[rid]).filter(Boolean)
        .sort((a, b) => b.position - a.position);
    if (!resolved.length) return '';
    return resolved.map(r => {
        const c = r.color ? '#' + r.color.toString(16).padStart(6, '0') : 'var(--muted)';
        return `<span class="role-badge" style="--role-color:${c}">${escHtml(r.name)}</span>`;
    }).join('');
}

function resolveRoleNames(m, s) {
    if (!s.roles || !m.roles || !m.roles.length) return [];
    return m.roles.map(rid => s.roles[rid]).filter(Boolean)
        .sort((a, b) => b.position - a.position).map(r => r.name);
}

function getTopRoleColor(m) {
    const s = servers[activeServer];
    if (!s || !s.roles || !m.roles || !m.roles.length) return null;
    let top = null;
    for (const rid of m.roles) {
        const r = s.roles[rid];
        if (r && r.color && (!top || r.position > top.position)) top = r;
    }
    return top ? '#' + top.color.toString(16).padStart(6, '0') : null;
}

function buildMemberRows(filtered, q) {
    if (filtered.length === 0)
        return `<div class="no-members">${q ? 'No matches.' : 'No members.'}</div>`;
    return filtered.map(m => {
        const mid = m.id;
        const displayName = m.name || m.username || 'Unknown';
        const username = m.username || '';
        const color = stringToColor(mid);
        const topRoleColor = getTopRoleColor(m);
        const avatarHtml = m.avatar
            ? `<img src="https://cdn.discordapp.com/avatars/${mid}/${m.avatar}.png?size=64" alt="">`
            : escHtml(displayName.slice(0, 2).toUpperCase());
        const quirksHtml = (m.quirks || []).slice(0, 4).map(() => `<span class="quirk-dot"></span>`).join('');
        const roleAccent = topRoleColor ? `style="border-left:3px solid ${topRoleColor}"` : '';
        return `
      <div class="member-row${activeMember === mid ? ' active' : ''}" onclick="selectMember('${mid}')" ${roleAccent}>
        <div class="member-avatar" style="background:${color}">${avatarHtml}</div>
        <div class="member-row-info">
          <div class="member-row-name">${escHtml(displayName)}</div>
          ${username && username !== displayName ? `<div class="member-row-handle">@${escHtml(username)}</div>` : ''}
          ${quirksHtml ? `<div class="member-row-quirks">${quirksHtml}</div>` : ''}
        </div>
        <button class="icon-btn danger member-row-del" onclick="deleteMember(event,'${mid}')" title="Remove">
          <svg width="10" height="10" viewBox="0 0 12 12"><path d="M1 1l10 10M11 1L1 11" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>
        </button>
      </div>`;
    }).join('');
}

function buildDetailHtml(s) {
    if (!activeMember || !s.members[activeMember]) {
        return `<div class="detail-empty">
      <div class="detail-empty-icon">👈</div>
      <div class="detail-empty-text">Select a member from the list<br>to view and edit their info</div>
    </div>`;
    }
    const m = s.members[activeMember];
    const displayName = m.name || m.username || 'Unknown';
    const username = m.username || '';
    const color = stringToColor(m.id);
    const avatarHtml = m.avatar
        ? `<img src="https://cdn.discordapp.com/avatars/${m.id}/${m.avatar}.png?size=128" alt="">`
        : escHtml(displayName.slice(0, 2).toUpperCase());
    const joinedStr = m.joined_at
        ? new Date(m.joined_at).toLocaleDateString('en-US', { year: 'numeric', month: 'short', day: 'numeric' })
        : null;
    const quirksHtml = (m.quirks || []).map(q => `
    <span class="quirk-tag">${escHtml(q)}
      <button onclick="removeQuirk(${JSON.stringify(q)})">✕</button>
    </span>`).join('');
    const rolesBadges = buildRoleBadges(m, s);
    return `
    <div class="detail-header">
      <div class="detail-avatar" style="background:${color}">${avatarHtml}</div>
      <div class="detail-name-block">
        <div class="detail-name">${escHtml(displayName)}</div>
        ${username ? `<div class="detail-handle">@${escHtml(username)}</div>` : ''}
        ${joinedStr ? `<div class="detail-joined">Joined ${joinedStr}</div>` : ''}
      </div>
    </div>
    ${rolesBadges ? `<div class="detail-section"><div class="section-label">Roles</div><div class="role-badges">${rolesBadges}</div></div>` : ''}
    <div class="detail-section">
      <div class="section-label">Quirk Tags</div>
      <div class="quirk-tags" id="quirkTagsList">
        ${quirksHtml || `<span style="font-family:'JetBrains Mono',monospace;font-size:11px;color:var(--muted)">No tags yet</span>`}
      </div>
      <div class="quirk-input-row">
        <input class="quirk-input" id="quirkInput" placeholder="Add a tag (e.g. lurker, mod, funny)..." onkeydown="if(event.key==='Enter')addQuirk()" />
        <button class="btn btn-primary btn-sm" onclick="addQuirk()">Add</button>
      </div>
    </div>
    <div class="detail-section">
      <div class="section-label">Notes</div>
      <textarea class="notes-area" id="notesArea" placeholder="Write anything about this person..." oninput="saveNotesDebounced(this.value)">${escHtml(m.notes || '')}</textarea>
    </div>`;
}

function renderPanel() {
    const s = servers[activeServer];
    if (!s) return;
    const allMembers = Object.values(s.members || {});
    const filtered = getFilteredMembers(s);

    const panelEl = document.getElementById('memberPanel');

    // ── First render: build full skeleton ────────────────────────────────────
    if (!panelEl.querySelector('.panel')) {
        const iconHtml = s.icon
            ? `<img src="https://cdn.discordapp.com/icons/${activeServer}/${s.icon}.png?size=32" style="width:28px;height:28px;border-radius:8px;object-fit:cover" alt="">`
            : `<div style="width:28px;height:28px;border-radius:8px;background:var(--accent);display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:800;color:white">${escHtml(s.name.charAt(0))}</div>`;
        panelEl.innerHTML = `
      <div class="panel">
        <div class="panel-topbar">
          <div class="panel-server-info">
            ${iconHtml}
            <span class="panel-server-name">${escHtml(s.name)}</span>
            <span class="badge" id="memberBadge">${allMembers.length}</span>
          </div>
          <div class="panel-controls">
            <select class="panel-select" id="sortSelect" onchange="handleSort(this.value)" title="Sort by">
              <option value="name"${sortBy === 'name' ? ' selected' : ''}>A → Z</option>
              <option value="joined"${sortBy === 'joined' ? ' selected' : ''}>Oldest first</option>
              <option value="joined-desc"${sortBy === 'joined-desc' ? ' selected' : ''}>Newest first</option>
            </select>
            <select class="panel-select" id="filterSelect" onchange="handleFilter(this.value)" title="Filter">
              <option value="all"${filterTag === 'all' ? ' selected' : ''}>All</option>
              <option value="tagged"${filterTag === 'tagged' ? ' selected' : ''}>Has tags</option>
              <option value="untagged"${filterTag === 'untagged' ? ' selected' : ''}>No tags</option>
              ${buildRoleFilterOptions(s)}
            </select>
            <button class="export-btn" onclick="exportCSV()" title="Export CSV">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
              CSV
            </button>
            <button class="export-btn" onclick="exportJSON()" title="Export JSON">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
              JSON
            </button>
          </div>
        </div>
        <div class="panel-body">
          <div class="member-sidebar">
            <div class="sidebar-search-wrap">
              <input class="sidebar-search" id="sidebarSearch"
                placeholder="Search by name or @username..."
                oninput="handleSearch(this.value)"
                spellcheck="false" autocomplete="off" />
            </div>
            <div class="member-list" id="memberList"></div>
          </div>
          <div class="sidebar-divider" id="sidebarDivider"></div>
          <div class="member-detail" id="memberDetail"></div>
        </div>
      </div>`;
    }

    // ── Always patch only list + detail — never touch the input ──────────────
    const listEl = document.getElementById('memberList');
    const detailEl = document.getElementById('memberDetail');
    const badgeEl = document.getElementById('memberBadge');
    if (listEl) listEl.innerHTML = buildMemberRows(filtered, searchVal);
    if (detailEl) detailEl.innerHTML = buildDetailHtml(s);
    if (badgeEl) badgeEl.textContent = allMembers.length;
}

// ── Actions ───────────────────────────────────────────────────────────────────
function toggleServer(id) {
    if (activeServer === id) { activeServer = null; activeMember = null; }
    else { activeServer = id; activeMember = null; searchVal = ''; sortBy = 'name'; filterTag = 'all'; }
    render();
    if (activeServer) setTimeout(() => document.getElementById('memberPanel')?.scrollIntoView({ behavior: 'smooth', block: 'nearest' }), 50);
}

async function rescrapeServer(e, id) {
    e.stopPropagation();
    if (!token) { toast('error', 'Connect your token first.'); return; }
    toast('info', 'Re-scraping...');
    try {
        await new Promise((resolve, reject) => {
            fetch(`${API}/api/scrape`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ token, guild_id: id })
            }).then(res => {
                const reader = res.body.getReader();
                const decoder = new TextDecoder();
                let buf = '';
                function read() {
                    reader.read().then(({ done, value }) => {
                        if (done) { resolve(); return; }
                        buf += decoder.decode(value, { stream: true });
                        const lines = buf.split('\n'); buf = lines.pop();
                        for (const line of lines) {
                            if (!line.startsWith('data: ')) continue;
                            try {
                                const evt = JSON.parse(line.slice(6));
                                if (evt.type === 'done') { toast('success', `Updated: ${evt.scraped} members`); resolve(evt); }
                                if (evt.type === 'error') reject(new Error(evt.detail));
                            } catch (e) { }
                        }
                        read();
                    }).catch(reject);
                }
                read();
            }).catch(reject);
        });
        await loadServers();
        if (activeServer === id) renderPanel();
    } catch (e) { toast('error', e.message); }
}

async function deleteServer(e, id) {
    e.stopPropagation();
    if (!confirm('Delete this server and all its data?')) return;
    await fetch(`${API}/api/servers/${id}`, { method: 'DELETE' });
    delete servers[id];
    if (activeServer === id) { activeServer = null; activeMember = null; }
    render();
}

function deleteMember(e, mid) {
    e.stopPropagation();
    delete servers[activeServer].members[mid];
    if (activeMember === mid) activeMember = null;
    renderPanel();
}

function selectMember(mid) {
    activeMember = activeMember === mid ? null : mid;
    renderPanel();
}

function handleSearch(val) {
    searchVal = val;
    const s = servers[activeServer];
    if (!s) return;
    const filtered = getFilteredMembers(s);
    const listEl = document.getElementById('memberList');
    if (listEl) listEl.innerHTML = buildMemberRows(filtered, val);
}

function handleSort(val) {
    sortBy = val;
    const s = servers[activeServer];
    if (!s) return;
    const filtered = getFilteredMembers(s);
    const listEl = document.getElementById('memberList');
    if (listEl) listEl.innerHTML = buildMemberRows(filtered, searchVal);
}

function handleFilter(val) {
    filterTag = val;
    const s = servers[activeServer];
    if (!s) return;
    const filtered = getFilteredMembers(s);
    const listEl = document.getElementById('memberList');
    if (listEl) listEl.innerHTML = buildMemberRows(filtered, searchVal);
}

async function addQuirk() {
    const input = document.getElementById('quirkInput');
    const val = input.value.trim();
    if (!val || !activeMember) return;
    const m = servers[activeServer]?.members[activeMember];
    if (!m) return;
    if (!m.quirks) m.quirks = [];
    if (!m.quirks.includes(val)) {
        m.quirks.push(val);
        await patchMember(activeMember, { quirks: m.quirks });
    }
    input.value = '';
    renderPanel();
    setTimeout(() => document.getElementById('quirkInput')?.focus(), 10);
}

async function removeQuirk(q) {
    const m = servers[activeServer]?.members[activeMember];
    if (!m) return;
    m.quirks = (m.quirks || []).filter(x => x !== q);
    await patchMember(activeMember, { quirks: m.quirks });
    renderPanel();
}

let notesTimer = null;
function saveNotesDebounced(val) {
    const m = servers[activeServer]?.members[activeMember];
    if (!m) return;
    m.notes = val;
    clearTimeout(notesTimer);
    notesTimer = setTimeout(() => patchMember(activeMember, { notes: val }), 600);
}

async function patchMember(mid, body) {
    try {
        await fetch(`${API}/api/servers/${activeServer}/members/${mid}`, {
            method: 'PATCH', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        });
    } catch (e) { toast('error', 'Failed to save.'); }
}

function closeModal(id) { document.getElementById(id).classList.add('hidden'); }
document.querySelectorAll('.modal-overlay').forEach(o => o.addEventListener('click', e => { if (e.target === o) o.classList.add('hidden'); }));

// ── Export ─────────────────────────────────────────────────────────────────────
function exportCSV() {
    const s = servers[activeServer];
    if (!s) return;
    const members = Object.values(s.members || {});
    const rows = [['Name', 'Username', 'ID', 'Joined', 'Roles', 'Quirks', 'Notes']];
    members.forEach(m => {
        rows.push([
            m.name || '',
            m.username || '',
            m.id || '',
            m.joined_at ? new Date(m.joined_at).toLocaleDateString('en-US') : '',
            resolveRoleNames(m, s).join('; '),
            (m.quirks || []).join('; '),
            (m.notes || '').replace(/"/g, '""'),
        ]);
    });
    const csv = rows.map(r => r.map(c => `"${c}"`).join(',')).join('\n');
    downloadBlob(csv, `${s.name || 'server'}_members.csv`, 'text/csv');
    toast('success', `Exported ${members.length} members as CSV`);
}

function exportJSON() {
    const s = servers[activeServer];
    if (!s) return;
    const members = Object.values(s.members || {});
    const data = {
        server: { id: s.id, name: s.name, member_count: members.length },
        exported_at: new Date().toISOString(),
        members: members.map(m => ({
            id: m.id, name: m.name, username: m.username,
            avatar: m.avatar, joined_at: m.joined_at,
            roles: resolveRoleNames(m, s),
            quirks: m.quirks || [], notes: m.notes || '',
        })),
    };
    const json = JSON.stringify(data, null, 2);
    downloadBlob(json, `${s.name || 'server'}_members.json`, 'application/json');
    toast('success', `Exported ${members.length} members as JSON`);
}

function downloadBlob(content, filename, mimeType) {
    const blob = new Blob([content], { type: mimeType });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function escHtml(s) {
    return String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
function stringToColor(str) {
    let hash = 0;
    for (let i = 0; i < str.length; i++) hash = str.charCodeAt(i) + ((hash << 5) - hash);
    return ['#5865f2', '#eb459e', '#3ba55c', '#faa81a', '#9b59b6', '#e67e22', '#00b5d4', '#e74c3c'][Math.abs(hash) % 8];
}
function toast(type, msg) {
    const el = document.createElement('div');
    el.className = `toast ${type}`;
    el.innerHTML = `<span>${{ success: '✓', error: '✕', info: 'ℹ' }[type] || ''}</span> ${escHtml(msg)}`;
    document.getElementById('toastContainer').appendChild(el);
    setTimeout(() => el.remove(), 3500);
}

// ── Sidebar resize ────────────────────────────────────────────────────────────
document.addEventListener('mousedown', e => {
    const divider = e.target.closest('#sidebarDivider');
    if (!divider) return;
    e.preventDefault();
    divider.classList.add('dragging');
    const sidebar = document.querySelector('.member-sidebar');
    const startX = e.clientX;
    const startW = sidebar ? sidebar.offsetWidth : 300;

    function onMove(e) {
        if (!sidebar) return;
        const newW = Math.max(200, Math.min(500, startW + e.clientX - startX));
        sidebar.style.width = newW + 'px';
    }
    function onUp() {
        divider.classList.remove('dragging');
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
    }
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
});
