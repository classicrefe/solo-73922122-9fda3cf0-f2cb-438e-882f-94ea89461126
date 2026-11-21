"""FastAPI 入口:操作页 + 核对接口 + 样例下载。"""
from __future__ import annotations

import json

from fastapi import FastAPI, File, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from .core import (
    EvidenceError,
    InputError,
    load_receipt_csv,
    load_waybill_csv,
    parse_evidence,
    verify,
)
from .sample import build_sample

app = FastAPI(title="货运数仓来源核对")

PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>货运数仓来源核对</title>
<style>
  :root { color-scheme: light; }
  body { font-family: "PingFang SC", "Microsoft YaHei", system-ui, sans-serif;
         margin: 0; background: #f4f6f8; color: #1f2933; }
  .wrap { max-width: 920px; margin: 0 auto; padding: 24px 16px 64px; }
  h1 { font-size: 22px; margin: 8px 0 4px; }
  .sub { color: #6b7280; font-size: 13px; margin-bottom: 20px; }
  .card { background: #fff; border: 1px solid #e5e7eb; border-radius: 10px;
          padding: 18px 20px; margin-bottom: 16px; }
  .card h2 { font-size: 15px; margin: 0 0 12px; }
  label.f { display: block; margin: 10px 0 4px; font-size: 13px; font-weight: 600; }
  input[type=file] { font-size: 13px; }
  button { background: #2563eb; color: #fff; border: 0; border-radius: 8px;
           padding: 10px 26px; font-size: 14px; cursor: pointer; margin-top: 14px; }
  button:disabled { background: #93c5fd; cursor: wait; }
  .banner { border-radius: 8px; padding: 12px 16px; font-size: 14px; font-weight: 600; }
  .ok    { background: #ecfdf5; color: #047857; border: 1px solid #a7f3d0; }
  .warn  { background: #fffbeb; color: #b45309; border: 1px solid #fde68a; }
  .err   { background: #fef2f2; color: #b91c1c; border: 1px solid #fecaca; }
  table { border-collapse: collapse; width: 100%; font-size: 13px; margin-top: 8px; }
  th, td { border: 1px solid #e5e7eb; padding: 6px 10px; text-align: left; }
  th { background: #f9fafb; }
  code, pre { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
  pre { background: #f8fafc; border: 1px solid #e5e7eb; border-radius: 8px;
        padding: 12px; font-size: 12px; overflow: auto; }
  details { font-size: 13px; }
  details summary { cursor: pointer; font-weight: 600; margin-bottom: 6px; }
  .muted { color: #6b7280; font-size: 12px; }
  .iv { display: inline-block; background: #eff6ff; color: #1d4ed8; border-radius: 6px;
        padding: 2px 8px; margin: 2px 4px 2px 0; font-family: ui-monospace, monospace; }
  #result { display: none; }
  a { color: #2563eb; }
</style>
</head>
<body>
<div class="wrap">
  <h1>货运数仓来源核对</h1>
  <div class="sub">上传两份未标版本的导出 CSV 与一份 JSON 证据包,核对它们是否可能来自同一次提交。</div>

  <div class="card">
    <h2>1. 上传文件</h2>
    <form id="form">
      <label class="f">运单导出 CSV(列:单号,状态,件数;仅已签收运单)</label>
      <input type="file" name="waybill_csv" accept=".csv" required>
      <label class="f">签收导出 CSV(列:签收号,单号,实收件数;仅已签收运单的签收明细)</label>
      <input type="file" name="receipt_csv" accept=".csv" required>
      <label class="f">证据包 JSON(起点快照 + 起止提交号 + 逐事务行变更前后值)</label>
      <input type="file" name="evidence" accept=".json,application/json" required>
      <br>
      <button id="go" type="submit">开始核查</button>
      <span class="muted" id="hint"></span>
    </form>
  </div>

  <div id="result">
    <div class="card">
      <h2>2. 核查结论</h2>
      <div class="banner" id="banner"></div>
      <div id="detail"></div>
    </div>
  </div>

  <div class="card">
    <h2>格式说明与样例</h2>
    <details>
      <summary>证据包 JSON 结构(点击展开)</summary>
      <pre>{
  "start_commit": 100,            // 起始提交号(快照所处提交)
  "end_commit": 110,              // 终止提交号
  "snapshots": {                  // 起点快照:两张表的全量行
    "waybills": [{"单号": "WB0001", "状态": "已签收", "件数": 3, ...}],
    "receipts": [{"签收号": "RC0001", "单号": "WB0001", "实收件数": 3, ...}]
  },
  "transactions": [               // 逐事务记录,提交号必须连续无缺号
    {"commit": 101, "changes": [
      {"table": "waybills", "op": "update", "key": "WB0001",
       "before": {...}, "after": {...}},     // insert: before=null; delete: after=null
      {"table": "receipts", "op": "insert", "key": "RC0009",
       "before": null, "after": {...}}
    ]}
  ]
}</pre>
      <div class="muted">
        表名支持 waybills/运单表、receipts/签收表。比对只在起点和每笔事务整体应用之后进行;
        日志缺号或变更前值与重放状态对不上时,判定证据不足并停止。变更条数上限 50000。
      </div>
    </details>
    <p class="muted" style="margin-top:10px">
      样例文件:
      <a href="/api/sample/waybills.csv" download>运单 CSV</a> ·
      <a href="/api/sample/receipts.csv" download>签收 CSV</a> ·
      <a href="/api/sample/evidence.json" download>证据包 JSON</a>
      (三者可直接上传试跑,预期交集:100、102、106、108、110)
    </p>
  </div>
</div>

<script>
const fmtIv = ([a, b]) => a === b ? `${a}` : `${a}–${b}`;
const ivsHtml = ivs => ivs.length
  ? ivs.map(iv => `<span class="iv">${fmtIv(iv)}</span>`).join("")
  : '<span class="muted">(无)</span>';

document.getElementById("form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = document.getElementById("go");
  const hint = document.getElementById("hint");
  btn.disabled = true; hint.textContent = "核查中…";
  try {
    const fd = new FormData(e.target);
    const resp = await fetch("/api/verify", { method: "POST", body: fd });
    const data = await resp.json();
    render(data, resp.status);
  } catch (err) {
    render({ status: "invalid_input", message: "请求失败: " + err }, 0);
  } finally {
    btn.disabled = false; hint.textContent = "";
  }
});

function render(data, httpStatus) {
  const banner = document.getElementById("banner");
  const detail = document.getElementById("detail");
  document.getElementById("result").style.display = "block";
  let html = "";
  if (data.status === "ok") {
    banner.className = "banner ok";
    banner.textContent = "两份文件在所查范围内存在共同来源提交区间。";
  } else if (data.status === "no_common_source") {
    banner.className = "banner warn";
    banner.textContent = "所查范围内没有共同来源:两份文件的吻合提交区间交集为空。";
  } else if (data.status === "insufficient_evidence") {
    banner.className = "banner err";
    banner.textContent = "证据不足,已停止核查:" + data.message;
  } else {
    banner.className = "banner err";
    banner.textContent = "输入有误:" + (data.message || ("HTTP " + httpStatus));
  }
  const r = data.result;
  if (r) {
    html += `<table>
      <tr><th>文件</th><th>完全吻合的提交区间(按编号和值)</th><th>吻合提交数</th></tr>
      <tr><td>运单 CSV</td><td>${ivsHtml(r.waybill_csv.intervals)}</td>
          <td>${r.waybill_csv.match_commits}</td></tr>
      <tr><td>签收 CSV</td><td>${ivsHtml(r.receipt_csv.intervals)}</td>
          <td>${r.receipt_csv.match_commits}</td></tr>
      <tr><td><b>交集</b></td><td><b>${ivsHtml(r.intersection)}</b></td><td>—</td></tr>
    </table>
    <p class="muted">比对范围:提交 ${r.start_commit}–${r.end_commit}(共 ${r.compare_points} 个比对点);
    已应用变更 ${r.changes_applied} 条;起点快照:运单 ${r.snapshot_rows.waybills} 行、
    签收 ${r.snapshot_rows.receipts} 行。</p>`;
  }
  detail.innerHTML = html;
  document.getElementById("result").scrollIntoView({ behavior: "smooth" });
}
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return PAGE


@app.post("/api/verify")
async def api_verify(
    waybill_csv: UploadFile = File(...),
    receipt_csv: UploadFile = File(...),
    evidence: UploadFile = File(...),
):
    try:
        tgt_a = load_waybill_csv(await waybill_csv.read())
        tgt_b = load_receipt_csv(await receipt_csv.read())
    except InputError as e:
        return JSONResponse({"status": "invalid_input", "message": str(e)}, status_code=400)

    raw = await evidence.read()
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        return JSONResponse(
            {"status": "invalid_input", "message": f"证据包不是合法 JSON: {e}"},
            status_code=400)
    try:
        ev = parse_evidence(data)
    except InputError as e:
        return JSONResponse({"status": "invalid_input", "message": str(e)}, status_code=400)

    try:
        result = verify(ev, tgt_a, tgt_b)
    except EvidenceError as e:
        return {"status": "insufficient_evidence", "message": str(e)}

    if result["intersection"]:
        status, message = "ok", "两份文件在所查范围内存在共同来源提交区间"
    else:
        status, message = "no_common_source", "所查范围内没有共同来源"
    return {"status": status, "message": message, "result": result}


@app.get("/api/sample/waybills.csv", response_class=PlainTextResponse)
async def sample_waybills():
    _, wb_csv, _, _ = build_sample()
    return PlainTextResponse(wb_csv, media_type="text/csv; charset=utf-8")


@app.get("/api/sample/receipts.csv", response_class=PlainTextResponse)
async def sample_receipts():
    _, _, rc_csv, _ = build_sample()
    return PlainTextResponse(rc_csv, media_type="text/csv; charset=utf-8")


@app.get("/api/sample/evidence.json", response_class=PlainTextResponse)
async def sample_evidence():
    ev, _, _, _ = build_sample()
    return PlainTextResponse(
        json.dumps(ev, ensure_ascii=False, indent=2),
        media_type="application/json; charset=utf-8")
