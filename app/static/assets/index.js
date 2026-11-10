// CloudClone UI state and behaviour (Alpine.js). The markup lives in index.html.

const DAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];

// ---- API ------------------------------------------------------------------

class AuthError extends Error {
  constructor() { super('Not logged in'); this.auth = true; }
}

let onUnauthorized = () => {};

async function api(path, method = 'GET', body) {
  const r = await fetch('/api' + path, {
    method, headers: { 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined,
  });
  if (r.status === 401 && path !== '/login') { onUnauthorized(); throw new AuthError(); }
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || r.statusText);
  return j;
}

// Show an error unless it's the "logged out" case, which already switches to the login screen.
const report = (e, show = alert) => { if (!e.auth) show(e.message); };

// ---- formatting -------------------------------------------------------------

function fmtBytes(n) {
  if (!n) return '0 B';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.min(4, Math.floor(Math.log(n) / Math.log(1024)));
  return (n / 1024 ** i).toFixed(i ? 1 : 0) + ' ' + u[i];
}

const fmtDate = s => s ? new Date(s).toLocaleString() : '—';

const fmtEta = s => s == null ? '' : s > 3600 ? Math.floor(s / 3600) + 'h ' + Math.floor(s % 3600 / 60) + 'm'
  : s > 60 ? Math.floor(s / 60) + 'm ' + (s % 60) + 's' : s + 's';

function progress(c) {
  const scanning = !c.total_transfers && !c.bytes;
  const pct = c.total_bytes ? Math.min(100, c.bytes / c.total_bytes * 100) : 0;
  const label = scanning
    ? `Comparing with Drive… ${c.checks}${c.total_checks ? ' / ' + c.total_checks : ''} files checked`
    : `${pct.toFixed(1)}% · ${fmtBytes(c.bytes)} / ${fmtBytes(c.total_bytes)} · ${c.transfers} / ${c.total_transfers} files · `
      + `${fmtBytes(c.speed)}/s${c.eta != null ? ' · ETA ' + fmtEta(c.eta) : ''}`;
  return {
    scanning, pct, label, errors: c.errors,
    files: c.transferring.slice(0, 3).map(t => t.name),
    log: c.log.slice(-8).reverse().join('\n'),
  };
}

const emptyAddForm = () => ({ name: '', client_id: '', client_secret: '', save_as_default: true, shared_drive_id: '' });
const emptyUserForm = (id = null) => ({ id, username: '', password: '', confirm: '', err: '', loading: !!id });

document.addEventListener('alpine:init', () => {

  // ---- whole page: session, routing, backups, users, folder picker ----------
  Alpine.data('app', () => ({
    days: DAYS, fmtBytes, fmtDate,

    session: null,          // { configured, authed, user }
    login: { username: '', password: '', err: '' },
    view: 'backups',        // backups | users | user-form
    st: null,               // last /api/state
    polling: null,

    addOpen: false, add: emptyAddForm(), addErr: '', ownCreds: false,
    users: null, usersErr: '',
    uf: emptyUserForm(),
    picker: null,           // { id, selected: [], children: {path: {status, names, error}}, open: {path: bool} }

    async init() {
      onUnauthorized = () => this.loggedOut();
      window.addEventListener('hashchange', () => this.route());
      await this.loadSession();
    },

    get me() { return this.session?.user; },

    // ---- session ----

    async loadSession() {
      this.session = await api('/session');
      if (this.session.authed) this.route(); else this.loggedOut();
    },

    loggedOut() {
      this.stopPolling();
      this.picker = null;
      this.session = { configured: this.session?.configured ?? true, authed: false, user: null };
      this.$nextTick(() => this.$refs.username.focus());
    },

    async submitLogin() {
      try {
        const user = await api(this.session.configured ? '/login' : '/setup', 'POST',
          { username: this.login.username, password: this.login.password });
        this.login = { username: '', password: '', err: '' };
        this.session = { configured: true, authed: true, user };
        this.route();
      } catch (e) {
        this.login.err = e.message;
      }
    },

    async logout() {
      await api('/logout', 'POST');
      await this.loadSession();
    },

    // ---- routing: #/ (backups), #/users, #/users/new, #/users/<id> ----

    route() {
      if (!this.session?.authed) return;
      const path = location.hash.replace(/^#/, '') || '/';
      const m = path.match(/^\/users(?:\/([^/]+))?\/?$/);
      this.stopPolling();
      if (!m) {
        this.view = 'backups';
        this.refresh();
        this.polling = setInterval(() => this.refresh(), 1500);
      } else if (!m[1]) {
        this.view = 'users';
        this.loadUsers();
      } else {
        this.view = 'user-form';
        this.openUserForm(m[1] === 'new' ? null : decodeURIComponent(m[1]));
      }
    },

    stopPolling() { clearInterval(this.polling); this.polling = null; },

    // ---- backups ----

    async refresh() {
      try { this.st = await api('/state'); } catch { /* logged out, or a transient error; the next poll retries */ }
    },

    isRunning(a) { return this.st.run.current?.account_id === a.id; },
    isQueued(a) { return this.st.run.queued.includes(a.id); },

    status(a) {
      const lr = a.last_run;
      if (!a.connected) return { text: 'Not connected', cls: 'warn' };
      if (this.isRunning(a)) return { text: 'Syncing', cls: 'syncing' };
      if (this.isQueued(a)) return { text: 'Queued', cls: '' };
      if (lr) return { text: lr.status, cls: lr.status === 'success' ? 'ok' : 'bad' };
      return { text: 'Never run', cls: '' };
    },

    lastRunText(lr) {
      return `${fmtDate(lr.finished)} (${lr.trigger}) · ${lr.transfers} files, ${fmtBytes(lr.bytes)} downloaded, `
        + `${lr.deletes} removed, ${lr.checks} checked`;
    },

    scheduleLine(a) {
      if (!a.enabled) return 'Schedule paused';
      const s = a.schedule;
      const when = s.freq === 'daily' ? `Daily at ${s.time}`
        : s.freq === 'weekly' ? `Weekly on ${DAYS[s.weekday]} at ${s.time}`
        : `Monthly on day ${s.monthday} at ${s.time}`;
      return when + (a.next_run ? ' · next: ' + fmtDate(a.next_run) : '');
    },

    async createAccount() {
      try {
        await api('/accounts', 'POST', this.add);
        this.add = emptyAddForm();
        this.addErr = '';
        this.addOpen = false;
        this.refresh();
      } catch (e) {
        report(e, msg => this.addErr = msg);
      }
    },

    async runNow(id) {
      try { await api(`/accounts/${id}/run`, 'POST'); this.refresh(); } catch (e) { report(e); }
    },

    async cancelRun() {
      try { await api('/cancel', 'POST'); this.refresh(); } catch (e) { report(e); }
    },

    async deleteAccount(a) {
      if (!confirm(`Delete the account "${a.name}"? Backed-up files stay on disk.`)) return;
      try { await api('/accounts/' + a.id, 'DELETE'); this.refresh(); } catch (e) { report(e); }
    },

    // ---- users ----

    async loadUsers() {
      this.users = null;
      this.usersErr = '';
      try { this.users = await api('/users'); } catch (e) { report(e, msg => this.usersErr = msg); }
    },

    async openUserForm(id) {
      this.uf = emptyUserForm(id);
      if (id) {
        try { this.uf.username = (await api('/users/' + encodeURIComponent(id))).username; }
        catch (e) { report(e, msg => this.uf.err = msg); return; }
        this.uf.loading = false;
      }
      this.$nextTick(() => this.$refs.ufUsername.focus());
    },

    async saveUser() {
      const { id, username, password, confirm } = this.uf;
      if (password !== confirm) { this.uf.err = 'Passwords do not match'; return; }
      try {
        if (id) {
          const user = await api('/users/' + encodeURIComponent(id), 'PATCH', password ? { username, password } : { username });
          if (user.id === this.me?.id) this.session.user = user;
        } else {
          await api('/users', 'POST', { username, password });
        }
        location.hash = '#/users';
      } catch (e) {
        report(e, msg => this.uf.err = msg);
      }
    },

    async deleteUser(u) {
      if (!confirm(`Delete user "${u.username}"? They will no longer be able to log in.`)) return;
      try { await api('/users/' + encodeURIComponent(u.id), 'DELETE'); } catch (e) { report(e); return; }
      if (this.view === 'users') this.loadUsers(); else location.hash = '#/users';
    },

    // ---- folder picker ----
    // Selections are folder paths ("a/b", which include everything inside) or "a/b/*" / "*" (only the files
    // directly in that folder). The tree is shown as a flat list of rows so it needs no recursive components.

    openPicker(a) {
      this.picker = { id: a.id, selected: [...a.folders], children: {}, open: {} };
      this.loadChildren('');
    },

    closePicker() { this.picker = null; },

    async savePicker() {
      try {
        await api('/accounts/' + this.picker.id, 'PATCH', { folders: this.picker.selected });
        this.closePicker();
        this.refresh();
      } catch (e) {
        report(e);
      }
    },

    async loadChildren(path) {
      const p = this.picker;
      p.children[path] = { status: 'loading' };
      try {
        const names = await api(`/accounts/${p.id}/folders?path=${encodeURIComponent(path)}`);
        p.children[path] = { status: 'ok', names };
      } catch (e) {
        p.children[path] = { status: 'error', error: e.message };
      }
    },

    toggleOpen(path) {
      const p = this.picker;
      p.open[path] = !p.open[path];
      if (p.open[path] && !p.children[path]) this.loadChildren(path);
    },

    get pickerRows() {
      const p = this.picker, rows = [];
      const walk = (path, depth) => {
        const c = p.children[path];
        if (!c || c.status === 'loading') return rows.push({ type: 'msg', key: path + '#msg', depth, text: 'Loading…' });
        if (c.status === 'error') return rows.push({ type: 'err', key: path + '#err', depth, text: c.error });
        rows.push({ type: 'files', key: path + '#files', path, depth,
                    label: path ? 'files only' : 'Files in the root folder only' });
        if (!c.names.length) rows.push({ type: 'msg', key: path + '#msg', depth, text: 'No sub-folders' });
        for (const name of c.names) {
          const full = path ? path + '/' + name : name;
          rows.push({ type: 'folder', key: full, path: full, name, depth });
          if (p.open[full]) walk(full, depth + 1);
        }
      };
      walk('', 0);
      return rows;
    },

    // True when `path` is selected itself or sits inside a selected folder.
    covered(path) { return this.picker.selected.some(x => path === x || path.startsWith(x + '/')); },
    picked(key) { return this.picker.selected.includes(key); },
    filesKey: path => path ? path + '/*' : '*',

    folderLocked(path) { return !this.picked(path) && this.covered(path); },
    filesChecked(path) { return this.picked(this.filesKey(path)) || this.covered(path); },
    filesLocked(path) { return !this.picked(this.filesKey(path)) && !!path && this.covered(path); },

    pickFolder(path, on) {
      const p = this.picker;
      // Selecting a folder replaces any selections inside it.
      p.selected = p.selected.filter(x => x !== path && !(on && x.startsWith(path + '/')));
      if (on) p.selected.push(path);
    },

    pickFiles(path, on) {
      const p = this.picker, key = this.filesKey(path);
      p.selected = p.selected.filter(x => x !== key);
      if (on) p.selected.push(key);
    },
  }));

  // ---- one account card ------------------------------------------------------
  // Holds what the user is editing, so the 1.5s state poll never overwrites it.
  Alpine.data('accountCard', () => ({
    draft: null,            // schedule form
    authUrl: '', code: '', authErr: '',

    init() { this.resetDraft(); },

    get prog() { return this.isRunning(this.a) ? progress(this.st.run.current) : null; },

    resetDraft() { this.draft = { ...this.a.schedule, enabled: this.a.enabled }; },

    async saveSchedule() {
      const { enabled, ...schedule } = this.draft;
      try {
        await api('/accounts/' + this.a.id, 'PATCH', { enabled, schedule });
        this.refresh();
      } catch (e) {
        report(e);
      }
    },

    async connect() {
      let url;
      try { ({ url } = await api(`/accounts/${this.a.id}/auth-url`)); } catch (e) { report(e); return; }
      if (this.st.direct_oauth) { location.href = url; return; }
      this.code = this.authErr = '';
      this.authUrl = url;
    },

    async submitCode() {
      try {
        await api(`/accounts/${this.a.id}/auth-code`, 'POST', { code: this.code });
        this.authUrl = '';
        this.refresh();
      } catch (e) {
        report(e, msg => this.authErr = msg);
      }
    },
  }));
});
