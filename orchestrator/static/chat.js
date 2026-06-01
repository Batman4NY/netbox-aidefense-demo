// ─────────────────────────────────────────────────────────────
// Cisco AI Defense demo — single-turn dashboard with live flow diagram
// SSE event stream drives:
//   1. The SVG flow diagram (node states, edge animations, latency badges)
//   2. The event-stream pane (left)
//   3. The result-pane (right) — final answer OR block verdict
// Each new submission RESETS the diagram and panel — no transcript.
// ─────────────────────────────────────────────────────────────

const form       = document.getElementById('chatform');
const input      = document.getElementById('msg');
const sendBtn    = document.getElementById('send');
const sendText   = document.getElementById('send-text');
const idleState  = document.getElementById('idle-state');
const turnState  = document.getElementById('turn-state');
const eventPane  = document.getElementById('event-stream');
const resultPane = document.getElementById('result-pane');
const turnSummary= document.getElementById('turn-summary');

// Sample-prompt chips
document.querySelectorAll('.sample').forEach(btn => {
  btn.addEventListener('click', () => {
    input.value = btn.dataset.msg;
    input.focus();
  });
});

// ─── Diagram helpers ────────────────────────────────────────
const NODES = ['n-user', 'n-input', 'n-nemo', 'n-toolargs', 'n-netbox', 'n-synth', 'n-output', 'n-user-out'];
const EDGES = [
  'e-user-input', 'e-input-nemo', 'e-nemo-toolargs',
  'e-toolargs-netbox', 'e-netbox-down', 'e-synth-output', 'e-output-user'
];

function resetDiagram() {
  NODES.forEach(id => {
    const el = document.getElementById(id);
    if (el) el.className.baseVal = 'node' + (id.includes('input') || id.includes('toolargs') || id === 'n-output' ? ' gate' : '');
  });
  EDGES.forEach(id => {
    const el = document.getElementById(id);
    if (el) el.className.baseVal = 'edge';
  });
  ['lat-input', 'lat-toolargs', 'lat-output', 'netbox-tool'].forEach(id => {
    const el = document.getElementById(id);
    if (el) { el.textContent = ''; el.className.baseVal = el.id.startsWith('lat') ? 'lat-badge' : 'tool-badge'; }
  });
}

function nodeClass(nodeId, ...extra) {
  const el = document.getElementById(nodeId);
  if (!el) return;
  const base = 'node' + (['n-input','n-toolargs','n-output'].includes(nodeId) ? ' gate' : '');
  el.className.baseVal = [base, ...extra].join(' ').trim();
}

function edgeClass(edgeId, ...extra) {
  const el = document.getElementById(edgeId);
  if (!el) return;
  el.className.baseVal = ['edge', ...extra].join(' ').trim();
}

function setLat(id, txt, blocked = false) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = txt;
  el.className.baseVal = 'lat-badge visible' + (blocked ? ' blocked' : '');
}

function setToolBadge(toolName) {
  const el = document.getElementById('netbox-tool');
  if (!el) return;
  el.textContent = toolName;
  el.className.baseVal = 'tool-badge visible';
}

// Map gate name → diagram element ids
const GATE_TO_NODE = { input: 'n-input', tool_args: 'n-toolargs', output: 'n-output' };
const GATE_TO_LAT  = { input: 'lat-input', tool_args: 'lat-toolargs', output: 'lat-output' };
const GATE_TO_INCOMING_EDGE = { input: 'e-user-input', tool_args: 'e-nemo-toolargs', output: 'e-synth-output' };

// ─── Event-stream pane helpers ──────────────────────────────
function pushEvent(kind, icon, body, sub = '') {
  const el = document.createElement('div');
  el.className = `evt ${kind}`;
  const time = new Date().toLocaleTimeString('en-US', { hour12: false }).slice(3);
  el.innerHTML =
    `<div class="evt-time">${time}</div>
     <div class="evt-icon">${icon}</div>
     <div class="evt-body">${body}${sub ? `<div class="evt-sub">${sub}</div>` : ''}</div>`;
  eventPane.appendChild(el);
  eventPane.scrollTop = eventPane.scrollHeight;
}

// ─── Result-pane helpers ────────────────────────────────────
function showUserEcho(text) {
  resultPane.innerHTML = `<div class="user-echo">▸ ${escapeHTML(text)}</div>
    <div class="verdict verdict-thinking">
      <div class="verdict-icon">⚡</div>
      <div>
        <div class="verdict-title">Processing turn…</div>
        <div class="verdict-sub">AI Defense gates firing — watch the diagram</div>
      </div>
    </div>`;
}

function showAllow(markdown, userText) {
  resultPane.innerHTML = `<div class="user-echo">▸ ${escapeHTML(userText)}</div>
    <div class="verdict verdict-allow">
      <div class="verdict-icon">✓</div>
      <div>
        <div class="verdict-title">All three gates passed</div>
        <div class="verdict-sub">Input · Tool args · Output — Cisco AI Defense allowed</div>
      </div>
    </div>
    <div class="markdown">${marked.parse(markdown || '')}</div>`;
}

function showBlock(payload, userText) {
  const where = payload.where || 'unknown';
  const where_human = where === 'input' ? 'prompt before reaching Nemotron'
                    : where === 'tool_args' ? 'tool call before executing against NetBox'
                    : 'response before reaching the user';
  const sevBadge = payload.severity ? `<span class="sev sev-${payload.severity.toLowerCase()}">${payload.severity}</span>` : '';
  const atkBadge = payload.attack_technique ? `<span class="atk">${escapeHTML(payload.attack_technique)}</span>` : '';

  const violations = payload.violations || [];
  const violationHtml = violations.length
    ? `<ul class="vio-list">${violations.map(v => {
        const ents = (v.entity_types && v.entity_types.length)
          ? `<div class="vio-ents">${v.entity_types.map(e => `<span class="vio-ent">${escapeHTML(e)}</span>`).join('')}</div>`
          : '';
        return `<li class="vio-item">
          <span class="vio-name">${escapeHTML(v.rule_name)}</span>
          <span class="vio-class">${escapeHTML(v.classification)}</span>
          ${ents}
        </li>`;
      }).join('')}</ul>`
    : `<div class="text-slate-500 text-xs italic">(no violation details — category: ${escapeHTML(payload.category || 'unknown')})</div>`;

  resultPane.innerHTML = `<div class="user-echo">▸ ${escapeHTML(userText)}</div>
    <div class="verdict verdict-block">
      <div class="verdict-icon">🛑</div>
      <div>
        <div class="verdict-title">Blocked by Cisco AI Defense ${sevBadge}${atkBadge}</div>
        <div class="verdict-sub">${where} gate — ${where_human}</div>
      </div>
    </div>
    <div class="text-[11px] uppercase tracking-widest text-slate-500 mb-1">Rules triggered (${violations.length})</div>
    ${violationHtml}`;
}

function showError(msg, userText) {
  resultPane.innerHTML = `<div class="user-echo">▸ ${escapeHTML(userText)}</div>
    <div class="verdict" style="background:rgba(245,158,11,0.12);border:1px solid #f59e0b;color:#fde68a">
      <div class="verdict-icon">⚠</div>
      <div>
        <div class="verdict-title">Error</div>
        <div class="verdict-sub">${escapeHTML(msg)}</div>
      </div>
    </div>`;
}

// ─── Submit handler ─────────────────────────────────────────
let currentTurnText = '';

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text) return;
  currentTurnText = text;
  input.value = '';
  sendBtn.disabled = true;
  sendText.textContent = 'Sending…';

  // Reset everything for new turn
  resetDiagram();
  eventPane.innerHTML = '';
  idleState.classList.add('hidden');
  turnState.classList.remove('hidden');
  turnSummary.textContent = '';
  showUserEcho(text);

  // Mark user node as active immediately
  nodeClass('n-user', 'active');
  edgeClass('e-user-input', 'active');

  const t0 = performance.now();
  try {
    const resp = await fetch('/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: text, history: [], session_id: null })
    });
    if (!resp.ok || !resp.body) throw new Error(`chat request failed: ${resp.status}`);

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true }).replace(/\r\n/g, '\n');
      let i;
      while ((i = buf.indexOf('\n\n')) !== -1) {
        const chunk = buf.slice(0, i);
        buf = buf.slice(i + 2);
        const evt = parseSSE(chunk);
        if (evt) handleEvent(evt);
      }
    }
  } catch (err) {
    pushEvent('error', '⚠', `fetch failed: ${escapeHTML(err.message)}`);
    showError(err.message, currentTurnText);
  } finally {
    sendBtn.disabled = false;
    sendText.textContent = 'Send';
    const totalMs = Math.round(performance.now() - t0);
    turnSummary.textContent = `total: ${(totalMs/1000).toFixed(1)}s`;
    input.focus();
  }
});

// ─── SSE parser ─────────────────────────────────────────────
function parseSSE(chunk) {
  let event = 'message', data = '';
  for (const ln of chunk.split('\n')) {
    if (ln.startsWith(':')) continue;
    if (ln.startsWith('event:')) event = ln.slice(6).trim();
    else if (ln.startsWith('data:')) data += ln.slice(5).trim();
  }
  if (!data) return null;
  try { return { event, data: JSON.parse(data) }; }
  catch { return { event, data: {} }; }
}

// ─── Event router ───────────────────────────────────────────
function handleEvent({ event, data: d }) {
  switch (event) {

    case 'turn_start':
      pushEvent('llm', '▸', `turn ${d.session_id?.slice(0,8) || '—'}`, `model: ${d.model || ''}`);
      return;

    case 'gate_start': {
      const w = d.where;
      const nodeId = GATE_TO_NODE[w];
      const inEdge = GATE_TO_INCOMING_EDGE[w];
      if (nodeId) nodeClass(nodeId, 'active');
      if (inEdge) edgeClass(inEdge, 'active');
      pushEvent('llm', '🛡', `<b>AI Defense</b> · ${w} scan started`);
      return;
    }

    case 'gate_result': {
      const w = d.where;
      const nodeId = GATE_TO_NODE[w];
      const inEdge = GATE_TO_INCOMING_EDGE[w];
      const latId  = GATE_TO_LAT[w];
      const allow = d.action === 'allow';

      if (latId) setLat(latId, `${d.latency_ms}ms`, !allow);
      if (nodeId) nodeClass(nodeId, allow ? 'allow' : 'block');
      if (inEdge) edgeClass(inEdge, allow ? 'allow' : 'block');

      if (allow) {
        pushEvent('gate-allow', '✓', `${w} scan → <b>allow</b>`, `${d.latency_ms}ms`);
        // Advance: light up the next outgoing edge as active
        if (w === 'input')     edgeClass('e-input-nemo',     'active');
        if (w === 'tool_args') edgeClass('e-toolargs-netbox','active');
        if (w === 'output')    edgeClass('e-output-user',    'active');
      } else {
        const sev = d.severity ? `<span class="sev sev-${d.severity.toLowerCase()}">${d.severity}</span>` : '';
        const atk = d.attack_technique ? `<span class="atk">${escapeHTML(d.attack_technique)}</span>` : '';
        const vlist = (d.violations || []).map(v => `<div class="evt-sub">↳ <b style="color:#fca5a5">${escapeHTML(v.rule_name)}</b> <span style="color:#6b7280">${escapeHTML(v.classification)}</span></div>`).join('');
        pushEvent('gate-block', '⛔', `${w} scan → <b>BLOCK</b> ${sev}${atk}`, `${d.latency_ms}ms${vlist}`);
        // Dim everything downstream
        dimDownstream(w);
      }
      return;
    }

    case 'blocked':
      showBlock(d, currentTurnText);
      return;

    case 'llm_call_start':
      nodeClass(d.hop === 0 ? 'n-nemo' : 'n-synth', 'active');
      if (d.hop > 0) edgeClass('e-netbox-down', 'active');
      pushEvent('llm', '🧠', `<b>Nemotron</b> call <span style="color:#475569">(hop ${d.hop})</span>`);
      return;

    case 'tool_call_proposed':
      nodeClass('n-nemo', 'allow');
      edgeClass('e-input-nemo', 'complete');
      edgeClass('e-nemo-toolargs', 'active');
      setToolBadge(d.name);
      pushEvent('tool', '🔧', `Nemotron → <code>${escapeHTML(d.name)}</code>`, escapeHTML(d.arguments));
      return;

    case 'tool_executing':
      nodeClass('n-toolargs', 'allow');
      nodeClass('n-netbox', 'active');
      edgeClass('e-toolargs-netbox', 'complete');
      pushEvent('netbox', '📦', `Executing <code>${escapeHTML(d.name)}</code> against NetBox`);
      return;

    case 'tool_result': {
      nodeClass('n-netbox', 'allow');
      edgeClass('e-netbox-down', 'active');
      const r = d.result || {};
      const summary = r.error
        ? `<span style="color:#fca5a5">error: ${escapeHTML(r.error)}</span>`
        : `count=${r.count !== undefined ? r.count : '—'}`;
      pushEvent('netbox', '✓', `NetBox returned`, summary);
      return;
    }

    case 'assistant_message':
      nodeClass('n-synth', 'allow');
      edgeClass('e-synth-output', 'complete');
      lastAssistant = d.content || '';
      // Output gate result will fire next — final render happens at turn_end
      return;

    case 'turn_end':
      if (d.reason === 'ok') {
        nodeClass('n-output', nodeStateOf('n-output') || 'allow');
        nodeClass('n-user-out', 'allow');
        edgeClass('e-output-user', 'allow');
        // If we got here cleanly, look for the assistant_message we cached
        if (lastAssistant) showAllow(lastAssistant, currentTurnText);
      }
      pushEvent('llm', '◼', `<b>turn end</b>`, d.reason || '');
      lastAssistant = '';
      return;

    case 'error':
      pushEvent('error', '⚠', escapeHTML(d.message || 'unknown error'));
      showError(d.message || 'unknown error', currentTurnText);
      return;
  }
}

// Cache last assistant_message so the allow-verdict renders after turn_end
let lastAssistant = '';

function dimDownstream(blockedAt) {
  const order = ['input', 'nemo', 'tool_args', 'netbox', 'synth', 'output', 'user-out'];
  let idx;
  if (blockedAt === 'input')     idx = order.indexOf('input');
  else if (blockedAt === 'tool_args') idx = order.indexOf('tool_args');
  else if (blockedAt === 'output')    idx = order.indexOf('output');
  else return;
  const downstream = order.slice(idx + 1);
  for (const stage of downstream) {
    const nid = ({nemo:'n-nemo', tool_args:'n-toolargs', netbox:'n-netbox', synth:'n-synth', output:'n-output', 'user-out':'n-user-out'})[stage];
    if (nid && !document.getElementById(nid).className.baseVal.includes('block')) {
      nodeClass(nid, 'dim');
    }
  }
}

function nodeStateOf(id) {
  const el = document.getElementById(id);
  if (!el) return '';
  const cls = el.className.baseVal;
  if (cls.includes('block')) return 'block';
  if (cls.includes('allow')) return 'allow';
  return '';
}

function escapeHTML(s) {
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
