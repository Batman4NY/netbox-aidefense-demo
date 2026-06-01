// Tab switching + Tetragon SSE consumer for the Runtime eBPF tab.
// Independent from chat.js — different SSE source (always-streaming, not per-turn).

(() => {
  // ── Tab switching ────────────────────────────────────────────
  const tabs = document.querySelectorAll('.tab-btn');
  const panes = {
    demo:    document.getElementById('tab-demo'),
    runtime: document.getElementById('tab-runtime'),
  };
  const demoFooter = document.getElementById('demo-footer');

  function activateTab(name) {
    for (const t of tabs) {
      const active = t.dataset.tab === name;
      t.classList.toggle('text-cisco',          active);
      t.classList.toggle('bg-cisco/15',         active);
      t.classList.toggle('border-cisco/40',     active);
      t.classList.toggle('text-slate-400',      !active);
      t.classList.toggle('border-transparent',  !active);
    }
    for (const [k, el] of Object.entries(panes)) {
      el.classList.toggle('hidden', k !== name);
    }
    // Footer only relevant on the demo tab
    demoFooter.classList.toggle('hidden', name !== 'demo');
  }
  tabs.forEach(t => t.addEventListener('click', () => activateTab(t.dataset.tab)));

  // ── Tetragon event consumer ──────────────────────────────────
  const stream      = document.getElementById('rt-stream');
  const badge       = document.getElementById('runtime-badge');
  const tcpCountEl  = document.getElementById('rt-tcp-count');
  const execCountEl = document.getElementById('rt-exec-count');
  const statusEl    = document.getElementById('rt-status');

  let tcpCount = 0, execCount = 0, totalCount = 0;
  const MAX_EVENTS = 200;

  function fmtTime(iso) {
    if (!iso) return '';
    try { return new Date(iso).toLocaleTimeString('en-US', { hour12: false }); }
    catch { return iso.slice(11, 19); }
  }

  function escapeHTML(s) {
    return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  }

  function renderEvent(d) {
    // Clear "waiting" placeholder on first event
    if (totalCount === 0) stream.innerHTML = '';

    const el = document.createElement('div');
    el.className = `rt-evt ${d.kind === 'tcp_connect' ? 'tcp' : 'exec'}`;
    el.innerHTML = `
      <span class="rt-evt-time">${fmtTime(d.time)}</span>
      <span class="rt-evt-kind">${d.kind === 'tcp_connect' ? 'TCP' : 'EXEC'}</span>
      <span class="rt-evt-binary">${escapeHTML(d.binary)}<span class="text-slate-500"> · pid ${d.pid ?? '?'}</span></span>
      <span class="rt-evt-container">${escapeHTML(d.container || 'host')}</span>
      <span class="rt-evt-dest">${escapeHTML(d.kind === 'tcp_connect' ? (d.dest || '—') : (d.args || ''))}</span>
    `;
    stream.insertBefore(el, stream.firstChild);
    // Trim to MAX_EVENTS
    while (stream.children.length > MAX_EVENTS) {
      stream.removeChild(stream.lastChild);
    }

    totalCount++;
    if (d.kind === 'tcp_connect') tcpCount++;
    else                          execCount++;
    tcpCountEl.textContent  = tcpCount;
    execCountEl.textContent = execCount;
    badge.textContent       = totalCount;
  }

  // Open the SSE stream (always-on, not per-turn)
  function connect() {
    statusEl.textContent = '◐';
    statusEl.className   = 'text-2xl font-mono text-warn';
    const es = new EventSource('/tetragon/stream');
    es.addEventListener('tetragon_ready', () => {
      statusEl.textContent = '●';
      statusEl.className   = 'text-2xl font-mono text-accent';
    });
    es.addEventListener('tetragon_unavailable', () => {
      statusEl.textContent = '✗';
      statusEl.className   = 'text-2xl font-mono text-danger';
      stream.innerHTML = '<div class="text-danger text-center py-12">Tetragon log not available — is the tetragon container running?</div>';
    });
    es.addEventListener('tetragon_event', (e) => {
      try { renderEvent(JSON.parse(e.data)); } catch {}
    });
    es.onerror = () => {
      statusEl.textContent = '✗';
      statusEl.className   = 'text-2xl font-mono text-danger';
      es.close();
      setTimeout(connect, 5000); // reconnect after 5s
    };
  }
  connect();

  // Clear button
  document.getElementById('rt-clear').addEventListener('click', () => {
    stream.innerHTML = '<div class="text-slate-500 text-center py-12">Cleared. Waiting for next event…</div>';
    tcpCount = execCount = totalCount = 0;
    tcpCountEl.textContent = '0';
    execCountEl.textContent = '0';
    badge.textContent = '0';
  });
})();
