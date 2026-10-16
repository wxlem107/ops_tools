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
