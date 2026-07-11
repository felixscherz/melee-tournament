const status = document.getElementById('status-bar');

function setStatus(msg, cls = '') {
  status.textContent = msg;
  status.className = cls;
}

// --- WebSocket game state feed ---
function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const ws = new WebSocket(`${proto}//${location.host}/ws/gamestate`);

  ws.onopen = () => setStatus('Connected', 'ok');
  ws.onclose = () => {
    setStatus('Disconnected — retrying...', 'err');
    setTimeout(connectWS, 2000);
  };
  ws.onerror = () => ws.close();

  ws.onmessage = (evt) => {
    const d = JSON.parse(evt.data);
    document.getElementById('p1-stock').textContent = d.p1?.stock ?? '—';
    document.getElementById('p2-stock').textContent = d.p2?.stock ?? '—';
    document.getElementById('p1-pct').textContent = d.p1?.percent != null ? d.p1.percent + '%' : '—';
    document.getElementById('p2-pct').textContent = d.p2?.percent != null ? d.p2.percent + '%' : '—';
    document.getElementById('p1-action').textContent = d.p1?.action ?? '—';
  };
}

connectWS();

// --- LLM prompt ---
async function submitPrompt() {
  const prompt = document.getElementById('prompt-input').value.trim();
  if (!prompt) return;
  setStatus('Sending prompt...', '');
  const res = await fetch('/api/prompt', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ prompt }),
  });
  const data = await res.json();
  setStatus(res.ok ? 'Prompt queued' : (data.detail ?? 'Error'), res.ok ? 'ok' : 'err');
}

// --- Bot upload ---
async function uploadBot(file) {
  if (!file || !file.name.endsWith('.py')) {
    setStatus('Only .py files are accepted', 'err');
    return;
  }
  setStatus(`Uploading ${file.name}...`, '');
  const form = new FormData();
  form.append('file', file);
  const res = await fetch('/api/bot/upload', { method: 'POST', body: form });
  const data = await res.json();
  setStatus(
    res.ok ? `Bot loaded: ${data.filename}` : (data.detail ?? 'Upload failed'),
    res.ok ? 'ok' : 'err'
  );
}

// --- Deactivate bot ---
async function deactivateBot() {
  const res = await fetch('/api/bot/deactivate', { method: 'POST' });
  const data = await res.json();
  setStatus(res.ok ? 'Bot deactivated — using LLM' : 'Error', res.ok ? 'ok' : 'err');
}
