/* 局域网轻量记事本 —— 单页前端
 *
 * 视觉结构移植自参考包 posts-ui-kit-2026-09-22：
 *   三栏 Grid（左栏工具栏 / 中栏主内容 / 右栏小组件）+ 分级退让
 *   时间分组采用参考包的「组后时间戳」约定（先内容后分隔）
 *
 * 阶段推进的关键语义：
 *   "推进" 是任务级的 —— 把当前阶段标为已完成并进入下一个，不是同一阶段内累加。
 */

const app = document.getElementById('app');
const toastEl = document.getElementById('toast');
const toTopEl = document.getElementById('toTop');

const state = {
  boot: null,
  me: null,
  authMode: 'login',
  authErr: null,
  authInfo: null,
  route: { name: 'list' },

  tab: 'all',
  q: '',
  mode: 'filter',
  tag: '',
  items: [],
  total: 0,
  page: 1,
  hasMore: false,
  loadingMore: false,

  feed: [],
  detail: null,
  timeline: [],

  stats: null,
  tags: [],
  templates: [],
  todos: null,
  checklist: { pending: [], done: [] },
  doneOpen: false,
  recurring: [],
  invites: [],
  catFolded: true,
  tagFolded: false,
  showRecurForm: false,
  recurFreq: 'daily',
  recurWeekdays: [0, 1, 2, 3, 4],

  membersOpen: false,
  members: null,

  profileOpen: false,
  profileErr: null,
  newDraft: null,
};

/* ============================================================ 工具 */

const esc = (s) =>
  String(s ?? '').replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const pad2 = (n) => String(n).padStart(2, '0');

function toast(msg, bad = false) {
  toastEl.textContent = msg;
  toastEl.className = 'toast show' + (bad ? ' bad' : '');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { toastEl.className = 'toast'; }, 2400);
}

function fmtTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
}

function fmtDate(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  return d.toLocaleString('zh-CN', {
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit',
  });
}

function dayLabel(d) {
  const today = new Date();
  const yest = new Date(); yest.setDate(yest.getDate() - 1);
  if (d.toDateString() === today.toDateString()) return '今天';
  if (d.toDateString() === yest.toDateString()) return '昨天';
  return d.toLocaleDateString('zh-CN', { year: 'numeric', month: 'long', day: 'numeric' });
}

/* 相对时间：列表里显示"最后动过是什么时候"更直观 */
function relTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  const diff = (Date.now() - d.getTime()) / 1000;
  if (diff < 60) return '刚刚';
  if (diff < 3600) return Math.floor(diff / 60) + ' 分钟前';
  const today = new Date();
  if (d.toDateString() === today.toDateString()) return '今天 ' + fmtTime(iso);
  const yest = new Date(); yest.setDate(yest.getDate() - 1);
  if (d.toDateString() === yest.toDateString()) return '昨天 ' + fmtTime(iso);
  return d.toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' }) + ' ' + fmtTime(iso);
}

const TRASH_SVG =
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
  + 'stroke-linecap="round" stroke-linejoin="round">'
  + '<path d="M4 7h16M10 11v6M14 11v6"/>'
  + '<path d="M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12"/>'
  + '<path d="M9 7V5a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v2"/></svg>';

function trashIcon(act, id, title = '删除') {
  return `<button class="icon-btn" data-act="${act}" data-id="${id}" title="${title}">${TRASH_SVG}</button>`;
}

function avatar(u, cls = '') {
  if (!u) return '';
  if (u.avatar_url) return `<img class="avatar ${cls}" src="${esc(u.avatar_url)}" alt="">`;
  return `<span class="avatar ${cls}">${esc((u.nickname || u.username || '?').slice(0, 1))}</span>`;
}

/* ------------------------------------------------------------------ 彩蛋
 * 昵称是 Lanyora 时，资料卡不显示文字，改用手写签名。
 * 签名路径直接内嵌在代码里（见文件末尾的 SIGNATURE_PATH），不单独放 .svg 文件——
 * 免得有人翻文件夹时一眼看见。平时它只是一段字符串，只有昵称匹配才会被渲染出来。
 */
const SIGNATURE_NICK = 'lanyora';

function isSignatureUser(user) {
  return String(user?.nickname || '').trim().toLowerCase() === SIGNATURE_NICK;
}

function profileNameHtml(user) {
  if (!isSignatureUser(user)) {
    return `<div class="about-name">${esc(user.nickname)}</div>`;
  }
  return `
    <svg class="about-signature" viewBox="-30 -610 1988 987"
         xmlns="http://www.w3.org/2000/svg" role="img"
         aria-label="${esc(user.nickname)}">
      <title>${esc(user.nickname)}</title>
      <defs>
        <linearGradient id="ly-sig" x1="0" y1="0" x2="1" y2="0.85">
          <stop offset="0" stop-color="#4a90d9"/>
          <stop offset="0.38" stop-color="#8fbce0"/>
          <stop offset="0.72" stop-color="#b8a8d8"/>
          <stop offset="1" stop-color="#d6809a"/>
        </linearGradient>
      </defs>
      <path fill="url(#ly-sig)" fill-rule="nonzero" d="${SIGNATURE_PATH}"/>
    </svg>`;
}

/* 摘要把 Markdown 记号去掉，只留纯文本 */
function plainText(s) {
  return String(s || '')
    .replace(/!\[[^\]]*\]\([^)]*\)/g, '')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    .replace(/[`*_>#]/g, '')
    .replace(/\s+/g, ' ')
    .trim();
}

/* 极简 Markdown：够看就行 */
function mdToHtml(md) {
  if (!md) return '';
  let s = esc(md);
  s = s.replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, (m, alt, url) =>
    `<img src="${url}" alt="${alt}" loading="lazy">`);
  s = s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
  s = s.replace(/\*\*([^*\n]+)\*\*/g, '<b>$1</b>');
  s = s.replace(/^### (.+)$/gm, '<h3>$1</h3>')
       .replace(/^## (.+)$/gm, '<h2>$1</h2>')
       .replace(/^# (.+)$/gm, '<h1>$1</h1>');
  return s.split(/\n{2,}/).map((b) => {
    const t = b.trim();
    if (!t) return '';
    if (/^<(h\d|ul|img|blockquote)/.test(t)) return t;
    if (/^[-*] /m.test(t)) {
      return '<ul>' + t.split('\n').map((l) => `<li>${l.replace(/^[-*] /, '')}</li>`).join('') + '</ul>';
    }
    return `<p>${t.replace(/\n/g, '<br>')}</p>`;
  }).join('');
}

/* ============================================================ 请求 */

async function api(path, { method = 'GET', body, form } = {}) {
  const opts = { method, credentials: 'same-origin', headers: {} };
  if (form) opts.body = form;
  else if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  let data = null;
  try { data = await res.json(); } catch (_) { /* 可能是空响应 */ }
  if (!res.ok) {
    if (res.status === 401 && state.me) { state.me = null; render(); }
    const err = new Error(data?.error?.message || `请求失败（${res.status}）`);
    err.code = data?.error?.code;
    throw err;
  }
  return data;
}

/* ============================================================ 路由 */

function parseHash() {
  const h = location.hash.replace(/^#\/?/, '');
  const [p, arg] = h.split('/');
  if (p === 'item' && arg) return { name: 'item', id: Number(arg) };
  if (p === 'new') return { name: 'new' };
  if (p === 'feed') return { name: 'feed' };
  if (p === 't' && arg) return { name: 'list', tab: arg };
  return { name: 'list' };
}

function go(hash) {
  if (location.hash === hash) { onRoute(); return; }
  location.hash = hash;
}

window.addEventListener('hashchange', onRoute);

/* ============================================================ 启动 */

async function boot() {
  try {
    state.boot = await api('/api/bootstrap');
  } catch (e) {
    app.innerHTML = `<div class="auth-wrap"><div class="auth-card">
      <h1>无法连接服务</h1><p class="hint">${esc(e.message)}</p></div></div>`;
    return;
  }
  document.title = state.boot.app_title || '局域网轻量记事本';
  if (state.boot.needs_setup) state.authMode = 'register';

  if (state.boot.logged_in) {
    try { state.me = await api('/api/me'); } catch (_) { state.me = null; }
  }
  if (state.me) await refreshSide();
  onRoute();
}

async function onRoute() {
  state.route = parseHash();
  if (state.route.tab) state.tab = state.route.tab;
  if (!state.me) return render();
  try {
    if (state.route.name === 'item') await loadDetail(state.route.id);
    else if (state.route.name === 'feed') await loadFeed();
    else if (state.route.name === 'list') await loadList();
  } catch (e) { toast(e.message, true); }
  render();
}

async function refreshSide() {
  try {
    const tagUrl = state.tab === 'all' ? '/api/tags' : '/api/tags?kind=' + state.tab;
    const [tags, templates, todos, checklist, recurring, invites, stats] = await Promise.all([
      api(tagUrl), api('/api/templates'), api('/api/todos'), api('/api/checklist'),
      api('/api/recurring'), api('/api/invites'), api('/api/stats'),
    ]);
    state.tags = tags.tags || [];
    state.templates = templates.templates || [];
    state.todos = todos;
    state.checklist = { pending: checklist.pending || [], done: checklist.done || [] };
    state.recurring = recurring.rules || [];
    state.invites = invites.invites || [];
    state.stats = stats;
  } catch (e) { /* 侧栏失败不阻塞主流程 */ }
}

/* ============================================================ 数据加载 */

function listQuery(page) {
  const p = new URLSearchParams();
  if (state.tab !== 'all') p.set('kind', state.tab);
  if (state.tag) p.set('tag', state.tag);
  p.set('page', page);
  if (state.q) p.set('q', state.q);
  return p;
}

async function loadList() {
  state.page = 1;
  let data;
  if (state.q && state.mode !== 'filter') {
    data = await api('/api/search?' + new URLSearchParams({ q: state.q, scope: state.mode }));
  } else {
    data = await api('/api/items?' + listQuery(1).toString());
  }
  state.items = data.items || [];
  state.total = data.total || 0;
  state.hasMore = state.items.length < state.total;
}

async function loadMore() {
  if (state.loadingMore || !state.hasMore) return;
  if (state.q && state.mode !== 'filter') return;   // 详细搜索不做分页追加
  state.loadingMore = true;
  try {
    state.page += 1;
    const data = await api('/api/items?' + listQuery(state.page).toString());
    state.items = state.items.concat(data.items || []);
    state.hasMore = state.items.length < state.total;
    const box = document.getElementById('rowsBox');
    if (box) box.innerHTML = state.items.map(itemRow).join('');
    bindPaste(document.getElementById('comment'));
  } catch (e) { toast(e.message, true); }
  finally { state.loadingMore = false; }
}

async function loadFeed() {
  const data = await api('/api/feed?limit=150');
  state.feed = data.events || [];
}

async function loadDetail(id) {
  const [detail, events] = await Promise.all([
    api('/api/items/' + id),
    api(`/api/items/${id}/events?limit=80`),
  ]);
  state.detail = detail;
  state.timeline = events.events || [];
}

/* ============================================================ 渲染 */

function render() {
  if (!state.me) return renderAuth();

  let main;
  if (state.route.name === 'item') main = viewDetail();
  else if (state.route.name === 'new') main = viewNew();
  else if (state.route.name === 'feed') main = viewFeed();
  else main = viewList();

  app.innerHTML = topbar() + `
    <div class="page">
      <aside class="side">${sideLeft()}</aside>
      ${main}
      <aside class="side-right">${sideRight()}</aside>
    </div>` + membersModal() + profileModal();

  afterRender();
}

/* ---------------------------------------------------------- 登录页 */

function renderAuth() {
  const setup = state.boot?.needs_setup;
  const isReg = state.authMode === 'register';
  const title = state.boot?.app_title || '局域网轻量记事本';

  app.innerHTML = `
  <div class="auth-wrap"><div class="auth-card">
    <h1>${esc(title)}<span class="dot">.</span></h1>
    <p class="hint">${
      setup ? '首次启动 · 请创建管理员账号（需要终端打印的初始管理员邀请码）'
            : isReg ? '使用邀请码注册新账号' : '请登录后使用'
    }</p>
    ${state.authErr ? `<div class="err">${esc(state.authErr)}</div>` : ''}
    ${state.authInfo ? `<div class="ok">${esc(state.authInfo)}</div>` : ''}
    <form id="authForm">
      <div class="field"><label>账号</label>
        <input name="username" autocomplete="username" required></div>
      <div class="field"><label>密码</label>
        <input name="password" type="password"
               autocomplete="${isReg ? 'new-password' : 'current-password'}" required></div>
      ${isReg ? `
        <div class="field"><label>昵称（可留空，默认同账号）</label>
          <input name="nickname" autocomplete="off"></div>
        <div class="field"><label>${setup ? '初始管理员邀请码' : '邀请码'}</label>
          <input name="${setup ? 'initial_admin_code' : 'invite_code'}"
                 placeholder="${setup ? '见启动终端' : 'XXXX-XXXX'}" autocomplete="off" required></div>` : ''}
      <button class="btn primary block" type="submit">${isReg ? '注册并进入' : '登录'}</button>
    </form>
    ${setup ? '' : `<div class="switch-line">
      ${isReg ? '已有账号？<a data-act="auth-mode" data-mode="login">去登录</a>'
              : '有邀请码？<a data-act="auth-mode" data-mode="register">去注册</a>'}
    </div>`}
  </div></div>`;

  document.getElementById('authForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const body = Object.fromEntries(new FormData(e.target).entries());
    state.authErr = null; state.authInfo = null;
    try {
      if (isReg) {
        const r = await api('/api/register', { method: 'POST', body });
        state.authMode = 'login';
        state.authInfo = r.is_admin ? '管理员账号已创建，请登录' : '注册成功，请登录';
        state.boot.needs_setup = false;
        render();
      } else {
        await api('/api/login', { method: 'POST', body });
        state.me = await api('/api/me');
        state.boot.needs_setup = false;
        state.boot.logged_in = true;
        await refreshSide();
        go('#/');
        await onRoute();
      }
    } catch (err) { state.authErr = err.message; render(); }
  });
}

/* ---------------------------------------------------------- 顶栏 */

function topbar() {
  const t = state.route.name;
  const item = (to, label, on) =>
    `<button data-act="nav" data-to="${to}" class="${on ? 'cur' : ''}">${label}</button>`;
  return `
  <nav class="navbar"><div class="navbar-inner">
    <div class="brand">${esc(state.boot?.app_title || '局域网轻量记事本')}<span class="dot">.</span></div>
    <div class="nav-links">
      ${item('#/', '全部', t === 'list' && state.tab === 'all')}
      ${item('#/t/task', '任务', t === 'list' && state.tab === 'task')}
      ${item('#/t/article', '文章', t === 'list' && state.tab === 'article')}
      ${item('#/feed', '动态', t === 'feed')}
    </div>
    <div class="nav-user">
      <button class="user-btn" data-act="profile">
        ${avatar(state.me)}<span>${esc(state.me.nickname)}${state.me.is_admin ? ' · 管理员' : ''}</span>
      </button>
      <button class="btn sm" data-act="logout">退出</button>
    </div>
  </div></nav>`;
}

/* ---------------------------------------------------------- 左栏 */

function sideLeft() {
  const me = state.me;
  const s = state.stats || {};
  const cats = [
    ['#/', '全部', (s.tasks || 0) + (s.articles || 0), state.route.name === 'list' && state.tab === 'all' && !state.tag],
    ['#/t/task', '任务', s.tasks || 0, state.route.name === 'list' && state.tab === 'task'],
    ['#/t/article', '文章', s.articles || 0, state.route.name === 'list' && state.tab === 'article'],
    ['#/feed', '动态', s.events || 0, state.route.name === 'feed'],
  ];
  const curName = cats.find((c) => c[3])?.[1] || '全部';

  return `
  <div class="s-card about-card">
    <div class="about-banner"></div>
    <div class="about-body">
      ${me.avatar_url
        ? `<img class="about-avatar" src="${esc(me.avatar_url)}" alt="">`
        : `<div class="about-avatar">${esc((me.nickname || '?').slice(0, 1))}</div>`}
      <div class="about-name-slot">${profileNameHtml(me)}</div>
      <div class="about-sub">${esc(me.username)} · ${me.is_admin ? '管理员' : '成员'}</div>
      <div class="about-meta">
        注册 ${fmtDate(me.created_at)}<br>
        上次登录 ${me.last_login_at ? fmtDate(me.last_login_at) : '—'}<br>
        当前 IP ${esc(me.current_ip)}
      </div>
      <button class="btn sm block" data-act="profile">我的资料</button>
    </div>
  </div>

  <div class="s-card cat-card ${state.catFolded ? 'folded' : ''}" id="catCard">
    <div class="cat-toggle" data-act="toggle-cat">
      <span>分类</span>
      <span class="arrow">▾</span>
    </div>
    <div class="cat-list">
      ${cats.map(([to, name, cnt, on]) => `
        <button class="cat-row ${on ? 'cur' : ''}" data-act="nav" data-to="${to}">
          <span>${name}</span><span class="cnt">${cnt}</span>
        </button>`).join('')}
    </div>
  </div>

  <div class="s-card cat-card tag-card ${state.tagFolded ? 'folded' : ''}" id="tagCard">
    <div class="cat-toggle" data-act="toggle-tag">
      <span>标签</span>
      <span class="tiny">${esc(scopeLabel())}</span>
      <span class="arrow">▾</span>
    </div>
    <div class="cat-list">
      ${state.tags.length
        ? `<div class="cloud">${state.tags.map((tg, i) => `
            <span class="tag ${state.tag === tg.name ? 'cur' : ''}" data-act="tag"
                  data-tag="${esc(tg.name)}" style="animation-delay:${Math.min(i, 20) * 25}ms"
            >${esc(tg.name)}<span style="opacity:.5;margin-left:4px">${tg.used}</span></span>`).join('')}</div>`
        : `<div class="empty">${
            state.tab === 'all' ? '还没有标签。新建内容时填上标签，这里就会长出来。'
                                : `「${kindName(state.tab)}」里还没有标签。`}</div>`}
    </div>
  </div>

  <div class="s-card">
    <div class="s-head">邀请码<span class="spacer"></span>
      <button class="btn sm" data-act="gen-invite">生成</button></div>
    ${state.invites.length
      ? state.invites.slice(0, 4).map((iv) => `
          <div class="todo">
            <span class="t mono">${esc(iv.display)}</span>
            <span class="tiny">${
              iv.state === 'valid' ? '可用' : iv.state === 'used' ? `已用·${esc(iv.used_by || '')}` : '已过期'
            }</span>
          </div>`).join('')
      : '<div class="empty">还没有生成过邀请码。同事凭一次性邀请码自助注册。</div>'}
  </div>`;
}

/* ---------------------------------------------------------- 右栏 */

function todoBlock(arr, emptyText) {
  return arr && arr.length
    ? arr.map((it) => `
      <div class="todo">
        <span class="t" data-act="open" data-id="${it.id}">${esc(it.title)}</span>
        ${it.next_progress
          ? `<button class="btn sm" data-act="advance" data-id="${it.id}"
               title="推进「${esc(it.next_progress.title)}」">+1</button>` : ''}
      </div>`).join('')
    : `<div class="empty">${emptyText}</div>`;
}

function sideRight() {
  const s = state.stats || {};
  const t = state.todos || {};
  const mb = ((s.images_bytes || 0) / 1048576).toFixed(1);
  const cl = state.checklist || { pending: [], done: [] };

  // 概览：只放有明确去处的前四项，一行排开，高度压到最低
  const stats = [
    ['#/t/task', s.tasks || 0, '任务'],
    ['#/t/article', s.articles || 0, '文章'],
    ['#/feed', s.events || 0, '动态'],
    ['members', s.users || 0, '成员'],
  ];

  return `
  <div class="s-card">
    <div class="s-head">概览</div>
    <div class="stat-grid">
      ${stats.map(([to, n, label]) => `
        <div class="stat" data-act="stat-go" data-to="${to}" title="查看${label}">
          <div class="n">${n}</div><div class="l">${label}</div>
        </div>`).join('')}
    </div>
    <div class="tiny" style="margin-top:8px;text-align:center">
      标签 ${s.tags || 0} 个 · 图片 ${mb} MB
    </div>
  </div>

  <div class="s-card">
    <div class="s-head">待办<span class="spacer"></span>
      <span class="cn">${cl.pending.length ? cl.pending.length + ' 项' : '已清空'}</span></div>
    ${cl.pending.length
      ? cl.pending.map(todoRow).join('')
      : '<div class="empty">眼下没有待办。</div>'}
    <form class="check-add" id="checkForm">
      <input name="title" placeholder="添加一条待办…" autocomplete="off" required>
      <button class="btn sm primary" type="submit">加</button>
    </form>
    ${cl.done.length ? `
      <div class="done-toggle ${state.doneOpen ? 'open' : ''}" data-act="toggle-done">
        <span class="arrow">▸</span>已完成 ${cl.done.length} 项
      </div>
      ${state.doneOpen
        ? `<div style="margin-top:8px">${cl.done.map(todoRow).join('')}</div>` : ''}` : ''}
  </div>

  <div class="s-card">
    <div class="s-head">我的任务</div>
    ${todoBlock(t.my_open_tasks, '没有未完成的任务')}
  </div>

  <div class="s-card">
    <div class="s-head">模板</div>
    ${state.templates.length
      ? state.templates.map((tpl) => `
          <div class="todo">
            <span class="t" data-act="use-template" data-id="${tpl.id}">${esc(tpl.name)}</span>
            ${trashIcon('del-template', tpl.id, '删除模板')}
          </div>`).join('')
      : '<div class="empty">还没有模板。新建任务时勾选「存为模板」。</div>'}
  </div>

  <div class="s-card">
    <div class="s-head">定时规则<span class="spacer"></span>
      <button class="btn sm" data-act="toggle-recur-form">${state.showRecurForm ? '收起' : '新建'}</button></div>
    ${state.recurring.length
      ? state.recurring.map((r) => `
          <div class="todo">
            <span class="t">${esc(r.title)}
              <span class="tiny"> · ${recurText(r)}</span>
            </span>
            <button class="btn sm" data-act="toggle-recurring" data-id="${r.id}"
                    data-on="${r.enabled ? 1 : 0}">${r.enabled ? '停' : '启'}</button>
          </div>`).join('')
      : '<div class="empty">还没有定时规则。例：每日例行检查、每周汇总。</div>'}
    ${state.showRecurForm ? `
      <form id="recurForm" style="margin-top:14px">
        <div class="form-row"><input type="text" name="title" placeholder="如：每日例行检查" required></div>
        <div class="form-row">
          <label>频率</label>
          <select name="freq" id="recurFreq">
            <option value="daily"${state.recurFreq === 'daily' ? ' selected' : ''}>每天</option>
            <option value="weekly"${state.recurFreq === 'weekly' ? ' selected' : ''}>每周</option>
          </select>
        </div>
        <div class="form-row" id="wkRow" style="display:${state.recurFreq === 'weekly' ? 'block' : 'none'}">
          <label>星期几（可多选）</label>
          <div class="wk-picker" id="wkPicker">
            ${'一二三四五六日'.split('').map((d, i) => `
              <button type="button" data-act="wk-toggle" data-wd="${i}"
                      class="${state.recurWeekdays.includes(i) ? 'on' : ''}">${d}</button>`).join('')}
          </div>
          <div class="wk-quick">
            <button type="button" data-act="wk-preset" data-days="0,1,2,3,4">工作日</button>
            <button type="button" data-act="wk-preset" data-days="0,1,2,3,4,5">周一到周六</button>
            <button type="button" data-act="wk-preset" data-days="0,1,2,3,4,5,6">全选</button>
          </div>
        </div>
        <div class="form-row"><input type="text" name="time_of_day" value="09:00" placeholder="HH:MM"></div>
        <button class="btn primary block" type="submit">添加规则</button>
      </form>` : ''}
  </div>`;
}

/* 分类与标签的联动：标签云只显示"当前分类内"的标签与计数。
   规则取自参考包 DESIGN-DETAILS §3.3 / §7 —— 标签跟随分类、点标签只过滤不切分类、
   切分类清空搜索、搜索与分类标签叠加。 */
const KIND_NAMES = { all: '全部', task: '任务', article: '文章' };

function kindName(kind) {
  return KIND_NAMES[kind] || '全部';
}

function scopeLabel() {
  if (state.tag) return `已筛选 #${state.tag}`;
  return state.tab === 'all' ? '全站' : `仅${kindName(state.tab)}`;
}

function recurText(r) {
  if (r.freq === 'daily') return `每天 ${r.time_of_day}`;
  const names = '一二三四五六日';
  const wd = (r.weekdays || []).slice().sort((a, b) => a - b);
  const key = wd.join(',');
  if (!wd.length || key === '0,1,2,3,4,5,6') return `每天 ${r.time_of_day}`;
  if (key === '0,1,2,3,4') return `工作日 ${r.time_of_day}`;
  if (key === '0,1,2,3,4,5') return `周一~周六 ${r.time_of_day}`;
  if (key === '5,6') return `周末 ${r.time_of_day}`;
  return `周${wd.map((i) => names[i]).join('、')} ${r.time_of_day}`;
}

function todoRow(t) {
  const flag = t.done ? '1' : '0';
  return `
  <div class="check ${t.done ? 'done' : ''}">
    <button class="box" data-act="check-toggle" data-id="${t.id}" data-done="${flag}"
            title="${t.done ? '取消完成' : '标记完成'}">${t.done ? '✓' : ''}</button>
    <span class="t" data-act="check-toggle" data-id="${t.id}" data-done="${flag}"
          title="${t.done && t.done_by ? '由 ' + esc(t.done_by) + ' 完成' : '点击标记完成'}">${esc(t.title)}</span>
    ${t.source === 'recurring' ? '<span class="src">定时</span>' : ''}
    ${trashIcon('check-del', t.id, '删除这条待办')}
  </div>`;
}

/* ---------------------------------------------------------- 中栏：列表 */

function viewList() {
  const titles = { all: ['记事本', 'Notes & Tasks'], task: ['任务', 'Tasks'], article: ['文章', 'Articles'] };
  const [h1, en] = titles[state.tab] || titles.all;

  return `
  <div class="main">
    <div class="main-head">
      <h1>${h1}</h1><span class="en">${en}</span>
      <p class="desc">共 ${state.total} 条${
        state.q ? ` · 关键词「${esc(state.q)}」` : ''}${
        state.tag ? ` · 标签「${esc(state.tag)}」` : ''}</p>
    </div>

    <div class="search-bar">
      <label class="box">🔍<input id="q" placeholder="搜索标题或标签…" value="${esc(state.q)}"></label>
      <select id="mode">
        <option value="filter"${state.mode === 'filter' ? ' selected' : ''}>外层过滤</option>
        <option value="title"${state.mode === 'title' ? ' selected' : ''}>标题</option>
        <option value="body"${state.mode === 'body' ? ' selected' : ''}>正文</option>
        <option value="events"${state.mode === 'events' ? ' selected' : ''}>评论</option>
        <option value="all"${state.mode === 'all' ? ' selected' : ''}>全部内容</option>
      </select>
      <button class="btn primary" data-act="new">新建</button>
    </div>

    ${state.tag ? `<div class="s-card" style="padding:11px 16px;display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">
      <span style="font-size:12.5px">标签筛选：<b>${esc(state.tag)}</b></span>
      <button class="btn sm" data-act="clear-tag">清除</button></div>` : ''}

    <div class="rows" id="rowsBox">${state.items.map(itemRow).join('')}</div>
    ${state.items.length ? '' : '<div class="s-card empty">还没有内容，点右上角「新建」开始。</div>'}

    <div id="sentinel" style="height:1px"></div>
    <div class="empty" style="text-align:center;padding:14px 0">
      ${state.hasMore ? '继续下滑加载…' : (state.total ? `已经到底了 · 共 ${state.total} 条` : '')}
    </div>
  </div>`;
}

function itemRow(it, i = 0) {
  const pr = it.progress || { done: 0, total: 0, percent: 0 };
  const le = it.latest_event;
  const np = it.next_progress;
  const sum = plainText(it.excerpt);

  return `
  <article class="row-item anim" style="animation-delay:${Math.min(i, 10) * 60}ms">
    <div class="row-num">${pad2(it.id)}</div>
    <div class="row-body">
      <div class="row-title" data-act="open" data-id="${it.id}">${esc(it.title)}</div>
      ${sum ? `<div class="row-sum">${esc(sum)}</div>` : ''}
      ${it.tags.length ? `<div class="row-tags">${
        it.tags.map((t) => `<span class="tag" data-act="tag" data-tag="${esc(t)}">${esc(t)}</span>`).join('')
      }</div>` : ''}
      ${it.kind === 'task' && pr.total ? `
        <div class="row-prog">
          <div class="bar"><i style="width:${pr.percent}%"></i></div>
          <span class="pct">${pr.done}/${pr.total}</span>
          ${np ? `<button class="btn sm primary" data-act="advance" data-id="${it.id}"
                   title="把「${esc(np.title)}」标为已完成">推进 +1</button>` : ''}
        </div>` : ''}
      ${le ? `<div class="row-latest">
        ${avatar(le.actor)}
        <span class="ellip"><b>${esc(le.actor.nickname || le.actor.username)}</b> ${esc(le.text)}</span>
        <span style="margin-left:auto;white-space:nowrap">${fmtTime(le.created_at)}</span>
      </div>` : ''}
      <div class="row-meta">
        <span class="kind ${it.kind === 'article' ? 'article' : ''}">${esc(it.kind_label || '')}</span>
        <span>${esc(it.author.nickname || it.author.username)}</span>
        <span>创建 ${fmtDate(it.created_at)}</span>
        <span>最后活动 ${relTime(it.last_activity_at)}</span>
        <span>阅读 ${it.view_count}</span>
        ${it.end_at ? `<span>结束 ${fmtDate(it.end_at)}</span>` : ''}
        <span class="acts">
          ${(state.me.is_admin || it.author.username === state.me.username)
            ? trashIcon('del-item', it.id, '删除这条内容') : ''}
        </span>
      </div>
    </div>
  </article>`;
}

/* ---------------------------------------------------------- 中栏：动态 */

function viewFeed() {
  const groups = groupByDay(state.feed, (e) => new Date(e.created_at));
  return `
  <div class="main">
    <div class="main-head">
      <h1>动态</h1><span class="en">Activity</span>
      <p class="desc">全组所有人的推进、标记与发言，按时间倒序 · 点标题可直接跳转</p>
    </div>
    ${groups.length
      ? groups.map((g) => `
          <div class="rows">${g.items.map((e, i) => eventRow(e, true, i)).join('')}</div>
          ${timeGap(g.label, g.items.length)}`).join('')
      : '<div class="s-card empty">还没有任何动态。</div>'}
  </div>`;
}

/* 时间分组：参考包约定「先内容后时间戳」——分隔行在组内容之后 */
function groupByDay(list, getDate) {
  const groups = [];
  let cur = null;
  for (const it of list) {
    const d = getDate(it);
    const key = isNaN(d.getTime()) ? '?' : d.toDateString();
    if (!cur || cur.key !== key) {
      cur = { key, label: isNaN(d.getTime()) ? '未知时间' : dayLabel(d), items: [] };
      groups.push(cur);
    }
    cur.items.push(it);
  }
  return groups;
}

function timeGap(label, count) {
  return `<div class="time-gap"><span class="dia"></span>${esc(label)}<span class="cnt">${count} 条</span><span class="dia"></span></div>`;
}

/* 事件行：详情页时间线与动态页共用 */
function eventRow(e, showItem, i = 0) {
  const isComment = e.type === 'comment';
  const content = isComment ? e.text : (e.type === 'note' ? e.body : '');
  const head = isComment ? '' : esc(e.text);
  const canDelete = state.me.is_admin || e.actor.username === state.me.username;
  const isTodo = e.type.startsWith('todo_');

  // 待办事件的标题已经写在一行描述里了，这里只标一个来源徽章，不重复标题
  const target = !showItem ? ''
    : e.item
      ? `<a class="itemlink" data-act="open" data-id="${e.item.id}">${
          esc(e.item.kind_label || '内容')}：${esc(e.item.title)}</a>`
      : (isTodo ? '<span class="kind todo">待办</span>' : '');

  return `
  <div class="ev anim" style="animation-delay:${Math.min(i, 10) * 60}ms">
    ${avatar(e.actor)}
    <div class="txt">
      <div class="who-line">
        <b>${esc(e.actor.nickname || e.actor.username)}</b>${head ? ' ' + head : ''}
        ${canDelete ? trashIcon('del-event', e.id, '删除这条记录') : ''}
      </div>
      ${content ? `<div class="msg">${mdToHtml(content)}</div>` : ''}
      <div class="sub">
        ${target}
        <span>${fmtTime(e.created_at)} · ${esc(e.actor_ip)}</span>
      </div>
    </div>
  </div>`;
}

/* ---------------------------------------------------------- 中栏：详情 */

function viewDetail() {
  const d = state.detail;
  if (!d) return '<div class="main"><div class="s-card">加载中…</div></div>';

  const isTask = d.kind === 'task';
  const open = (d.progress_items || []).find((p) => p.status !== 'done');

  return `
  <div class="main">
    <button class="btn sm" data-act="back" style="margin-bottom:16px">← 返回列表</button>

    <div class="s-card">
      <span class="kind ${isTask ? '' : 'article'}">${esc(d.kind_label || '')}</span>
      <h1 class="detail-title" style="margin-top:10px">${esc(d.title)}</h1>
      <div class="meta-row">
        ${avatar(d.author)}
        <span>${esc(d.author.nickname || d.author.username)}</span>
        <span>创建 ${fmtDate(d.created_at)}</span>
        <span>阅读 ${d.view_count}</span>
        ${d.end_at ? `<span>结束 ${fmtDate(d.end_at)}</span>` : ''}
        <span>最后活动 ${relTime(d.last_activity_at)}</span>
        ${(state.me.is_admin || d.author.username === state.me.username)
          ? trashIcon('del-item', d.id, '删除这条内容') : ''}
      </div>
      ${d.tags.length ? `<div class="row-tags" style="margin-left:0;margin-top:12px">${
        d.tags.map((t) => `<span class="tag" data-act="tag" data-tag="${esc(t)}">${esc(t)}</span>`).join('')
      }</div>` : ''}
      ${d.body_md ? `<div class="tpost-body">${mdToHtml(d.body_md)}</div>` : ''}
    </div>

    ${isTask && d.progress_items.length ? `
    <div class="s-card">
      <div class="stage-head">
        <div class="s-head" style="margin:0">阶段</div>
        <span class="tiny">已完成 ${d.progress.done}/${d.progress.total} · ${d.progress.percent}%</span>
      </div>
      <div class="bar" style="margin-bottom:14px"><i style="width:${d.progress.percent}%"></i></div>
      <div class="advance-row">
        ${open
          ? `<button class="btn primary" data-act="advance" data-id="${d.id}">
               推进：把 阶段[${open.seq}]「${esc(open.title)}」标为已完成</button>`
          : '<span class="all-done">✓ 所有阶段都已完成</span>'}
        <button class="btn sm" data-act="rollback-item" data-id="${d.id}">回退</button>
        <button class="btn sm" data-act="reset-item" data-id="${d.id}">重置</button>
      </div>
      <div class="stages">${d.progress_items.map(stageRow).join('')}</div>
    </div>` : ''}

    <div class="s-card">
      <div class="s-head">时间线<span class="spacer"></span>
        <span class="cn">${state.timeline.length} 条</span></div>
      <div class="timeline">
        ${state.timeline.length ? state.timeline.map((e, i) => eventRow(e, false, i)).join('')
                                : '<div class="empty">还没有动态</div>'}
      </div>
      <div class="composer">
        <textarea id="comment" placeholder="发布一条进展消息…（可直接 Ctrl+V 粘贴截图）"></textarea>
        <div class="row">
          <span class="tip">消息会留在时间线上，所有人可见</span>
          <button class="btn primary" data-act="comment" data-id="${d.id}">发布</button>
        </div>
      </div>
    </div>
  </div>`;
}

function stageRow(p) {
  const hint = p.status_hint ? `（${p.status_hint}）` : '';
  return `
  <div class="stage ${p.status}">
    <span class="seq">${p.seq}</span>
    <span class="name">
      <span class="t">${esc(p.title)}</span>
      <span class="st ${p.status}" title="${esc(p.status_label + hint)}">${esc(p.status_label)}</span>
      ${p.note ? `<div class="note">${esc(p.note)}</div>` : ''}
    </span>
    <span class="ops">
      <select data-act="status" data-pid="${p.id}">${statusOptions(p.status)}</select>
      <button class="btn sm" data-act="note" data-pid="${p.id}">备注</button>
    </span>
  </div>`;
}

function statusOptions(current) {
  const list = state.boot?.statuses || [
    { value: 'pending', label: '未开始', hint: '' },
    { value: 'done', label: '已完成', hint: '' },
    { value: 'blocked', label: '卡住', hint: '等别人或等条件' },
    { value: 'delayed', label: '延期', hint: '比计划慢' },
  ];
  return list.map((s) => {
    const h = s.hint ? `（${s.hint}）` : '';
    return `<option value="${s.value}"${current === s.value ? ' selected' : ''}>${esc(s.label + h)}</option>`;
  }).join('');
}

/* ---------------------------------------------------------- 中栏：新建 */

function viewNew() {
  const d = state.newDraft || { kind: 'task', title: '', tags: '', body_md: '', progress_titles: '', end_at: '' };
  state.newDraft = d;

  return `
  <div class="main">
    <button class="btn sm" data-act="back" style="margin-bottom:16px">← 返回列表</button>
    <div class="main-head">
      <h1>新建</h1><span class="en">Compose</span>
      <p class="desc">任务可以带多个阶段；文章发布后不可编辑，只能由管理员删除</p>
    </div>

    <div class="s-card">
      <form id="newForm">
        <div class="form-row">
          <label>类型</label>
          <div class="radio-row">
            <label><input type="radio" name="kind" value="task" ${d.kind === 'task' ? 'checked' : ''}> 任务（带阶段）</label>
            <label><input type="radio" name="kind" value="article" ${d.kind === 'article' ? 'checked' : ''}> 文章 / 经验</label>
          </div>
        </div>
        <div class="form-row"><label>标题</label>
          <input type="text" name="title" value="${esc(d.title)}" required></div>
        <div class="form-row"><label>标签（逗号分隔）</label>
          <input type="text" name="tags" value="${esc(d.tags)}" placeholder="运维, 部署"></div>
        <div class="form-row"><label>阶段（每行一个，仅任务需要）</label>
          <textarea name="progress_titles" style="min-height:96px"
            placeholder="需求确认&#10;开发实现&#10;测试验收">${esc(d.progress_titles)}</textarea></div>
        <div class="form-row"><label>结束时间（可选）</label>
          <input type="text" name="end_at" value="${esc(d.end_at)}" placeholder="2026-10-20 18:00"></div>
        <div class="form-row"><label>正文（Markdown，可直接 Ctrl+V 粘贴截图）</label>
          <textarea name="body_md" id="newBody" style="min-height:220px">${esc(d.body_md)}</textarea></div>
        <div class="form-row"><label>从模板载入</label>
          <select id="tplPick">
            <option value="">— 不使用模板 —</option>
            ${state.templates.map((t) => `<option value="${t.id}">${esc(t.name)}</option>`).join('')}
          </select></div>
        <div class="form-row">
          <label><input type="checkbox" id="saveTpl"> 把当前配置存为模板</label>
          <input type="text" id="tplName" placeholder="模板名称" style="margin-top:6px">
        </div>
        <button class="btn primary block" type="submit">创建</button>
      </form>
    </div>
  </div>`;
}

/* ---------------------------------------------------------- 组员树弹窗 */

function membersModal() {
  if (!state.membersOpen) return '';
  const m = state.members;
  const nodeHtml = (n) => `
    <div class="mnode">
      ${avatar({ username: n.username, nickname: n.nickname, avatar_url: n.avatar_url }, 'lg')}
      <div class="who">
        <div class="nm">${esc(n.nickname)}${
          n.is_admin ? ' <span class="badge">管理员</span>' : ''}</div>
        <div class="sub">
          ID ${n.id} · ${esc(n.username)} · 注册 ${fmtDate(n.created_at)}<br>
          ${esc(n.created_ip)}${n.invited_count ? ` · 邀请了 ${n.invited_count} 人` : ''}
        </div>
      </div>
    </div>
    ${n.children && n.children.length
      ? `<div class="mkids">${n.children.map(nodeHtml).join('')}</div>` : ''}`;

  return `
  <div class="modal-mask" data-act="close-members-bg">
    <div class="modal-card" style="max-width:520px">
      <h3>组员 · 共 ${m ? m.total : 0} 人</h3>
      ${m
        ? `<div class="mtree">${m.roots.map((r) => `<div class="mroot">${nodeHtml(r)}</div>`).join('')}</div>`
        : '<div class="empty">加载中…</div>'}
      <div class="hintline" style="margin-top:16px">
        缩进表示邀请关系：上层的人用自己的一次性邀请码邀请了下面的人。
      </div>
      <div class="modal-actions">
        <button class="btn" data-act="close-members">关闭</button>
      </div>
    </div>
  </div>`;
}

/* ---------------------------------------------------------- 资料弹窗 */

function profileModal() {
  if (!state.profileOpen) return '';
  return `
  <div class="modal-mask" data-act="close-profile-bg">
    <div class="modal-card" data-stop="1">
      <h3>我的资料</h3>
      <div class="form-row"><label>账号（不可修改）</label>
        <input type="text" value="${esc(state.me.username)}" disabled></div>
      <div class="form-row"><label>昵称</label>
        <input type="text" id="nickInput" value="${esc(state.me.nickname)}" maxlength="32"></div>
      <div class="form-row"><label>头像</label>
        <div class="avatar-row">
          ${avatar(state.me, 'lg')}
          <input type="file" id="avatarFile" accept="image/*">
        </div>
        <div class="hintline">支持 png / jpg / gif / webp，单张不超过 ${state.boot?.max_upload_mb || 5}MB</div>
      </div>
      ${state.profileErr ? `<div class="err">${esc(state.profileErr)}</div>` : ''}
      <div class="modal-actions">
        <button class="btn" data-act="close-profile">取消</button>
        <button class="btn primary" data-act="save-profile">保存</button>
      </div>
      <div class="hintline" style="margin-top:16px">
        注册于 ${fmtDate(state.me.created_at)} · 注册 IP ${esc(state.me.created_ip)}<br>
        上次登录 ${fmtDate(state.me.last_login_at)} · 当前 IP ${esc(state.me.current_ip)}
      </div>
    </div>
  </div>`;
}

/* ============================================================ 渲染后挂钩 */

let observer = null;

function afterRender() {
  const q = document.getElementById('q');
  if (q) {
    q.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { state.q = q.value.trim(); state.tag = ''; onRoute(); }
    });
  }
  const mode = document.getElementById('mode');
  if (mode) {
    mode.addEventListener('change', (e) => {
      state.mode = e.target.value;
      if (state.q) onRoute();
    });
  }

  bindPaste(document.getElementById('newBody'));
  bindPaste(document.getElementById('comment'));

  const tpl = document.getElementById('tplPick');
  if (tpl) tpl.addEventListener('change', onPickTemplate);
  const form = document.getElementById('newForm');
  if (form) form.addEventListener('submit', onCreateItem);
  const av = document.getElementById('avatarFile');
  if (av) av.addEventListener('change', onAvatarPicked);

  const checkForm = document.getElementById('checkForm');
  if (checkForm) checkForm.addEventListener('submit', onAddTodo);

  // 频率切换只切显隐，不重渲染——否则会把用户敲了一半的标题冲掉
  const rf = document.getElementById('recurFreq');
  if (rf) {
    rf.addEventListener('change', (e) => {
      state.recurFreq = e.target.value;
      const row = document.getElementById('wkRow');
      if (row) row.style.display = state.recurFreq === 'weekly' ? 'block' : 'none';
    });
  }

  // 无限滚动
  if (observer) { observer.disconnect(); observer = null; }
  const sentinel = document.getElementById('sentinel');
  if (sentinel && state.hasMore) {
    observer = new IntersectionObserver((entries) => {
      if (entries.some((en) => en.isIntersecting)) loadMore();
    }, { rootMargin: '500px' });
    observer.observe(sentinel);
  }

  updateToTop();
}

/* 回到顶部 */
function updateToTop() {
  const y = window.scrollY || document.documentElement.scrollTop;
  const nearBottom = y + window.innerHeight > document.body.scrollHeight - 260;
  toTopEl.style.setProperty('--lift-h', nearBottom ? '110px' : '0px');
  toTopEl.classList.toggle('show', y > 320);
}
window.addEventListener('scroll', updateToTop, { passive: true });
toTopEl.addEventListener('click', () => window.scrollTo({ top: 0, behavior: 'smooth' }));

/* ============================================================ 粘贴上传 */

function insertAtCursor(ta, text) {
  const s = ta.selectionStart ?? ta.value.length;
  const e = ta.selectionEnd ?? ta.value.length;
  ta.value = ta.value.slice(0, s) + text + ta.value.slice(e);
  ta.selectionStart = ta.selectionEnd = s + text.length;
}

function bindPaste(ta) {
  if (!ta || ta.dataset.pasteBound) return;
  ta.dataset.pasteBound = '1';
  ta.addEventListener('paste', async (ev) => {
    const items = ev.clipboardData?.items || [];
    let file = null;
    for (const it of items) {
      if (it.type && it.type.startsWith('image/')) { file = it.getAsFile(); break; }
    }
    if (!file) return;
    ev.preventDefault();
    const placeholder = '![上传中…]()';
    insertAtCursor(ta, placeholder);
    try {
      const fd = new FormData();
      fd.append('file', file, 'paste.png');
      const res = await fetch('/api/upload', { method: 'POST', body: fd, credentials: 'same-origin' });
      const data = await res.json();
      if (!res.ok) throw new Error(data?.error?.message || '上传失败');
      ta.value = ta.value.replace(placeholder, `![](${data.url})`);
      toast(data.deduped ? '图片已插入（内容重复，已复用）' : '图片已插入');
    } catch (err) {
      ta.value = ta.value.replace(placeholder, '');
      toast(err.message, true);
    }
  });
}

/* ============================================================ 表单处理 */

async function onPickTemplate(e) {
  const id = e.target.value;
  if (!id) return;
  try {
    const tpl = await api('/api/templates/' + id);
    const p = tpl.payload || {};
    state.newDraft = {
      ...(state.newDraft || {}),
      title: p.title_pattern || state.newDraft?.title || '',
      tags: (p.tags || []).join(', '),
      body_md: p.body_md || '',
      progress_titles: (p.progress_titles || []).join('\n'),
      templateId: tpl.id,
    };
    render();
    toast('已载入模板：' + tpl.name);
  } catch (err) { toast(err.message, true); }
}

async function onCreateItem(e) {
  e.preventDefault();
  const f = new FormData(e.target);
  const kind = f.get('kind');
  const body = {
    kind,
    title: (f.get('title') || '').trim(),
    tags: (f.get('tags') || '').split(/[,，]/).map((s) => s.trim()).filter(Boolean),
    body_md: f.get('body_md') || '',
    progress_titles: kind === 'task'
      ? (f.get('progress_titles') || '').split('\n').map((s) => s.trim()).filter(Boolean)
      : [],
    end_at: (f.get('end_at') || '').trim() || null,
    template_id: state.newDraft?.templateId || null,
  };
  try {
    const saveTpl = document.getElementById('saveTpl');
    const tplName = document.getElementById('tplName');
    if (saveTpl && saveTpl.checked && tplName.value.trim()) {
      await api('/api/templates', {
        method: 'POST',
        body: { name: tplName.value.trim(), payload: {
          title_pattern: body.title, tags: body.tags, body_md: body.body_md,
          progress_titles: body.progress_titles,
        } },
      });
    }
    const r = await api('/api/items', { method: 'POST', body });
    state.newDraft = null;
    await refreshSide();
    toast('已创建');
    go('#/item/' + r.id);
  } catch (err) { toast(err.message, true); }
}

async function onAvatarPicked(e) {
  const file = e.target.files?.[0];
  if (!file) return;
  try {
    const fd = new FormData();
    fd.append('file', file, file.name || 'avatar.png');
    const res = await fetch('/api/upload', { method: 'POST', body: fd, credentials: 'same-origin' });
    const data = await res.json();
    if (!res.ok) throw new Error(data?.error?.message || '上传失败');
    await api('/api/me', { method: 'PATCH', body: { avatar_hash: data.hash } });
    state.me = await api('/api/me');
    toast('头像已更新');
    render();
  } catch (err) { state.profileErr = err.message; render(); }
}

async function onAddTodo(e) {
  e.preventDefault();
  const input = e.target.querySelector('input[name=title]');
  const title = (input.value || '').trim();
  if (!title) return;
  try {
    await api('/api/checklist', { method: 'POST', body: { title } });
    input.value = '';
    toast('已加入待办');
    await afterChange();
  } catch (err) { toast(err.message, true); }
}

/* ============================================================ 事件委派 */

app.addEventListener('click', async (e) => {
  const el = e.target.closest('[data-act]');
  if (!el) {
    // 点弹窗外的空白处关闭
    if (e.target.classList?.contains('modal-mask')) {
      const changed = state.profileOpen || state.membersOpen;
      state.profileOpen = false; state.profileErr = null;
      state.membersOpen = false;
      if (changed) render();
    }
    return;
  }
  const act = el.dataset.act;

  try {
    if (act === 'nav') {
      const to = el.dataset.to;
      state.tag = ''; state.q = '';
      if (to === '#/') state.tab = 'all';
      else if (to === '#/t/task') state.tab = 'task';
      else if (to === '#/t/article') state.tab = 'article';
      await refreshSide();   // 标签云跟随分类，切分类后要重取标签与计数
      go(to);
      return;
    }
    if (act === 'auth-mode') {
      state.authMode = el.dataset.mode; state.authErr = null; state.authInfo = null;
      return render();
    }
    if (act === 'logout') {
      await api('/api/logout', { method: 'POST' });
      state.me = null;
      return render();
    }
    if (act === 'toggle-cat') {
      state.catFolded = !state.catFolded;
      const card = document.getElementById('catCard');
      if (card) card.classList.toggle('folded', state.catFolded);
      return;
    }
    if (act === 'toggle-tag') {
      state.tagFolded = !state.tagFolded;
      const card = document.getElementById('tagCard');
      if (card) card.classList.toggle('folded', state.tagFolded);
      return;
    }
    if (act === 'toggle-recur-form') { state.showRecurForm = !state.showRecurForm; return render(); }
    if (act === 'toggle-done') { state.doneOpen = !state.doneOpen; return render(); }

    if (act === 'stat-go') {
      const to = el.dataset.to;
      if (to === 'members') return openMembers();
      state.tag = ''; state.q = '';
      state.tab = to === '#/t/task' ? 'task' : to === '#/t/article' ? 'article' : 'all';
      return go(to);
    }
    if (act === 'wk-toggle') {
      const wd = Number(el.dataset.wd);
      const i = state.recurWeekdays.indexOf(wd);
      if (i >= 0) state.recurWeekdays.splice(i, 1); else state.recurWeekdays.push(wd);
      el.classList.toggle('on', i < 0);
      return;
    }
    if (act === 'wk-preset') {
      state.recurWeekdays = el.dataset.days.split(',').map(Number);
      const picker = document.getElementById('wkPicker');
      if (picker) {
        [...picker.querySelectorAll('button')].forEach((b) => {
          b.classList.toggle('on', state.recurWeekdays.includes(Number(b.dataset.wd)));
        });
      }
      return;
    }
    if (act === 'close-members' || act === 'close-members-bg') {
      state.membersOpen = false; return render();
    }

    if (act === 'check-toggle') {
      const nowDone = el.dataset.done !== '1';
      await api('/api/checklist/' + el.dataset.id, { method: 'PATCH', body: { done: nowDone } });
      if (nowDone) toast('已完成，已收进下方');
      return afterChange();
    }
    if (act === 'check-del') {
      await api('/api/checklist/' + el.dataset.id, { method: 'DELETE' });
      return afterChange();
    }
    if (act === 'tag') {
      state.tag = state.tag === el.dataset.tag ? '' : el.dataset.tag;
      state.q = '';
      if (state.route.name !== 'list') go('#/'); else onRoute();
      return;
    }
    if (act === 'clear-tag') { state.tag = ''; return onRoute(); }
    if (act === 'open') { return go('#/item/' + el.dataset.id); }
    if (act === 'back') { return go('#/'); }
    if (act === 'new') { state.newDraft = null; return go('#/new'); }

    if (act === 'profile') { state.profileOpen = true; state.profileErr = null; return render(); }
    if (act === 'close-profile' || act === 'close-profile-bg') {
      state.profileOpen = false; state.profileErr = null; return render();
    }
    if (act === 'save-profile') {
      const nick = document.getElementById('nickInput').value.trim();
      if (!nick) { state.profileErr = '昵称不能为空'; return render(); }
      await api('/api/me', { method: 'PATCH', body: { nickname: nick } });
      state.me = await api('/api/me');
      state.profileOpen = false; state.profileErr = null;
      toast('已保存');
      return render();
    }

    if (act === 'advance') {
      const r = await api(`/api/items/${el.dataset.id}/advance`, { method: 'POST' });
      toast('已推进：' + r.progress.title);
      return afterChange();
    }
    if (act === 'rollback-item') {
      if (!confirm('把最后一个已完成的阶段退回未开始？')) return;
      const r = await api(`/api/items/${el.dataset.id}/rollback`, { method: 'POST' });
      toast('已回退：' + r.progress.title);
      return afterChange();
    }
    if (act === 'reset-item') {
      if (!confirm('把所有阶段重置为未开始？')) return;
      await api(`/api/items/${el.dataset.id}/reset`, { method: 'POST' });
      toast('已重置全部阶段');
      return afterChange();
    }
    if (act === 'note') {
      const note = prompt('这个阶段的备注：');
      if (note === null) return;
      await api(`/api/progress/${el.dataset.pid}`, { method: 'PATCH', body: { note } });
      toast('备注已保存');
      return afterChange();
    }
    if (act === 'comment') {
      const ta = document.getElementById('comment');
      const message = (ta.value || '').trim();
      if (!message) return toast('内容不能为空', true);
      await api(`/api/items/${el.dataset.id}/events`, {
        method: 'POST', body: { type: 'comment', message },
      });
      ta.value = '';
      toast('已发布');
      return afterChange();
    }
    if (act === 'del-event') {
      if (!confirm('删除这条消息？')) return;
      await api('/api/events/' + el.dataset.id, { method: 'DELETE' });
      return afterChange();
    }
    if (act === 'del-item') {
      if (!confirm('删除后不可恢复，确定？')) return;
      await api('/api/items/' + el.dataset.id, { method: 'DELETE' });
      await refreshSide();
      toast('已删除');
      return go('#/');
    }
    if (act === 'add-progress') {
      const ta = document.getElementById('newStages');
      const titles = (ta?.value || '').split('\n').map((s) => s.trim()).filter(Boolean);
      if (!titles.length) return toast('请填写阶段名称', true);
      await api(`/api/items/${el.dataset.id}/progress`, { method: 'POST', body: { titles } });
      toast('已追加 ' + titles.length + ' 个阶段');
      return afterChange();
    }
    if (act === 'gen-invite') {
      if (!confirm('生成一个一次性邀请码？24 小时内有效，用过即废。')) return;
      const r = await api('/api/invites', { method: 'POST' });
      await refreshSide(); render();
      toast('邀请码：' + r.display);
      return;
    }
    if (act === 'use-template') {
      state.newDraft = null;
      go('#/new');
      setTimeout(() => {
        const sel = document.getElementById('tplPick');
        if (sel) { sel.value = el.dataset.id; sel.dispatchEvent(new Event('change')); }
      }, 60);
      return;
    }
    if (act === 'del-template') {
      if (!confirm('删除该模板？')) return;
      await api('/api/templates/' + el.dataset.id, { method: 'DELETE' });
      await refreshSide(); render();
      return;
    }
    if (act === 'toggle-recurring') {
      await api('/api/recurring/' + el.dataset.id, { method: 'PATCH', body: { enabled: el.dataset.on !== '1' } });
      await refreshSide(); render();
      return;
    }
  } catch (err) { toast(err.message, true); }
});

app.addEventListener('change', async (e) => {
  const el = e.target.closest('[data-act="status"]');
  if (!el) return;
  try {
    await api('/api/progress/' + el.dataset.pid, { method: 'PATCH', body: { status: el.value } });
    const label = (el.options[el.selectedIndex].text || '').split('（')[0];
    toast('已标记为「' + label + '」');
    await afterChange();
  } catch (err) { toast(err.message, true); }
});

app.addEventListener('submit', async (e) => {
  if (e.target.id !== 'recurForm') return;
  e.preventDefault();
  const f = new FormData(e.target);
  const freq = f.get('freq');
  if (freq === 'weekly' && !state.recurWeekdays.length) {
    return toast('每周规则至少要选一个星期几', true);
  }
  try {
    await api('/api/recurring', { method: 'POST', body: {
      title: (f.get('title') || '').trim(),
      freq,
      weekdays: freq === 'weekly' ? state.recurWeekdays : [],
      time_of_day: (f.get('time_of_day') || '09:00').trim(),
    } });
    state.showRecurForm = false;
    await refreshSide(); render();
    toast('规则已添加');
  } catch (err) { toast(err.message, true); }
});

async function openMembers() {
  state.membersOpen = true;
  state.members = null;
  render();
  try {
    state.members = await api('/api/members');
    render();
  } catch (err) { toast(err.message, true); }
}

/* 变更后刷新当前视图 */
async function afterChange() {
  if (state.route.name === 'item') await loadDetail(state.route.id);
  else if (state.route.name === 'feed') await loadFeed();
  else await loadList();
  await refreshSide();
  render();
}

/* ============================================================ 彩蛋素材
 * Lanyora 的手写签名，单条 path、15 个子路径。
 * 内嵌在这里而不是单独放 .svg 文件：文件夹里少一个显眼的文件，
 * 而它本来就只在昵称为 Lanyora 时才会出现在页面上。
 * 填充规则为 nonzero —— 由 15 个子路径的缠绕方向判定得出（7 正 / 8 负），
 * 字母里的孔依赖反向缠绕挖出，改成 evenodd 会算错。
 */
const SIGNATURE_PATH = 'M2.0 -55Q-6.0 -74 -6.0 -94.0Q-6.0 -114 13.5 -136.5Q33.0 -159 75.5 -159.0Q118.0 -159 211.0 -113Q222.0 -135 234.0 -171Q227.0 -171 220.0 -171Q147.0 -171 113.0 -209Q93.0 -232 93.0 -259.5Q93.0 -287 106.0 -315Q70.0 -366 70.0 -428Q70.0 -537 148.0 -576Q167.0 -586 191.5 -586.0Q216.0 -586 244.0 -558Q289.0 -513 289.0 -403Q289.0 -335 267.0 -246Q282.0 -246 302.0 -251Q333.0 -414 373.0 -485Q428.0 -585 480.0 -585Q525.0 -585 542.0 -522Q549.0 -496 549.0 -468Q549.0 -414 521.5 -357.0Q494.0 -300 439.0 -252.5Q384.0 -205 310.0 -183Q293.0 -127 270.0 -80Q329.0 -44 409.0 -11Q598.0 66 627.0 -13Q631.0 -22 635.0 -21Q637.0 -20 637.0 -17.5Q637.0 -15 636.0 -12Q622.0 27 582.0 42Q552.0 54 531.0 54Q469.0 54 402.0 22.5Q335.0 -9 297.0 -35L260.0 -61Q209.0 17 133.0 17Q119.0 17 103.0 14Q27.0 1 2.0 -55ZM273.0 -403Q273.0 -533 215.0 -567Q198.0 -577 177.5 -577.0Q157.0 -577 132.0 -556Q87.0 -516 87.0 -444Q87.0 -388 115.0 -331Q131.0 -358 158.0 -376Q165.0 -381 166.5 -378.5Q168.0 -376 166.0 -375Q134.0 -349 123.0 -315Q143.0 -284 179.0 -265.5Q215.0 -247 254.0 -246Q273.0 -335 273.0 -403ZM496.0 -367Q527.0 -435 527.0 -498.0Q527.0 -561 497.0 -561Q443.0 -561 364.0 -347Q350.0 -310 336.0 -264Q394.0 -294 428.0 -363Q430.0 -368 433.5 -365.0Q437.0 -362 435.0 -360Q397.0 -283 333.0 -253L316.0 -203Q439.0 -237 496.0 -367ZM110.0 -18Q108.0 -18 106.0 -18Q100.0 -18 102.0 -23Q102.0 -26 104.5 -26.0Q107.0 -26 109.0 -26Q162.0 -26 200.0 -93Q146.0 -117 127.5 -120.5Q109.0 -124 96.0 -124Q60.0 -124 42.5 -105.0Q25.0 -86 25.0 -70.0Q25.0 -54 33.0 -38Q57.0 4 124.0 4Q171.0 4 199.0 -18Q230.0 -42 239.0 -62L244.0 -71Q229.0 -79 209.0 -89Q166.0 -18 110.0 -18ZM119.0 -286Q119.0 -246 154.5 -218.0Q190.0 -190 239.0 -190Q244.0 -206 252.0 -238Q170.0 -241 120.0 -297Q119.0 -291 119.0 -286ZM246.0 -172Q232.0 -133 220.0 -108L254.0 -90Q264.0 -110 286.0 -179Q272.0 -176 246.0 -172ZM300.0 -242Q284.0 -238 265.0 -238Q257.0 -206 252.0 -190Q270.0 -192 291.0 -196ZM541.0 -142Q512.0 -103 500.0 -74.0Q488.0 -45 488.0 -31.0Q488.0 -17 497.0 -17Q524.0 -17 606.0 -137Q638.0 -178 638.0 -136Q636.0 -110 636.0 -92Q636.0 -16 668.0 -16Q702.0 -16 751.0 -71Q754.0 -74 758.0 -67Q755.0 -60 737.5 -42.5Q720.0 -25 694.0 -11.0Q668.0 3 654.5 3.0Q641.0 3 633.0 0Q596.0 -13 596.0 -102Q556.0 -39 538.0 -22Q510.0 4 492.0 4Q491.0 4 489.0 4Q456.0 -1 456.0 -40.5Q456.0 -80 481.5 -128.5Q507.0 -177 549.0 -216.0Q591.0 -255 633.0 -255Q653.0 -255 672.0 -245Q689.0 -235 689.0 -213.5Q689.0 -192 670.0 -189Q677.0 -201 677.0 -211.5Q677.0 -222 667.0 -227.0Q657.0 -232 649.0 -232Q606.0 -232 541.0 -142ZM974.0 -21Q974.0 2 935.0 20Q921.0 26 907.0 26Q875.0 23 865.5 3.5Q856.0 -16 856.0 -30.0Q856.0 -44 858.0 -62.5Q860.0 -81 865.5 -107.5Q871.0 -134 879.5 -160.5Q888.0 -187 888.0 -188Q879.0 -176 848.0 -140Q768.0 -51 760.5 -30.0Q753.0 -9 731.0 -2Q719.0 2 719.0 -7Q719.0 -9 726.0 -36Q748.0 -120 748.0 -170L747.0 -197Q747.0 -214 770.0 -225Q780.0 -230 788.0 -230Q802.0 -230 799.0 -216.5Q796.0 -203 789.0 -166Q773.0 -79 769.0 -71Q828.0 -125 896.0 -231Q900.0 -238 908.0 -238Q937.0 -238 940.0 -228Q942.0 -221 929.5 -183.5Q917.0 -146 911.0 -122Q896.0 -61 896.0 -34.5Q896.0 -8 900.0 3.0Q904.0 14 914.0 13Q943.0 9 970.0 -24Q974.0 -28 974.0 -21ZM1181.0 -208Q1181.0 -180 1163.0 -101.0Q1145.0 -22 1132.0 7Q1163.0 0 1166.0 -17Q1167.0 -23 1170.5 -26.0Q1174.0 -29 1176.5 -26.5Q1179.0 -24 1178.0 -16Q1174.0 13 1124.0 25Q1094.0 108 1040.0 188Q942.0 332 826.0 351Q814.0 353 792.5 353.0Q771.0 353 744.0 340Q706.0 321 692.0 279Q687.0 262 687.0 243.5Q687.0 225 696.0 204Q711.0 168 748.0 143Q838.0 82 1097.0 15Q1114.0 -25 1137.0 -166Q1096.0 -92 1050.0 -38Q1044.0 -17 1025.0 -3.5Q1006.0 10 1007.0 -10Q1007.0 -15 1015.0 -51.5Q1023.0 -88 1023.0 -127.5Q1023.0 -167 1002.0 -167Q992.0 -167 981.0 -160.0Q970.0 -153 968.5 -158.5Q967.0 -164 985.0 -177.5Q1003.0 -191 1017.0 -191.0Q1031.0 -191 1042.0 -184.5Q1053.0 -178 1055.5 -155.0Q1058.0 -132 1058.0 -118.0Q1058.0 -104 1055.5 -85.5Q1053.0 -67 1051.0 -65Q1051.0 -58 1140.0 -183Q1145.0 -216 1168.0 -216Q1181.0 -216 1181.0 -208ZM698.0 246Q698.0 298 752.0 315Q780.0 323 800.0 323.0Q820.0 323 841.0 320.5Q862.0 318 888.5 307.0Q915.0 296 934.5 283.5Q954.0 271 973.5 248.5Q993.0 226 1005.0 210.0Q1017.0 194 1032.0 164.5Q1047.0 135 1054.0 120.5Q1061.0 106 1074.0 73.5Q1087.0 41 1092.0 31Q1024.0 47 915.0 91.5Q806.0 136 766.0 163.0Q726.0 190 712.0 211.5Q698.0 233 698.0 246ZM1443.0 -180 1490.0 -185Q1493.0 -185 1493.0 -183.0Q1493.0 -181 1490.0 -178L1482.0 -171Q1458.0 -160 1433.0 -160.0Q1408.0 -160 1400.0 -164Q1400.0 -162 1400.0 -153.0Q1400.0 -144 1390.5 -114.0Q1381.0 -84 1362.0 -51.0Q1343.0 -18 1321.0 -3.0Q1299.0 12 1288.5 12.0Q1278.0 12 1266.5 9.0Q1255.0 6 1241.0 -12Q1225.0 -30 1225.0 -62.5Q1225.0 -95 1235.0 -129Q1272.0 -241 1326.0 -246Q1333.0 -254 1343.5 -254.0Q1354.0 -254 1371.0 -244Q1401.0 -225 1404.0 -197Q1418.0 -180 1443.0 -180ZM1279.0 -10Q1284.0 -8 1290.0 -8Q1314.0 -8 1344.0 -55Q1361.0 -82 1371.0 -115.0Q1381.0 -148 1381.0 -157.0Q1381.0 -166 1380.0 -169Q1361.0 -172 1350.0 -183.0Q1339.0 -194 1338.0 -203L1337.0 -212Q1320.0 -208 1300.0 -173.0Q1280.0 -138 1270.5 -103.0Q1261.0 -68 1261.0 -43.5Q1261.0 -19 1279.0 -10ZM1639.0 -192Q1626.0 -209 1612.0 -209Q1572.0 -209 1515.0 -121Q1486.0 -77 1465.0 -6Q1464.0 2 1456.0 2Q1447.0 2 1447.0 -6L1462.0 -112Q1472.0 -192 1473.0 -212.5Q1474.0 -233 1500.0 -241Q1508.0 -243 1516.5 -243.0Q1525.0 -243 1526.0 -234Q1526.0 -231 1525.0 -227.5Q1524.0 -224 1510.5 -174.5Q1497.0 -125 1493.0 -116Q1517.0 -158 1554.0 -197.5Q1591.0 -237 1611.0 -237Q1612.0 -237 1612.0 -237Q1639.0 -237 1647.0 -204Q1649.0 -196 1649.0 -190.5Q1649.0 -185 1646.0 -185Q1643.0 -184 1639.0 -192ZM1717.0 -142Q1688.0 -103 1676.0 -74.0Q1664.0 -45 1664.0 -31.0Q1664.0 -17 1673.0 -17Q1700.0 -17 1782.0 -137Q1814.0 -178 1814.0 -136Q1812.0 -110 1812.0 -92Q1812.0 -16 1844.0 -16Q1878.0 -16 1927.0 -71Q1930.0 -74 1934.0 -67Q1931.0 -60 1913.5 -42.5Q1896.0 -25 1870.0 -11.0Q1844.0 3 1830.5 3.0Q1817.0 3 1809.0 0Q1772.0 -13 1772.0 -102Q1732.0 -39 1714.0 -22Q1686.0 4 1668.0 4Q1667.0 4 1665.0 4Q1632.0 -1 1632.0 -40.5Q1632.0 -80 1657.5 -128.5Q1683.0 -177 1725.0 -216.0Q1767.0 -255 1809.0 -255Q1829.0 -255 1848.0 -245Q1865.0 -235 1865.0 -213.5Q1865.0 -192 1846.0 -189Q1853.0 -201 1853.0 -211.5Q1853.0 -222 1843.0 -227.0Q1833.0 -232 1825.0 -232Q1782.0 -232 1717.0 -142Z';

boot();
