// Custom SSE client (EventSource only supports GET, we POST with body)
const transcript = document.getElementById('transcript');
const form = document.getElementById('chatform');
const input = document.getElementById('msg');
const sendBtn = document.getElementById('send');

let history = [];
let sessionId = null;

// Sample-prompt buttons
document.querySelectorAll('.sample').forEach(btn => {
  btn.addEventListener('click', () => {
    input.value = btn.dataset.msg;
    input.focus();
  });
});

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text) return;
  input.value = '';
  sendBtn.disabled = true;
  appendUser(text);

  let turnEl = appendTurnContainer();

  try {
    const resp = await fetch('/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: text, history: history, session_id: sessionId }),
    });
    if (!resp.ok || !resp.body) throw new Error('chat request failed: ' + resp.status);

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    let assistantText = '';

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });

      // Parse SSE: event: x\ndata: y\n\n
      let i;
      while ((i = buf.indexOf('\n\n')) !== -1) {
        const chunk = buf.slice(0, i);
        buf = buf.slice(i + 2);
        const evt = parseSSE(chunk);
        if (!evt) continue;
        const handled = handleEvent(evt, turnEl);
        if (handled?.assistantText) assistantText = handled.assistantText;
      }
    }

    if (assistantText) {
      history.push({ role: 'user', content: text });
      history.push({ role: 'assistant', content: assistantText });
    }
  } catch (err) {
    addStep(turnEl, 'error', err.message);
  } finally {
    sendBtn.disabled = false;
    input.focus();
  }
});

function parseSSE(chunk) {
  const lines = chunk.split('\n');
  let event = 'message', data = '';
  for (const ln of lines) {
    if (ln.startsWith('event:')) event = ln.slice(6).trim();
    else if (ln.startsWith('data:')) data += ln.slice(5).trim();
  }
  if (!data) return null;
  try { return { event, data: JSON.parse(data) }; }
  catch { return { event, data: {} }; }
}

function appendUser(text) {
  const div = document.createElement('div');
  div.className = 'flex justify-end';
  div.innerHTML = `<div class="max-w-[80%] bg-ciscoblue text-white rounded-2xl rounded-br-sm px-4 py-2 text-sm">${escapeHTML(text)}</div>`;
  transcript.appendChild(div);
  div.scrollIntoView({ behavior: 'smooth', block: 'end' });
}

function appendTurnContainer() {
  const div = document.createElement('div');
  div.className = 'rounded-lg border border-slate-800 bg-panel/40 overflow-hidden';
  div.innerHTML = `
    <div class="px-3 py-2 bg-slate-900/50 border-b border-slate-800 text-xs uppercase tracking-wider text-slate-400 flex items-center gap-2">
      <span class="inline-block w-2 h-2 rounded-full bg-ciscoblue animate-pulse"></span>
      Processing turn…
    </div>
    <div class="steps p-3 space-y-2 text-xs"></div>
    <div class="answer hidden border-t border-slate-800 p-4 bubble-assistant text-sm"></div>
  `;
  transcript.appendChild(div);
  div.scrollIntoView({ behavior: 'smooth', block: 'end' });
  return div;
}

function addStep(turn, kind, html) {
  const steps = turn.querySelector('.steps');
  const div = document.createElement('div');
  div.className = 'flex items-start gap-2';
  let icon = '·', color = 'text-slate-400';
  if (kind === 'allow')   { icon = '✓'; color = 'text-accent'; }
  if (kind === 'block')   { icon = '🛑'; color = 'text-danger font-semibold'; }
  if (kind === 'tool')    { icon = '🔧'; color = 'text-ciscoblue'; }
  if (kind === 'llm')     { icon = '🧠'; color = 'text-slate-300'; }
  if (kind === 'error')   { icon = '⚠'; color = 'text-warn'; }
  div.innerHTML = `<span class="${color} mt-px font-mono">${icon}</span><span class="${color} flex-1">${html}</span>`;
  steps.appendChild(div);
  turn.scrollIntoView({ behavior: 'smooth', block: 'end' });
}

function setHeader(turn, html, klass='') {
  const head = turn.querySelector(':scope > div:first-child');
  head.className = `px-3 py-2 bg-slate-900/50 border-b border-slate-800 text-xs uppercase tracking-wider flex items-center gap-2 ${klass}`;
  head.innerHTML = html;
}

function showAnswer(turn, markdown) {
  const ans = turn.querySelector('.answer');
  ans.classList.remove('hidden');
  ans.innerHTML = marked.parse(markdown || '');
  turn.scrollIntoView({ behavior: 'smooth', block: 'end' });
}

function flashPipe(where, blocked=false) {
  const map = { input: 'pipe-input', tool_args: 'pipe-args', output: 'pipe-output' };
  const el = document.getElementById(map[where]);
  if (!el) return;
  el.classList.remove('pipe-fire', 'pipe-block');
  void el.offsetWidth; // reflow
  el.classList.add(blocked ? 'pipe-block' : 'pipe-fire');
}

function escapeHTML(s) {
  return s.replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

let assistantText = '';

function handleEvent(evt, turn) {
  const d = evt.data;
  switch (evt.event) {
    case 'turn_start':
      sessionId = d.session_id;
      assistantText = '';
      return;

    case 'gate_start':
      addStep(turn, 'llm', `<b>AI Defense ${d.where}-scan</b> starting…`);
      flashPipe(d.where, false);
      return;

    case 'gate_result':
      if (d.action === 'allow') {
        addStep(turn, 'allow', `AI Defense ${d.where}-scan → <b>allow</b> <span class="text-slate-500">(${d.latency_ms}ms)</span>`);
      } else {
        addStep(turn, 'block', `AI Defense ${d.where}-scan → <b>BLOCK</b> · ${d.category || '?'}${d.subcategory ? '/'+d.subcategory : ''} <span class="text-slate-500">(${d.latency_ms}ms)</span>`);
        flashPipe(d.where, true);
      }
      return;

    case 'blocked':
      setHeader(turn,
        `<span class="text-danger">🛑 BLOCKED BY CISCO AI DEFENSE</span> · ${escapeHTML(d.where || '')} · ${escapeHTML(d.category || '')}${d.subcategory ? '/'+escapeHTML(d.subcategory) : ''}`,
        'text-danger bg-danger/10');
      showAnswer(turn, `**Request blocked by Cisco AI Defense** at the **${d.where}** gate.\n\n- Category: \`${d.category || 'unknown'}\`\n- Subcategory: \`${d.subcategory || 'n/a'}\`\n- Message: ${d.message || '(no details)'}\n\nThe ${d.where === 'input' ? 'prompt was prevented from reaching Nemotron' : d.where === 'tool_args' ? 'tool call was prevented from executing against NetBox' : 'response was prevented from reaching the user'}.`);
      return;

    case 'llm_call_start':
      addStep(turn, 'llm', `Nemotron call <span class="text-slate-500 font-mono">(hop ${d.hop})</span>…`);
      return;

    case 'tool_call_proposed':
      addStep(turn, 'tool', `Nemotron proposes <code>${escapeHTML(d.name)}</code> with args <span class="font-mono text-slate-400">${escapeHTML(d.arguments)}</span>`);
      return;

    case 'tool_executing':
      addStep(turn, 'tool', `Executing <code>${escapeHTML(d.name)}</code> against NetBox…`);
      return;

    case 'tool_result':
      const summary = JSON.stringify(d.result).slice(0, 140);
      addStep(turn, 'allow', `<code>${escapeHTML(d.name)}</code> → <span class="font-mono text-slate-400">${escapeHTML(summary)}${summary.length >= 140 ? '…' : ''}</span>`);
      return;

    case 'assistant_message':
      assistantText = d.content;
      setHeader(turn, `<span class="text-accent">✓ Turn complete</span>`, 'text-accent');
      showAnswer(turn, d.content);
      return { assistantText };

    case 'turn_end':
      return { assistantText };

    case 'error':
      addStep(turn, 'error', escapeHTML(d.message || 'unknown error'));
      return;
  }
}
