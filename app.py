#!/usr/bin/env python3
"""
Local web harness for the Claude frontier benchmark.

    pip install flask anthropic
    python app.py
    open http://127.0.0.1:5001

Paste your Anthropic key in the page, pick models and trial count, hit Run, and
results stream into the table live. The key is held only for the duration of the
request, is sent only to api.anthropic.com, and is never written to disk or logged.

Note: this executes model-generated Python locally in a timed subprocess. Run it
on a machine you're fine with that on. Tasks come from tasks.py so the CLI and
web harness stay in sync.
"""

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time

from flask import Flask, Response, request, stream_with_context
from anthropic import Anthropic

from tasks import TASKS

# --- models + pricing ($/MTok), verified against Anthropic docs 2026-06-30 ----
# Sonnet 5 is on introductory pricing through 2026-08-31; standard is $3/$15.
# effort defaults to "high" on the adaptive-thinking models; pinned so all
# thinking models spend a comparable budget. Verify the kwarg against your SDK.
MODELS = {
    "opus-4.7": {"id": "claude-opus-4-7", "in": 5.0, "out": 25.0,
                 "in_std": 5.0, "out_std": 25.0, "effort_ok": True},
    "opus-4.8": {"id": "claude-opus-4-8", "in": 5.0, "out": 25.0,
                 "in_std": 5.0, "out_std": 25.0, "effort_ok": True},
    "sonnet-5": {"id": "claude-sonnet-5", "in": 2.0, "out": 10.0,
                 "in_std": 3.0, "out_std": 15.0, "effort_ok": True},
}

# Thinking decision. With adaptive thinking models, thinking is OFF unless you
# set it explicitly, even at high effort. THINK=True is the realistic enterprise
# number (and where 4.8/Sonnet 5 spend differs). Run all three in the SAME mode
# or the comparison isn't apples to apples.
THINK = True
EFFORT = "high"   # applied to all effort-capable models when THINK is True

SYSTEM = "You are a senior software engineer. Produce correct, idiomatic Python."
MAX_TOKENS = 16000
TIMEOUT_S = 25

app = Flask(__name__)


def call_model(model, prompt, api_key):
    client = Anthropic(api_key=api_key)
    kwargs = dict(model=model["id"], max_tokens=MAX_TOKENS, system=SYSTEM,
                  messages=[{"role": "user", "content": prompt}])
    # effort lives INSIDE output_config (a top-level field), not as a bare
    # "effort" field. Sending it bare returns a 400. Thinking is off unless set
    # explicitly, so enable adaptive thinking when THINK is True.
    if THINK and model.get("effort_ok"):
        kwargs["thinking"] = {"type": "adaptive"}
        kwargs["output_config"] = {"effort": EFFORT}
    try:
        resp = client.messages.create(**kwargs)
    except TypeError:
        # Older SDK without native output_config/thinking kwargs: pass via extra_body.
        extra = {}
        for k in ("output_config", "thinking"):
            if k in kwargs:
                extra[k] = kwargs.pop(k)
        if extra:
            kwargs["extra_body"] = extra
        resp = client.messages.create(**kwargs)
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    return text, resp.usage.input_tokens, resp.usage.output_tokens, getattr(resp, "stop_reason", None)


def extract_code(text):
    m = re.search(r"```(?:python)?\s*(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    # If a response is truncated mid-fence, still run the Python portion so the
    # failure reason points at the generated code rather than the Markdown fence.
    m = re.search(r"```(?:python)?\s*(.*)", text, re.DOTALL)
    return (m.group(1) if m else text).strip()


def verify(code, task):
    code = "from __future__ import annotations\n" + code
    for bad in task["forbidden"]:
        if bad.endswith("("):          # banned builtin CALL: eval( exec( compile(
            name = re.escape(bad[:-1])  # lookbehind allows re.compile, obj.eval, etc.
            hit = re.search(r"(?<![\w.])" + name + r"\s*\(", code)
        elif bad.endswith("."):        # banned module attr: ast.
            name = re.escape(bad[:-1])
            hit = re.search(r"(?<![\w.])" + name + r"\.", code)
        else:
            hit = bad in code
        if hit:
            return False, f"forbidden:{bad}"
    program = code + "\n\n" + task["test_code"]
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(program)
        path = f.name
    try:
        out = subprocess.run([sys.executable, path], capture_output=True,
                             text=True, timeout=TIMEOUT_S)
        if out.returncode == 0 and "ALL_TESTS_PASSED" in out.stdout:
            return True, "ok"
        reason = (out.stderr.strip().splitlines() or ["assert_failed"])[-1]
        return False, reason[:120]
    except subprocess.TimeoutExpired:
        return False, "timeout"
    finally:
        os.unlink(path)


def cost(in_tok, out_tok, in_price, out_price):
    return in_tok / 1e6 * in_price + out_tok / 1e6 * out_price


def save_failure(model_key, task_id, trial, code, reason):
    """Write a failing generation to ./failures so you can see what tripped it."""
    os.makedirs("failures", exist_ok=True)
    path = os.path.join("failures", f"{model_key}__{task_id}__t{trial}.py")
    with open(path, "w") as f:
        f.write(f"# reason: {reason}\n\n{code}\n")


@app.route("/")
def index():
    task_meta = [
        {
            "id": t["id"],
            "level": t["level"],
            "weight": t.get("weight", 1),
            "buyer_signal": t.get("buyer_signal", ""),
        }
        for t in TASKS
    ]
    return Response(PAGE.replace("__TASK_META__", json.dumps(task_meta)), mimetype="text/html")


@app.route("/run", methods=["POST"])
def run():
    cfg = request.get_json(force=True)
    api_key = (cfg.get("api_key") or "").strip()
    trials = max(1, min(int(cfg.get("trials", 5)), 20))
    chosen = [k for k in cfg.get("models", []) if k in MODELS]
    chosen_task_ids = {t for t in cfg.get("tasks", [])}
    selected_tasks = [t for t in TASKS if not chosen_task_ids or t["id"] in chosen_task_ids]

    def gen():
        if not api_key:
            yield json.dumps({"type": "error", "msg": "No API key provided."}) + "\n"
            return
        if not chosen:
            yield json.dumps({"type": "error", "msg": "No models selected."}) + "\n"
            return
        if not selected_tasks:
            yield json.dumps({"type": "error", "msg": "No tasks selected."}) + "\n"
            return
        total = len(chosen) * len(selected_tasks) * trials
        done = 0
        for mk in chosen:
            model = MODELS[mk]
            for task in selected_tasks:
                for trial in range(1, trials + 1):
                    try:
                        text, itok, otok, stop_reason = call_model(model, task["prompt"], api_key)
                        code = extract_code(text)
                        if stop_reason == "max_tokens" or otok >= MAX_TOKENS:
                            passed, reason = False, "token_budget_exhausted:max_tokens"
                        else:
                            passed, reason = verify(code, task)
                        if not passed:
                            save_failure(mk, task["id"], trial, code, reason)
                    except Exception as e:
                        itok = otok = 0
                        passed, reason = False, f"error:{type(e).__name__}:{str(e)[:160]}"
                    done += 1
                    yield json.dumps({
                        "type": "row",
                        "model": mk, "task": task["id"], "level": task["level"],
                        "weight": task.get("weight", 1),
                        "buyer_signal": task.get("buyer_signal", ""),
                        "trial": trial,
                        "input_tokens": itok, "output_tokens": otok,
                        "passed": passed, "reason": reason,
                        "cost": round(cost(itok, otok, model["in"], model["out"]), 6),
                        "cost_std": round(cost(itok, otok, model["in_std"], model["out_std"]), 6),
                        "done": done, "total": total,
                    }) + "\n"
                    time.sleep(0.4)
        yield json.dumps({"type": "done"}) + "\n"

    return Response(stream_with_context(gen()), mimetype="application/x-ndjson")


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claude frontier benchmark</title>
<style>
  :root{
    --bg:#101114; --panel:#181a1f; --line:#333840; --txt:#edf0f2;
    --muted:#a2a9b2; --accent:#2f8f83; --amber:#d99a2b; --pass:#47b66d; --fail:#de5a52;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--txt);
       font:14px/1.5 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
  header{padding:22px 28px;border-bottom:1px solid var(--line)}
  h1{margin:0;font-size:20px;letter-spacing:0}
  .sub{color:var(--muted);font-size:13px;margin-top:4px}
  main{max-width:1080px;margin:0 auto;padding:24px 28px 60px}
  .panel{background:var(--panel);border:1px solid var(--line);border-radius:8px;
         padding:18px 20px;margin-bottom:22px}
  label{display:block;font-size:12px;color:var(--muted);margin-bottom:6px;
        text-transform:uppercase;letter-spacing:.5px}
  input[type=password],input[type=number]{
    background:#0b0e13;border:1px solid var(--line);color:var(--txt);
    border-radius:7px;padding:9px 11px;font-size:14px;width:100%}
  input[type=number]{width:90px}
  .row{display:flex;gap:22px;flex-wrap:wrap;align-items:flex-end}
  .row > div{flex:0 0 auto}
  .grow{flex:1 1 320px}
  .models{display:flex;gap:18px;flex-wrap:wrap}
  .chk{display:flex;align-items:center;gap:7px;background:#0b0e13;
       border:1px solid var(--line);border-radius:7px;padding:8px 12px;cursor:pointer}
  .chk input{accent-color:var(--accent)}
  button{background:var(--accent);color:#fff;border:0;border-radius:7px;
         padding:10px 20px;font-size:14px;font-weight:600;cursor:pointer}
  button:disabled{opacity:.5;cursor:default}
  .ghost{background:transparent;border:1px solid var(--line);color:var(--txt)}
  .bar{height:6px;background:#0b0e13;border-radius:4px;overflow:hidden;margin-top:14px}
  .bar > div{height:100%;background:var(--accent);width:0;transition:width .2s}
  .note{color:var(--muted);font-size:12px;margin-top:10px}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line)}
  th{color:var(--muted);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.5px}
  td.num{text-align:right;font-variant-numeric:tabular-nums}
  .pill{font-size:11px;padding:2px 8px;border-radius:20px;font-weight:600}
  .pill.p{background:rgba(63,185,80,.15);color:var(--pass)}
  .pill.f{background:rgba(248,81,73,.15);color:var(--fail)}
  .cbar{display:inline-block;height:10px;background:var(--accent);border-radius:3px;vertical-align:middle}
  .score{font-size:22px;font-weight:700}
  .taskgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:10px;margin-top:12px}
  .task{border:1px solid var(--line);border-radius:8px;padding:10px 12px;background:#111318}
  .task b{display:block;font-size:12px}
  .task span{display:block;color:var(--muted);font-size:12px;margin-top:3px}
  h2{font-size:14px;margin:0 0 14px;letter-spacing:.3px}
  .muted{color:var(--muted)}
  .hide{display:none}
</style></head>
<body>
<header>
  <h1>Claude frontier benchmark</h1>
  <div class="sub">Tests where premium models should win: policy reasoning, incident correlation, and production migration planning. Key stays in memory, sent only to Anthropic.</div>
</header>
<main>
  <div class="panel">
    <h2>Buyer Lens</h2>
    <div class="note" style="margin-top:0">This suite treats simple tasks as calibration and weights frontier tasks most heavily. The goal is not to prove that cheaper models can solve known coding puzzles; it is to show when higher capability buys reliability on ambiguous, high-blast-radius work.</div>
    <div id="taskMeta" class="taskgrid"></div>
  </div>

  <div class="panel">
    <div class="row">
      <div class="grow">
        <label>Anthropic API key</label>
        <input id="key" type="password" placeholder="sk-ant-..." autocomplete="off">
      </div>
      <div>
        <label>Trials per task</label>
        <input id="trials" type="number" value="5" min="1" max="20">
      </div>
    </div>
    <div style="margin-top:16px">
      <label>Models</label>
      <div id="modelChecks" class="models">
        <label class="chk"><input type="checkbox" value="opus-4.7" checked> Opus 4.7</label>
        <label class="chk"><input type="checkbox" value="opus-4.8" checked> Opus 4.8</label>
        <label class="chk"><input type="checkbox" value="sonnet-5" checked> Sonnet 5</label>
      </div>
    </div>
    <div style="margin-top:16px">
      <label>Tasks</label>
      <div id="taskChecks" class="models"></div>
    </div>
    <div style="margin-top:18px;display:flex;gap:12px;align-items:center">
      <button id="run">Run benchmark</button>
      <button id="import" class="ghost">Import CSV</button>
      <button id="csv" class="ghost">Download CSV</button>
      <input id="csvFile" type="file" accept=".csv,text/csv" class="hide">
      <span id="status" class="muted"></span>
    </div>
    <div class="bar"><div id="prog"></div></div>
    <div class="note">Score is weighted by task difficulty. Cost is shown per successful trial; `token_budget_exhausted:max_tokens` is counted as a real failure because truncated work is not a completed enterprise outcome.</div>
  </div>

  <div id="summaryPanel" class="panel hide">
    <h2>Executive Summary <span class="muted" style="font-weight:400">weighted frontier score and cost per success</span></h2>
    <div id="summary"></div>
  </div>

  <div id="logPanel" class="panel hide">
    <h2>Trial log</h2>
    <div style="overflow:auto;max-height:380px">
      <table id="log"><thead><tr>
        <th>Model</th><th>Task</th><th>Trial</th>
        <th class="num">In</th><th class="num">Out</th>
        <th>Result</th><th class="num">Cost</th>
      </tr></thead><tbody></tbody></table>
    </div>
  </div>
</main>
<script>
const $ = s => document.querySelector(s);
let rows = [];
const TASK_META = __TASK_META__;

function renderTaskMeta(){
  $('#taskMeta').innerHTML = TASK_META.map(t =>
    `<div class="task"><b>${esc(t.id)} <span style="display:inline;color:var(--amber)">w${t.weight}</span></b><span>${esc(t.buyer_signal)}</span></div>`
  ).join('');
  $('#taskChecks').innerHTML = TASK_META.map(t =>
    `<label class="chk"><input type="checkbox" value="${esc(t.id)}" checked> ${esc(t.id)}</label>`
  ).join('');
}
renderTaskMeta();

function selectedModels(){
  return [...document.querySelectorAll('#modelChecks input:checked')].map(c=>c.value);
}

function selectedTasks(){
  return [...document.querySelectorAll('#taskChecks input:checked')].map(c=>c.value);
}

$('#run').onclick = async () => {
  const api_key = $('#key').value.trim();
  const trials = parseInt($('#trials').value || '5', 10);
  const models = selectedModels();
  const tasks = selectedTasks();
  if(!api_key){ $('#status').textContent = 'Enter your API key first.'; return; }
  if(!models.length){ $('#status').textContent = 'Pick at least one model.'; return; }
  if(!tasks.length){ $('#status').textContent = 'Pick at least one task.'; return; }

  const keepImported = rows.length && confirm('Keep imported/current rows and append this run? Choose OK to merge, Cancel to start fresh.');
  if(!keepImported) rows = [];
  $('#log tbody').innerHTML = '';
  $('#summary').innerHTML = '';
  $('#logPanel').classList.remove('hide');
  $('#summaryPanel').classList.add('hide');
  $('#run').disabled = true;
  $('#status').textContent = 'Running...';
  $('#prog').style.width = '0%';

  let res;
  try{
    res = await fetch('/run', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({api_key, trials, models, tasks})});
  }catch(e){ $('#status').textContent = 'Request failed: ' + e; $('#run').disabled=false; return; }

  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = '';
  while(true){
    const {value, done} = await reader.read();
    if(done) break;
    buf += dec.decode(value, {stream:true});
    let i;
    while((i = buf.indexOf('\n')) >= 0){
      const line = buf.slice(0, i); buf = buf.slice(i+1);
      if(line.trim()) handle(JSON.parse(line));
    }
  }
  $('#run').disabled = false;
};

function handle(m){
  if(m.type === 'error'){ $('#status').textContent = 'Error: ' + m.msg; return; }
  if(m.type === 'done'){
    $('#status').textContent = 'Done. ' + rows.length + ' trials.';
    renderSummary();
    $('#csv').classList.remove('hide');
    return;
  }
  rows = rows.filter(r => !(r.model === m.model && r.task === m.task && r.trial === m.trial));
  rows.push(m);
  $('#prog').style.width = (100*m.done/m.total).toFixed(1) + '%';
  $('#status').textContent = m.done + ' / ' + m.total + ' trials';
  const tb = $('#log tbody');
  const tr = document.createElement('tr');
  const pill = m.passed ? '<span class="pill p">PASS</span>'
                        : '<span class="pill f">FAIL</span> <span class="muted">'+esc(m.reason)+'</span>';
  tr.innerHTML = `<td>${m.model}</td><td>${m.task}</td><td>${m.trial}</td>
    <td class="num">${m.input_tokens}</td><td class="num">${m.output_tokens}</td>
    <td>${pill}</td><td class="num">$${m.cost.toFixed(4)}</td>`;
  tb.appendChild(tr);
}

function renderSummary(){
  const byModel = {};
  const byTask = {};
  for(const r of rows){
    (byModel[r.model] = byModel[r.model] || []).push(r);
    const k = r.model + '||' + r.task;
    (byTask[k] = byTask[k] || []).push(r);
  }
  const modelAgg = Object.entries(byModel).map(([model, g]) => {
    let earned = 0, possible = 0, passCount = 0, totalCost = 0, successCost = 0;
    for(const r of g){
      possible += r.weight;
      if(r.passed){ earned += r.weight; passCount++; successCost += r.cost; }
      totalCost += r.cost;
    }
    return {
      model,
      score: possible ? earned / possible : 0,
      pass: g.length ? passCount / g.length : 0,
      successCost: passCount ? successCost / passCount : null,
      totalCost
    };
  }).sort((a,b)=> b.score - a.score || a.model.localeCompare(b.model));

  let html = `<table><thead><tr><th>Model</th><th class="num">Frontier Score</th><th class="num">Pass</th><th class="num">Mean Cost / Success</th><th class="num">Run Spend</th></tr></thead><tbody>`;
  for(const a of modelAgg){
    html += `<tr><td>${a.model}</td><td class="num"><span class="score">${Math.round(a.score*100)}</span>%</td><td class="num">${Math.round(a.pass*100)}%</td><td class="num">${a.successCost == null ? 'n/a' : '$'+a.successCost.toFixed(4)}</td><td class="num">$${a.totalCost.toFixed(4)}</td></tr>`;
  }
  html += '</tbody></table>';
  html += `<h2 style="margin-top:22px">Task Detail</h2>`;
  html += `<table><thead><tr><th>Model</th><th>Task</th><th>Level</th><th class="num">Weight</th><th class="num">Pass</th><th class="num">Mean Cost / Success</th></tr></thead><tbody>`;
  for(const [k, g] of Object.entries(byTask).sort()){
    const [model, task] = k.split('||');
    const ok = g.filter(r=>r.passed);
    const mean = ok.length ? ok.reduce((a,r)=>a+r.cost,0)/ok.length : null;
    html += `<tr><td>${model}</td><td>${task}</td><td>${g[0].level}</td><td class="num">${g[0].weight}</td><td class="num">${Math.round(ok.length/g.length*100)}%</td><td class="num">${mean == null ? 'n/a' : '$'+mean.toFixed(4)}</td></tr>`;
  }
  html += '</tbody></table>';
  html += `<div class="note">Interpretation: choose the less expensive model when the frontier score is tied on your task mix. Choose the stronger model when it materially reduces failures on weighted tasks, because those failures represent review cycles, delayed migrations, unsafe policy decisions, and incident-response drag.</div>`;
  $('#summary').innerHTML = html;
  $('#summaryPanel').classList.remove('hide');
}

$('#csv').onclick = () => {
  if(!rows.length){
    $('#status').textContent = 'No rows to download yet.';
    return;
  }
  const head = ['model','task','level','weight','trial','input_tokens','output_tokens','passed','reason','cost_usd','cost_usd_standard'];
  const lines = [head.join(',')];
  for(const r of rows){
    lines.push([r.model,r.task,r.level,r.weight,r.trial,r.input_tokens,r.output_tokens,
      r.passed?1:0, r.reason, r.cost, r.cost_std].map(csvCell).join(','));
  }
  const blob = new Blob([lines.join('\n')], {type:'text/csv'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob); a.download = 'results_raw.csv'; a.click();
};

function csvCell(v){
  const s = String(v ?? '');
  return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

$('#import').onclick = () => $('#csvFile').click();

$('#csvFile').onchange = async e => {
  const file = e.target.files[0];
  if(!file) return;
  const text = await file.text();
  const imported = parseCsv(text);
  for(const r of imported){
    rows = rows.filter(x => !(x.model === r.model && x.task === r.task && x.trial === r.trial));
    rows.push(r);
  }
  $('#status').textContent = `Imported ${imported.length} rows.`;
  $('#summaryPanel').classList.remove('hide');
  renderSummary();
  $('#csv').classList.remove('hide');
  e.target.value = '';
};

function parseCsv(text){
  const lines = text.trim().split(/\r?\n/).filter(Boolean);
  if(lines.length < 2) return [];
  const header = splitCsvLine(lines[0]);
  const out = [];
  for(const line of lines.slice(1)){
    const cols = splitCsvLine(line);
    const r = Object.fromEntries(header.map((h,i)=>[h, cols[i] ?? '']));
    out.push({
      model: r.model,
      task: r.task,
      level: r.level,
      weight: Number(r.weight || TASK_META.find(t=>t.id === r.task)?.weight || 1),
      trial: Number(r.trial),
      input_tokens: Number(r.input_tokens || 0),
      output_tokens: Number(r.output_tokens || 0),
      passed: r.passed === '1' || r.passed === 'true' || r.passed === 'TRUE',
      reason: r.reason || '',
      cost: Number(r.cost_usd || r.cost || 0),
      cost_std: Number(r.cost_usd_standard || r.cost_std || r.cost_usd || 0)
    });
  }
  return out.filter(r => r.model && r.task && Number.isFinite(r.trial));
}

function splitCsvLine(line){
  const cells = [];
  let cur = '', quoted = false;
  for(let i = 0; i < line.length; i++){
    const ch = line[i];
    if(ch === '"' && quoted && line[i+1] === '"'){ cur += '"'; i++; continue; }
    if(ch === '"'){ quoted = !quoted; continue; }
    if(ch === ',' && !quoted){ cells.push(cur); cur = ''; continue; }
    cur += ch;
  }
  cells.push(cur);
  return cells;
}

function esc(s){ return String(s).replace(/[&<>]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }
</script>
</body></html>"""


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5001, debug=False, threaded=True)
