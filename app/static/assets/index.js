// CloudClone JavaScript functionality
const $ = s => document.querySelector(s);

const api = async (path, method = 'GET', body) => {
  const r = await fetch('/api' + path, { method, headers: { 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined });
  if (r.status === 401 && path !== '/login') { showLogin(); throw new Error('auth'); }
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || r.statusText);
  return j;
};

const fmtBytes = n => { 
  if (!n) return '0 B'; 
  const u = ['B','KB','MB','GB','TB']; 
  const i = Math.min(4, Math.floor(Math.log(n) / Math.log(1024))); 
  return (n / 1024 ** i).toFixed(i ? 1 : 0) + ' ' + u[i]; 
};

const fmtDate = s => s ? new Date(s).toLocaleString() : '—';

const fmtEta = s => s == null ? '' : s > 3600 ? Math.floor(s/3600) + 'h ' + Math.floor(s%3600/60) + 'm' : s > 60 ? Math.floor(s/60) + 'm ' + (s%60) + 's' : s + 's';

const days = ['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'];

const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

let mode = 'login', polling = null, lastSig = null, lastState = null;

function showLogin() {
  clearInterval(polling); $('#app').classList.add('hidden'); $('#logout').classList.add('hidden'); $('#login').classList.remove('hidden');
}

async function init() {
  const s = await api('/session');
  if (s.authed) return showApp();
  mode = s.configured ? 'login' : 'setup';
  $('#login-title').textContent = mode === 'setup' ? 'Set a password' : 'Log in';
  $('#login-hint').textContent = mode === 'setup' ? 'First run: choose a password (8+ characters) to protect this page.' : '';
  showLogin();
}

function showApp() {
  $('#login').classList.add('hidden'); $('#app').classList.remove('hidden'); $('#logout').classList.remove('hidden');
  refresh(); polling = setInterval(refresh, 1500);
}

$('#login-btn').onclick = async () => {
  try { 
    await api(mode === 'setup' ? '/setup' : '/login', 'POST', { password: $('#pw').value }); 
    $('#pw').value = ''; 
    showApp(); 
  }
  catch (e) { 
    $('#login-err').textContent = e.message; 
  }
};

$('#pw').onkeydown = e => { if (e.key === 'Enter') $('#login-btn').click(); };

$('#logout').onclick = async () => { await api('/logout', 'POST'); init(); };

$('#toggle-add').onclick = () => {
  const form = $('#add-form');
  form.classList.toggle('hidden');
  $('#toggle-add').setAttribute('aria-expanded', !form.classList.contains('hidden'));
};

$('#add-btn').onclick = async () => {
  try {
    await api('/accounts', 'POST', { 
      name: $('#a-name').value, 
      client_id: $('#a-cid').value, 
      client_secret: $('#a-secret').value, 
      save_as_default: $('#a-default').checked, 
      shared_drive_id: $('#a-team').value 
    });
    ['#a-name','#a-cid','#a-secret','#a-team'].forEach(i => $(i).value = ''); 
    $('#add-err').textContent = ''; 
    $('#add-form').classList.add('hidden'); 
    refresh();
  } catch (e) { 
    $('#add-err').textContent = e.message; 
  }
};

async function refresh() {
  try { lastState = await api('/state'); } catch { return; }
  $('#version').textContent = 'CloudClone v' + lastState.version;
  // Don't clobber a card while the user is typing in it.
  const hasDef = lastState.has_default_client;
  $('#creds').classList.toggle('hidden', hasDef && !$('#creds').dataset.forced);
  $('#creds-saved').classList.toggle('hidden', !hasDef || !!$('#creds').dataset.forced);
  $('#help-type').innerHTML = lastState.direct_oauth
    ? `<b>Credentials → Create credentials → OAuth client ID</b> → type <b>Web application</b>, with Authorized redirect URI <code>${esc(lastState.redirect_uri)}</code>.`
    : '<b>Credentials → Create credentials → OAuth client ID</b> → type <b>Desktop app</b>.';
  const cur = lastState.run.current;
  const sig = JSON.stringify([lastState.accounts, lastState.run.queued, cur && cur.account_id]);
  const active = document.activeElement;
  const typing = active && active.closest('#accounts') && /INPUT|SELECT/.test(active.tagName);
  if (sig !== lastSig && !typing) { lastSig = sig; render(lastState); }
  else if (cur) { const el = $('#prog-' + cur.account_id); if (el) el.innerHTML = progress(cur); }
}

function render(st) {
  const cur = st.run.current;
  const html = st.accounts.map(a => card(a, st, cur)).join('') || '<div class="card muted">No accounts yet. Add a Google account below.</div>';
  $('#accounts').innerHTML = html;
}

function card(a, st, cur) {
  const running = cur && cur.account_id === a.id;
  const queued = st.run.queued.includes(a.id);
  const lr = a.last_run;
  let status;
  if (!a.connected) status = '<span class="badge warn">Not connected</span>';
  else if (running) status = '<span class="badge" style="color:var(--accent)">Syncing</span>';
  else if (queued) status = '<span class="badge">Queued</span>';
  else if (lr) status = `<span class="badge ${lr.status === 'success' ? 'ok' : 'bad'}">${esc(lr.status)}</span>`;
  else status = '<span class="badge">Never run</span>';

  let body = '';
  if (running) body = `<div id="prog-${a.id}">${progress(cur)}</div>`;
  else if (lr) body = `<div class="muted">Last run: ${fmtDate(lr.finished)} (${esc(lr.trigger)}) · ${lr.transfers} files, ${fmtBytes(lr.bytes)} downloaded, ${lr.deletes} removed, ${lr.checks} checked${lr.error ? `<br><span class="bad">${esc(lr.error)}</span>` : ''}</div>`;

  const sch = a.schedule;
  const when = sch.freq === 'daily' ? `Daily at ${sch.time}` : sch.freq === 'weekly' ? `Weekly on ${days[sch.weekday]} at ${sch.time}` : `Monthly on day ${sch.monthday} at ${sch.time}`;

  return `<div class="card" id="acc-${a.id}">
    <div class="row">
      <div class="grow"><h2>${esc(a.name)} ${status}</h2>
        <div class="muted">${esc(a.email || '')} → <code>${esc(a.destination)}</code></div></div>
      ${a.connected ? `<button class="primary" onclick="runNow('${a.id}')" ${running || queued ? 'disabled' : ''} aria-label="Run backup for ${a.name}">Run now</button>` : `<button class="primary" onclick="connect('${a.id}')">Connect Google</button>`}
      ${running ? `<button class="danger" onclick="cancelRun()" aria-label="Cancel backup">Cancel</button>` : ''}
    </div>
    ${body}
    <div class="muted" style="margin-top:8px">${a.enabled ? esc(when) + (a.next_run ? ' · next: ' + fmtDate(a.next_run) : '') : 'Schedule paused'}</div>
    <details><summary>Settings</summary>
      <div class="row" style="margin-top:8px">
        <select id="f-${a.id}" onchange="schedUI('${a.id}')" aria-label="Select backup frequency">
          ${['daily','weekly','monthly'].map(f => `<option ${f === sch.freq ? 'selected' : ''}>${f}</option>`).join('')}
        </select>
        <select id="w-${a.id}" class="${sch.freq === 'weekly' ? '' : 'hidden'}" aria-label="Select backup day of week">${days.map((d, i) => `<option value="${i}" ${i === sch.weekday ? 'selected' : ''}>${d}</option>`).join('')}</select>
        <select id="m-${a.id}" class="${sch.freq === 'monthly' ? '' : 'hidden'}" aria-label="Select backup day of month">${Array.from({length: 28}, (_, i) => `<option ${i + 1 === sch.monthday ? 'selected' : ''}>${i + 1}</option>`).join('')}</select>
        <input type="time" id="t-${a.id}" value="${sch.time}" aria-label="Select backup time">
        <label style="margin:0;display:flex;gap:6px;align-items:center"><input type="checkbox" id="e-${a.id}" ${a.enabled ? 'checked' : ''}> enabled</label>
        <button onclick="saveSched('${a.id}')" aria-label="Save backup settings">Save</button>
      </div>
      ${a.connected ? `<div class="row" style="margin-top:10px"><div class="grow"><b>Folders:</b> ${a.folders.length ? a.folders.map(f => `<code>${esc(f)}</code>`).join(' ') : '<span class="muted">Entire Drive</span>'}</div><button onclick="openPicker('${a.id}')" aria-label="Choose folders for backup">Choose folders…</button></div>` : ''}
      <div class="row" style="margin-top:10px">
        ${a.connected ? `<button onclick="connect('${a.id}')" aria-label="Re-connect Google account">Re-connect Google</button>` : ''}
        <button class="danger" onclick="del('${a.id}')" aria-label="Delete account">Delete account</button>
        <span class="muted">Deleting keeps already-backed-up files on disk. Backup is an exact mirror: files removed from Drive are removed from the backup on the next run.</span>
      </div>
    </details>
    <div id="auth-${a.id}"></div>
  </div>`;
}

function progress(c) {
  const scanning = !c.total_transfers && !c.bytes;
  const pct = c.total_bytes ? Math.min(100, c.bytes / c.total_bytes * 100) : 0;
  const label = scanning
    ? `Comparing with Drive… ${c.checks}${c.total_checks ? ' / ' + c.total_checks : ''} files checked`
    : `${pct.toFixed(1)}% · ${fmtBytes(c.bytes)} / ${fmtBytes(c.total_bytes)} · ${c.transfers} / ${c.total_transfers} files · ${fmtBytes(c.speed)}/s${c.eta != null ? ' · ETA ' + fmtEta(c.eta) : ''}`;
  const files = c.transferring.map(t => esc(t.name)).slice(0, 3).join('<br>');
  return `<div class="bar ${scanning ? 'indet' : ''}"><div style="width:${pct}%"></div></div>
    <div class="muted">${label}${c.errors ? ` · <span class="bad">${c.errors} errors</span>` : ''}</div>
    ${files ? `<div class="files">${files}</div>` : ''}
    ${c.log.length ? `<pre>${esc(c.log.slice(-8).reverse().join('\n'))}</pre>` : ''}`;
}

window.schedUI = id => {
  const f = $('#f-' + id).value;
  $('#w-' + id).classList.toggle('hidden', f !== 'weekly');
  $('#m-' + id).classList.toggle('hidden', f !== 'monthly');
};

window.saveSched = async id => {
  try {
    await api('/accounts/' + id, 'PATCH', { 
      enabled: $('#e-' + id).checked, 
      schedule: { 
        freq: $('#f-' + id).value, 
        time: $('#t-' + id).value, 
        weekday: +$('#w-' + id).value, 
        monthday: +$('#m-' + id).value 
      } 
    });
    document.activeElement.blur(); 
    refresh();
  } catch (e) { 
    alert(e.message); 
  }
};

window.runNow = async id => { 
  try { 
    await api(`/accounts/${id}/run`, 'POST'); 
    refresh(); 
  } catch (e) { 
    alert(e.message); 
  } 
};

window.cancelRun = async () => { 
  await api('/cancel', 'POST'); 
  refresh(); 
};

window.del = async id => { 
  if (confirm('Delete this account? Backed-up files stay on disk.')) { 
    await api('/accounts/' + id, 'DELETE'); 
    refresh(); 
  } 
};

window.connect = async id => {
  const { url } = await api(`/accounts/${id}/auth-url`);
  if (lastState.direct_oauth) { location.href = url; return; }
  const box = $('#auth-' + id);
  box.innerHTML = `<div style="margin-top:12px;border-top:1px solid var(--line);padding-top:12px">
    <ol class="muted">
      <li><a href="${url}" target="_blank" rel="noopener">Open Google sign-in</a> and approve read-only access.</li>
      <li>Your browser will then land on a page that fails to load (<code>127.0.0.1:53682</code>). That's expected.</li>
      <li>Copy the <b>entire URL</b> from the address bar and paste it here:</li>
    </ol>
    <input type="text" id="code-${id}" placeholder="http://127.0.0.1:53682/?state=...&code=..." aria-label="Paste Google authentication URL">
    <div class="err" id="auth-err-${id}" role="alert"></div>
    <button class="primary" onclick="submitCode('${id}')">Finish</button></div>`;
  box.querySelector('input').focus();
};

window.submitCode = async id => {
  try { 
    await api(`/accounts/${id}/auth-code`, 'POST', { code: $('#code-' + id).value }); 
    document.activeElement.blur(); 
    refresh(); 
  }
  catch (e) { 
    $('#auth-err-' + id).textContent = e.message; 
  }
};

let pick = null; // { id, selected:Set }
const covered = p => [...pick.selected].some(x => p === x || p.startsWith(x + '/'));

window.openPicker = async id => {
  const a = lastState.accounts.find(x => x.id === id);
  pick = { id, selected: new Set(a.folders) };
  $('#modal').classList.remove('hidden');
  $('#tree').innerHTML = '';
  await loadKids('', $('#tree'));
  addFilesOnly('', $('#tree'), 'Files in the root folder only', true);
  updateCount();
};

window.closePicker = () => { 
  $('#modal').classList.add('hidden'); 
  pick = null; 
};

window.savePicker = async () => {
  try { 
    await api('/accounts/' + pick.id, 'PATCH', { folders: [...pick.selected] }); 
    closePicker(); 
    lastSig = null; 
    refresh(); 
  }
  catch (e) { 
    alert(e.message); 
  }
};

function updateCount() { 
  $('#pick-count').textContent = pick.selected.size ? pick.selected.size + ' folder(s) selected' : 'Entire Drive'; 
}

function addFilesOnly(path, container, label, prepend) {
  const key = path ? path + '/*' : '*';
  const row = document.createElement('div');
  row.className = 'node';
  row.innerHTML = `<span class="twisty"></span><label style="margin:0;display:flex;gap:6px;align-items:center;color:var(--muted);font-size:14px"><input type="checkbox"> ${label}</label>`;
  const cb = row.querySelector('input');
  cb._sync = () => { 
    const own = pick.selected.has(key); 
    cb.checked = own || covered(path); 
    cb.disabled = !own && !!path && covered(path); 
  };
  cb.onchange = () => { 
    cb.checked ? pick.selected.add(key) : pick.selected.delete(key); 
    updateCount(); 
  };
  cb._sync();
  prepend ? container.prepend(row) : container.appendChild(row);
}

async function loadKids(path, container) {
  container.innerHTML = '<div class="muted">Loading…</div>';
  let names;
  try { names = await api(`/accounts/${pick.id}/folders?path=${encodeURIComponent(path)}`); }
  catch (e) { 
    container.innerHTML = `<div class="err">${esc(e.message)}</div>`; 
    return; 
  }
  container.innerHTML = names.length ? '' : '<div class="muted">No sub-folders</div>';
  for (const name of names) {
    const full = path ? path + '/' + name : name;
    const node = document.createElement('div');
    node.innerHTML = `<div class="node"><span class="twisty">▸</span><label style="margin:0;display:flex;gap:6px;align-items:center;color:var(--text);font-size:15px"><input type="checkbox"> ${esc(name)}</label></div><div class="kids hidden"></div>`;
    const cb = node.querySelector('input'), tw = node.querySelector('.twisty'), kids = node.querySelector('.kids');
    const sync = () => { 
      const own = pick.selected.has(full); 
      cb.checked = own || covered(full); 
      cb.disabled = !own && covered(full); 
    };
    const syncAll = () => node.querySelectorAll('input').forEach(i => i._sync && i._sync());
    cb.onchange = () => {
      if (cb.checked) { 
        [...pick.selected].forEach(x => x.startsWith(full + '/') && pick.selected.delete(x)); 
        pick.selected.add(full); 
      }
      else pick.selected.delete(full);
      syncAll(); updateCount();
    };
    cb._sync = sync; sync();
    let loaded = false;
    tw.onclick = async () => {
      const open = kids.classList.toggle('hidden') === false;
      tw.textContent = open ? '▾' : '▸';
      if (open && !loaded) { 
        loaded = true; 
        await loadKids(full, kids); 
        addFilesOnly(full, kids, 'files only', true); 
        kids.querySelectorAll('input').forEach(i => i._sync && i._sync()); 
      }
    };
    container.appendChild(node);
  }
}

// Close modal when pressing Escape key
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !$('#modal').classList.contains('hidden')) {
    closePicker();
  }
});

init();