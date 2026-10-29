# -*- coding: utf-8 -*-
"""Generate a dependency-free, filterable HTML view of logs/run_events.jsonl."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOG = ROOT / "logs" / "run_events.jsonl"
OUT = ROOT / "logs" / "index.html"

TEMPLATE = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>首分机器人 · 运行日志</title>
<style>
:root{font-family:Inter,"Microsoft YaHei",sans-serif;color:#17202a;background:#f5f7fa}body{margin:24px}h1{font-size:24px}p{color:#667085}.panel{background:white;border:1px solid #e4e7ec;border-radius:12px;padding:16px;margin:16px 0}.filters{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:10px}input,select,button{box-sizing:border-box;width:100%;padding:9px;border:1px solid #d0d5dd;border-radius:7px;background:white}button{cursor:pointer}.table-wrap{overflow:auto}table{width:100%;border-collapse:collapse;font-size:13px;background:white}th,td{padding:10px;border-bottom:1px solid #eaecf0;text-align:left;white-space:nowrap}th{position:sticky;top:0;background:#f9fafb;cursor:pointer}tr:hover{background:#f8fafc}.success{color:#067647}.failed{color:#b42318}.running{color:#175cd3}.cancelled{color:#b54708}.error{max-width:320px;white-space:normal;color:#b42318}a{color:#175cd3;text-decoration:none}#count{font-weight:600}
</style></head><body>
<h1>运行日志</h1><p>数据源：logs/run_events.jsonl ｜ 每次开始、成功、失败和取消均保留事件。</p>
<div class="panel"><div class="filters">
<input id="date" type="date" aria-label="运行日期"><input id="experiment" placeholder="实验 ID"><input id="run" placeholder="Run ID"><input id="arm" placeholder="Arm ID">
<select id="module"><option value="">全部模块</option></select><select id="status"><option value="">全部状态</option><option>running</option><option>success</option><option>failed</option><option>cancelled</option></select>
<button id="reset">清除筛选</button></div></div>
<p id="count"></p><div class="table-wrap"><table><thead><tr>
<th data-key="timestamp">时间</th><th data-key="experiment_id">实验</th><th data-key="run_id">Run ID</th><th data-key="arm_id">Arm</th><th data-key="module">模块</th><th data-key="event">事件</th><th data-key="status">状态</th><th data-key="duration_ms">耗时 ms</th><th data-key="input_tokens">输入 Token</th><th data-key="output_tokens">输出 Token</th><th data-key="cost">费用</th><th data-key="query_count">查询</th><th>产物 / 错误</th>
</tr></thead><tbody id="rows"></tbody></table></div>
<script>const EVENTS=__DATA__;
let sortKey='timestamp',sortAsc=false;const ids=['date','experiment','run','arm','module','status'];
const el=Object.fromEntries(ids.map(id=>[id,document.getElementById(id)]));const countEl=document.getElementById('count'),rowsEl=document.getElementById('rows');
const modules=[...new Set(EVENTS.map(e=>e.module).filter(Boolean))];for(const m of modules)el.module.add(new Option(m,m));
function val(e,k){return e[k]??''}function text(e,k){const v=val(e,k);return String(v)}
function filtered(){return EVENTS.filter(e=>{const day=text(e,'as_of')||text(e,'timestamp').slice(0,10);return(!el.date.value||day===el.date.value)&&(!el.experiment.value||text(e,'experiment_id').toLowerCase().includes(el.experiment.value.toLowerCase()))&&(!el.run.value||text(e,'run_id').toLowerCase().includes(el.run.value.toLowerCase()))&&(!el.arm.value||text(e,'arm_id').toLowerCase().includes(el.arm.value.toLowerCase()))&&(!el.module.value||e.module===el.module.value)&&(!el.status.value||e.status===el.status.value)}).sort((a,b)=>{const x=val(a,sortKey),y=val(b,sortKey);return(x<y?-1:x>y?1:0)*(sortAsc?1:-1)})}
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function render(){const rows=filtered();countEl.textContent=`显示 ${rows.length} / ${EVENTS.length} 条事件`;rowsEl.innerHTML=rows.map(e=>{let last='';if(e.artifact_path)last+=`<a href="../${esc(e.artifact_path)}">打开产物</a> `;if(e.error)last+=`<span class="error">${esc(e.error)}</span>`;return `<tr><td>${esc(e.timestamp)}</td><td>${esc(e.experiment_id)}</td><td>${esc(e.run_id)}</td><td>${esc(e.arm_id)}</td><td>${esc(e.module)}</td><td>${esc(e.event)}</td><td class="${esc(e.status)}">${esc(e.status)}</td><td>${esc(e.duration_ms)}</td><td>${esc(e.input_tokens)}</td><td>${esc(e.output_tokens)}</td><td>${esc(e.cost)}</td><td>${esc(e.query_count)}</td><td>${last}</td></tr>`}).join('')}
for(const id of ids)el[id].addEventListener('input',render);document.getElementById('reset').onclick=()=>{for(const id of ids)el[id].value='';render()};document.querySelectorAll('th[data-key]').forEach(th=>th.onclick=()=>{const k=th.dataset.key;if(sortKey===k)sortAsc=!sortAsc;else{sortKey=k;sortAsc=true}render()});render();
</script></body></html>'''


def main() -> None:
    events = []
    if LOG.exists():
        for line_no, line in enumerate(LOG.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{LOG}:{line_no} JSONL 损坏：{exc}")
    data = json.dumps(events, ensure_ascii=False).replace("<", "\\u003c")
    OUT.write_text(TEMPLATE.replace("__DATA__", data), encoding="utf-8")
    print(f"[viewer] {OUT}（{len(events)} 条事件）")


if __name__ == "__main__":
    main()
