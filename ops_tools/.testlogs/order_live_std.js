/* ---------------- 发布单：实时进度 + 脚本输出控制台 ----------------
   两件事：
     ① 执行到哪一步  —— 步骤状态/耗时/进度条/当前步骤高亮
     ② 脚本返回信息  —— 按步骤实时追加 stdout/stderr（执行过的可回看）
   数据来源：/api/orders/<id>/stream（SSE）snapshot / start / step / log / done。
   另一份等价实现在平台页 platform_console/static/app.js，改动请同步。
------------------------------------------------------------------ */
var STREAM = null, CUR_ORDER = null, OM = null, O_FOLLOW = null, O_FOLLOW_ON = true;
var O_RENDERED = null, O_RECONNECT = null;
var O_LIMIT = 200000;
var STEP_ZH = { pending: '待执行', running: '执行中', success: '成功', failed: '失败',
  canceled: '已取消', skipped: '已跳过', rolled_back: '已回滚' };

function oKey(server, name) { return (server || '') + '|' + (name || ''); }
function oStepZh(s) { return STEP_ZH[s] || s || '待执行'; }

function stopStream() { if (STREAM) { try { STREAM.close(); } catch (e) {} STREAM = null; } }

function showOrderDetail(id) {
  stopStream();
  CUR_ORDER = id; OM = null; O_FOLLOW = null; O_RENDERED = null; O_FOLLOW_ON = true;
  openStream(id);          /* 先订阅：执行中的事件一个都不漏 */
  refreshDetail();         /* 再取一份快照（流里也会推一份，谁先到都能画） */
}

function refreshDetail() {
  if (!CUR_ORDER) return;
  api('GET', '/api/orders/' + encodeURIComponent(CUR_ORDER)).then(function (d) {
    if (d && d.order) setSnapshot(d.order);
    else if (d && d.id) setSnapshot(d);
  });
}

function openStream(id) {
  try {
    STREAM = new EventSource('/api/orders/' + encodeURIComponent(id)
      + '/stream?token=' + encodeURIComponent(TOKEN));
  } catch (e) { return; }
  var ondata = function (ev) {
    var m; try { m = JSON.parse(ev.data); } catch (x) { return; }
    applyEvent(m && m.type ? m : ((m && m.data && m.data.type) ? m.data : (m && m.data) || {}));
  };
  STREAM.onmessage = ondata;                                  /* 直连：默认 message */
  STREAM.addEventListener('release', ondata);                 /* 经平台透传时被命名 */
  STREAM.addEventListener('close', function () { stopStream(); });
  STREAM.onerror = function () {
    if (O_RECONNECT) return;
    O_RECONNECT = setTimeout(function () {
      O_RECONNECT = null; if (CUR_ORDER === id) refreshDetail();
    }, 1500);
  };
}

/* ---------- 模型 ---------- */
function setSnapshot(o) {
  if (!o) return;
  OM = { order: o, logs: {}, percent: null, cleared: null };
  (o.server_steps || []).forEach(function (run) {
    (run.steps || []).forEach(function (st) {
      OM.logs[oKey(run.server, st.name)] = (st.logs || []).join('\n');
    });
  });
  autoFollow();
  paint();
}

function autoFollow() {
  if (!OM || !O_FOLLOW_ON) return;
  var running = null, last = null;
  (OM.order.server_steps || []).forEach(function (r) {
    (r.steps || []).forEach(function (st) {
      var k = oKey(r.server, st.name);
      if (st.logs && st.logs.length) last = k;
      if (st.state === 'running') running = k;
    });
  });
  O_FOLLOW = running || last || O_FOLLOW;
}

function appendLog(key, text) {
  if (!OM || !text) return;
  OM.logs[key] = (OM.logs[key] || '') + text;
}

function progress() {
  var o = OM && OM.order;
  if (!o) return { pct: 0, total: 0, running: null };
  var done = 0, total = 0, running = null;
  (o.server_steps || []).forEach(function (r) {
    (r.steps || []).forEach(function (st, i) {
      total++;
      if (st.state === 'success' || st.state === 'failed') done++;
      if (st.state === 'running') {
        running = { server: r.server, label: st.label || st.name,
          index: i + 1, total: (r.steps || []).length };
      }
    });
  });
  var fin = ['success', 'failed', 'rolled_back', 'canceled'].indexOf(o.state || '') >= 0;
  var pct = (OM.percent != null) ? OM.percent : (total ? Math.round(done * 100 / total) : 0);
  if (fin) pct = 100;
  return { pct: pct, total: total, running: running };
}

function progressText() {
  var o = OM && OM.order; if (!o) return '';
  var p = progress();
  if ((o.state || '') === 'running' && p.running) {
    return '正在执行：' + p.running.label + '（' + p.running.server + ' · 第 '
      + p.running.index + '/' + p.running.total + ' 步）· ' + p.pct + '%';
  }
  if ((o.state || '') === 'running') return '正在执行 · ' + p.pct + '%';
  if (['success', 'failed', 'rolled_back', 'canceled'].indexOf(o.state || '') >= 0) {
    var r = o.result || {};
    return '执行结束：成功 ' + ((r.succeeded || []).length) + ' 台 / 失败 '
      + ((r.failed || []).length) + ' 台';
  }
  return '待执行：共 ' + p.total + ' 步';
}

function stepLabel(key) {
  var o = OM && OM.order; if (!o || !key) return key || '';
  var out = String(key).split('|')[1] || '';
  (o.server_steps || []).forEach(function (r) {
    (r.steps || []).forEach(function (st) {
      if (oKey(r.server, st.name) === key) out = st.label || st.name || out;
    });
  });
  return out;
}

/* ---------- 渲染 ---------- */
function paint() {
  var o = OM && OM.order; if (!o) return;
  var box = document.getElementById('order-detail'); if (!box) return;
  var oldConsole = document.getElementById('ord-console');
  var p = progress();
  var h = '<div class="card"><div class="row"><div><h3>发布单 ' + esc(o.id) + ' '
    + stateTag(o.state) + '</h3></div>'
    + '<div style="text-align:right"><button class="btn small" onclick="runOrder(\''
    + esc(o.id) + '\')">执行</button> '
    + '<button class="btn small gray" onclick="rollbackOrder(\'' + esc(o.id) + '\')">回滚</button> '
    + '<button class="btn small danger" onclick="cancelOrder(\'' + esc(o.id) + '\')">取消</button></div></div>'
    + '<div class="muted">应用: ' + esc(o.app) + ' · 版本: ' + esc(o.version)
    + ' · 策略: ' + (o.strategy === 'parallel' ? '并行' : '串行')
    + ' · 目标: ' + (o.servers || []).join(', ') + '</div>'
    + '<div style="margin-top:4px">包: ' + (pkgChips(o) || '<span class="muted">-</span>') + '</div>'
    + (o.params && Object.keys(o.params).length
       ? '<div style="margin-top:6px">' + Object.keys(o.params).map(function (k) {
           return '<span class="chip">' + esc(k) + '=' + esc(o.params[k]) + '</span>'; }).join('') + '</div>'
       : '')
    + '<div class="ord-bar"><div style="width:' + p.pct + '%"></div></div>'
    + '<div class="muted" id="ord-prog-text">' + esc(progressText()) + '</div></div>';
  (o.server_steps || []).forEach(function (run) {
    h += '<div class="card" style="margin-top:8px"><b>' + esc(run.server) + '</b> '
      + stateTag(run.state) + '</div>';
    h += '<div class="pipeline">';
    (run.steps || []).forEach(function (st) {
      var key = oKey(run.server, st.name);
      var cls = st.state === 'success' ? 'success' : (st.state === 'failed' ? 'failed'
        : (st.state === 'running' ? 'running' : ''));
      h += '<div class="step ' + cls + (O_FOLLOW === key ? ' active' : '')
        + '" data-step-key="' + esc(encodeURIComponent(key)) + '">'
        + esc(st.label || st.name) + ' ' + stateTag(st.state)
        + ' <span class="muted">' + esc(st.message || '') + ' · '
        + (st.elapsed || 0) + 's</span></div>';
    });
    h += '</div>';
  });
  if (!(o.server_steps || []).length) {
    h += '<div class="card"><span class="muted">尚未执行 —— 点「执行」后这里会实时显示每一步与脚本输出</span></div>';
  }
  h += '<div id="ord-console-wrap"></div>';
  box.innerHTML = h;
  var wrap = document.getElementById('ord-console-wrap');
  if (wrap) {
    if (oldConsole) wrap.appendChild(oldConsole);   /* 复用原节点：保住已显示输出与滚动位置 */
    else wrap.innerHTML = consoleHtml();
  }
  updateConsoleHead();
  if (O_FOLLOW !== O_RENDERED) renderConsole();
}

function consoleHtml() {
  return '<div class="ord-console" id="ord-console">'
    + '<div class="ord-console-h"><span>脚本输出</span><span class="muted" id="ord-where"></span>'
    + '<button class="btn small gray" onclick="toggleFollow()" id="ord-follow-btn"></button>'
    + '<button class="btn small gray" onclick="clearConsole()">清空显示</button></div>'
    + '<div class="ord-console-body" id="ord-console-body"></div></div>';
}

function updateConsoleHead() {
  var where = document.getElementById('ord-where'), btn = document.getElementById('ord-follow-btn');
  if (where) where.textContent = O_FOLLOW ? (String(O_FOLLOW).split('|')[0] + ' · ' + stepLabel(O_FOLLOW)) : '（未选中步骤）';
  if (btn) btn.textContent = O_FOLLOW_ON ? '跟随当前步骤：开' : '跟随当前步骤：关';
}

function renderConsole() {
  O_RENDERED = O_FOLLOW;
  var body = document.getElementById('ord-console-body'); if (!body) return;
  var text = O_FOLLOW ? ((OM && OM.logs[O_FOLLOW]) || '') : '';
  if (!O_FOLLOW) {
    body.innerHTML = '<div class="muted">点上方任意步骤可回看它的脚本输出；执行中会自动跟随当前步骤</div>';
    return;
  }
  if (!text || (OM && OM.cleared === O_FOLLOW)) {
    body.innerHTML = '<div class="muted">' + (text ? '（已清空，后续输出继续显示）' : '该步骤暂无输出') + '</div>';
    return;
  }
  body.textContent = text;
  scrollConsole(true);
}

function appendConsole(text) {
  var body = document.getElementById('ord-console-body'); if (!body || !text) return;
  var atBottom = consoleAtBottom(body);
  if (body.querySelector('.muted')) body.textContent = '';
  body.textContent += text;
  if (body.textContent.length > O_LIMIT * 1.5) {
    body.textContent = '（已截断较早输出）\n' + body.textContent.slice(-O_LIMIT);
  }
  if (atBottom) scrollConsole(true);
}

function consoleAtBottom(body) {
  try { return (body.scrollHeight - body.scrollTop - body.clientHeight) < 24; } catch (e) { return true; }
}
function scrollConsole(force) {
  var body = document.getElementById('ord-console-body'); if (!body) return;
  if (force || consoleAtBottom(body)) { try { body.scrollTop = body.scrollHeight; } catch (e) {} }
}
function toggleFollow() { O_FOLLOW_ON = !O_FOLLOW_ON; if (O_FOLLOW_ON) autoFollow(); updateConsoleHead(); }
function followStep(keyEnc) {
  O_FOLLOW_ON = false;
  try { O_FOLLOW = decodeURIComponent(keyEnc); } catch (e) { O_FOLLOW = keyEnc; }
  renderConsole(); updateConsoleHead();
}
function clearConsole() {
  var body = document.getElementById('ord-console-body');
  if (body) body.innerHTML = '<div class="muted">（已清空，后续输出继续显示）</div>';
  if (OM) OM.cleared = O_FOLLOW;
}

/* ---------- 事件 ---------- */
function applyEvent(ev) {
  if (!ev || (ev.order_id && ev.order_id !== CUR_ORDER)) return;
  var t = ev.type;
  if (t === 'snapshot') { setSnapshot(ev.order); return; }
  if (t === 'log') {
    var key = oKey(ev.server, ev.step);
    appendLog(key, ev.text || '');
    if (O_FOLLOW_ON && O_FOLLOW !== key) { O_FOLLOW = key; renderConsole(); updateConsoleHead(); }
    else if (O_FOLLOW === key) appendConsole(ev.text || '');
    return;
  }
  if (t === 'start' || t === 'rollback_start') {
    if (OM) { OM.order.state = 'running'; OM.percent = 0; }
    paint(); return;
  }
  if (t === 'done') {
    if (OM) { OM.order.state = ev.state || OM.order.state; OM.percent = 100; }
    paint(); stopStream(); loadOrders(); return;
  }
  if (t === 'step') {
    var o = OM && OM.order; if (!o) return;
    (o.server_steps || []).forEach(function (run) {
      if (run.server !== ev.server) return;
      (run.steps || []).forEach(function (st) {
        if (st.name !== ev.step) return;
        st.state = ev.state || st.state;
        if (ev.message) st.message = ev.message;
        if (ev.elapsed) st.elapsed = ev.elapsed;
        if (ev.label) st.label = ev.label;
      });
    });
    if (typeof ev.percent === 'number') OM.percent = ev.percent;
    if (ev.state === 'running' && O_FOLLOW_ON) O_FOLLOW = oKey(ev.server, ev.step);
    paint(); return;
  }
  paint();
}

document.addEventListener('click', function (e) {
  var t = e.target, node = null;
  while (t && t !== document) {
    if (t.getAttribute && t.getAttribute('data-step-key')) { node = t; break; }
    t = t.parentNode;
  }
  if (node) followStep(node.getAttribute('data-step-key'));
});

function runOrder(id) {
  if (!confirm('确认执行发布单 ' + id + ' ？执行后将按步骤发布到所选服务器。')) return;
  api('POST', '/api/orders/' + encodeURIComponent(id) + '/run').then(function (d) {
    if (d.status === 'success') { O_FOLLOW_ON = true; showOrderDetail(id); }
    else alert(d.message || '提交失败');
  });
}
function rollbackOrder(id) {
  if (!confirm('确认回滚发布单 ' + id + ' ？将恢复最近一份备份并重启服务。')) return;
  api('POST', '/api/orders/' + encodeURIComponent(id) + '/rollback').then(function (d) {
    if (d.status === 'success') { O_FOLLOW_ON = true; showOrderDetail(id); }
    else alert(d.message || '提交失败');
  });
}
function cancelOrder(id) {
  if (!confirm('确认取消发布单 ' + id + ' ？')) return;
  api('POST', '/api/orders/' + encodeURIComponent(id) + '/cancel').then(function () { showOrderDetail(id); });
}
