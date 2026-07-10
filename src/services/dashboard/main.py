from __future__ import annotations

import os

import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

app = FastAPI(title="Cloud-Edge MVP - Dashboard", version="0.1.0")
RECORDER_URL = os.getenv("RECORDER_URL", "http://localhost:8004").rstrip("/")

HTML = r"""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>云边协同决策 MVP</title>
  <style>
    :root { color-scheme: light; font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
    body { margin: 0; background: #f5f7fb; color: #172033; }
    header { padding: 28px 36px 18px; background: #102a43; color: white; }
    header h1 { margin: 0 0 8px; font-size: 24px; }
    header p { margin: 0; opacity: .78; }
    main { padding: 24px 36px 48px; max-width: 1400px; margin: auto; }
    .cards { display: grid; grid-template-columns: repeat(auto-fit,minmax(180px,1fr)); gap: 14px; margin-bottom: 20px; }
    .card { background: white; border: 1px solid #e2e8f0; border-radius: 12px; padding: 16px; box-shadow: 0 3px 14px rgba(15,23,42,.05); }
    .label { font-size: 12px; color: #64748b; text-transform: uppercase; letter-spacing: .06em; }
    .value { font-size: 28px; font-weight: 700; margin-top: 8px; }
    .panel { background: white; border: 1px solid #e2e8f0; border-radius: 12px; overflow: hidden; }
    .panel h2 { font-size: 16px; margin: 0; padding: 16px 18px; border-bottom: 1px solid #e2e8f0; }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th, td { text-align: left; padding: 11px 14px; border-bottom: 1px solid #edf2f7; vertical-align: top; }
    th { color: #64748b; background: #f8fafc; position: sticky; top: 0; }
    .route { display: inline-block; padding: 3px 8px; border-radius: 999px; font-weight: 700; background: #e2e8f0; }
    .EDGE { background:#dcfce7; color:#166534; }
    .CLOUD { background:#dbeafe; color:#1d4ed8; }
    .EDGE_FALLBACK { background:#fef3c7; color:#92400e; }
    .EDGE_SAFETY { background:#fee2e2; color:#991b1b; }
    .muted { color:#64748b; }
    .error { color:#b91c1c; }
  </style>
</head>
<body>
<header>
  <h1>云边协同决策 MVP</h1>
  <p>实时展示 EDGE / CLOUD / EDGE_FALLBACK / EDGE_SAFETY 路由结果</p>
</header>
<main>
  <section class="cards" id="cards"></section>
  <section class="panel">
    <h2>最近决策事件 <span class="muted" id="updated"></span></h2>
    <div style="overflow:auto;max-height:650px">
      <table>
        <thead><tr><th>时间</th><th>任务</th><th>组件</th><th>路径</th><th>决策原因</th><th>最终结果</th><th>耗时</th></tr></thead>
        <tbody id="rows"><tr><td colspan="7">正在载入…</td></tr></tbody>
      </table>
    </div>
  </section>
</main>
<script>
function esc(v){ return String(v ?? '').replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m])); }
async function refresh(){
  try {
    const [summaryRes, eventsRes] = await Promise.all([fetch('/api/summary'), fetch('/api/events?limit=80')]);
    const summary = await summaryRes.json();
    const events = await eventsRes.json();
    const routes = summary.routes || {};
    const cards = [
      ['总事件', summary.total_events || 0],
      ['边缘直返', routes.EDGE || 0],
      ['云端增强', routes.CLOUD || 0],
      ['本地降级', routes.EDGE_FALLBACK || 0],
      ['紧急安全', routes.EDGE_SAFETY || 0]
    ];
    document.getElementById('cards').innerHTML = cards.map(c => `<div class="card"><div class="label">${c[0]}</div><div class="value">${c[1]}</div></div>`).join('');
    const decisions = events.filter(e => e.event_type === 'decision');
    document.getElementById('rows').innerHTML = decisions.length ? decisions.map(e => {
      const d = e.data || {};
      return `<tr>
        <td>${esc(new Date(e.created_at).toLocaleTimeString())}</td>
        <td>${esc(e.task_id)}</td>
        <td>${esc(e.component)}</td>
        <td><span class="route ${esc(e.route)}">${esc(e.route)}</span></td>
        <td>${esc(d.decision_reason)}</td>
        <td>${esc(d.final_prediction)} / ${esc(d.final_action)}</td>
        <td>${esc(d.total_latency_ms)} ms</td>
      </tr>`;
    }).join('') : '<tr><td colspan="7">暂无决策事件。运行 smoke test 后刷新。</td></tr>';
    document.getElementById('updated').textContent = '· ' + new Date().toLocaleTimeString();
  } catch (e) {
    document.getElementById('rows').innerHTML = `<tr><td colspan="7" class="error">读取 Recorder 失败：${esc(e)}</td></tr>`;
  }
}
refresh(); setInterval(refresh, 2000);
</script>
</body>
</html>
"""


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return HTML


@app.get("/api/summary")
async def proxy_summary():
    async with httpx.AsyncClient(timeout=1.0) as client:
        response = await client.get(f"{RECORDER_URL}/v1/summary")
        response.raise_for_status()
        return response.json()


@app.get("/api/events")
async def proxy_events(limit: int = 100):
    async with httpx.AsyncClient(timeout=1.0) as client:
        response = await client.get(f"{RECORDER_URL}/v1/events", params={"limit": limit})
        response.raise_for_status()
        return response.json()
