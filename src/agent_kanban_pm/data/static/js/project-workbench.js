let wbSelectedSessionId = null;
let wbAllSessions = [];
let wbSessionsLoaded = false;
let wbHashHandled = false;
let wbTerminalData = null;
let wbTerminalRequestSerial = 0;
let wbHandoffRequestSerial = 0;
let wbTerminalRefreshTimer = null;
let wbTerminalMode = 'focused';
try { if (localStorage.getItem('kanban.terminal.mode') === 'raw') wbTerminalMode = 'raw'; } catch(e) {}
let wbWs = null;

function esc(s){ if(!s)return''; const d=document.createElement('div'); d.textContent=String(s); return d.innerHTML; }
function ta(ds){
  if(!ds)return'never';
  const s=Math.floor((new Date()-new Date(ds))/1000);
  if(s<60)return s+'s ago';
  const m=Math.floor(s/60); if(m<60)return m+'m ago';
  const h=Math.floor(m/60); if(h<24)return h+'h ago';
  return Math.floor(h/24)+'d ago';
}
function showToast(msg,type='info'){
  const c=document.getElementById('wb-toast');
  const el=document.createElement('div');
  el.className='wb-toast-item wb-toast-'+type;
  el.textContent=msg;
  c.appendChild(el);
  setTimeout(()=>{el.style.opacity='0';setTimeout(()=>el.remove(),400);},3500);
}
function workbenchFetch(url,opts={}){
  const h={'Content-Type':'application/json',...(opts.headers||{})};
  if(CURRENT_ENTITY_ID)h['X-Entity-ID']=CURRENT_ENTITY_ID;
  return window.apiFetch(url,{...opts,headers:h});
}

/* ── Tab switching ── */
function switchWbTab(tab){
  if(tab==='live'||tab==='terminal'||tab==='handoff')tab='sessions';
  document.querySelectorAll('.wb-tab').forEach(b=>b.classList.toggle('active',b.dataset.tab===tab));
  document.querySelectorAll('.wb-pane').forEach(p=>p.classList.toggle('active',p.id==='wb-pane-'+tab));
  if(tab==='sessions'){
    renderTerminalSessionList();
    if(wbSelectedSessionId&&(!wbTerminalData||wbTerminalData.session.id!==wbSelectedSessionId))loadWbTerminal(wbSelectedSessionId);
  }
  if(tab==='queue')loadLaunchQueue();
}

/* ── LIVE ── */
async function loadLive(){
  try{
    const [heartbeats, sessions]=await Promise.all([
      workbenchFetch('/agents/status'),
      workbenchFetch('/agents/sessions?project_id='+PROJECT_ID+'&limit=50')
    ]);
    renderAgentGrid(heartbeats);
    renderSessionsTable(sessions);
    wbAllSessions=sessions;
    wbSessionsLoaded=true;
    renderTerminalSessionList();
    renderHandoffSessionList();
    if(!wbHashHandled)selectTerminalFromHash();
    document.getElementById('wb-sessions-count').textContent=sessions.filter(s=>!s.ended_at).length;
    const working=(heartbeats||[]).filter(h=>h.status_type&&h.status_type!=='idle'&&h.status_type!=='done').length;
    document.getElementById('wb-working-count').textContent=working;
  }catch(e){console.error('loadLive',e);}
}

function sBadge(status){
  const map={active:'s-active',done:'s-done',error:'s-error',idle:'s-idle',starting:'as-thinking'};
  const cls=map[status]||'s-idle';
  return `<span class="s-badge ${cls}">${esc(status)}</span>`;
}
function asBadge(st){
  const cls='as-'+(st||'idle');
  return `<span class="s-badge ${cls}">${esc(st||'idle')}</span>`;
}

function renderAgentGrid(heartbeats){
  const g=document.getElementById('wb-agent-grid');
  if(!heartbeats||!heartbeats.length){g.innerHTML='<div style="font-size:0.82rem;color:var(--text-muted);">No agents reporting.</div>';return;}
  g.innerHTML=heartbeats.map(a=>`
    <div class="agent-card-wb">
      <div class="agent-card-wb-header">
        <span class="agent-name-wb">${esc(a.agent_name||'Agent '+a.agent_id)}</span>
        ${asBadge(a.status_type)}
      </div>
      ${a.task_id?`<div class="agent-task-chip" onclick="filterToTask(${a.task_id})">&#128203; Task #${a.task_id}</div>`:'<div style="font-size:0.72rem;color:var(--text-muted);">No task assigned</div>'}
      <div class="agent-msg-wb">${esc(a.message||'')}</div>
      <div class="agent-ts-wb">${ta(a.updated_at)}</div>
    </div>`).join('');
}

function renderSessionsTable(sessions){
  const tb=document.getElementById('wb-sessions-body');
  if(!sessions||!sessions.length){tb.innerHTML='<tr><td colspan="6" style="text-align:center;color:var(--text-muted);font-size:0.8rem;padding:1.5rem;">No sessions recorded.</td></tr>';return;}
  tb.innerHTML=sessions.slice(0,30).map(s=>`
    <tr onclick="openSessionFromTable(${s.id})" title="Open session details">
      <td style="font-weight:600;">${esc(s.agent_name||'Agent '+s.agent_id)}</td>
      <td>${s.task_id?`<span style="color:var(--accent-primary);">Task #${s.task_id}</span>`:'-'}</td>
      <td>${sBadge(s.status)}</td>
      <td><code>${esc((s.workspace_path||'').split('/').slice(-3).join('/'))}</code></td>
      <td style="font-size:0.75rem;">${s.started_at?new Date(s.started_at).toLocaleString():'-'}</td>
      <td style="font-size:0.75rem;">${ta(s.last_seen_at)}</td>
    </tr>`).join('');
}

/* ── TERMINAL ── */
function renderTerminalSessionList(){
  const list=document.getElementById('wb-terminal-session-list');
  const filter=document.getElementById('wb-task-filter').value.trim();
  let sessions=wbAllSessions;
  if(filter)sessions=sessions.filter(s=>String(s.task_id||'').includes(filter)||String(s.id).includes(filter));
  if(!sessions.length){list.innerHTML='<div style="padding:1rem;font-size:0.8rem;color:var(--text-muted);">No sessions match.</div>';return;}
  list.innerHTML=sessions.map(s=>`
    <div class="terminal-session-item${wbSelectedSessionId===s.id?' selected':''}" onclick="loadWbTerminal(${s.id})">
      <div class="si-agent">${esc(s.agent_name||'Agent '+s.agent_id)} ${sBadge(s.status)}</div>
      ${s.task_id?`<div class="si-task">Task #${s.task_id}</div>`:''}
      <div class="si-time">${ta(s.last_seen_at)}</div>
    </div>`).join('');
}
function filterTerminalSessions(){renderTerminalSessionList();}
function filterToTask(taskId){
  document.getElementById('wb-task-filter').value=String(taskId);
  switchWbTab('sessions');
  renderTerminalSessionList();
}
function openSessionFromTable(sessionId){switchWbTab('sessions');loadWbTerminal(sessionId);}

function renderWbTerminal(forceBottom=false){
  if(!wbTerminalData)return;
  const pre=document.getElementById('wb-terminal-pre');
  const hint=document.getElementById('wb-terminal-hint');
  const wasAtBottom=pre.scrollHeight-pre.scrollTop-pre.clientHeight<40;
  const oldScrollTop=pre.scrollTop;
  pre.textContent=wbTerminalMode==='raw'
    ?window.KanbanTerminalFeed.raw(wbTerminalData.activities,wbTerminalData.session)
    :window.KanbanTerminalFeed.focused(wbTerminalData.activities,18);
  hint.textContent=wbTerminalMode==='raw'
    ?'Recent captured entries, including unfiltered tmux output. Switch to Focused to reduce repetition.'
    :'Recent useful output and key events. Raw output keeps every captured entry in this feed.';
  pre.scrollTop=forceBottom||wasAtBottom?pre.scrollHeight:oldScrollTop;
}
function setWbTerminalMode(mode){
  if(mode!=='focused'&&mode!=='raw')return;
  wbTerminalMode=mode;
  document.getElementById('wb-terminal-focused').setAttribute('aria-pressed',String(mode==='focused'));
  document.getElementById('wb-terminal-raw').setAttribute('aria-pressed',String(mode==='raw'));
  try{localStorage.setItem('kanban.terminal.mode',mode);}catch(e){}
  renderWbTerminal(true);
}
async function loadWbTerminal(sessionId){
  const changing=wbSelectedSessionId!==sessionId;
  wbSelectedSessionId=sessionId;
  renderTerminalSessionList();
  const pre=document.getElementById('wb-terminal-pre');
  const info=document.getElementById('wb-terminal-info');
  if(changing)pre.textContent='Loading session #'+sessionId+'…';
  loadWbHandoff(sessionId,false,changing);
  const requestSerial=++wbTerminalRequestSerial;
  try{
    const data=await workbenchFetch('/agents/sessions/'+sessionId+'/terminal?limit=200');
    if(requestSerial!==wbTerminalRequestSerial||wbSelectedSessionId!==sessionId)return;
    wbTerminalData=data;
    const s=data.session;
    info.textContent='Agent '+(s.agent_name||s.agent_id)+' · Task #'+(s.task_id||'–')+' · '+s.status+' · '+(s.mode||'');
    renderWbTerminal(changing);
  }catch(e){
    if(requestSerial===wbTerminalRequestSerial)pre.textContent='Failed to load output: '+e.message;
  }
}
function refreshWbTerminal(){if(wbSelectedSessionId)loadWbTerminal(wbSelectedSessionId);}
function scheduleWbTerminalRefresh(){
  if(wbTerminalRefreshTimer)return;
  wbTerminalRefreshTimer=setTimeout(()=>{
    wbTerminalRefreshTimer=null;
    refreshWbTerminal();
  },300);
}
function selectTerminalFromHash(){
  if(!wbSessionsLoaded)return;
  wbHashHandled=true;
  const match=location.hash.match(/^#terminal:task:(\d+)$/);
  if(!match)return;
  const taskId=Number(match[1]);
  document.getElementById('wb-task-filter').value=String(taskId);
  switchWbTab('sessions');
  const session=wbAllSessions.find(s=>Number(s.task_id)===taskId);
  if(session)loadWbTerminal(session.id);
  else{
    document.getElementById('wb-terminal-info').textContent='Task #'+taskId;
    document.getElementById('wb-terminal-pre').textContent='No recent session found for this task.';
  }
}
window.addEventListener('hashchange',()=>{wbHashHandled=false;selectTerminalFromHash();});

/* ── HANDOFF ── */
function renderHandoffSessionList(){
  const list=document.getElementById('wb-handoff-session-list');
  if(!list)return;
  const sessions=wbAllSessions||[];
  if(!sessions.length){list.innerHTML='<div class="handoff-empty">No sessions recorded.</div>';return;}
  list.innerHTML=sessions.map(s=>`
    <div class="terminal-session-item${wbSelectedSessionId===s.id?' selected':''}" onclick="loadWbHandoff(${s.id}, true)">
      <div class="si-agent">${esc(s.agent_name||'Agent '+s.agent_id)} ${sBadge(s.status)}</div>
      ${s.task_id?`<div class="si-task">Task #${s.task_id}</div>`:''}
      <div class="si-time">${esc((s.workspace_path||'').split('/').slice(-3).join('/'))}</div>
    </div>`).join('');
}
function renderHandoff(data){
  const el=document.getElementById('wb-handoff-view');
  if(!el)return;
  const durable=data&&data.durable||{};
  if(!data||(!data.exists&&!durable.summary)){
    el.innerHTML='<div class="handoff-empty">No handoff has been submitted for this session.</div>';
    return;
  }
  const durableArtifacts=Array.isArray(durable.artifacts)&&durable.artifacts.length
    ?`<h5 style="font-size:0.82rem;margin:0.85rem 0 0.4rem;">Verified artifacts</h5><pre class="handoff-pre">${esc(JSON.stringify(durable.artifacts,null,2))}</pre>`:'';
  const durableBlock=durable.summary?`
    <div class="handoff-summary">
      <div class="section-hdr"><h5>Recorded handoff</h5><span class="text-secondary">${esc(durable.state||'done')}</span></div>
      <pre class="handoff-pre">${esc(durable.summary)}</pre>
      ${durableArtifacts}
    </div>`:'';
  if(!data.exists){
    el.innerHTML=durableBlock;
    return;
  }
  if(data.identity_matches_session===false){
    el.innerHTML=durableBlock+'<div class="handoff-empty">This workspace STATUS.md belongs to a different session or run.</div>';
    return;
  }
  const fm=data.frontmatter||{};
  const outputs=Array.isArray(fm.outputs)?fm.outputs.join('\n'):String(fm.outputs||'');
  el.innerHTML=durableBlock+`
    <div class="section-hdr"><h5>STATUS.md</h5><code style="font-size:0.72rem;color:var(--text-muted);">${esc(data.path)}</code></div>
    <dl class="handoff-kv">
      <dt>State</dt><dd>${esc(data.state||'-')}</dd>
      <dt>Ready</dt><dd>${data.handoff_ready?'yes':'no'}</dd>
      <dt>Current agent</dt><dd>${esc(fm.current_agent||'-')}</dd>
      <dt>Assigned role</dt><dd>${esc(fm.assigned_role||'-')}</dd>
      <dt>Task</dt><dd>${esc(fm.task_id||'-')}</dd>
      <dt>Blockers</dt><dd>${esc(fm.blockers||'none')}</dd>
    </dl>
    <h5 style="font-size:0.82rem;margin:0 0 0.4rem;">Summary</h5>
    <pre class="handoff-pre">${esc(fm.summary||'')}</pre>
    <h5 style="font-size:0.82rem;margin:0.85rem 0 0.4rem;">Outputs</h5>
    <pre class="handoff-pre">${esc(outputs)}</pre>
    <h5 style="font-size:0.82rem;margin:0.85rem 0 0.4rem;">Signals To Next</h5>
    <pre class="handoff-pre">${esc(fm.signals_to_next||'')}</pre>
  `;
}
async function loadWbHandoff(sessionId, switchTab, showLoading=true){
  wbSelectedSessionId=sessionId;
  renderTerminalSessionList();
  renderHandoffSessionList();
  if(switchTab)switchWbTab('sessions');
  const el=document.getElementById('wb-handoff-view');
  if(el&&showLoading)el.innerHTML='<div class="handoff-empty">Loading handoff for session #'+sessionId+'…</div>';
  const requestSerial=++wbHandoffRequestSerial;
  try{
    const data=await workbenchFetch('/agents/sessions/'+sessionId+'/handoff');
    if(requestSerial!==wbHandoffRequestSerial||wbSelectedSessionId!==sessionId)return;
    renderHandoff(data);
  }catch(e){
    if(requestSerial!==wbHandoffRequestSerial||wbSelectedSessionId!==sessionId)return;
    if(el)el.innerHTML='<div class="handoff-empty">Failed to load handoff: '+esc(e.message)+'</div>';
  }
}

/* ── APPROVALS ── */
async function loadApprovals(){
  try{
    const [pending,recent]=await Promise.all([
      workbenchFetch('/agents/approvals?project_id='+PROJECT_ID+'&status_filter=pending&limit=50'),
      workbenchFetch('/agents/approvals?project_id='+PROJECT_ID+'&limit=20')
    ]);
    renderPendingApprovals(pending);
    renderResolvedApprovals(recent.filter(a=>a.status!=='pending'));
    const n=pending.length;
    document.getElementById('wb-pending-count').textContent=n;
    const badge=document.getElementById('wb-approvals-badge');
    badge.style.display=n>0?'':'none';
    if(n>0)badge.textContent=n;
  }catch(e){console.error('loadApprovals',e);}
}

function colorDiff(raw){
  if(!raw)return'';
  return raw.split('\n').map(line=>{
    if(line.startsWith('+++')|| line.startsWith('---'))return`<span class="t-time">${esc(line)}</span>`;
    if(line.startsWith('+'))return`<span class="diff-add">${esc(line)}</span>`;
    if(line.startsWith('-'))return`<span class="diff-rm">${esc(line)}</span>`;
    if(line.startsWith('@@'))return`<span class="diff-hunk">${esc(line)}</span>`;
    return esc(line);
  }).join('\n');
}

function approvalCard(a,withControls){
  const typeColors={pending:'#f59e0b',approved:'#10b981',rejected:'#ef4444',expired:'#64748b',cancelled:'#64748b'};
  const color=typeColors[a.status]||'#64748b';
  const diffHtml=a.diff_content?`<details class="approval-diff-wrap"><summary>Show diff (${a.diff_content.split('\n').length} lines)</summary><pre class="approval-diff">${colorDiff(a.diff_content)}</pre></details>`:'';
  const controls=withControls?`<div class="approval-controls"><input class="approval-note-input" id="wb-note-${a.id}" type="text" placeholder="Optional note"/><button class="btn btn-success btn-sm" onclick="resolveWbApproval(${a.id},'approved')">Approve</button><button class="btn btn-danger btn-sm" onclick="resolveWbApproval(${a.id},'rejected')">Reject</button></div>`
    :`<div style="font-size:0.75rem;color:var(--text-muted);margin-top:0.35rem;">${a.response_message?'"'+esc(a.response_message)+'"':''} ${a.resolved_at?ta(a.resolved_at):''}</div>`;
  return`<div class="approval-card" id="wb-approval-${a.id}">
    <div class="approval-card-header">
      <span class="approval-title">${esc(a.title)}</span>
      <div style="display:flex;gap:0.4rem;align-items:center;">
        <span style="font-size:0.68rem;padding:0.15rem 0.45rem;border-radius:1rem;background:rgba(var(--accent-primary-rgb),0.1);color:var(--accent-primary);font-weight:700;">${esc(a.approval_type)}</span>
        <span style="font-size:0.7rem;padding:0.15rem 0.5rem;border-radius:1rem;background:${color}22;color:${color};font-weight:700;">${a.status}</span>
      </div>
    </div>
    <div class="approval-meta">Agent #${a.agent_id}${a.task_id?' · Task #'+a.task_id:''}${a.session_id?' · Session #'+a.session_id:''}</div>
    <div class="approval-msg">${esc(a.message)}</div>
    ${a.command?`<pre class="approval-command">${esc(a.command)}</pre>`:''}
    ${diffHtml}
    ${controls}
  </div>`;
}

function renderPendingApprovals(list){
  const el=document.getElementById('wb-pending-list');
  el.innerHTML=list.length?list.map(a=>approvalCard(a,true)).join(''):'<p style="color:var(--text-muted);font-size:0.82rem;">No pending approvals.</p>';
}
function renderResolvedApprovals(list){
  const el=document.getElementById('wb-resolved-list');
  el.innerHTML=list.length?list.map(a=>approvalCard(a,false)).join(''):'<p style="color:var(--text-muted);font-size:0.82rem;">No resolved approvals yet.</p>';
}
async function resolveWbApproval(id,decision){
  const note=(document.getElementById('wb-note-'+id)||{}).value||'';
  try{
    await workbenchFetch('/agents/approvals/'+id+'/resolve',{method:'PATCH',body:JSON.stringify({decision,response_message:note})});
    showToast('Approval '+decision,'success');
    loadApprovals();
  }catch(e){showToast('Failed: '+e.message,'error');}
}

/* ── DECISIONS ── */
/* LAUNCH QUEUE */
function queueActions(row){
  const actions=[];
  if(['queued','blocked','failed','cancelled'].includes(row.status)&&!row.session_id){
    if(row.status!=='cancelled')actions.push(`<button class="btn btn-outline-danger btn-sm" onclick="cancelLaunchRequest(${row.id})">Cancel</button>`);
    if(['blocked','failed','cancelled'].includes(row.status))actions.push(`<button class="btn btn-primary btn-sm" onclick="retryLaunchRequest(${row.id})">Retry</button>`);
  }
  if(row.session_id)actions.push(`<a class="btn btn-outline-secondary btn-sm" href="#terminal:task:${row.task_id}" onclick="switchWbTab('sessions')">Session #${row.session_id}</a>`);
  return actions.join('')||'<span style="color:var(--text-muted);">-</span>';
}
function renderLaunchQueue(rows){
  const body=document.getElementById('wb-queue-body');
  if(!rows.length){
    body.innerHTML='<tr><td colspan="7" style="text-align:center;color:var(--text-muted);padding:1.5rem;">No launch requests for this project.</td></tr>';
  }else{
    body.innerHTML=rows.map(row=>`<tr id="wb-queue-${row.id}">
      <td><strong>#${row.id}</strong><div style="color:var(--text-muted);font-size:0.7rem;">${ta(row.created_at)}</div></td>
      <td>Task #${row.task_id}<div style="color:var(--text-muted);">${esc(row.role||'worker')} / Agent #${row.agent_id}</div></td>
      <td>${sBadge(row.status)}</td><td>${row.attempts}</td>
      <td>${row.retry_at?ta(row.retry_at):'-'}</td>
      <td class="queue-error">${esc(row.last_error||'')}</td>
      <td><div class="queue-actions">${queueActions(row)}</div></td>
    </tr>`).join('');
  }
  const active=rows.filter(row=>['queued','blocked','reserved','starting','failed'].includes(row.status)).length;
  const badge=document.getElementById('wb-queue-badge');
  badge.style.display=active?'':'none';
  badge.textContent=active;
}
async function loadLaunchQueue(){
  try{
    renderLaunchQueue(await workbenchFetch('/agents/launch-requests?project_id='+PROJECT_ID+'&limit=200'));
  }catch(e){
    document.getElementById('wb-queue-body').innerHTML='<tr><td colspan="7" class="queue-error">Failed to load queue: '+esc(e.message)+'</td></tr>';
  }
}
async function cancelLaunchRequest(id){
  try{
    await workbenchFetch('/agents/launch-requests/'+id+'/cancel',{method:'POST',body:JSON.stringify({reason:'Cancelled from workbench'})});
    showToast('Launch request cancelled','success'); await loadLaunchQueue();
  }catch(e){showToast('Cancel failed: '+e.message,'error');}
}
async function retryLaunchRequest(id){
  try{
    await workbenchFetch('/agents/launch-requests/'+id+'/retry',{method:'POST',body:JSON.stringify({reason:'Retried from workbench'})});
    showToast('Launch request queued','success'); await loadLaunchQueue();
  }catch(e){showToast('Retry failed: '+e.message,'error');}
}
async function cleanupLaunchQueue(){
  try{
    const result=await workbenchFetch('/agents/launch-requests/cleanup?older_than_days=7',{method:'POST',body:'{}'});
    showToast('Archived '+result.archived+' old launch requests','success'); await loadLaunchQueue();
  }catch(e){showToast('Cleanup failed: '+e.message,'error');}
}

/* DECISIONS */
async function loadDecisions(){
  const el=document.getElementById('wb-decisions-list');
  try{
    const decisions=await workbenchFetch('/agents/projects/'+PROJECT_ID+'/decisions?limit=50');
    if(!decisions||!decisions.length){el.innerHTML='<p style="color:var(--text-muted);font-size:0.82rem;">No decisions recorded yet.</p>';return;}
    el.innerHTML=decisions.map(d=>`
      <div class="decision-card">
        <div class="decision-header">
          <span class="decision-type-badge">${esc(d.decision_type)}</span>
          <span class="decision-ts">${d.created_at?new Date(d.created_at).toLocaleString():''}</span>
        </div>
        <div class="decision-summary">${esc(d.input_summary)}</div>
        <div class="decision-rationale">${esc(d.rationale)}</div>
        <div class="decision-chips">
          ${(d.affected_task_ids||'').split(',').filter(Boolean).map(t=>`<span class="decision-chip" style="color:var(--accent-primary);">Task #${esc(t.trim())}</span>`).join('')}
          ${(d.affected_agent_ids||'').split(',').filter(Boolean).map(a=>`<span class="decision-chip" style="color:#f59e0b;">Agent #${esc(a.trim())}</span>`).join('')}
        </div>
      </div>`).join('');
  }catch(e){el.innerHTML='<p style="color:var(--text-muted);font-size:0.82rem;">Failed to load decisions.</p>';}
}

/* ── WebSocket ── */
let wbWsBackoff = 1000;
const WB_WS_MAX_BACKOFF = 30000;
function connectWbWs(){
  const proto=location.protocol==='https:'?'wss:':'ws:';
  wbWs=new WebSocket(proto+'//'+location.host+'/ws/projects/'+PROJECT_ID);
  wbWs.onopen=function(){ wbWsBackoff=1000; };
  wbWs.onmessage=function(ev){
    try{
      const msg=JSON.parse(ev.data);
      const et=msg.event_type||msg.type||'';
      if(et==='agent_status_updated'){loadLive();loadLaunchQueue();}
      if(et==='task_assigned')loadLaunchQueue();
      if(et==='agent_approval_requested'){showToast('&#9888; Approval: '+(msg.data&&msg.data.title||'new request'),'warning');loadApprovals();}
      if(et==='agent_approval_resolved'){showToast('Approval resolved','info');loadApprovals();}
      if(et==='agent_activity_logged'&&wbSelectedSessionId&&msg.data&&msg.data.session_id===wbSelectedSessionId)scheduleWbTerminalRefresh();
    }catch(e){}
  };
  wbWs.onclose=()=>{setTimeout(connectWbWs,wbWsBackoff);wbWsBackoff=Math.min(wbWsBackoff*2,WB_WS_MAX_BACKOFF);};
}

/* ── Init ── */
document.addEventListener('DOMContentLoaded',function(){
  setWbTerminalMode(wbTerminalMode);
  loadLive();
  loadApprovals();
  loadDecisions();
  loadLaunchQueue();
  connectWbWs();
  setInterval(loadLive,5000);
  // Approvals refresh via WebSocket events only (no polling)
});
