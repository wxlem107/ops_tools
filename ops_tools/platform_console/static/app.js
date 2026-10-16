'use strict';
/* =========================================================================
 * ops_Platform 前端逻辑（纯原生 JS，零依赖）
 * 对应 API_CONTRACT.md v2.0。字段名严格对照后端实现。
 * ========================================================================= */
const VIEWS = ['dashboard','servers','files','term','release','cmdb','audit','tasks','settings'];
const $ = id => document.getElementById(id);
function el(id){return document.getElementById(id);}
function storeGet(k){try{return localStorage.getItem(k)}catch(e){return null}}
function storeSet(k,v){try{localStorage.setItem(k,v)}catch(e){}}
function storeDel(k){try{localStorage.removeItem(k)}catch(e){}}

/* ---------- 全局状态 ---------- */
let token = storeGet('ops_token') || '';
let me = {username:'', role:'', role_label:'', perms:[], servers:['*']};
let servers = [];           // 服务器列表（含 status 探测结果）
let meta = {groups:[], envs:[], tags:[], types:[]};
let settings = {access_mode:'auto', upload_concurrency:6, chunk_mb:0, release_url:'http://127.0.0.1:8094', release_token:'', release_user:'', release_pass:'', metrics_interval:10, server_probe_interval:30, theme:'auto'};
let lockedCfg = {};          // 被启动参数钉死的设置项（如 --release-url）
let currentView = 'dashboard';
let directProbe = {};        // 浏览器侧直连探测缓存
let lastMetrics = {};        // 实时指标缓存 name->metrics
const QUICK_CMDS = ['uptime','df -h','free -h','hostname','whoami','ps aux | head -15','ip addr','date','uname -a','systemctl status'];

/* ---------- 小工具 ---------- */
// 同时转义引号：esc() 被大量用在双引号属性（data-name="…" / value="…" / title="…"）里，
// 只转义 &<> 时，值里含 " 就能闭合属性注入 onXXX=…（存储型 XSS）。
// 文本语境下 &quot;/&#39; 会被浏览器还原为引号，视觉不变，故可直接加固。
function esc(s){const d=document.createElement('div');d.textContent=s==null?'':String(s);return d.innerHTML.replace(/"/g,'&quot;').replace(/'/g,'&#39;')}
// 用于「双引号属性里、JS 单引号字符串中」的数据（onclick="fn('…')"）：esc 只做 HTML 转义，
// 其 &#39; 会在属性解析阶段还原成 '，仍能逃出 JS 字符串；这里补上 JS 字符串转义。
function escJs(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/\\/g,'\\\\').replace(/'/g,"\\'")}
function fmtSize(s){s=Number(s)||0;if(s<1024)return s+' B';if(s<1048576)return (s/1024).toFixed(1)+' KB';if(s<1073741824)return (s/1048576).toFixed(2)+' MB';return (s/1073741824).toFixed(2)+' GB'}
function fmtUptime(sec){sec=Number(sec)||0;if(sec<60)return sec+'s';if(sec<3600)return Math.floor(sec/60)+'m';if(sec<86400)return Math.floor(sec/3600)+'h'+Math.floor((sec%3600)/60)+'m';return Math.floor(sec/86400)+'d'+Math.floor((sec%86400)/3600)+'h'}
function fmtTime(ms){if(ms==null)return '-';const s=ms/1000;if(s<60)return s.toFixed(2)+'s';const m=Math.floor(s/60);return m+'m'+(s%60).toFixed(0)+'s'}
function can(perm){return me.perms.indexOf('*')>=0 || me.perms.indexOf(perm)>=0}
function toast(msg,type){const t=$('toast');t.className=type||'ok';t.textContent=msg;t.style.display='block';clearTimeout(t._timer);t._timer=setTimeout(()=>t.style.display='none',3000)}
function showLogin(){$('loginMask').style.display='flex';setTimeout(()=>{const u=$('loginUser');if(u)u.focus()},50)}
function hideLogin(){$('loginMask').style.display='none'}

/* 确认弹层（二次确认） */
let confirmCb=null;
function confirmDialog(title,msg,onOk){
  $('confirmTitle').textContent=title;$('confirmMsg').textContent=msg;
  confirmCb=onOk;$('confirmModal').classList.add('show');
}
function closeConfirm(){confirmCb=null;$('confirmModal').classList.remove('show')}
$('confirmOk').onclick=()=>{const c=confirmCb;confirmCb=null;$('confirmModal').classList.remove('show');if(c)c()};

/* ---------- 请求封装 ---------- */
async function api(url,opts){
  opts=opts||{};
  const h=Object.assign({},opts.headers||{});
  if(token)h['X-Auth-Token']=token;
  opts.headers=h;
  const res=await fetch(url,opts);
  const raw=await res.text();
  let data={};try{data=raw?JSON.parse(raw):{}}catch(_){data={}}
  if(res.status===401 && url!=='/api/login'){
    /* 只有「平台自身会话失效」才踢回登录页。
       上游（发布中心等）透传回来的 401 属于业务错误 —— 若在这里 showLogin()，
       用户会看到「登录成功 → 发布视图一加载就退出 → 仿佛登不进去」的死循环。 */
    if(String(data.code||'').indexOf('upstream')===0)throw new Error(data.message||'上游服务未授权');
    showLogin();throw new Error('未授权，请重新登录');
  }
  if(!res.ok && (data.status!=='success' && data.status!==undefined && data.status!=='ok')){
    throw new Error(data.message||('HTTP '+res.status));
  }
  return data;
}
function authUrl(u){return u+(u.indexOf('?')>=0?'&':'?')+'token='+encodeURIComponent(token)}

/* ================= 登录 / 身份 ================= */
async function doLogin(){
  const u=$('loginUser').value.trim(), p=$('loginPass').value;
  if(!u||!p){toast('请输入用户名和口令','warn');return}
  $('loginErr').style.display='none';
  try{
    const d=await api('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:u,password:p})});
    token=d.token;storeSet('ops_token',token);
    hideLogin();await startApp();
  }catch(e){$('loginErr').textContent=e.message||'登录失败';$('loginErr').style.display='block'}
}
async function doLogout(){
  try{await api('/api/logout',{method:'POST'})}catch(e){}
  storeDel('ops_token');token='';location.reload();
}
async function openChangePwd(){$('userMenu').classList.remove('show');$('p_old').value='';$('p_new').value='';$('pwdModal').classList.add('show')}
async function submitPwd(){
  const o=$('p_old').value,p=$('p_new').value;
  try{await api('/api/password',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({old_password:o,new_password:p})});
    toast('口令已修改');$('pwdModal').classList.remove('show');}catch(e){toast(e.message,'err')}
}
function toggleUserMenu(){if(!can('platform.view'))return;$('userMenu').classList.toggle('show')}
function applyUserUI(){
  $('suName').textContent=me.username||'未登录';
  $('suAvatar').textContent=(me.username||'?').slice(0,1).toUpperCase();
  const r=$('suRole');r.textContent=me.role_label||me.role||'-';
  r.className='badge role';
}
async function applyMe(){
  const m=await api('/api/me');
  me={username:m.username,role:m.role,role_label:m.role_label,perms:m.perms||[],servers:m.servers||['*']};
}

/* ================= 主题 ================= */
function applyTheme(){
  let t=storeGet('ops_theme')||(settings.theme||'auto');
  if(t==='auto') t=(window.matchMedia&&matchMedia('(prefers-color-scheme: light)').matches)?'light':'dark';
  document.body.setAttribute('data-theme',t);
}
function initThemeWatch(){
  if(window.matchMedia)matchMedia('(prefers-color-scheme: light)').addEventListener('change',()=>{if((storeGet('ops_theme')||settings.theme)==='auto')applyTheme()});
}

/* ================= 权限：隐藏写操作（幂等：权限满足时恢复显示） ================= */
function applyPerms(){
  document.querySelectorAll('[data-perm]').forEach(e=>{
    e.style.display=can(e.getAttribute('data-perm'))?'':'none';
  });
}

/* ================= 侧栏 / 路由 ================= */
function toggleSidebar(){$('sidebar').classList.toggle('open')}
function activateView(view){
  if(VIEWS.indexOf(view)<0)view='dashboard';
  currentView=view;
  document.querySelectorAll('.nav-item').forEach(n=>n.classList.toggle('active',n.dataset.view===view));
  document.querySelectorAll('.view').forEach(v=>v.classList.remove('active'));
  const sec=$('view-'+view);if(sec)sec.classList.add('active');
  $('crumbTitle').textContent={dashboard:'仪表盘',servers:'服务器墙',files:'文件管理',term:'命令终端',release:'发布中心',cmdb:'资产台账',audit:'审计',tasks:'任务中心',settings:'设置'}[view]||view;
  if(window.innerWidth<=768)$('sidebar').classList.remove('open');
  if(view==='dashboard')loadDashboard();
  else if(view==='servers')renderServers();
  else if(view==='files'){renderFileSrvList();}
  else if(view==='term'){renderTermServers();renderTermChips();}
  else if(view==='release')loadRelease();
  else if(view==='cmdb')loadCmdb();
  else if(view==='audit')loadAudit();
  else if(view==='tasks')loadTasks();
  else if(view==='settings')loadSettings();
}
function go(view){location.hash=view;}
window.addEventListener('hashchange',()=>{const v=location.hash.slice(1);if(v&&v!==currentView)activateView(v);});
document.querySelectorAll('.nav-item').forEach(n=>n.addEventListener('click',()=>go(n.dataset.view)));
function refreshView(){activateView(currentView)}

/* ================= 实时通道（统一 SSE） ================= */
let esConn=null, esRetry=0, esWasDown=false;
function connectEvents(){
  if(esConn)esConn.close();
  const qs='topics=task,metrics,release,alert&token='+encodeURIComponent(token);
  try{esConn=new EventSource('/api/events?'+qs);}catch(e){return;}
  esConn.onmessage=e=>{
    try{const m=JSON.parse(e.data);handleEvent(m.topic,m.data);}catch(_){}
  };
  esConn.onerror=()=>{
    esConn.close();esWasDown=true;
    const wait=Math.min(30000,1000*Math.pow(2,esRetry++));
    setTimeout(connectEvents,wait);
  };
  esConn.onopen=()=>{
    const wasDown=esWasDown;esRetry=0;esWasDown=false;
    if(wasDown)refreshAll(); // 重连成功拉一次全量兜底
  };
}
function handleEvent(topic,data){
  if(!data)return;
  if(topic==='metrics'){
    if(data.name)lastMetrics[data.name]=data.metrics;
    pushMetricPoint(data.metrics);
    if(currentView==='dashboard')drawCurve();
    if(currentView==='servers')throttledSrvMetrics();
  }else if(topic==='task'){
    if(currentView==='tasks')updateTaskItem(data);
    if(data.id===openTaskId)renderTaskDetail(data);
  }else if(topic==='release'){
    if(currentView==='release'&&data.order_id===openOrderId)updateOrderStep(data);
  }else if(topic==='alert'){
    if(data&&data.message)toast('告警: '+data.message,'warn');
  }
}
function refreshAll(){
  loadServers().then(()=>{if(currentView==='servers')renderServers();});
  if(currentView==='dashboard')loadDashboard();
}

/* ================= 仪表盘 ================= */
let cpuBuf=[],loadBuf=[],memBuf=[];
function pushMetricPoint(m){
  if(!m)return;
  const avg=avgCpu();
  cpuBuf.push(m.cpu_percent!=null?m.cpu_percent:avg);
  if(cpuBuf.length>60)cpuBuf.shift();
}
function avgCpu(){
  const ks=Object.keys(lastMetrics);if(!ks.length)return 0;
  let s=0,n=0;ks.forEach(k=>{const mm=lastMetrics[k];if(mm&&mm.cpu_percent!=null){s+=mm.cpu_percent;n++}});
  return n?s/n:0;
}
async function loadDashboard(){
  try{
    const ov=await api('/api/monitor/overview');
    lastMetrics={};
    (ov.items||[]).forEach(it=>{if(it.name)lastMetrics[it.name]=it.metrics;});
    cpuBuf=[];(ov.items||[]).forEach(it=>{if(it.metrics&&it.metrics.cpu_percent!=null)cpuBuf.push(it.metrics.cpu_percent);});
    renderDashStats(ov.items||[]);
  }catch(e){toast(e.message,'err')}
  try{
    const ts=await api('/api/tasks/stats');
    $('dashAudit').insertAdjacentHTML('beforeend','');
    renderTaskStat(ts);
  }catch(e){}
  try{
    const au=await api('/api/audit/stats?days=7');
    renderAuditPreview(au);
  }catch(e){}
  drawCurve();
}
function renderDashStats(items){
  const total=servers.length||items.length;
  let online=0;items.forEach(it=>{const m=it.metrics;if(m&&m.online!==false)online++;});
  if(!items.length)online=servers.filter(s=>s.status&&s.status.online).length;
  let running=0;
  const html=
    statCard('🖥️','纳管服务器',total,'统一管理节点')+
    statCard('📡','在线节点',online,'实时连通状态')+
    statCard('◷','运行中任务',running,'/api/tasks/stats')+
    statCard('☰','审计(7天)','-','点击查看');
  $('dashStats').innerHTML=html;
}
function renderTaskStat(ts){
  const card=$('dashStats').querySelectorAll('.stat')[2];
  if(card)card.querySelector('.st-val').textContent=((ts.by_state&&(ts.by_state.running||0))||0);
}
function renderAuditPreview(au){
  const days=au.days||7;
  $('dashAudit').innerHTML=
    '<div class="da"><b>'+au.total+'</b>总操作</div>'+
    '<div class="da"><b style="color:var(--red)">'+au.failed+'</b>失败</div>'+
    '<div class="da" style="grid-column:1/-1">近'+days+'天 按动作: '+
      (au.by_action||[]).slice(0,5).map(a=>'<span class="tag">'+esc(a.label)+' '+a.count+'</span>').join('')+'</div>';
}
function statCard(ico,label,val,sub){
  return '<div class="stat"><span class="st-ico">'+ico+'</span><div class="st-label">'+label+'</div><div class="st-val">'+val+'</div><div class="st-sub">'+sub+'</div></div>';
}
function drawCurve(){
  const cv=$('dashCurve');if(!cv)return;
  const ctx=cv.getContext('2d');
  const w=cv.width=cv.clientWidth||520,h=120;cv.height=120;
  ctx.clearRect(0,0,w,h);
  const data=cpuBuf;const n=data.length;
  ctx.strokeStyle=getCSS('--border2');ctx.beginPath();ctx.moveTo(0,h-1);ctx.lineTo(w,h-1);ctx.stroke();
  if(n<2)return;
  const max=100;
  ctx.strokeStyle=getCSS('--accent');ctx.beginPath();
  for(let i=0;i<n;i++){
    const x=w*(i/(60-1));const y=h-(data[i]/max)*(h-10)-5;
    if(i===0)ctx.moveTo(x,y);else ctx.lineTo(x,y);
  }
  ctx.stroke();
  $('dashLegend').innerHTML='CPU 平均占用 (最近 '+n+' 点, 当前 '+avgCpu().toFixed(1)+'%)';
}
function getCSS(v){return getComputedStyle(document.body).getPropertyValue(v).trim()}

/* ================= 服务器墙 ================= */
let srvFilter={group:'',env:'',tag:'',kw:''};
let srvTableView=false;
let srvSel=new Set();
let srvMetricsTimer=null;
function throttledSrvMetrics(){
  if(srvMetricsTimer)return;
  srvMetricsTimer=setTimeout(()=>{srvMetricsTimer=null;renderServers(true);},1500);
}
async function loadServers(){
  try{
    const d=await api('/api/servers');
    servers=d.servers||[];
    return true;
  }catch(e){toast(e.message,'err');return false;}
}
async function loadMeta(){
  try{const d=await api('/api/meta');meta=d;}catch(e){}
}
function renderFilterBar(){
  const groups=meta.groups.map(g=>'<option value="'+esc(g)+'"'+(srvFilter.group===g?' selected':'')+'>'+esc(g)+'</option>').join('');
  const envs=meta.envs.map(e=>'<option value="'+esc(e)+'"'+(srvFilter.env===e?' selected':'')+'>'+esc(e)+'</option>').join('');
  const tags=meta.tags.map(t=>'<option value="'+esc(t)+'"'+(srvFilter.tag===t?' selected':'')+'>'+esc(t)+'</option>').join('');
  $('srvFilterBar').innerHTML=
    '<input type="text" id="sfKw" placeholder="🔍 关键字" value="'+esc(srvFilter.kw)+'" oninput="srvFilter.kw=this.value;renderServers()">'+
    '<select onchange="srvFilter.group=this.value;renderServers()"><option value="">全部分组</option>'+groups+'</select>'+
    '<select onchange="srvFilter.env=this.value;renderServers()"><option value="">全部环境</option>'+envs+'</select>'+
    '<select onchange="srvFilter.tag=this.value;renderServers()"><option value="">全部标签</option>'+tags+'</select>'+
    '<button class="btn btn-sm" onclick="srvFilter={group:\'\',env:\'\',tag:\'\',kw:\'\'};renderFilterBar();renderServers()">重置</button>';
}
function filteredServers(){
  return servers.filter(s=>{
    if(srvFilter.group&&s.group!==srvFilter.group)return false;
    if(srvFilter.env&&s.env!==srvFilter.env)return false;
    if(srvFilter.tag&&(s.tags||[]).indexOf(srvFilter.tag)<0)return false;
    if(srvFilter.kw){const k=srvFilter.kw.toLowerCase();if((s.name||'').toLowerCase().indexOf(k)<0&&(s.host||'').toLowerCase().indexOf(k)<0)return false;}
    return true;
  });
}
function renderServers(skipFilter){
  if(!skipFilter)renderFilterBar();
  const list=skipFilter?servers:filteredServers();
  if(!list.length){$('srvGrid').innerHTML='<div class="empty">暂无符合条件的服务器</div>';return;}
  if(srvTableView)renderSrvTable(list);else renderSrvCards(list);
  $('srvSelCount').textContent=srvSel.size;
  $('srvBatchBar').style.display=srvSel.size?'flex':'none';
}
function diskHtml(s){
  const m=lastMetrics[s.name];const d=m&&m.disks&&m.disks[0];
  if(!d||d.percent==null)return '';
  const pct=Math.min(100,Math.round(d.percent));
  const cls=pct>=90?'danger':(pct>=75?'warn':'');
  return '<div style="margin-top:8px"><div style="display:flex;justify-content:space-between;font-size:11px;color:var(--muted)"><span>💾 磁盘 '+pct+'%</span><span>'+esc(d.mount||'/')+'</span></div><div class="disk"><div class="'+cls+'" style="width:'+pct+'%"></div></div></div>';
}
function renderSrvCards(list){
  $('srvGrid').className='srv-grid';
  $('srvGrid').innerHTML=list.map(s=>{
    const st=s.status||{};const on=st.online;
    const led=st.loading?'load':(on?'on':'off');
    const badge=st.loading?'<span class="badge off">检测中</span>':(on?'<span class="badge on">在线</span>':'<span class="badge off">离线</span>');
    const sel=srvSel.has(s.name)?' style="outline:2px solid var(--accent)"':'';
    const typeTxt=s.type==='both'?'上传+终端':(s.type==='upload'?'上传':'终端');
    const tags=(s.tags||[]).map(t=>'<span class="tag">'+esc(t)+'</span>').join('');
    let acts='';
    if(hasUpload(s))acts+='<button class="btn btn-sm btn-success" data-act="files" data-name="'+esc(s.name)+'">📁 文件</button>';
    if(hasShell(s))acts+='<button class="btn btn-sm" style="background:rgba(124,58,237,.18);border-color:rgba(124,58,237,.4);color:#c4b5fd" data-act="term" data-name="'+esc(s.name)+'">⌨ 终端</button>';
    acts+='<button class="btn btn-sm" data-act="edit" data-name="'+esc(s.name)+'">编辑</button>';
    acts+='<button class="btn btn-sm btn-danger" data-act="del" data-name="'+esc(s.name)+'">删除</button>';
    return '<div class="srv-card '+(on?'on':'off')+'"'+sel+' data-name="'+esc(s.name)+'">'+
      '<div style="display:flex;justify-content:space-between;align-items:flex-start">'+
      '<div style="flex:1;min-width:0"><div class="sc-name"><span class="led '+led+'"></span>'+esc(s.name)+' <span class="badge type">'+typeTxt+'</span></div>'+
      '<div class="sc-host">'+esc(s.host)+(hasUpload(s)?':'+(s.upload_port||8080):'')+(hasShell(s)?' / sh:'+(s.shell_port||9100):'')+'</div></div>'+
      '<input type="checkbox" class="srv-check" '+(srvSel.has(s.name)?'checked':'')+' data-sel="'+esc(s.name)+'"></div>'+
      '<div class="sc-meta">'+badge+(s.group?'<span class="tag">组:'+esc(s.group)+'</span>':'')+(s.env?'<span class="badge env-'+esc(s.env)+'">'+esc(s.env)+'</span>':'')+tags+'</div>'+
      diskHtml(s)+
      '<div class="sc-actions">'+acts+'</div></div>';
  }).join('');
}
function renderSrvTable(list){
  $('srvGrid').className='';
  let rows=list.map(s=>{
    const st=s.status||{};const on=st.online;
    return '<tr><td><input type="checkbox" class="srv-check" '+(srvSel.has(s.name)?'checked':'')+' data-sel="'+esc(s.name)+'"></td>'+
      '<td><span class="led '+(on?'on':'off')+'"></span> '+esc(s.name)+'</td>'+
      '<td>'+esc(s.host)+'</td><td>'+(s.type==='both'?'上传+终端':(s.type==='upload'?'上传':'终端'))+'</td>'+
      '<td>'+(esc(s.group)||'-')+'</td><td>'+(s.env?('<span class="badge env-'+esc(s.env)+'">'+esc(s.env)+'</span>'):'-')+'</td>'+
      '<td>'+((s.tags||[]).map(t=>'<span class="tag">'+esc(t)+'</span>').join('')||'-')+'</td>'+
      '<td>'+(on?'<span class="badge on">在线</span>':'<span class="badge off">离线</span>')+'</td>'+
      '<td>'+((lastMetrics[s.name]&&lastMetrics[s.name].disks&&lastMetrics[s.name].disks[0])?lastMetrics[s.name].disks[0].percent+'%':'-')+'</td>'+
      '<td>'+(hasUpload(s)?'<button class="btn btn-sm btn-success" data-act="files" data-name="'+esc(s.name)+'">📁</button>':'')+
      (hasShell(s)?'<button class="btn btn-sm" data-act="term" data-name="'+esc(s.name)+'">⌨</button>':'')+
      '<button class="btn btn-sm" data-act="edit" data-name="'+esc(s.name)+'">编辑</button>'+
      '<button class="btn btn-sm btn-danger" data-act="del" data-name="'+esc(s.name)+'">删</button></td></tr>';
  }).join('');
  $('srvGrid').innerHTML='<table class="srv-table"><thead><tr><th></th><th>名称</th><th>主机</th><th>类型</th><th>分组</th><th>环境</th><th>标签</th><th>状态</th><th>磁盘</th><th>操作</th></tr></thead><tbody>'+rows+'</tbody></table>';
}
$('srvGrid').addEventListener('click',e=>{
  const chk=e.target.closest('.srv-check');
  if(chk){const n=chk.dataset.sel;if(chk.checked)srvSel.add(n);else srvSel.delete(n);$('srvSelCount').textContent=srvSel.size;$('srvBatchBar').style.display=srvSel.size?'flex':'none';return;}
  const b=e.target.closest('button[data-act]');if(!b)return;
  const act=b.dataset.act,name=b.dataset.name;
  if(act==='files')openFilesFor(name);
  else if(act==='term'){selectedTermServers.clear();selectedTermServers.add(name);go('term');}
  else if(act==='edit')openServerModal(name);
  else if(act==='del')removeServer(name);
});
$('srvGrid').addEventListener('change',e=>{
  const chk=e.target.closest('.srv-check');if(!chk)return;
  const n=chk.dataset.sel;if(chk.checked)srvSel.add(n);else srvSel.delete(n);
  $('srvSelCount').textContent=srvSel.size;$('srvBatchBar').style.display=srvSel.size?'flex':'none';
});
function toggleSrvView(){srvTableView=!srvTableView;$('srvViewToggle').textContent=srvTableView?'⊞ 卡片':'▤ 表格';renderServers(true);}
function clearSrvSel(){srvSel.clear();renderServers(true);}
async function batchProbe(){
  if(!srvSel.size){toast('请先选择服务器','warn');return;}
  try{const d=await api('/api/servers/batch-probe',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({selector:{names:[...srvSel]}})});
    const res=d.results||{};
    servers.forEach(s=>{if(res[s.name])s.status=res[s.name];});
    renderServers(true);toast('批量探测完成');}catch(e){toast(e.message,'err')}
}
function batchUpdate(){$('batchModal').classList.add('show')}
async function submitBatchUpdate(){
  if(!srvSel.size){toast('请先选择服务器','warn');return;}
  const set={};
  if($('b_group').value.trim())set.group=$('b_group').value.trim();
  if($('b_env').value)set.env=$('b_env').value;
  if($('b_tags_add').value.trim())set.tags_add=$('b_tags_add').value.split(',').map(x=>x.trim()).filter(Boolean);
  if($('b_tags_remove').value.trim())set.tags_remove=$('b_tags_remove').value.split(',').map(x=>x.trim()).filter(Boolean);
  try{const d=await api('/api/servers/batch-update',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({selector:{names:[...srvSel]},set})});
    toast('已更新 '+d.count+' 台');$('batchModal').classList.remove('show');await loadServers();await loadMeta();renderServers();}catch(e){toast(e.message,'err')}
}
function bulkExec(){$('be_cmd').value='';$('bulkExecModal').classList.add('show')}
async function submitBulkExec(){
  if(!srvSel.size){toast('请先选择服务器','warn');return;}
  const cmd=$('be_cmd').value.trim();if(!cmd){toast('请输入命令','warn');return;}
  const timeout=parseInt($('be_timeout').value)||30;
  try{const d=await api('/api/servers/bulk-exec',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({selector:{names:[...srvSel]},cmd,timeout})});
    toast('已提交批量任务 #'+d.task_id);$('bulkExecModal').classList.remove('show');openTaskId=d.task_id;go('tasks');}catch(e){toast(e.message,'err')}
}
async function batchDelete(){
  if(!srvSel.size){toast('请先选择服务器','warn');return;}
  confirmDialog('批量删除','确定删除选中的 '+srvSel.size+' 台服务器配置吗？（不影响远端服务）',async()=>{
    let ok=0;for(const n of [...srvSel]){try{await api('/api/servers/'+encodeURIComponent(n),{method:'DELETE'});ok++;}catch(e){}}
    srvSel.clear();await loadServers();await loadMeta();renderServers();toast('已删除 '+ok+' 台');
  });
}
async function removeServer(name){
  confirmDialog('删除服务器','确定删除「'+name+'」的配置吗？',async()=>{
    try{await api('/api/servers/'+encodeURIComponent(name),{method:'DELETE'});toast('已删除 '+name);await loadServers();await loadMeta();renderServers();}catch(e){toast(e.message,'err')}
  });
}

/* ----- 服务器增删改（复用直连/代理逻辑） ----- */
function hasUpload(s){return s.type==='upload'||s.type==='both'}
function hasShell(s){return s.type==='shell'||s.type==='both'}
function directBase(s,kind){return 'http://'+s.host+':'+(kind==='upload'?(s.upload_port||8089):(s.shell_port||8088))}
function proxyBase(s,kind){return '/api/servers/'+encodeURIComponent(s.name)+'/r/'+kind}
function remoteTokenHeader(s,kind){const t=kind==='upload'?s.upload_token:s.shell_token;return t?{'X-Auth-Token':t}:{}}
async function fetchWithTimeout(url,opts,ms){const ctl=new AbortController();const timer=setTimeout(()=>ctl.abort(),ms);try{return await fetch(url,Object.assign({},opts,{signal:ctl.signal}))}finally{clearTimeout(timer)}}
async function probeDirect(s,kind){const key=s.name+':'+kind;const c=directProbe[key];if(c&&Date.now()-c.ts<60000)return c.ok;let ok=false;try{await fetchWithTimeout(directBase(s,kind)+'/api/health',{headers:remoteTokenHeader(s,kind)},3500);ok=true}catch(e){ok=false}directProbe[key]={ok,ts:Date.now()};return ok}
async function resolveMode(s,kind){const m=s.access_mode||'auto';if(m!=='auto')return m;const g=settings.access_mode||'auto';if(g!=='auto')return g;return (await probeDirect(s,kind))?'direct':'proxy'}
function linkMode(s,kind){const m=s.access_mode||'auto';if(m!=='auto')return m;const g=settings.access_mode||'auto';if(g!=='auto')return g;const p=directProbe[s.name+':'+kind];if(p&&Date.now()-p.ts<120000)return p.ok?'direct':'proxy';return 'auto'}
function modeLabel(m){return m==='direct'?'直连':(m==='proxy'?'代理':'自动')}
async function remoteFetch(s,kind,path,opts){opts=opts||{};const ms=opts.timeout||20000;const base=Object.assign({},opts);delete base.timeout;
  const doDirect=async()=>({mode:'direct',res:await fetchWithTimeout(directBase(s,kind)+path,Object.assign({},base,{headers:Object.assign({},remoteTokenHeader(s,kind),opts.headers||{}),mode:'cors'}),ms)});
  const doProxy=async()=>({mode:'proxy',res:await fetchWithTimeout(proxyBase(s,kind)+path,Object.assign({},base,{headers:Object.assign({},token?{'X-Auth-Token':token}:{},opts.headers||{})}),ms)});
  const mode=await resolveMode(s,kind);
  if(mode==='proxy')return doProxy();
  if(mode==='direct'){try{return await doDirect()}catch(e){directProbe[s.name+':'+kind]={ok:false,ts:Date.now()};return doProxy()}}
  try{return await doDirect()}catch(e){directProbe[s.name+':'+kind]={ok:false,ts:Date.now()};return doProxy()}
}
async function loadAppSettings(){try{const d=await api('/api/settings');settings=Object.assign(settings,d.settings||{});}catch(e){}}
async function saveGlobalMode(){settings.access_mode=$('modeSel').value;try{await api('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({access_mode:settings.access_mode})});toast('全局模式: '+modeLabel(settings.access_mode));renderServers(true);}catch(e){toast(e.message,'err')}}
async function saveUpSettings(){settings.upload_concurrency=parseInt($('concSel').value)||6;settings.chunk_mb=parseInt($('chunkSel').value)||0;try{await api('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({upload_concurrency:settings.upload_concurrency,chunk_mb:settings.chunk_mb})});toast('并发 '+settings.upload_concurrency+' · 分片 '+(settings.chunk_mb?settings.chunk_mb+'MB':'自动'));}catch(e){toast(e.message,'err')}}
async function toggleFileMode(){const s=currentFileServer;if(!s){toast('请先选择服务器','warn');return;}const cur=linkMode(s,'upload');const next=cur==='proxy'?'direct':'proxy';await setServerMode(s.name,next);}
async function setServerMode(name,mode){try{await api('/api/servers/'+encodeURIComponent(name),{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:name,access_mode:mode})});toast(name+' 链路: '+modeLabel(mode));await loadServers();renderServers(true);}catch(e){toast(e.message,'err')}}

/* 服务器弹层 */
let editName=null;
function toggleTypeFields(){const t=$('f_type').value;const up=t==='upload'||t==='both',sh=t==='shell'||t==='both';$('row_upload_port').style.display=up?'':'none';$('row_upload_token').style.display=up?'':'none';$('row_upload_dir').style.display=up?'':'none';$('row_shell_port').style.display=sh?'':'none';$('row_shell_token').style.display=sh?'':'none';}
function openServerModal(name){const s=name?servers.find(x=>x.name===name):null;editName=name||null;$('serverModalTitle').textContent=s?('✏️ 编辑 '+s.name):'＋ 添加服务器';
  $('f_name').value=s?s.name:'';$('f_host').value=s?s.host:'';$('f_type').value=s?s.type:'both';
  $('f_upload_port').value=s?(s.upload_port||8080):8080;$('f_shell_port').value=s?(s.shell_port||9100):9100;
  $('f_upload_token').value=s?(s.upload_token||''):'';$('f_shell_token').value=s?(s.shell_token||''):'';
  $('f_upload_dir').value=s?(s.upload_dir||''):'';
  $('f_group').value=s?(s.group||''):'';$('f_env').value=s?(s.env||''):'';$('f_tags').value=s?(s.tags||[]).join(','):'';
  $('f_access_mode').value=s?(s.access_mode||'auto'):'auto';$('f_note').value=s?(s.note||''):'';
  toggleTypeFields();$('serverModal').classList.add('show');}
function closeServerModal(){$('serverModal').classList.remove('show');editName=null;}
async function saveServer(){const name=$('f_name').value.trim(),host=$('f_host').value.trim();if(!name||!host){toast('请填写名称和IP','warn');return;}
  const payload={name,host,type:$('f_type').value,upload_port:parseInt($('f_upload_port').value)||8080,shell_port:parseInt($('f_shell_port').value)||9100,upload_token:$('f_upload_token').value.trim(),shell_token:$('f_shell_token').value.trim(),upload_dir:$('f_upload_dir').value.trim(),group:$('f_group').value.trim(),env:$('f_env').value,access_mode:$('f_access_mode').value,note:$('f_note').value.trim()};
  const tags=$('f_tags').value.split(',').map(x=>x.trim()).filter(Boolean);payload.tags=tags;
  try{const isEdit=!!editName;const url=isEdit?'/api/servers/'+encodeURIComponent(editName):'/api/servers';const method=isEdit?'PUT':'POST';
    await api(url,{method,headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    toast(isEdit?'已更新 '+name:'已添加 '+name);closeServerModal();await loadServers();await loadMeta();renderServers();}catch(e){toast(e.message,'err')}
}

/* ----- 服务器批量导入 / 导出 ----- */
function exportServers(fmt){window.open(authUrl('/api/servers/export?format='+fmt),'_blank');}
function openImportServers(){$('imp_text').value='';$('imp_file').value='';$('imp_hint').textContent='支持先「导出JSON」再修改后回导；单次最多 500 台，坏条目会跳过并在结果中列出。';$('importSrvModal').classList.add('show');}
function importSrvReadFile(){const f=$('imp_file').files[0];if(!f)return;const r=new FileReader();
  r.onload=()=>{$('imp_text').value=r.result;$('imp_hint').textContent='已读取 '+f.name+'（'+f.size+' 字节），可直接导入或先编辑。';};
  r.onerror=()=>toast('读取文件失败','err');r.readAsText(f,'utf-8');}
async function submitImportServers(){
  let data;try{data=JSON.parse($('imp_text').value);}catch(e){toast('JSON 解析失败：'+e.message,'err');return;}
  const list=Array.isArray(data)?data:(data&&data.servers)||[];
  if(!list.length){toast('没有可导入的服务器条目','warn');return;}
  try{const d=await api('/api/servers/import',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({servers:list,mode:$('imp_mode').value})});
    let msg='新增 '+d.added+'，更新 '+d.updated+'，跳过 '+(d.skipped||0);
    const errs=d.errors||[];let etype='ok';
    if(d.error_total){msg+='，失败 '+d.error_total+'（'+(errs.map(x=>x.name||('#'+x.index)).join(', ')+(d.error_total>errs.length?' ...':''))+'）';etype='warn';}
    toast('导入完成：'+msg,etype);
    $('importSrvModal').classList.remove('show');
    await loadServers();await loadMeta();renderServers();}catch(e){toast(e.message,'err')}
}

/* ================= 文件管理 ================= */
let currentFileServer=null;
let remoteFilesCache=[];
let filePage=1,fileSort='name',fileOrder='asc',fileTypeFilter='',fileKw='';
let fileSel=new Set();
async function renderFileSrvList(){
  const list=servers.filter(hasUpload);
  if(!list.length){$('fileSrvList').innerHTML='<div class="empty" style="padding:20px">暂无上传服务器</div>';return;}
  $('fileSrvList').innerHTML=list.map(s=>{
    const on=s.status&&s.status.online;const active=currentFileServer&&currentFileServer.name===s.name;
    const ledCls=on?'on':(s.status?'off':'');
    return '<div class="sl-item '+(active?'active':'')+'" data-name="'+esc(s.name)+'"><span class="led '+ledCls+'"></span><div style="min-width:0"><div class="sl-name">'+esc(s.name)+'</div><div class="sl-host">'+esc(s.host)+':'+(s.upload_port||8080)+'</div></div></div>';
  }).join('');
}
$('fileSrvList').addEventListener('click',e=>{const it=e.target.closest('.sl-item');if(it&&it.dataset.name)openFilesFor(it.dataset.name);});
function openFilesFor(name){const s=servers.find(x=>x.name===name);if(!s)return;currentFileServer=s;go('files');renderFileSrvList();$('fileTitle').textContent='📂 '+s.name;const bd=$('fileBadge');bd.style.display='inline-block';bd.textContent=s.host+':'+s.upload_port+(s.upload_dir?' → '+s.upload_dir:'');bd.title=s.upload_dir?('上传目录：'+s.upload_dir):'';renderFileMode();loadRemoteFiles();}
function renderFileMode(){const btn=$('linkModeBtn'),dz=$('dzMode');if(!btn)return;if(!currentFileServer){btn.textContent='🔗 链路';btn.className='btn btn-sm';if(dz)dz.textContent='自动';return;}const m=linkMode(currentFileServer,'upload');btn.textContent='🔗 '+modeLabel(m)+(m==='auto'?'':' · 点此切换');btn.className='btn btn-sm'+(m==='proxy'?'':' btn-success');if(dz)dz.textContent=modeLabel(m);}
async function loadRemoteFiles(){
  if(!currentFileServer){$('fileListContent').innerHTML='<div class="empty">← 从左侧选择一台服务器</div>';return;}
  $('fileListContent').innerHTML='<div class="empty">加载中...</div>';
  try{const r=await remoteFetch(currentFileServer,'upload','/api/list',{timeout:15000});const d=await r.res.json().catch(()=>({}));
    if(!r.res.ok)throw new Error(d.message||('HTTP '+r.res.status));
    remoteFilesCache=d.files||[];filePage=1;renderRemoteFiles();}
  catch(e){$('fileListContent').innerHTML='<div class="empty" style="color:var(--red)">'+esc(e.message)+'</div>'}
}
function fileTypeOf(f){const n=(f.name||'').toLowerCase();if(f.is_dir)return 'dir';if(/\.(zip|tar|gz|tgz|tar\.gz|7z|rar|bz2|xz)$/.test(n))return 'zip';if(/\.jar$/.test(n))return 'jar';if(/\.(xls|xlsx|csv)$/.test(n))return 'excel';if(/\.(log|out|err)$/.test(n))return 'log';if(/\.(txt|conf|xml|json|yml|yaml|md|sh|py|ini|properties|cfg)$/.test(n))return 'text';if(/\.(png|jpg|jpeg|gif|bmp|svg|webp)$/.test(n))return 'image';return 'other';}
function renderRemoteFiles(){
  const c=$('fileListContent');if(!currentFileServer){c.innerHTML='<div class="empty">← 从左侧选择一台服务器</div>';return;}
  fileKw=($('fileSearch').value||'').trim().toLowerCase();
  let files=remoteFilesCache.slice();
  if(fileKw)files=files.filter(f=>(f.name||'').toLowerCase().indexOf(fileKw)>=0);
  if(fileTypeFilter)files=files.filter(f=>fileTypeOf(f)===fileTypeFilter);
  files.sort((a,b)=>{let r=0;if(fileSort==='name')r=(a.name||'').localeCompare(b.name||'');else if(fileSort==='size')r=(Number(a.size)||0)-(Number(b.size)||0);else if(fileSort==='mtime')r=String(a.mtime||'').localeCompare(String(b.mtime||''));else if(fileSort==='type')r=fileTypeOf(a).localeCompare(fileTypeOf(b));return fileOrder==='asc'?r:-r;});
  const size=parseInt($('pageSize').value)||20;const pages=Math.max(1,Math.ceil(files.length/size));if(filePage>pages)filePage=pages;
  const slice=files.slice((filePage-1)*size,filePage*size);
  $('pageInfo').textContent=filePage+'/'+pages+' (共'+files.length+')';
  if(!slice.length){c.innerHTML='<div class="empty">'+(files.length?'无匹配':'暂无文件')+'</div>';updateBatchBar();return;}
  let rows='<table class="file-table"><thead><tr><th style="width:30px"><input type="checkbox" id="rmCheckAll" onclick="toggleAllRemote(this)"></th><th onclick="setSort(\'name\')">名称</th><th style="width:90px" onclick="setSort(\'size\')">大小</th><th style="width:150px" onclick="setSort(\'mtime\')">时间</th><th style="width:120px" onclick="setSort(\'type\')">类型</th><th style="width:120px">操作</th></tr></thead><tbody>';
  slice.forEach(f=>{rows+='<tr><td style="text-align:center"><input type="checkbox" class="rm-check" value="'+encodeURIComponent(f.name)+'" onchange="updateBatchBar()"></td>'+
    '<td style="word-break:break-all">'+esc(f.name)+'</td><td>'+fmtSize(f.size)+'</td><td>'+esc(f.mtime||'')+'</td><td>'+fileTypeOf(f)+'</td>'+
    '<td class="op"><a onclick="downloadFile(\''+encodeURIComponent(f.name)+'\')">下载</a><a class="del" onclick="deleteFile(\''+encodeURIComponent(f.name)+'\')" data-perm="file.delete">删除</a></td></tr>';});
  rows+='</tbody></table>';c.innerHTML=rows;updateBatchBar();applyPerms();
}
function setSort(k){if(fileSort===k)toggleOrder();else{fileSort=k;renderRemoteFiles();}}
function toggleOrder(){fileOrder=fileOrder==='asc'?'desc':'asc';$('sortOrderBtn').textContent=fileOrder==='asc'?'↑ 升序':'↓ 降序';renderRemoteFiles();}
function pagePrev(){if(filePage>1){filePage--;renderRemoteFiles();}}
function pageNext(){filePage++;renderRemoteFiles();}
function selectedRemotePaths(){const arr=Array.from(document.querySelectorAll('.rm-check:checked')).map(cb=>decodeURIComponent(cb.value));return Array.from(new Set(arr));}
function updateBatchBar(){const n=selectedRemotePaths().length;$('batchCount').textContent=n;$('batchBar').style.display=n?'flex':'none';const all=$('rmCheckAll');if(all)all.checked=!!document.querySelectorAll('.rm-check').length&&n===document.querySelectorAll('.rm-check').length;}
function toggleAllRemote(cb){document.querySelectorAll('.rm-check').forEach(c=>c.checked=cb.checked);updateBatchBar();}
function clearBatchSel(){document.querySelectorAll('.rm-check').forEach(c=>c.checked=false);updateBatchBar();}
function downloadFile(enc){let url='/api/servers/'+encodeURIComponent(currentFileServer.name)+'/download?path='+enc;window.open(authUrl(url),'_blank');}
async function deleteFile(enc){confirmDialog('删除文件','确定删除该文件吗？',async()=>{try{await api('/api/servers/'+encodeURIComponent(currentFileServer.name)+'/files?path='+enc,{method:'DELETE'});toast('已删除');loadRemoteFiles();}catch(e){toast(e.message,'err')}});}
async function batchDownloadFiles(){const paths=selectedRemotePaths();if(!paths.length)return;let url='/api/servers/'+encodeURIComponent(currentFileServer.name)+'/batch-download?'+paths.map(p=>'paths='+encodeURIComponent(p)).join('&');window.open(authUrl(url),'_blank');}
async function batchDeleteFiles(){const paths=selectedRemotePaths();if(!paths.length)return;confirmDialog('批量删除','确定删除选中的 '+paths.length+' 项吗？',async()=>{try{const d=await api('/api/servers/'+encodeURIComponent(currentFileServer.name)+'/batch-delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({paths})});toast('批量删除完成');clearBatchSel();loadRemoteFiles();}catch(e){toast(e.message,'err')}});}

/* ---------- 分片上传（复用已验证逻辑） ---------- */
let uploadItems=[],taskQueue=[],activeCount=0,startingCount=0,uploadSeq=0,paintTimer=null,lastPaint=0,reloadTimer=null;
const CHUNK_RETRY=3,MB=1048576;
function upConcurrency(){return Math.min(16,Math.max(1,Number(settings.upload_concurrency||6)));}
function pickChunkSize(size){const manual=Number(settings.chunk_mb||0);if(manual>0)return manual*MB;const mb=size/MB;if(mb<=32)return 4*MB;if(mb<=256)return 8*MB;return 16*MB;}
function fileKey(file,rel,cs){const str=(rel||file.name)+'|'+file.size+'|'+(file.lastModified||0)+'|'+cs;let h1=0x811c9dc5,h2=0x01000193;for(let i=0;i<str.length;i++){const c=str.charCodeAt(i);h1=Math.imul(h1^c,0x01000193)>>>0;h2=Math.imul(h2^c,0x85ebca6b)>>>0}return ('00000000'+h1.toString(16)).slice(-8)+('00000000'+h2.toString(16)).slice(-8);}
function isPending(it){return it.state==='wait'||it.state==='init'||it.state==='uploading';}
function sumInflight(it){let s=0;for(const k in it.inflight)s+=it.inflight[k];return s;}
function addSample(it){const now=Date.now();it.samples.push({t:now,b:it.loaded||0});while(it.samples.length>2&&now-it.samples[0].t>3000)it.samples.shift();if(it.samples.length>=2){const a=it.samples[0],b=it.samples[it.samples.length-1],dt=(b.t-a.t)/1000;if(dt>0.2)it.speed=Math.max(0,(b.b-a.b)/dt);}}
function getFilesFromItems(items){return new Promise(resolve=>{const files=[];const entries=[];for(const it of items){const en=it.webkitGetAsEntry&&it.webkitGetAsEntry();if(en)entries.push(en);}if(!entries.length){for(const it of items){const f=it.getAsFile&&it.getAsFile();if(f)files.push(f)}resolve(files);return;}const readEntries=dir=>new Promise(ok=>{const r=dir.createReader();const all=[];const batch=()=>{r.readEntries(b=>{if(b.length){all.push(...b);batch()}else ok(all)},()=>ok(all))};batch()});const walk=async(entry,base)=>{if(entry.isFile){const f=await new Promise(r=>entry.file(r));f._rel=base?base+'/'+entry.name:entry.name;files.push(f);}else if(entry.isDirectory){const kids=await readEntries(entry);for(const k of kids)await walk(k,base?base+'/'+entry.name:entry.name)}};(async()=>{for(const e of entries)await walk(e,'');resolve(files)})();});}
function buildQueue(files){const s=currentFileServer;if(!s){toast('请先选择服务器','warn');return;}if(!files.length)return;files.forEach(f=>{const rel=f._rel||(f.webkitRelativePath?f.webkitRelativePath.replace(/^[^/]*\//,''):'');const total=f.size||0;const cs=total?pickChunkSize(total):0;uploadItems.push({id:++uploadSeq,server:s,file:f,rel:rel,total:total,chunkSize:cs,totalChunks:total?Math.ceil(total/cs):1,key:fileKey(f,rel,cs),doneBytes:0,loaded:0,pct:0,pending:0,inflight:{},xhrs:new Set(),samples:[],speed:0,startTime:null,timeMs:null,state:'wait',error:'',mode:null,base:'',hdrs:{},uploadId:'',_aborted:false,el:null,els:null});});paintQueue();pumpStarts();}
function qStateText(st){return{wait:'等待',init:'准备中',uploading:'上传中',done:'成功',fail:'失败'}[st]||st;}
function itemElapsed(it){if(it.timeMs!=null)return fmtTime(it.timeMs);if(it.state==='uploading'&&it.startTime)return fmtTime(Date.now()-it.startTime);return '-';}
function scheduleRender(){if(paintTimer)return;const wait=Math.max(0,140-(performance.now()-lastPaint));paintTimer=setTimeout(()=>{paintTimer=null;lastPaint=performance.now();paintQueue()},wait);}
function updateQueue(){paintQueue();}
function ensureRow(it){if(it.el&&it.el.parentNode)return it.el;const row=document.createElement('div');row.className='uq-item';row.innerHTML='<span class="uq-name"></span><div class="uq-b"><div class="uq-bf"></div></div><span class="uq-pct" style="text-align:right;font-family:var(--mono);font-size:11px">0%</span><span class="uq-sp">-</span><span class="uq-st wait">等待</span><span class="uq-tm">-</span>';const name=row.querySelector('.uq-name');name.textContent=it.rel||it.file.name;name.title=it.rel||it.file.name;it.el=row;it.els={bf:row.querySelector('.uq-bf'),pct:row.querySelector('.uq-pct'),sp:row.querySelector('.uq-sp'),st:row.querySelector('.uq-st'),tm:row.querySelector('.uq-tm')};$('uqItems').appendChild(row);return row;}
function paintQueue(){const box=$('uqItems');$('upQueue').style.display=uploadItems.length?'block':'none';uploadItems.forEach(ensureRow);Array.from(box.children).forEach(c=>{if(!uploadItems.some(x=>x.el===c))box.removeChild(c)});let totalAll=0,loadedAll=0,pendTotal=0,pendLoaded=0,pendSpeed=0,pendCount=0;uploadItems.forEach(it=>{const e=it.els;e.bf.style.width=(it.pct||0)+'%';e.pct.textContent=(it.pct||0)+'%';e.sp.textContent=it.state==='uploading'?fmtSpeed(it.speed):'-';e.st.className='uq-st '+(it.state==='uploading'?'up':it.state);e.st.textContent=qStateText(it.state);e.tm.textContent=itemElapsed(it);totalAll+=(it.total||0);loadedAll+=(it.state==='done'?(it.total||0):(it.loaded||0));if(isPending(it)){pendCount++;pendTotal+=(it.total||0);pendLoaded+=(it.loaded||0);pendSpeed+=(it.speed||0)}});const pct=totalAll?Math.round(loadedAll/totalAll*100):0;$('uqFill').style.width=pct+'%';$('uqText').textContent=pct+'%';const f=$('upFloat');if(pendCount){const pp=pendTotal?Math.round(pendLoaded/pendTotal*100):0;f.style.display='flex';f.innerHTML='⬆ '+pp+'% · '+fmtSpeed(pendSpeed)+' · '+pendCount+' 文件'}else f.style.display='none';}
function fmtSpeed(bps){bps=Number(bps)||0;if(bps<=0)return '-';if(bps<1024)return bps.toFixed(0)+' B/s';if(bps<1048576)return (bps/1024).toFixed(1)+' KB/s';return (bps/1048576).toFixed(2)+' MB/s';}
function normDir(p){return String(p||'').replace(/\\/g,'/').replace(/\/+$/,'');}
/* 上传成功后按响应里的真实落盘路径校验「配的目录是否生效」：
   upload_server 未开 --allow-client-dir 时会静默回落其默认目录（不报错），
   不提示的话用户会以为传到了配置的目录。每台服务器只提示一次。 */
function checkLanded(s,d){if(!s||!s.upload_dir||!d)return;const real=d.dir||(d.path&&d.path.replace(/\/[^/]*$/,''))||'';if(!real)return;
  if(normDir(real).toLowerCase()!==normDir(s.upload_dir).toLowerCase()&&!s._dirWarned){s._dirWarned=true;
    toast(s.name+' 文件实际落盘 '+real+'（与配置的上传目录不一致：目标机需 upload_server v3.3.0+ 并以 --allow-client-dir 启动）','warn');}}
function putSingle(it,m){const s=it.server;const base=(m==='proxy'?proxyBase(s,'upload'):directBase(s,'upload'))+'/api/chunk/put';const hdrs=m==='proxy'?(token?{'X-Auth-Token':token}:{}):remoteTokenHeader(s,'upload');const q='?filename='+encodeURIComponent(it.file.name)+'&relative_path='+encodeURIComponent(it.rel||'')+'&size='+(it.total||0)+(s.upload_dir?'&dir='+encodeURIComponent(s.upload_dir):'');it.loaded=0;it.pct=0;it.speed=0;it.samples=[];it.mode=m;return new Promise((resolve,reject)=>{const xhr=new XMLHttpRequest();it.xhrs.add(xhr);xhr.open('POST',base+q,true);Object.keys(hdrs).forEach(k=>xhr.setRequestHeader(k,hdrs[k]));xhr.timeout=600000;xhr.upload.onprogress=e=>{if(!e.lengthComputable)return;it.loaded=e.loaded;it.pct=it.total?Math.min(99,Math.round(e.loaded/it.total*100)):0;addSample(it);scheduleRender();};const done=(ok,msg)=>{it.xhrs.delete(xhr);if(ok){it.state='done';it.pct=100;it.loaded=it.total;it.speed=0;it.timeMs=Date.now()-(it.startTime||Date.now());maybeReload();scheduleRender();pumpUploads();resolve();}else{scheduleRender();pumpUploads();reject(new Error(msg));}};xhr.onload=()=>{let ok=false,msg='';try{const d=JSON.parse(xhr.responseText);ok=(xhr.status===200&&d.status==='success');msg=(d&&(d.message||d.error))||('HTTP '+xhr.status);if(ok)checkLanded(s,d);}catch(e){msg='响应解析失败'}done(ok,msg)};xhr.onerror=()=>done(false,'网络错误');xhr.ontimeout=()=>done(false,'上传超时');xhr.onabort=()=>done(false,'已取消');xhr.send(it.file);});}
async function initItem(it){const s=it.server;const mode=await resolveMode(s,'upload');if(it.totalChunks<=1){it.startTime=Date.now();it.state='uploading';it.loaded=0;it.pct=0;try{return await putSingle(it,mode)}catch(e){if(mode==='proxy'||it._aborted)throw e;directProbe[s.name+':upload']={ok:false,ts:Date.now()};toast(s.name+' 直连失败，改用代理','warn');return await putSingle(it,'proxy');}}const doInit=async m=>{const base=(m==='proxy'?proxyBase(s,'upload'):directBase(s,'upload'))+'/api/chunk';const hdrs=m==='proxy'?(token?{'X-Auth-Token':token}:{}):remoteTokenHeader(s,'upload');const res=await fetch(base+'/init',{method:'POST',headers:Object.assign({'Content-Type':'application/json'},hdrs),body:JSON.stringify({filename:it.file.name,relative_path:it.rel,size:it.total,total_chunks:it.totalChunks,chunk_size:it.chunkSize,resume_key:it.key,dir:s.upload_dir||''})});const d=await res.json().catch(()=>({}));if(!res.ok||d.status!=='success')throw new Error((d&&d.message)||('HTTP '+res.status));return {base:base,hdrs:hdrs,d:d,mode:m};};let r;try{r=await doInit(mode)}catch(e){if(mode==='proxy'||it._aborted)throw e;directProbe[s.name+':upload']={ok:false,ts:Date.now()};r=await doInit('proxy');toast(s.name+' 直连失败，改用代理','warn');}it.mode=r.mode;it.base=r.base;it.hdrs=r.hdrs;const d=r.d;it.uploadId=d.upload_id;const uploaded=new Set(d.uploaded||[]);it.state='uploading';it.startTime=Date.now();const cs=it.chunkSize||0;for(let i=0;i<it.totalChunks;i++){const start=i*cs;const end=it.total?Math.min(it.total,start+cs):0;if(uploaded.has(i)){it.doneBytes+=(end-start);continue;}taskQueue.push({it:it,index:i,start:start,end:end,tries:0});it.pending++;}it.loaded=it.doneBytes;it.pct=it.total?Math.min(99,Math.round(it.loaded/it.total*100)):0;if(it.pending===0)await completeItem(it);}
function pumpStarts(){const limit=Math.max(1,Math.min(4,upConcurrency()));while(startingCount<limit){const it=uploadItems.find(x=>x.state==='wait');if(!it)break;it.state='init';startingCount++;initItem(it).then(()=>{startingCount--;pumpStarts();pumpUploads()}).catch(err=>{startingCount--;failItem(it,(err&&err.message)||String(err));pumpStarts();pumpUploads()});}scheduleRender();}
function pumpUploads(){const limit=upConcurrency();while(activeCount<limit&&taskQueue.length){const t=taskQueue.shift();if(t.it.state!=='uploading')continue;runChunkTask(t);}if(activeCount===0&&!taskQueue.length&&!uploadItems.some(x=>x.state==='init'))maybeReload();scheduleRender();}
function runChunkTask(t){activeCount++;const it=t.it;const xhr=new XMLHttpRequest();it.xhrs.add(xhr);const url=it.base+'/upload';xhr.open('POST',url,true);Object.keys(it.hdrs).forEach(k=>xhr.setRequestHeader(k,it.hdrs[k]));xhr.setRequestHeader('X-Upload-Id',it.uploadId);xhr.setRequestHeader('X-Chunk-Index',String(t.index));xhr.timeout=600000;xhr.upload.onprogress=e=>{if(!e.lengthComputable)return;it.inflight[t.index]=e.loaded;it.loaded=it.doneBytes+sumInflight(it);it.pct=it.total?Math.min(99,Math.round(it.loaded/it.total*100)):0;addSample(it);scheduleRender();};const done=(ok,msg)=>{it.xhrs.delete(xhr);finishTask(t,ok,msg)};xhr.onload=()=>{let ok=false,msg='';try{const d=JSON.parse(xhr.responseText);ok=(xhr.status===200&&d.status==='success');msg=(d&&d.message)||('HTTP '+xhr.status)}catch(e){msg='响应解析失败'}done(ok,msg)};xhr.onerror=()=>done(false,'网络错误');xhr.ontimeout=()=>done(false,'分片超时');xhr.onabort=()=>done(false,'已取消');xhr.send(it.file.slice(t.start,t.end));}
function finishTask(t,ok,msg){const it=t.it;delete it.inflight[t.index];activeCount=Math.max(0,activeCount-1);if(it.state!=='uploading'){pumpUploads();scheduleRender();return;}if(ok){it.doneBytes+=(t.end-t.start);it.loaded=it.doneBytes+sumInflight(it);it.pct=it.total?Math.min(99,Math.round(it.loaded/it.total*100)):0;addSample(it);it.pending--;if(it.pending<=0)completeItem(it);}else if(it._aborted){failItem(it,'已取消');}else if(t.tries<CHUNK_RETRY){t.tries++;setTimeout(()=>{if(!it._aborted&&it.state==='uploading')taskQueue.push(t);pumpUploads()},400*t.tries);}else{failItem(it,msg);}pumpUploads();scheduleRender();}
function failItem(it,msg){if(it.state==='fail'||it.state==='done')return;it.state='fail';it.error=msg||'';it.timeMs=Date.now()-(it.startTime||Date.now());it.pending=0;it.speed=0;taskQueue=taskQueue.filter(x=>x.it!==it);it.xhrs.forEach(x=>{try{x.abort()}catch(e){}});it.xhrs.clear();if((msg||'').indexOf('404')>=0)toast('分片接口 404：请确认目标运行 upload_server','err');else if((msg||'').indexOf('401')>=0)toast('上传令牌错误','err');maybeReload();}
async function completeItem(it){try{const res=await fetch(it.base+'/complete',{method:'POST',headers:Object.assign({'Content-Type':'application/json'},it.hdrs),body:JSON.stringify({upload_id:it.uploadId,filename:it.file.name,relative_path:it.rel,total_chunks:it.totalChunks,size:it.total,resume_key:it.key,dir:it.server.upload_dir||''})});const d=await res.json().catch(()=>({}));if(!res.ok||d.status!=='success')throw new Error((d&&d.message)||('HTTP '+res.status));checkLanded(it.server,d);it.state='done';it.pct=100;it.loaded=it.total;it.speed=0;it.timeMs=Date.now()-(it.startTime||Date.now());}catch(e){it.state='fail';it.error=e.message||String(e);it.timeMs=Date.now()-(it.startTime||Date.now());}maybeReload();scheduleRender();pumpUploads();}
function maybeReload(){if(reloadTimer)return;reloadTimer=setTimeout(()=>{reloadTimer=null;if(currentFileServer&&currentView==='files'&&!uploadItems.some(isPending)&&!activeCount)loadRemoteFiles();},900);}
function abortUploads(){taskQueue=[];uploadItems.forEach(it=>{it._aborted=true;if(isPending(it)){it.state='fail';it.error='已取消';it.timeMs=Date.now()-(it.startTime||Date.now());it.speed=0}it.xhrs.forEach(x=>{try{x.abort()}catch(e){}});it.xhrs.clear();});paintQueue();}
function clearFinished(){uploadItems=uploadItems.filter(isPending);uploadItems.forEach(it=>{if(it.el&&it.el.parentNode){it.el.parentNode.removeChild(it.el);it.el=null;it.els=null}});paintQueue();}
async function handleFileInput(input){if(!currentFileServer){toast('请先选择服务器','warn');return;}const files=Array.from(input.files).map(f=>{if(f.webkitRelativePath)f._rel=f.webkitRelativePath.replace(/^[^/]*\//,'');return f});if(files.length)buildQueue(files);input.value='';}
$('uploadInput').addEventListener('change',e=>handleFileInput(e.target));
$('folderInput').addEventListener('change',e=>handleFileInput(e.target));
(function bindDrop(){const dz=$('dropZone');const prevent=e=>{e.preventDefault();e.stopPropagation()};['dragenter','dragover'].forEach(t=>dz.addEventListener(t,e=>{prevent(e);dz.classList.add('dragover')}));['dragleave','drop'].forEach(t=>dz.addEventListener(t,e=>{prevent(e);dz.classList.remove('dragover')}));dz.addEventListener('click',()=>{if(currentFileServer)$('uploadInput').click();else toast('请先选择服务器','warn')});dz.addEventListener('drop',async e=>{if(!currentFileServer){toast('请先选择服务器','warn');return;}const files=await getFilesFromItems(e.dataTransfer.items);if(files.length)buildQueue(files);else toast('未能识别内容','warn');});})();

/* ================= 命令终端 ================= */
let selectedTermServers=new Set();
let termSSE=null,termRunning=false;
// 历史命令的本地存储可能损坏：顶层 JSON.parse 无兜底会让整个 SPA 白屏且刷新复现
let hist=[];try{const raw=JSON.parse(storeGet('ops_hist')||'[]');if(Array.isArray(raw))hist=raw;}catch(_){hist=[]}
let histIdx=hist.length;
let termBuf='';
function renderTermServers(){const box=$('termSrvs');const sh=servers.filter(hasShell);if(!sh.length){box.innerHTML='<span style="color:var(--dim);font-size:13px">暂无终端服务器</span>';return;}box.innerHTML=sh.map(s=>{const on=s.status&&s.status.online;const active=selectedTermServers.has(s.name);return '<span class="tchip srv '+(active?'active':'')+(on===false?' offline':'')+'" data-name="'+esc(s.name)+'">'+esc(s.name)+'</span>';}).join('');}
$('termSrvs').addEventListener('click',e=>{const el=e.target.closest('.tchip.srv');if(el&&el.dataset.name){const n=el.dataset.name;if(selectedTermServers.has(n))selectedTermServers.delete(n);else selectedTermServers.add(n);renderTermServers();}});
function renderTermChips(){$('termChips').innerHTML=QUICK_CMDS.map(c=>'<span class="tchip" data-cmd="'+esc(c)+'">'+esc(c)+'</span>').join('');}
$('termChips').addEventListener('click',e=>{const el=e.target.closest('.tchip');if(el&&el.dataset.cmd){$('cmdInput').value=el.dataset.cmd;execRemote();}});
function clearTerm(){$('term').innerHTML='';termBuf='';}
function addTermHtml(html){const t=$('term');t.insertAdjacentHTML('beforeend',html);t.scrollTop=t.scrollHeight;termBuf+=html;}
function exportTerm(){const text=termBuf.replace(/<[^>]+>/g,'');const blob=new Blob([text],{type:'text/plain'});const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='terminal-'+Date.now()+'.txt';a.click();}
function openTermFor(name){selectedTermServers.clear();selectedTermServers.add(name);go('term');}
async function execRemote(){const cmd=$('cmdInput').value.trim();if(!cmd||termRunning)return;if(!selectedTermServers.size){toast('请先选择服务器','warn');return;}
  termRunning=true;$('runBtn').disabled=true;
  try{
    if(hist[hist.length-1]!==cmd){hist.push(cmd);if(hist.length>200)hist.shift();storeSet('ops_hist',JSON.stringify(hist));}
    histIdx=hist.length;$('cmdInput').value='';
    const timeout=parseInt($('timeoutSel').value)||30;
    const targets=servers.filter(s=>selectedTermServers.has(s.name));
    if(targets.length>1)await runBatchTask(cmd,targets,timeout);
    else if(targets.length===1)await runSingleSSE(cmd,targets[0],timeout);
  }catch(e){addTermHtml('<div class="t-err">[错误] '+esc(e.message)+'</div>');}
  finally{termRunning=false;$('runBtn').disabled=false;$('cmdInput').focus();}
}
async function runSingleSSE(cmd,s,timeout){
  if(termSSE)termSSE.close();
  addTermHtml('<div class="t-cmd">'+esc(cmd)+'<span class="t-srv">🖥 '+esc(s.name)+'</span></div>');
  let url='';
  try{
    // 先换一次性执行券（命令放请求体）：这样浏览器 URL 上不会出现 cmd=...，避免被
    // 安全设备的「URI 命令执行」特征规则误判为 webshell 攻击（现场态势感知误报）。
    // 老平台没有该接口时 api() 抛错 → 退回旧的查询串形态，行为不变。
    const d=await api('/api/servers/'+encodeURIComponent(s.name)+'/exec-ticket',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cmd,timeout})});
    if(d&&d.ticket)url='/api/servers/'+encodeURIComponent(s.name)+'/exec-stream?ticket='+encodeURIComponent(d.ticket)+'&token='+encodeURIComponent(token);
  }catch(e){}
  if(!url)url='/api/servers/'+encodeURIComponent(s.name)+'/exec-stream?cmd='+encodeURIComponent(cmd)+'&timeout='+timeout+'&token='+encodeURIComponent(token);
  termSSE=new EventSource(url);
  termSSE.addEventListener('start',()=>{});
  termSSE.addEventListener('out',e=>{addTermHtml('<div class="t-out">'+esc(e.data)+'\n</div>');});
  termSSE.addEventListener('err',e=>{addTermHtml('<div class="t-err">'+esc(e.data)+'\n</div>');});
  termSSE.addEventListener('exit',e=>{let rc=0;const m=/returncode=(\d+)/.exec(e.data||'');if(m)rc=parseInt(m[1]);addTermHtml('<div class="t-meta">[返回码 '+rc+'] · '+(rc===0?'成功':'失败')+'</div>');termSSE.close();termSSE=null;});
  termSSE.onerror=()=>{addTermHtml('<div class="t-meta">[连接中断]</div>');if(termSSE){termSSE.close();termSSE=null;}};
}
async function runBatchTask(cmd,targets,timeout){
  addTermHtml('<div class="t-cmd">'+esc(cmd)+'<span class="t-srv">📡 批量 → '+targets.map(s=>s.name).join(', ')+'</span></div>');
  addTermHtml('<div class="t-meta">⏳ 已提交批量任务...</div>');
  let tid=null;
  try{const d=await api('/api/servers/bulk-exec',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({selector:{names:targets.map(s=>s.name)},cmd,timeout})});tid=d.task_id;addTermHtml('<div class="t-meta">任务 #'+tid+' 执行中</div>');}catch(e){addTermHtml('<div class="t-err">'+esc(e.message)+'</div>');return;}
  const es=new EventSource('/api/tasks/'+tid+'/stream?token='+encodeURIComponent(token));
  let lastLog=0;
  es.onmessage=e=>{try{const m=JSON.parse(e.data);const t=m.data;if(t&&t.log){for(let i=lastLog;i<t.log.length;i++){addTermHtml('<div class="t-out">'+esc(t.log[i].text)+'\n</div>');}lastLog=t.log.length;}if(t&&(t.state==='success'||t.state==='failed'||t.state==='canceled')){addTermHtml('<div class="t-meta">[批量完成 '+t.state_label+']</div>');es.close();}}catch(_){}};
  es.onerror=()=>{es.close();};
}
$('cmdInput').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();execRemote();}else if(e.key==='ArrowUp'&&$('cmdInput').selectionStart===0){if(hist.length){histIdx=Math.max(0,histIdx-1);$('cmdInput').value=hist[histIdx]||''}e.preventDefault();}else if(e.key==='ArrowDown'&&$('cmdInput').selectionEnd===$('cmdInput').value.length){histIdx=Math.min(hist.length,histIdx+1);$('cmdInput').value=hist[histIdx]||'';e.preventDefault();}});
$('cmdInput').addEventListener('input',()=>{const el=$('cmdInput');el.style.height='auto';el.style.height=Math.min(el.scrollHeight,120)+'px';});

/* ================= 发布中心 ================= */
function relOfflineHtml(url){return '<div class="rel-config offline"><b>⚠ 发布中心离线</b><div style="margin-top:6px;font-size:12px;color:var(--muted)">未检测到 release_center，请确认其已启动并配置地址。</div>'+
  '<div style="margin-top:8px;font-family:var(--mono);font-size:12px">地址：'+esc(url||'http://127.0.0.1:8094')+'</div>'+
  '<div style="margin-top:4px;font-family:var(--mono);font-size:12px">启动：<code>python release_center.py --port 8094</code></div>'+
  '<button class="btn btn-sm" style="margin-top:8px" onclick="goReleaseSettings()" data-perm="settings.edit">⚙ 修改发布中心地址</button></div>';}
function relUnauthHtml(c){return '<div class="rel-config offline"><b>⚠ 发布中心已连通，但未获授权（401）</b>'+
  '<div style="margin-top:6px;font-size:12px;color:var(--muted)">平台访问发布中心需要凭据：请在「设置」里填写发布中心的<b>管理员账号与口令</b>（平台会自动登录，无需重启任何服务）；或给 release_center 启动参数加 <code>--token X</code>，并把 X 填到「发布中心令牌」。</div>'+
  '<div style="margin-top:8px;font-family:var(--mono);font-size:12px">地址：'+esc((c&&c.url)||'')+'</div>'+
  '<button class="btn btn-sm btn-primary" style="margin-top:8px" onclick="goReleaseSettings()" data-perm="settings.edit">⚙ 去设置账号与口令</button></div>';}
function goReleaseSettings(){go('settings');setTimeout(()=>{const el=$('set_release_url');if(el){el.focus();el.select();}},80);}
async function loadRelease(){
  try{const c=await api('/api/release/config');const box=$('releaseConfig');
    if(!c.online){box.className='rel-config offline';box.innerHTML=relOfflineHtml(c.url);}
    else if(c.authorized===false){box.className='rel-config offline';box.innerHTML=relUnauthHtml(c);}
    else{box.className='rel-config online';box.innerHTML='<b>✅ 发布中心在线</b> '+esc(c.url)+(c.version?' · v'+esc(c.version):'');}
  }catch(e){$('releaseConfig').className='rel-config offline';$('releaseConfig').innerHTML=relOfflineHtml(settings.release_url);}
  bindRelPkgDrop();
  await Promise.all([loadRelApps(),loadRelPkgs(),loadRelOrders()]);
}
function asArr(d,keys){if(!d)return [];if(Array.isArray(d))return d;for(const k of keys){if(Array.isArray(d[k]))return d[k];}if(Array.isArray(d.items))return d.items;if(Array.isArray(d.data))return d.data;return [];}
async function loadRelApps(){try{const d=await api('/api/release/apps');const apps=asArr(d,['apps','applications']);$('relAppList').innerHTML=apps.length?apps.map(a=>'<div class="item">'+esc(a.name)+' <span class="tag">'+(a.biz||'')+'</span></div>').join(''):'<div class="empty">暂无应用</div>';}catch(e){$('relAppList').innerHTML='<div class="empty" style="color:var(--red)">'+esc(e.message)+'</div>';}}
async function loadRelPkgs(app){
  try{
    const d=await api('/api/release/packages'+(app?'?app='+encodeURIComponent(app):''));
    const pkgs=asArr(d,['packages']);
    RELPKG_LIST=pkgs;
    if(!pkgs.length){$('relPkgList').innerHTML='<div class="empty">暂无制品包<br><span style="font-size:11px">点上方「＋ 上传」或把文件拖进来</span></div>';return;}
    $('relPkgList').innerHTML=pkgs.map(p=>'<div class="item" style="cursor:default">'
      +'<div style="word-break:break-all">'+esc(p.name||p.filename||'')+'</div>'
      +'<div style="font-size:11px;color:var(--muted);margin-top:3px">'
      +(p.app?('<span class="tag">'+esc(p.app)+'</span> '):'')
      +(p.version?('<span class="tag">'+esc(p.version)+'</span> '):'')
      +(p.size?fmtSize(p.size):'')
      +(p.md5?(' · '+esc(String(p.md5).slice(0,8))):'')
      +(p.src?('<div style="word-break:break-all;margin-top:2px">来源 '+esc(p.src)+'</div>'):'')
      +'</div></div>').join('');
  }catch(e){$('relPkgList').innerHTML='<div class="empty" style="color:var(--red)">'+esc(e.message)+'</div>';}
}

/* ================= 制品包：拖拽 / 多选 / 整目录 批量上传 ================= */
const UP_SKIP=/^(\.DS_Store|Thumbs\.db|desktop\.ini)$/i;   /* macOS/Windows 的目录垃圾文件 */
let UP_QUEUE=[], UP_ING=false, UP_CANCEL=false, UP_XHRS=[], UP_ZONE_BOUND=false, REL_DROP_BOUND=false;
let RELPKG_LIST=[], RELPKG_CHECKED={}, RELPKG_ONLY_APP=true;
function relPkgName(p){return (p&&(p.name||p.filename))||'';}
function openUploadPkg(){
  UP_QUEUE=[];UP_ING=false;UP_CANCEL=false;UP_XHRS=[];
  $('up_app').value='';$('up_ver').value='';
  $('up_zone').querySelector('.up-big').innerHTML='把 <b>文件</b> 或 <b>文件夹</b> 拖到这里';
  $('up_bar').style.width='0%';
  $('up_list').className='up-list';$('up_list').innerHTML='';
  $('up_go').style.display='';$('up_stop').style.display='none';
  bindUpZone();renderUpQueue();
  $('upModal').classList.add('show');
}
/* 把文件 / 文件夹直接拖到左侧「制品包」列表上也能上传 */
function bindRelPkgDrop(){
  const box=$('relPkgList');if(!box||REL_DROP_BOUND)return;REL_DROP_BOUND=true;
  ['dragenter','dragover'].forEach(ev=>box.addEventListener(ev,e=>{e.preventDefault();box.classList.add('drop-over');}));
  ['dragleave','drop'].forEach(ev=>box.addEventListener(ev,e=>{e.preventDefault();box.classList.remove('drop-over');}));
  box.addEventListener('drop',e=>{
    const dt=e.dataTransfer;if(!dt)return;
    const items=(dt.items&&dt.items.length)?dt.items.length:0;
    if(!items&&(!dt.files||!dt.files.length))return;
    openUploadPkg();
    ingestDrop(dt);
  });
}
function bindUpZone(){
  const zone=$('up_zone');
  $('up_files').onchange=function(){addUpFiles(this.files);this.value='';};
  $('up_dir').onchange=function(){addUpFiles(this.files);this.value='';};
  zone.onclick=function(e){if(e.target.tagName==='BUTTON')return;$('up_files').click();};
  if(UP_ZONE_BOUND)return;UP_ZONE_BOUND=true;
  ['dragenter','dragover'].forEach(ev=>zone.addEventListener(ev,e=>{e.preventDefault();e.stopPropagation();zone.classList.add('over');}));
  ['dragleave','dragend'].forEach(ev=>zone.addEventListener(ev,e=>{e.preventDefault();e.stopPropagation();zone.classList.remove('over');}));
  zone.addEventListener('drop',e=>{e.preventDefault();e.stopPropagation();zone.classList.remove('over');ingestDrop(e.dataTransfer);});
}
/* 统一入口：既支持拖文件，也支持拖文件夹（webkitGetAsEntry 递归展开）。
   注意：必须在 await 之前把 items 全部取出来，事件里的 dataTransfer 随后即失效。 */
function ingestDrop(dt){
  if(!dt)return Promise.resolve();
  const entries=[];
  if(dt.items&&dt.items.length&&dt.items[0].webkitGetAsEntry){
    for(let i=0;i<dt.items.length;i++){const it=dt.items[i];if(it.kind!=='file')continue;const en=it.webkitGetAsEntry();if(en)entries.push(en);}
  }
  const files=dt.files;
  if(entries.length){
    const big=$('up_zone').querySelector('.up-big'),old=big.innerHTML;
    big.textContent='正在读取文件夹…';
    return Promise.all(entries.map(en=>readDropEntry(en,'',[]))).then(arrs=>{
      let out=[];arrs.forEach(a=>{out=out.concat(a)});
      addUpFiles(out,true);big.innerHTML=old;
    },()=>{big.innerHTML=old;});
  }
  if(files&&files.length)addUpFiles(files);
  return Promise.resolve();
}
function readDropEntry(entry,prefix,out){
  return new Promise(res=>{
    if(entry.isFile){entry.file(f=>{out.push({file:f,src:prefix+f.name});res();},()=>res());}
    else if(entry.isDirectory){
      const reader=entry.createReader();let kids=[];
      const pump=()=>{reader.readEntries(ents=>{
        if(!ents.length){                                  /* 每次最多返回 100 项，需循环读完 */
          const sub=entry.name+'/';
          Promise.all(kids.map(k=>readDropEntry(k,prefix+sub,out))).then(()=>res(),()=>res());
          return;
        }
        kids=kids.concat(Array.prototype.slice.call(ents));pump();
      },()=>res());};
      pump();
    }else res();
  });
}
function addUpFiles(list,isObj){
  if(!list||!list.length){renderUpQueue();return;}
  let skipped=0;
  Array.prototype.forEach.call(list,it=>{
    const f=isObj?it.file:it;
    if(!f||!f.name)return;
    const src=(isObj?it.src:(f.webkitRelativePath||f.name))||f.name;
    const base=src.split('/').pop();
    if(UP_SKIP.test(base)){skipped++;return;}
    const key=src+'|'+f.size;
    if(UP_QUEUE.some(q=>q.key===key)){skipped++;return;}
    UP_QUEUE.push({key:key,file:f,name:base,src:src,size:f.size,state:'wait',pct:0,msg:'等待'});
  });
  renderUpQueue();
  if(skipped)toast('已忽略 '+skipped+' 项（重复或系统文件）','warn');
}
function renderUpQueue(){
  const box=$('up_list'),sum=$('up_sum');
  box.innerHTML=UP_QUEUE.map((q,i)=>'<div class="row-i '+upRowCls(q)+'" id="upq_'+i+'" title="'+esc(q.src)+'">'
    +'<span class="nm">'+esc(q.src)+'</span><span class="st" id="upqst_'+i+'">'+esc(upRowText(q))+'</span></div>').join('');
  box.className=UP_QUEUE.length?'up-list on':'up-list';
  if(!UP_QUEUE.length){sum.textContent='尚未选择文件';$('up_bar').style.width='0%';return;}
  const done=UP_QUEUE.filter(q=>q.state==='ok'||q.state==='fail'||q.state==='dup').length;
  const upN=UP_QUEUE.filter(q=>q.state==='up').length;
  const okN=UP_QUEUE.filter(q=>q.state==='ok').length,dupN=UP_QUEUE.filter(q=>q.state==='dup').length;
  const failN=UP_QUEUE.filter(q=>q.state==='fail').length;
  const tot=UP_QUEUE.reduce((a,q)=>a+(q.size||0),0);
  sum.textContent='共 '+UP_QUEUE.length+' 个文件（'+fmtSize(tot)+'） · 完成 '+done+'/'+UP_QUEUE.length
    +(upN?(' · 上传中 '+upN):'')+(okN?(' · 新增 '+okN):'')+(dupN?(' · 重复 '+dupN):'')+(failN?(' · 失败 '+failN):'');
  let prog=0;UP_QUEUE.forEach(q=>{
    if(q.state==='ok'||q.state==='fail'||q.state==='dup')prog++;
    else if(q.state==='up')prog+=0.5+(q.pct?q.pct/200:0);            /* 进行中的算半格 + 半格按进度 */
  });
  $('up_bar').style.width=Math.min(100,Math.round(prog/UP_QUEUE.length*100))+'%';
}
function upRowCls(q){return q.state==='ok'?'ok':(q.state==='fail'?'fail':(q.state==='dup'?'dup':''));}
function upRowText(q){
  if(q.state==='wait')return q.msg||'等待';
  if(q.state==='up')return '上传中 '+(q.pct||0)+'%';
  if(q.state==='ok')return '完成';
  if(q.state==='dup')return '已存在（MD5 相同）';
  return q.msg||'失败';
}
function paintUpRow(q){
  const i=UP_QUEUE.indexOf(q);if(i<0)return;
  const row=$('upq_'+i),st=$('upqst_'+i);
  if(row)row.className='row-i '+upRowCls(q);
  if(st)st.textContent=upRowText(q);
}
async function startPkgUpload(){
  if(UP_ING)return;
  if(!UP_QUEUE.some(q=>q.state==='wait')){toast('队列里没有待上传的文件','warn');return;}
  const app=($('up_app').value||'').trim(),ver=($('up_ver').value||'').trim();
  const conc=Math.max(1,Math.min(4,parseInt($('up_conc').value,10)||2));
  const skip=$('up_skipdup').checked;
  UP_ING=true;UP_CANCEL=false;UP_XHRS=[];
  $('up_go').style.display='none';$('up_stop').style.display='';
  let idx=0;
  async function worker(){
    while(!UP_CANCEL){
      const q=UP_QUEUE[idx++];
      if(!q)return;
      if(q.state!=='wait')continue;
      q.state='up';q.pct=0;paintUpRow(q);renderUpQueue();
      try{
        const r=await xhrUploadPkg(q,app,ver,skip);
        if(r&&r.duplicate){q.state='dup';q.msg='已存在（MD5 相同）';}
        else{q.state='ok';q.msg='完成';}
      }catch(e){
        if(UP_CANCEL&&e.message==='已取消'){q.state='wait';q.pct=0;q.msg='已取消';}
        else{q.state='fail';q.msg=(e.message||'失败');}
      }
      paintUpRow(q);renderUpQueue();
    }
  }
  const workers=[];for(let i=0;i<conc;i++)workers.push(worker());
  await Promise.all(workers);
  UP_ING=false;
  $('up_stop').style.display='none';$('up_go').style.display='';
  const okN=UP_QUEUE.filter(q=>q.state==='ok').length;
  const dupN=UP_QUEUE.filter(q=>q.state==='dup').length;
  const failN=UP_QUEUE.filter(q=>q.state==='fail').length;
  toast('上传结束：新增 '+okN+' 个'+(dupN?('，已存在 '+dupN+' 个'):'')+(failN?('，失败 '+failN+' 个'):'')+'',failN?'warn':'ok');
  loadRelPkgs();
}
function cancelPkgUpload(){
  UP_CANCEL=true;
  UP_XHRS.forEach(x=>{try{x.abort()}catch(_){}});
  UP_XHRS=[];
  toast('已停止上传','warn');
}
/* 单个文件一个请求（与发布中心 /api/packages/upload 的语义一致），
   用 XHR 而不是 fetch，只为了拿到 upload.onprogress 做逐文件进度。 */
function xhrUploadPkg(item,app,ver,skip){
  return new Promise((res,rej)=>{
    const qs='/api/release/packages/upload?filename='+encodeURIComponent(item.name)
      +'&app='+encodeURIComponent(app||'')+'&version='+encodeURIComponent(ver||'')
      +'&src='+encodeURIComponent(item.src||item.name)
      +(skip?'&skip_dup=1':'');
    const x=new XMLHttpRequest();UP_XHRS.push(x);
    x.open('POST',qs,true);
    if(token)x.setRequestHeader('X-Auth-Token',token);
    x.upload.onprogress=e=>{if(e.lengthComputable){item.pct=Math.round(e.loaded/e.total*100);paintUpRow(item);}};
    x.onload=()=>{
      let d={};try{d=JSON.parse(x.responseText||'{}')}catch(_){d={}}
      if(x.status>=200&&x.status<300&&(d.status==='success'||d.status==='ok'||d.status===undefined)){res(d);return;}
      if(x.status===401&&String(d.code||'').indexOf('upstream')!==0){showLogin();rej(new Error('平台会话已失效，请重新登录'));return;}
      rej(new Error(d.message||('HTTP '+x.status)));
    };
    x.onerror=()=>rej(new Error('网络中断'));
    x.onabort=()=>rej(new Error('已取消'));
    x.send(item.file);
  });
}
async function loadRelOrders(){try{const d=await api('/api/release/orders');const orders=asArr(d,['orders']);$('relOrderList').innerHTML=orders.length?orders.map(o=>'<div class="item" data-id="'+esc(o.id)+'" onclick="openOrder(\''+escJs(o.id)+'\')">'+esc(o.id)+' <span class="badge '+(o.state==='success'?'on':(o.state==='failed'?'off':'warn'))+'">'+esc(o.state||'')+'</span><div style="font-size:11px;color:var(--muted)">'+esc(o.app||'')+' '+esc(o.version||'')+'</div></div>').join(''):'<div class="empty">暂无发布单</div>';}catch(e){$('relOrderList').innerHTML='<div class="empty" style="color:var(--red)">'+esc(e.message)+'</div>';}}
/* ---------------- 发布单：实时进度 + 脚本输出控制台 ----------------
   两件事：
     ① 执行到哪一步  —— 步骤状态/耗时/进度条/当前步骤高亮
     ② 脚本返回信息  —— 按步骤实时追加 stdout/stderr（执行过的可回看）

   数据来源：/api/release/orders/<id>/stream（SSE 透传到 release_center）
     snapshot(整单快照) / start / step(步骤状态) / log(输出增量) / done
   注意：平台侧 SSE 会把匿名事件包成 `event: release`，只写 onmessage 收不到，
   必须 addEventListener('release', ...)（页面「不刷新」的原因之一）。
   另一份等价实现在独立发布中心页 templates/release_center.html，改动请同步。
------------------------------------------------------------------ */
const ORD_STATE_ZH={pending:'待执行',running:'执行中',success:'成功',failed:'失败',
                    rolled_back:'已回滚',canceled:'已取消'};
const STEP_STATE_ZH={pending:'待执行',running:'执行中',success:'成功',failed:'失败',
                     canceled:'已取消',skipped:'已跳过'};
const ORD_CONSOLE_LIMIT=200000;
let openOrderId=null,orderES=null,orderModel=null,orderFollow=null,orderFollowOn=true;
let orderRenderedKey=null,orderReconnectTimer=null;

function ordStateZh(s){return ORD_STATE_ZH[s]||s||'';}
function stepStateZh(s){return STEP_STATE_ZH[s]||s||'待执行';}
function ordStateCls(s){return s==='success'?'on':(s==='failed'?'off':(s==='running'?'warn':'type'));}
function ordKey(server,name){return (server||'')+'|'+(name||'');}

async function openOrder(id){
  openOrderId=id;orderModel=null;orderFollow=null;orderRenderedKey=null;orderFollowOn=true;
  document.querySelectorAll('#relOrderList .item').forEach(x=>x.classList.toggle('active',x.dataset.id===id));
  openOrderStream(id);                      // 先订阅：避免错过执行中的事件
  try{const d=await api('/api/release/orders/'+encodeURIComponent(id));
      orderSetSnapshot(d.order||d);}
  catch(e){$('relOrderDetail').innerHTML='<div class="empty" style="color:var(--red)">'+esc(e.message)+'</div>';}
}

function orderSetSnapshot(o){
  if(!o)return;
  orderModel={order:o,stepLogs:{},percent:null,cleared:null};
  (o.server_steps||[]).forEach(function(run){(run.steps||[]).forEach(function(s){
    orderModel.stepLogs[ordKey(run.server,s.name)]=(s.logs||[]).join('\n');});});
  orderAutoFollow();
  paintOrder();
}

function orderAutoFollow(){
  if(!orderModel||!orderFollowOn)return;
  let running=null,last=null;
  (orderModel.order.server_steps||[]).forEach(function(r){(r.steps||[]).forEach(function(s){
    const k=ordKey(r.server,s.name);
    if(s.logs&&s.logs.length)last=k;
    if(s.state==='running')running=k;});});
  orderFollow=running||last||orderFollow;
}

function orderModelLog(key,text){
  if(!orderModel||!text)return;
  const map=orderModel.stepLogs||(orderModel.stepLogs={});
  map[key]=(map[key]||'')+text;
}

function orderProgress(){
  const o=orderModel&&orderModel.order;
  if(!o)return{pct:0,done:0,total:0,running:null};
  let done=0,total=0,running=null;
  (o.server_steps||[]).forEach(function(r){(r.steps||[]).forEach(function(s){
    total++;if(s.state==='success'||s.state==='failed')done++;
    if(s.state==='running')running={server:r.server,step:s.name,label:s.label||s.name,index:1,total:total};});});
  (o.server_steps||[]).forEach(function(r){(r.steps||[]).forEach(function(s,i){
    if(s.state==='running')running={server:r.server,step:s.name,label:s.label||s.name,
                                     index:i+1,total:(r.steps||[]).length};});});
  const fin=['success','failed','rolled_back','canceled'].indexOf(o.state||'')>=0;
  let pct=orderModel&&orderModel.percent!=null?orderModel.percent:(total?Math.round(done*100/total):0);
  if(fin)pct=100;
  return{pct:pct,done:done,total:total,running:running};
}

function orderProgressText(){
  const o=orderModel&&orderModel.order;if(!o)return'';
  const p=orderProgress();
  if((o.state||'')==='running'&&p.running)return '正在执行：'+p.running.label+'（'+p.running.server+' · 第 '+p.running.index+'/'+p.running.total+' 步）· '+p.pct+'%';
  if((o.state||'')==='running')return '正在执行 · '+p.pct+'%';
  if(['success','failed','rolled_back','canceled'].indexOf(o.state||'')>=0){
    const r=o.result||{};
    return '执行结束：成功 '+((r.succeeded||[]).length)+' 台 / 失败 '+((r.failed||[]).length)+' 台';
  }
  return '待执行：共 '+p.total+' 步';
}

function orderStepHtml(s,key,active){
  const cs=s.state||'';
  const cls=cs==='success'?'success':(cs==='running'?'running':(cs==='failed'?'failed':
            (cs==='rolled_back'?'rolled_back':'')));
  return '<div class="step '+cls+(active?' active':'')+'" data-step-key="'+esc(encodeURIComponent(key))+'">'
    +'<div><b>'+esc(s.label||s.name||'')+'</b> <span class="badge '+ordStateCls(cs)+'">'
    +esc(stepStateZh(cs))+'</span>'
    +(s.elapsed?('<span class="step-el">'+s.elapsed+'s</span>'):'')
    +'<span class="step-msg">'+esc(s.message||'')+'</span></div></div>';
}

function orderConsoleHtml(){
  return '<div class="ord-console" id="ordConsole">'
    +'<div class="panel-h">脚本输出<span class="spacer"></span>'
    +'<span class="muted" id="ordConsoleWhere"></span>'
    +'<button class="btn btn-sm" onclick="orderToggleFollow()" id="ordFollowBtn"></button>'
    +'<button class="btn btn-sm" onclick="orderClearConsole()">清空显示</button></div>'
    +'<div class="ord-console-body" id="ordConsoleBody"></div></div>';
}

function orderStepLabel(key){
  const o=orderModel&&orderModel.order;if(!o||!key)return key||'';
  let out=key.split('|')[1]||'';
  (o.server_steps||[]).forEach(function(r){(r.steps||[]).forEach(function(s){
    if(ordKey(r.server,s.name)===key)out=s.label||s.name||out;});});
  return out;
}

function orderUpdateConsoleHead(){
  const where=$('ordConsoleWhere'),btn=$('ordFollowBtn');
  if(where)where.textContent=orderFollow?((orderFollow.split('|')[0]||'')+' · '+orderStepLabel(orderFollow)):'（未选中步骤）';
  if(btn)btn.textContent=orderFollowOn?'跟随当前步骤：开':'跟随当前步骤：关';
}

/* 把模型里的日志整块画进控制台（切换查看对象时用；增量输出走 orderAppendConsole） */
function orderRenderConsole(){
  const box=$('ordConsoleBody');
  orderRenderedKey=orderFollow;
  if(!box)return;
  const text=orderFollow?(((orderModel||{}).stepLogs||{})[orderFollow]||''):'';
  if(!orderFollow){box.innerHTML='<div class="empty">点上方任意步骤可回看它的脚本输出；执行中会自动跟随当前步骤</div>';return;}
  if(!text||(orderModel&&orderModel.cleared===orderFollow)){
    box.innerHTML='<div class="empty">'+(text?'（已清空，后续输出继续显示）':'该步骤暂无输出')+'</div>';return;}
  box.textContent=text;
  orderScrollConsole(true);
}

function orderAppendConsole(text){
  const box=$('ordConsoleBody');
  if(!box||!text)return;
  const atBottom=orderConsoleAtBottom(box);
  if(box.querySelector('.empty'))box.textContent='';
  box.textContent+=text;
  if(box.textContent.length>ORD_CONSOLE_LIMIT*1.5){
    box.textContent='（已截断较早输出）\n'+box.textContent.slice(-ORD_CONSOLE_LIMIT);
  }
  if(atBottom)orderScrollConsole(true);
}

function orderConsoleAtBottom(box){
  try{return (box.scrollHeight-box.scrollTop-box.clientHeight)<24;}catch(e){return true;}
}
function orderScrollConsole(force){
  const box=$('ordConsoleBody');
  if(!box)return;
  if(force||orderConsoleAtBottom(box)){try{box.scrollTop=box.scrollHeight;}catch(e){}}
}

function orderToggleFollow(){
  orderFollowOn=!orderFollowOn;
  if(orderFollowOn)orderAutoFollow();
  orderUpdateConsoleHead();
}

function orderFollowStep(keyEnc){
  orderFollowOn=false;
  try{orderFollow=decodeURIComponent(keyEnc);}catch(e){orderFollow=keyEnc;}
  orderRenderConsole();
  orderUpdateConsoleHead();
}

function orderClearConsole(){
  const box=$('ordConsoleBody');
  if(box)box.innerHTML='<div class="empty">（已清空，后续输出继续显示）</div>';
  if(orderModel)orderModel.cleared=orderFollow;
}

function paintOrder(){
  const o=orderModel&&orderModel.order;if(!o)return;
  const el=$('relOrderDetail');if(!el)return;
  const oldConsole=$('ordConsole');
  const p=orderProgress(),st=o.state||'';
  let h='<div style="margin-bottom:8px"><b>'+esc(o.id)+'</b> '
    +'<span class="badge '+ordStateCls(st)+'">'+esc(ordStateZh(st))+'</span></div>';
  h+='<div class="sc-meta">应用 '+esc(o.app||'')+' · 版本 '+esc(o.version||'')+' · 策略 '
    +((o.strategy==='parallel')?'并行':'串行')+'</div>';
  const pk=(o.packages&&o.packages.length)?o.packages:(o.package?[o.package]:[]);
  if(pk.length)h+='<div class="sc-meta">制品包 '+pk.map((x,i)=>'<span class="tag">'
    +((pk.length>1)?('#'+(i+1)+' '):'')+esc(x)+'</span>').join('')+'</div>';
  if(o.servers&&o.servers.length)h+='<div class="sc-meta">目标: '+o.servers.map(s=>'<span class="tag">'+esc(s)+'</span>').join('')+'</div>';
  h+='<div class="ord-progress"><div style="width:'+p.pct+'%"></div></div>';
  h+='<div class="sc-meta" id="ordProgText">'+esc(orderProgressText())+'</div>';
  (o.server_steps||[]).forEach(function(run){
    h+='<div class="ord-srv"><div class="ord-srv-h"><b>'+esc(run.server||'')+'</b> '
      +'<span class="badge '+ordStateCls(run.state)+'">'+esc(ordStateZh(run.state))+'</span></div>'
      +'<div class="pipeline">';
    (run.steps||[]).forEach(function(s){const key=ordKey(run.server,s.name);
      h+=orderStepHtml(s,key,orderFollow===key);});
    h+='</div></div>';
  });
  if(!(o.server_steps||[]).length)h+='<div class="empty">尚未执行 —— 点「执行发布」后，这里会实时显示每一步与脚本输出</div>';
  h+='<div id="ordConsoleWrap"></div>';
  const canRun=can('release.run')&&['pending','failed','success'].indexOf(st)>=0;
  const canRoll=can('release.run')&&['success','rolled_back','failed'].indexOf(st)>=0;
  h+='<div style="margin-top:10px">';
  if(canRun)h+='<button class="btn btn-sm btn-primary" onclick="runOrder()">▶ 执行发布</button> ';
  if(canRoll)h+='<button class="btn btn-sm btn-danger" onclick="rollbackOrder()">↺ 回滚</button>';
  h+='</div>';
  el.innerHTML=h;
  const wrap=$('ordConsoleWrap');
  if(wrap){
    if(oldConsole)wrap.appendChild(oldConsole);   // 复用原节点：保留已显示输出与滚动位置
    else wrap.innerHTML=orderConsoleHtml();
  }
  orderUpdateConsoleHead();
  if(orderFollow!==orderRenderedKey)orderRenderConsole();
}

/* SSE 事件 → 模型 → 重画。日志走「只追加控制台」的快路径，避免每行都整体重画 */
function applyOrderEvent(ev){
  if(!ev||(ev.order_id&&ev.order_id!==openOrderId))return;
  const t=ev.type;
  if(t==='snapshot'){orderSetSnapshot(ev.order);return;}
  if(t==='log'){
    const key=ordKey(ev.server,ev.step);
    orderModelLog(key,ev.text||'');
    if(orderFollowOn&&orderFollow!==key){orderFollow=key;orderRenderConsole();orderUpdateConsoleHead();}
    else if(orderFollow===key)orderAppendConsole(ev.text||'');
    return;
  }
  if(t==='start'||t==='rollback_start'){
    if(orderModel){orderModel.order.state='running';orderModel.percent=0;}
    paintOrder();return;
  }
  if(t==='done'){
    if(orderModel){orderModel.order.state=ev.state||orderModel.order.state;orderModel.percent=100;}
    paintOrder();
    if(orderES){try{orderES.close();}catch(e){}orderES=null;}
    loadRelOrders();
    return;
  }
  if(t==='step'){
    const o=orderModel&&orderModel.order;
    if(!o)return;
    (o.server_steps||[]).forEach(function(run){
      if(run.server!==ev.server)return;
      (run.steps||[]).forEach(function(s){
        if(s.name!==ev.step)return;
        s.state=ev.state||s.state;
        if(ev.message)s.message=ev.message;
        if(ev.elapsed)s.elapsed=ev.elapsed;
        if(ev.label)s.label=ev.label;
      });
    });
    if(typeof ev.percent==='number')orderModel.percent=ev.percent;
    if(ev.state==='running'&&orderFollowOn)orderFollow=ordKey(ev.server,ev.step);
    paintOrder();
    return;
  }
  paintOrder();
}

/* 平台统一事件流（/api/events 里的 release 主题回推）也走同一条更新路径 */
function updateOrderStep(data){
  if(!data||data.order_id!==openOrderId)return;
  if(data.type){applyOrderEvent(data);return;}
  if(orderModel){
    if(typeof data.percent==='number')orderModel.percent=data.percent;
    if(data.state)orderModel.order.state=data.state;
  }
  paintOrder();
  if(data.state&&['success','failed','rolled_back','canceled'].indexOf(data.state)>=0)loadRelOrders();
}

async function runOrder(){if(!openOrderId)return;try{await api('/api/release/orders/'+encodeURIComponent(openOrderId)+'/run',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});toast('已触发发布');orderFollowOn=true;openOrderStream(openOrderId);}catch(e){toast(e.message,'err')}}
function rollbackOrder(){if(!openOrderId)return;confirmDialog('回滚发布','确定回滚该发布单吗？将恢复到上一备份版本。',async()=>{try{await api('/api/release/orders/'+encodeURIComponent(openOrderId)+'/rollback',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});toast('已触发回滚');orderFollowOn=true;openOrderStream(openOrderId);}catch(e){toast(e.message,'err')}});}

function openOrderStream(id){
  if(orderES){try{orderES.close();}catch(e){}orderES=null;}
  let es=null;
  try{es=new EventSource('/api/release/orders/'+encodeURIComponent(id)+'/stream?token='+encodeURIComponent(token));}catch(e){return;}
  orderES=es;
  const ondata=e=>{
    let m=null;
    try{m=JSON.parse(e.data);}catch(_){return;}
    /* 兼容三种包装：裸事件 / {data:事件} / {topic,data:事件} */
    applyOrderEvent(m&&m.type?m:((m&&m.data&&m.data.type)?m.data:(m&&m.data)||{}));
  };
  es.addEventListener('release',ondata);   // 平台 SSE 透传把匿名事件命名成 release
  es.onmessage=ondata;                     // 直连（不经平台）时走默认 message
  es.addEventListener('close',()=>{try{es.close();}catch(_){}});
  es.onerror=()=>{
    if(orderReconnectTimer)return;
    orderReconnectTimer=setTimeout(async function(){
      orderReconnectTimer=null;
      if(openOrderId!==id)return;
      try{const d=await api('/api/release/orders/'+encodeURIComponent(id));
          if(openOrderId===id&&d&&(d.order||d.id))orderSetSnapshot(d.order||d);}catch(_){}
    },1500);
  };
}

document.addEventListener('click',function(e){
  const t=e.target;
  const node=t&&t.closest?t.closest('[data-step-key]'):null;
  if(node&&node.dataset&&node.dataset.stepKey)orderFollowStep(node.dataset.stepKey);
});
async function openCreateOrder(){
  const d=await loadRelAppsSilent();const apps=asArr(d,['apps','applications']);REL_ORDER_APPS=apps;
  $('o_app').innerHTML=apps.map(a=>'<option value="'+esc(a.name)+'">'+esc(a.name)+'</option>').join('')||'<option value="">无应用</option>';
  $('o_servers').innerHTML=servers.filter(hasUpload).map(s=>'<label><input type="checkbox" value="'+esc(s.name)+'"> '+esc(s.name)+'</label>').join('')||'<span style="color:var(--dim)">无服务器</span>';
  RELPKG_CHECKED={};
  RELPKG_ONLY_APP=!$('o_pkg_only')||$('o_pkg_only').checked;
  $('o_pkg_q').value='';
  $('orderModal').classList.add('show');
  $('o_app').onchange=onOrderAppChange;
  renderOrderParams();
  /* 制品包一次取全量，「只看所选应用」在前端过滤（勾掉后仍能看到其他包） */
  try{const dp=await api('/api/release/packages');RELPKG_LIST=asArr(dp,['packages']);}
  catch(e){RELPKG_LIST=[];}
  onOrderAppChange();
}
/* 切换应用：刷新参数 + 自动勾上「该应用自己的包」（用户已勾选过则不覆盖） */
function onOrderAppChange(){
  renderOrderParams();
  if(RELPKG_ONLY_APP){
    const sel=$('o_app').value;
    const mine=RELPKG_LIST.filter(p=>(p.app||'')===sel);
    if(mine.length&&!relPickedPkgs().length)mine.forEach(p=>{RELPKG_CHECKED[relPkgName(p)]=true;});
  }
  renderPkgPicker();
}
function relPickedPkgs(){return RELPKG_LIST.filter(p=>RELPKG_CHECKED[relPkgName(p)]).map(relPkgName);}
function renderPkgPicker(){
  const box=$('o_pkg');if(!box)return;
  const q=($('o_pkg_q').value||'').trim().toLowerCase();
  const app=$('o_app').value;
  let list=RELPKG_LIST;
  if(RELPKG_ONLY_APP&&app)list=list.filter(p=>(p.app||'')===app||!p.app);
  if(q)list=list.filter(p=>((relPkgName(p)+' '+(p.app||'')+' '+(p.version||'')+' '+(p.src||'')).toLowerCase().indexOf(q)>=0));
  box.innerHTML=list.length?list.map(p=>{
    const n=relPkgName(p);
    return '<label><input type="checkbox" value="'+esc(n)+'" '+(RELPKG_CHECKED[n]?'checked':'')
      +' onchange="RELPKG_CHECKED[this.value]=this.checked;updatePkgSum()">'
      +'<span class="pk-nm">'+esc(n)+'</span>'
      +'<span class="pk-tg">'+esc(p.app||'')+(p.version?(' · '+esc(p.version)):'')+(p.size?(' · '+fmtSize(p.size)):'')+'</span></label>';
  }).join(''):'<span style="color:var(--dim);font-size:12px">没有匹配的制品包（可到左侧「制品包」上传）</span>';
  updatePkgSum();
}
function pkgCheckAll(v){
  const box=$('o_pkg');if(!box)return;
  box.querySelectorAll('input[type=checkbox]').forEach(c=>{c.checked=v;RELPKG_CHECKED[c.value]=v;});
  updatePkgSum();
}
function updatePkgSum(){
  const el=$('o_pkg_sum');if(!el)return;
  const sel=relPickedPkgs();
  let tot=0;sel.forEach(n=>{const p=RELPKG_LIST.find(x=>relPkgName(x)===n);if(p)tot+=(p.size||0);});
  el.textContent=sel.length?('已选 '+sel.length+' 个制品包 · 合计 '+fmtSize(tot)+'（${package1..N} 依次对应）'):'尚未选择制品包';
  el.style.color=sel.length?'var(--muted)':'var(--yellow)';
}
let REL_ORDER_APPS=[];
function renderOrderParams(){const box=$('o_params');if(!box)return;const app=REL_ORDER_APPS.find(a=>a.name===$('o_app').value);const defs=(app&&app.param_defs)||[];box.innerHTML=defs.length?defs.map(p=>'<label style="display:block;margin:4px 0 2px">'+esc(p.label||p.key)+'（${'+esc(p.key)+'}）'+(p.required?' <span style="color:var(--red)">*</span>':'')+'</label><input id="o_pm_'+esc(p.key)+'" value="'+esc(p.default||'')+'" placeholder="'+(p.required?'必填':'可留空')+'">').join(''):'<span style="color:var(--dim)">该应用未定义发布参数（可在发布中心的应用编辑里添加）</span>';}
async function loadRelAppsSilent(){try{return await api('/api/release/apps');}catch(e){return {};}}
async function submitOrder(){
  const app=$('o_app').value,version=$('o_version').value.trim(),strategy=$('o_strategy').value;
  const pkgs=relPickedPkgs();
  const svrs=Array.from(document.querySelectorAll('#o_servers input:checked')).map(c=>c.value);
  if(!app||!version){toast('请填写应用与版本','warn');return;}
  if(!pkgs.length){toast('请至少选择一个制品包','warn');return;}
  if(!svrs.length){toast('请选择目标服务器','warn');return;}
  const appDef=REL_ORDER_APPS.find(a=>a.name===app);const params={};const missing=[];
  ((appDef&&appDef.param_defs)||[]).forEach(p=>{const val=($('o_pm_'+p.key)?$('o_pm_'+p.key).value.trim():'');if(p.required&&!val)missing.push(p.label||p.key);params[p.key]=val;});
  if(missing.length){toast('必填参数未填写: '+missing.join(', '),'warn');return;}
  try{
    /* packages 是权威列表，package 保留第一个包 —— 旧版发布中心/旧模板零改动 */
    await api('/api/release/orders',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({app,version,package:pkgs[0],packages:pkgs,servers:svrs,strategy,user:me.username,params})});
    toast('发布单已创建（'+pkgs.length+' 个制品包）','ok');
    $('orderModal').classList.remove('show');
    loadRelOrders();
  }catch(e){toast(e.message,'err')}
}

/* ================= 资产台账 ================= */
async function loadCmdb(){
  const q=[];if($('cmdbKw').value.trim())q.push('kw='+encodeURIComponent($('cmdbKw').value.trim()));if($('cmdbEnv').value)q.push('env='+encodeURIComponent($('cmdbEnv').value));if($('cmdbGroup').value)q.push('group='+encodeURIComponent($('cmdbGroup').value));
  let items=[],total=0;
  try{const d=await api('/api/cmdb'+(q.length?'?'+q.join('&'):''));items=d.items||[];total=d.total||items.length;}catch(e){toast(e.message,'err')}
  renderCmdbTable(items);
  try{const d=await api('/api/cmdb/summary');renderCmdbSummary(d);}catch(e){}
  if(!$('cmdbGroup').options.length>1){}
}
function renderCmdbSummary(d){const parts=[];
  const env=d.by_env||{};parts.push('<div class="sum-card">按环境<b>'+Object.keys(env).length+'</b>'+Object.entries(env).map(([k,v])=>'<div style="font-size:11px">'+esc(k)+' '+v+'</div>').join('')+'</div>');
  const grp=d.by_group||{};parts.push('<div class="sum-card">按分组<b>'+Object.keys(grp).length+'</b>'+Object.entries(grp).slice(0,5).map(([k,v])=>'<div style="font-size:11px">'+esc(k)+' '+v+'</div>').join('')+'</div>');
  const os=d.by_os||{};parts.push('<div class="sum-card">按OS<b>'+Object.keys(os).length+'</b>'+Object.entries(os).slice(0,5).map(([k,v])=>'<div style="font-size:11px">'+esc(k)+' '+v+'</div>').join('')+'</div>');
  parts.push('<div class="sum-card">总台数<b>'+(d.total||0)+'</b></div>');
  $('cmdbSummary').innerHTML=parts.join('');
}
function renderCmdbTable(items){
  let rows=items.map(it=>{
    const a=it.auto||{},sys=a.sysinfo||{},m=a.metrics||{};
    const mn=it.manual||{};
    const editable=can('cmdb.edit');
    function cell(key,val){if(editable)return '<input data-cmdb="'+esc(it.name)+'" data-key="'+key+'" value="'+esc(val||'')+'" onchange="saveCmdbField(this)">';return esc(val||'');}
    return '<tr><td>'+esc(it.name)+'</td><td>'+(esc(it.group)||'-')+'</td><td>'+(it.env?('<span class="badge env-'+esc(it.env)+'">'+esc(it.env)+'</span>'):'-')+'</td>'+
      '<td>'+esc(sys.hostname||'')+'</td><td>'+esc(sys.os_name||'')+'</td><td>'+esc(sys.kernel||'')+'</td><td>'+esc(sys.cpu_model||'')+'</td><td>'+fmtSize(sys.mem_total||0)+'</td>'+
      '<td>'+cell('owner',mn.owner)+'</td><td>'+cell('business',mn.business)+'</td><td>'+cell('level',mn.level)+'</td><td>'+cell('idc',mn.idc)+'</td><td>'+cell('asset_no',mn.asset_no)+'</td><td>'+cell('note',mn.note)+'</td></tr>';
  }).join('');
  $('cmdbTable').innerHTML='<thead><tr><th>名称</th><th>分组</th><th>环境</th><th>主机名</th><th>OS</th><th>内核</th><th>CPU</th><th>内存</th><th>责任人</th><th>业务</th><th>等级</th><th>机房</th><th>资产编号</th><th>备注</th></tr></thead><tbody>'+(rows||'<tr><td colspan="14" class="empty">暂无数据</td></tr>')+'</tbody>';
}
async function saveCmdbField(inp){const name=inp.dataset.cmdb,key=inp.dataset.key,val=inp.value;try{await api('/api/cmdb/'+encodeURIComponent(name),{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({[key]:val})});toast('已保存 '+key);}catch(e){toast(e.message,'err')}}
async function cmdbCollect(){try{await api('/api/cmdb/collect',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({selector:{}})});toast('已提交采集任务，请稍后刷新');setTimeout(loadCmdb,3000);}catch(e){toast(e.message,'err')}}
function exportCmdb(){window.open(authUrl('/api/cmdb/export?format=csv'),'_blank');}

/* ================= 审计 ================= */
let auditPage=0;
async function loadAudit(){
  const q=[];if($('auditUser').value.trim())q.push('user='+encodeURIComponent($('auditUser').value.trim()));if($('auditAction').value.trim())q.push('action='+encodeURIComponent($('auditAction').value.trim()));if($('auditKw').value.trim())q.push('keyword='+encodeURIComponent($('auditKw').value.trim()));if($('auditResult').value)q.push('result='+encodeURIComponent($('auditResult').value));if($('auditStart').value)q.push('start='+Math.floor(new Date($('auditStart').value).getTime()/1000));if($('auditEnd').value)q.push('end='+Math.floor(new Date($('auditEnd').value).getTime()/1000)+86399);q.push('limit=50');q.push('offset='+(auditPage*50));
  let items=[],total=0;
  try{const d=await api('/api/audit?'+q.join('&'));items=d.items||[];total=d.total||0;}catch(e){toast(e.message,'err')}
  let rows=items.map(e=>'<tr><td>'+esc(e.time||'')+'</td><td>'+esc(e.user||'')+'</td><td>'+esc(e.ip||'')+'</td><td>'+esc(e.label||e.action||'')+'</td><td>'+esc(e.target||'')+'</td><td>'+(e.result==='ok'?'<span class="badge on">成功</span>':'<span class="badge off">失败</span>')+'</td><td style="max-width:280px;overflow:hidden;text-overflow:ellipsis">'+esc(e.detail||'')+'</td></tr>').join('');
  $('auditTable').innerHTML='<thead><tr><th>时间</th><th>操作人</th><th>IP</th><th>动作</th><th>对象</th><th>结果</th><th>详情</th></tr></thead><tbody>'+(rows||'<tr><td colspan="7" class="empty">暂无记录</td></tr>')+'</tbody>';
  const pages=Math.max(1,Math.ceil(total/50));
  $('auditPager').innerHTML='<button class="btn btn-xs" '+(auditPage<=0?'disabled':'')+' onclick="auditPage--;loadAudit()">‹ 上一页</button> 第 '+(auditPage+1)+'/'+pages+' 共'+total+' <button class="btn btn-xs" '+(auditPage>=pages-1?'disabled':'')+' onclick="auditPage++;loadAudit()">下一页 ›</button>';
  try{const s=await api('/api/audit/stats?days=7');renderAuditStats(s);}catch(e){}
}
function renderAuditStats(s){const html='<div class="as"><b>'+s.total+'</b>近7天总量</div><div class="as"><b style="color:var(--red)">'+s.failed+'</b>失败数</div><div class="as"><b>'+(s.by_action||[]).length+'</b>动作种类</div>';$('auditStats').innerHTML=html;drawAuditBar(s.by_action||[]);}
function drawAuditBar(byAction){const cv=$('auditBar');if(!cv)return;const ctx=cv.getContext('2d');const w=cv.width=cv.clientWidth||520,h=120;cv.height=120;ctx.clearRect(0,0,w,h);const top=(byAction||[]).slice(0,8);const max=Math.max(1,...top.map(a=>a.count));let y=10;top.forEach(a=>{const bw=(a.count/max)*(w-90);ctx.fillStyle=getCSS('--border2');ctx.fillRect(80,y,w-90-bw,14);ctx.fillStyle=getCSS('--accent');ctx.fillRect(80,y,bw,14);ctx.fillStyle=getCSS('--txt');ctx.font='11px sans-serif';ctx.fillText(esc(a.label),2,y+11);ctx.fillText(String(a.count),80+bw+4,y+11);y+=18;});}
async function verifyAudit(){try{const d=await api('/api/audit/verify');toast(d.ok?('审计链完整（校验 '+d.checked+' 条）'):('校验失败: '+d.message),d.ok?'ok':'err');}catch(e){toast(e.message,'err')}}
function exportAudit(){window.open(authUrl('/api/audit/export.csv'),'_blank');}

/* ================= 任务中心 ================= */
let openTaskId=null,taskES=null;
async function loadTasks(){const st=$('taskState').value;let tasks=[];try{const d=await api('/api/tasks'+(st?'?state='+encodeURIComponent(st):''));tasks=d.tasks||[];}catch(e){toast(e.message,'err')}renderTaskList(tasks);}
function renderTaskList(tasks){const html=tasks.map(t=>taskItemHtml(t)).join('');$('taskList').innerHTML=html||'<div class="empty">暂无任务</div>';}
function taskItemHtml(t){const cls=t.state==='running'?'running':(t.state==='failed'?'fail':'ok');return '<div class="task-item '+(t.id===openTaskId?'active':'')+'" data-id="'+esc(t.id)+'" onclick="openTask(\''+escJs(t.id)+'\')"><div class="ti-h"><b>'+esc(t.name||t.id)+'</b><span class="badge '+(t.state==='success'?'on':(t.state==='failed'?'off':'warn'))+'">'+esc(t.state_label||t.state)+'</span></div><div class="task-bar"><div style="width:'+(t.percent||0)+'%"></div></div><div style="font-size:11px;color:var(--muted)">'+(t.percent||0)+'% · '+esc(t.message||'')+'</div></div>';}
function updateTaskItem(t){const el=document.querySelector('#taskList .task-item[data-id="'+CSS.escape(t.id)+'"]');if(el)el.outerHTML=taskItemHtml(t);else loadTasks();}
$('taskList').addEventListener('click',e=>{const it=e.target.closest('.task-item');if(it&&it.dataset.id)openTask(it.dataset.id);});
async function openTask(id){openTaskId=id;document.querySelectorAll('#taskList .task-item').forEach(x=>x.classList.toggle('active',x.dataset.id===id));try{const d=await api('/api/tasks/'+encodeURIComponent(id));renderTaskDetail(d.task||d);}catch(e){$('taskDetail').innerHTML='<div class="empty" style="color:var(--red)">'+esc(e.message)+'</div>';}connectTaskStream(id);}
function renderTaskDetail(t){if(!t){$('taskDetail').innerHTML='<div class="empty">无详情</div>';return;}let html='<div style="margin-bottom:8px"><b>'+esc(t.name||t.id)+'</b> <span class="badge '+(t.state==='success'?'on':(t.state==='failed'?'off':'warn'))+'">'+esc(t.state_label||t.state)+'</span></div>';html+='<div style="font-size:12px;color:var(--muted)">进度 '+(t.percent||0)+'% · '+esc(t.message||'')+(t.duration!=null?' · 耗时 '+t.duration+'s':'')+' · 用户 '+esc(t.user||'')+'</div>';html+='<div class="task-bar"><div style="width:'+(t.percent||0)+'%"></div></div>';html+='<div id="taskLog">';(t.log||[]).forEach(l=>{html+=logLine(l);});html+='</div>';if(can('task.run')&&['running','pending'].indexOf(t.state)>=0)html+='<button class="btn btn-sm btn-danger" style="margin-top:10px" onclick="cancelTask()">取消任务</button>';$('taskDetail').innerHTML=html;}
function logLine(l){const cls=l.level==='error'?'error':(l.level==='warn'?'warn':'');return '<div class="log-line '+cls+'">'+esc((l.time||'')+' '+l.text)+'</div>';}
function connectTaskStream(id){if(taskES)taskES.close();try{taskES=new EventSource('/api/tasks/'+encodeURIComponent(id)+'/stream?token='+encodeURIComponent(token));taskES.onmessage=e=>{try{const m=JSON.parse(e.data);if(m.data){const t=m.data;if(t.id===openTaskId){const log=t.log||[];let h='';for(const l of log)h+=logLine(l);$('taskLog').innerHTML=h;$('taskDetail').querySelector('.task-bar>div').style.width=(t.percent||0)+'%';}}else loadTasks();}catch(_){}};taskES.onerror=()=>{};}catch(e){}}
async function cancelTask(){if(!openTaskId)return;try{await api('/api/tasks/'+encodeURIComponent(openTaskId)+'/cancel',{method:'POST'});toast('已请求取消');openTask(openTaskId);}catch(e){toast(e.message,'err')}}

/* ================= 设置 ================= */
async function loadSettings(){try{const d=await api('/api/settings');settings=Object.assign(settings,d.settings||{});lockedCfg=d.locked||{};}catch(e){}
  $('set_access_mode').value=settings.access_mode||'auto';
  $('set_upload_concurrency').value=settings.upload_concurrency||6;
  $('set_chunk_mb').value=settings.chunk_mb||0;
  $('set_metrics_interval').value=settings.metrics_interval||10;
  $('set_server_probe_interval').value=settings.server_probe_interval||30;
  $('set_theme').value=settings.theme||'auto';
  $('set_release_url').value=settings.release_url||'';
  $('set_release_token').value=settings.release_token||'';
  $('set_release_user').value=settings.release_user||'';
  $('set_release_pass').value=settings.release_pass||'';
  $('modeSel').value=settings.access_mode||'auto';
  $('concSel').value=String(settings.upload_concurrency||6);
  $('chunkSel').value=String(settings.chunk_mb||0);
  applyTheme();
  try{const n=await api('/api/notify/config');renderNotify(n);}catch(e){}
  refreshRelUrlHint();
}
/* 设置页显示「实际使用中的发布中心地址」（含回环地址自动回退本机网卡的结果）与授权状态 */
async function refreshRelUrlHint(){
  const el=$('relUrlHint');if(!el)return;
  const lockNote=lockedCfg.release_url
    ?'<div style="color:var(--yellow);margin-top:4px">⚠ 启动参数 <code>--release-url</code> 优先级更高，本页保存的地址暂不生效（实际使用：<code>'+esc(settings.release_url||'')+'</code>）</div>':'';
  const lockLogin=lockedCfg.release_login
    ?'<div style="color:var(--muted);margin-top:4px">已配置发布中心令牌（或启动参数 <code>--release-token</code>），下方的账号/口令不会被用到。</div>':'';
  el.innerHTML='<span style="color:var(--muted)">检测中…</span>'+lockNote;
  try{const c=await api('/api/release/config');
    let line;
    if(!c.online)line='<span style="color:var(--yellow)">⚠ 当前离线：</span><code>'+esc(c.url||settings.release_url||'')+'</code><span style="color:var(--muted)">（保存后将按新地址重连）</span>';
    else if(c.authorized)line='<span style="color:var(--green)">✅ 当前可用：</span><code>'+esc(c.url||'')+'</code>'+(c.version?' <span class="tag">v'+esc(c.version)+'</span>':'');
    else line='<span style="color:var(--red)">⚠ 已连通但未获授权(401)：</span><code>'+esc(c.url||'')+'</code>'+
      '<div style="color:var(--muted);margin-top:4px">请填写下方的发布中心账号/口令（管理员，平台会自动登录），或把 release_center 的 <code>--token</code> 值填到「发布中心令牌」。</div>';
    el.innerHTML=line+lockNote+lockLogin;
  }catch(e){el.innerHTML='<span style="color:var(--muted)">无法探测：'+esc(e.message)+'</span>'+lockNote;}
}
async function saveSettings(){const payload={access_mode:$('set_access_mode').value,upload_concurrency:parseInt($('set_upload_concurrency').value)||6,chunk_mb:parseInt($('set_chunk_mb').value)||0,metrics_interval:parseInt($('set_metrics_interval').value)||10,server_probe_interval:parseInt($('set_server_probe_interval').value)||30,theme:$('set_theme').value,release_url:$('set_release_url').value,release_token:$('set_release_token').value,release_user:$('set_release_user').value.trim(),release_pass:$('set_release_pass').value};
  try{const d=await api('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});settings=Object.assign(settings,(d&&d.settings)||payload);lockedCfg=(d&&d.locked)||lockedCfg;applyTheme();toast(lockedCfg.release_url?'设置已保存（发布中心地址被启动参数锁定，暂不生效）':'设置已保存');$('modeSel').value=payload.access_mode;$('concSel').value=String(payload.upload_concurrency);$('chunkSel').value=String(payload.chunk_mb);$('set_release_url').value=settings.release_url||payload.release_url;$('set_release_token').value=settings.release_token||'';$('set_release_user').value=settings.release_user||'';$('set_release_pass').value=settings.release_pass||'';refreshRelUrlHint();if(currentView==='release')loadRelease();}catch(e){toast(e.message,'err')}}
function renderNotify(n){const wrap=$('notifyChannels');if(!n||!n.channels){wrap.innerHTML='<div class="empty">暂无渠道</div>';return;}wrap.innerHTML=(n.channels||[]).map(c=>'<div class="ch"><div><b>'+esc(c.name||c.kind)+'</b> <span class="tag">'+(c.kind||'')+'</span> '+(c.enabled?'<span class="badge on">启用</span>':'<span class="badge off">禁用</span>')+'</div><div style="font-size:11px;color:var(--muted);max-width:200px;overflow:hidden;text-overflow:ellipsis">'+esc(c.url||'')+'</div></div>').join('');}
async function addChannel(){const url=$('newChanUrl').value.trim();if(!url){toast('请输入 webhook 地址','warn');return;}const channels=(await loadNotifyChannels()).slice();channels.push({name:'渠道'+(channels.length+1),kind:guessKind(url),url,enabled:true});await saveNotify({channels});$('newChanUrl').value='';loadSettings();}
async function testNotify(){try{const d=await api('/api/notify/test',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});const out=(d.results||[]).map(r=>esc(r.name||'')+': '+(r.ok?'✅':'❌')+' '+(r.message||'')).join('<br>');$('notifyTest').innerHTML=out||'无渠道';}catch(e){$('notifyTest').innerHTML=esc(e.message);}}
function guessKind(url){if(url.indexOf('qyapi')>=0)return 'wecom';if(url.indexOf('dingtalk')>=0)return 'dingtalk';if(url.indexOf('feishu')>=0)return 'feishu';if(url.indexOf('slack')>=0)return 'slack';return 'raw';}
async function loadNotifyChannels(){try{const n=await api('/api/notify/config');return n.channels||[];}catch(e){return [];}}
async function saveNotify(extra){const base={};if(extra.channels)base.channels=extra.channels;await api('/api/notify/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(base)});}

/* ================= 命令面板 / 快捷键 ================= */
let paletteItems=[],paletteIdx=0;
function openCmdPalette(){$('cmdPalette').classList.add('show');$('cmdInputPalette').value='';paletteItems=[];renderPalette();setTimeout(()=>$('cmdInputPalette').focus(),30);}
function buildPalette(q){q=q.toLowerCase();const items=[];VIEWS.forEach(v=>items.push({ico:'▦',label:({dashboard:'仪表盘',servers:'服务器墙',files:'文件管理',term:'命令终端',release:'发布中心',cmdb:'资产台账',audit:'审计',tasks:'任务中心',settings:'设置'})[v],key:v,act:()=>go(v)}));servers.forEach(s=>{items.push({ico:'🖥',label:'文件: '+s.name,key:s.name,act:()=>openFilesFor(s.name)});items.push({ico:'⌨',label:'终端: '+s.name,key:s.name,act:()=>{selectedTermServers.clear();selectedTermServers.add(s.name);go('term');}});});QUICK_CMDS.forEach(c=>items.push({ico:'⚡',label:'命令: '+c,key:c,act:()=>{$('cmdInput').value=c;if(currentView!=='term')go('term');$('cmdInput').focus();}}));if(q)return items.filter(i=>i.label.toLowerCase().indexOf(q)>=0||i.key.toLowerCase().indexOf(q)>=0);return items;}
function renderPalette(){const q=$('cmdInputPalette').value;paletteItems=buildPalette(q);paletteIdx=0;const box=$('cmdResults');box.innerHTML=paletteItems.map((it,i)=>'<div class="pr '+(i===0?'active':'')+'" data-i="'+i+'"><span><span class="pr-ico">'+it.ico+'</span> '+esc(it.label)+'</span><span class="pr-k">'+esc(it.key)+'</span></div>').join('');box.querySelectorAll('.pr').forEach(el=>el.onclick=()=>runPalette(parseInt(el.dataset.i)));}
function runPalette(i){const it=paletteItems[i];if(it){$('cmdPalette').classList.remove('show');it.act();}}
$('cmdInputPalette').addEventListener('keydown',e=>{if(e.key==='ArrowDown'){paletteIdx=Math.min(paletteItems.length-1,paletteIdx+1);highlightPalette();e.preventDefault();}else if(e.key==='ArrowUp'){paletteIdx=Math.max(0,paletteIdx-1);highlightPalette();e.preventDefault();}else if(e.key==='Enter'){runPalette(paletteIdx);e.preventDefault();}});
function highlightPalette(){document.querySelectorAll('#cmdResults .pr').forEach((el,i)=>el.classList.toggle('active',i===paletteIdx));}
document.addEventListener('keydown',e=>{
  if(e.key==='Escape'){document.querySelectorAll('.modal-mask.show').forEach(m=>m.classList.remove('show'));if(termSSE){termSSE.close();termSSE=null;}}
  if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='k'){e.preventDefault();openCmdPalette();return;}
  const tag=(e.target.tagName||'').toLowerCase();
  if(tag==='input'||tag==='textarea'||tag==='select')return;
  if(e.ctrlKey||e.metaKey||e.altKey)return;
  if(e.key==='g'){const map={d:'dashboard',s:'servers',f:'files',t:'term',r:'release',c:'cmdb',a:'audit',k:'tasks',e:'settings'};const k=map[e.key.toLowerCase()];if(k){go(k);e.preventDefault();}}
});

/* 通用弹层关闭 */
function closeModal(id){$(id).classList.remove('show');}
document.querySelectorAll('.modal-mask').forEach(m=>{if(m.id!=='cmdPalette')m.addEventListener('click',e=>{if(e.target===m)m.classList.remove('show');});});
$('cmdPalette').addEventListener('click',e=>{if(e.target.id==='cmdPalette')$('cmdPalette').classList.remove('show');});

/* ================= 时钟 / 启动 ================= */
function tickClock(){const d=new Date();$('clock').textContent=d.toLocaleDateString('zh-CN')+' '+d.toLocaleTimeString('zh-CN',{hour12:false});}
setInterval(tickClock,1000);tickClock();

async function startApp(){
  await applyMe();
  applyUserUI();applyTheme();initThemeWatch();applyPerms();
  await loadSettings();loadAppSettings();
  const ok=await loadServers();await loadMeta();
  renderFilterBar();
  connectEvents();
  const v=location.hash.slice(1);activateView(VIEWS.indexOf(v)>=0?v:'dashboard');
}
(async function init(){
  if(window.needLogin){token=storeGet('ops_token')||'';if(token){try{await startApp();return;}catch(e){storeDel('ops_token');token='';}}showLogin();}
  else{try{await startApp();}catch(e){showLogin();}}
})();
