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
from .recovery import RecoveryError, recover
from .sample import build_recovery_sample, build_sample

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
  .iv.mk { background: #fef3c7; color: #92400e; border: 1px solid #fcd34d; }
  .tail { color: #6b7280; font-size: 12px; margin-top: 8px; }
  .dl { display: none; margin-top: 12px; }
  .dl a { display: inline-block; background: #059669; color: #fff; text-decoration: none;
          border-radius: 8px; padding: 9px 20px; font-size: 14px; }
  #result, #rresult { display: none; }
  a { color: #2563eb; }
  .tabs { display: flex; gap: 8px; margin: 0 0 18px; }
  .tabs button { margin: 0; background: #e5e7eb; color: #374151; }
  .tabs button.on { background: #2563eb; color: #fff; }
  .pane { display: none; }
  .pane.on { display: block; }
</style>
</head>
<body>
<div class="wrap">
  <h1>货运数仓来源核对</h1>
  <div class="sub">核对两份未标版本的导出 CSV 是否来自同一次提交;缺一笔日志时按首尾快照补档。</div>

  <div class="tabs">
    <button type="button" class="on" data-pane="pane-verify">来源核查</button>
    <button type="button" data-pane="pane-recover">补档</button>
  </div>

  <div class="pane on" id="pane-verify">

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
  </div><!-- /pane-verify -->

  <div class="pane" id="pane-recover">
    <div class="card">
      <h2>补档:缺一笔日志时按首尾快照推算</h2>
      <form id="rform">
        <label class="f">缺一笔的证据包 JSON(起点快照 + 其余日志;提交号恰缺一个)</label>
        <input type="file" name="evidence" accept=".json,application/json" required>
        <label class="f">末次提交后的两表全量快照 JSON(waybills / receipts 两张全表)</label>
        <input type="file" name="end_snapshot" accept=".json,application/json" required>
        <br>
        <button id="rgo" type="submit">推算补档</button>
        <span class="muted" id="rhint"></span>
      </form>
      <p class="muted" style="margin-top:10px">
        缺号示例文件:
        <a href="/api/sample/recovery-evidence.json" download>缺 102 的证据包</a> ·
        <a href="/api/sample/end-snapshot.json" download>末快照(提交 110 后)</a>
        (两者可直接上传,补出提交 102 的「快照推算」净变更)
      </p>
    </div>

    <div id="rresult">
      <div class="card">
        <h2>补档结论</h2>
        <div class="banner" id="rbanner"></div>
        <div id="rdetail"></div>
        <div class="dl" id="rdl"></div>
      </div>
    </div>
  </div><!-- /pane-recover -->

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
        补档推算出的提交会带 <code>"source": "快照推算"</code> 标记,核查页若把它列入
        共同来源会一并显示该标记。
      </div>
    </details>
  </div>
  </div><!-- /wrap -->

<script>
const fmtIv = ([a, b]) => a === b ? `${a}` : `${a}–${b}`;
const esc = s => String(s).replace(/[&<>"']/g,
  c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const ivsHtml = (ivs, marked) => ivs.length
  ? ivs.map(iv => {
      const hit = (marked || []).map(m => m.commit);
      const has = hit.some(c => c >= iv[0] && c <= iv[1]);
      return `<span class="iv${has ? " mk" : ""}">${fmtIv(iv)}</span>`;
    }).join("")
  : '<span class="muted">(无)</span>';
const markerList = marked => (marked && marked.length)
  ? `<p class="tail">其中补档推算的提交:
       ${marked.map(m => `<span class="iv mk">${m.commit} · ${esc(m.source)}</span>`).join("")}
       (不是原始日志,由首尾快照与其余日志推算)</p>`
  : "";

// 页签切换
document.querySelectorAll(".tabs button").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tabs button").forEach(b => b.classList.toggle("on", b === btn));
    document.querySelectorAll(".pane").forEach(p => p.classList.toggle("on", p.id === btn.dataset.pane));
  });
});

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
    const mk = r.intersection_marked || [];
    html += `<table>
      <tr><th>文件</th><th>完全吻合的提交区间(按编号和值)</th><th>吻合提交数</th></tr>
      <tr><td>运单 CSV</td><td>${ivsHtml(r.waybill_csv.intervals, mk)}</td>
          <td>${r.waybill_csv.match_commits}</td></tr>
      <tr><td>签收 CSV</td><td>${ivsHtml(r.receipt_csv.intervals, mk)}</td>
          <td>${r.receipt_csv.match_commits}</td></tr>
      <tr><td><b>交集</b></td><td><b>${ivsHtml(r.intersection, mk)}</b></td><td>—</td></tr>
    </table>
    ${markerList(mk)}
    <p class="muted">比对范围:提交 ${r.start_commit}–${r.end_commit}(共 ${r.compare_points} 个比对点);
    已应用变更 ${r.changes_applied} 条;起点快照:运单 ${r.snapshot_rows.waybills} 行、
    签收 ${r.snapshot_rows.receipts} 行。
    ${mk.length ? "黄色区间/标签中的提交为补档推算(快照推算),并非原始日志。" : ""}</p>`;
  }
  detail.innerHTML = html;
  document.getElementById("result").scrollIntoView({ behavior: "smooth" });
}

// ---- 补档 ----
document.getElementById("rform").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = document.getElementById("rgo");
  const hint = document.getElementById("rhint");
  btn.disabled = true; hint.textContent = "推算中…";
  try {
    const fd = new FormData(e.target);
    const resp = await fetch("/api/recover", { method: "POST", body: fd });
    const data = await resp.json();
    renderRecover(data, resp.status);
  } catch (err) {
    renderRecover({ status: "recovery_failed", message: "请求失败: " + err }, 0);
  } finally {
    btn.disabled = false; hint.textContent = "";
  }
});

function renderRecover(data, httpStatus) {
  const banner = document.getElementById("rbanner");
  const detail = document.getElementById("rdetail");
  const dl = document.getElementById("rdl");
  document.getElementById("rresult").style.display = "block";
  dl.style.display = "none"; dl.innerHTML = "";
  let html = "";
  if (data.status === "recovered" && data.downloadable) {
    banner.className = "banner ok";
    banner.textContent = "补档成功:" + data.message;
    const s = data.summary;
    html += `<table>
      <tr><th>缺号提交</th><td>${s.missing_commit} · <span class="iv mk">${esc(s.marker)}</span></td></tr>
      <tr><th>推算净变更</th><td>${s.net_changes} 条
        (运单 ${s.by_table.waybills}、签收 ${s.by_table.receipts};
        新增 ${s.by_op.insert}、修改 ${s.by_op.update}、删除 ${s.by_op.delete})</td></tr>
      <tr><th>变更总数</th><td>原 ${s.original_changes} + 推算 ${s.net_changes}
        = ${s.total_changes}(上限 ${s.max_changes})</td></tr>
      <tr><th>完整重放</th><td>${s.replay_ok ? "补齐包可从起点快照完整重放到末快照 ✓" : "未通过"}</td></tr>
      <tr><th>末快照行数</th><td>运单 ${s.end_snapshot_rows.waybills} 行、
        签收 ${s.end_snapshot_rows.receipts} 行</td></tr>
    </table>
    <p class="muted">下载的补齐包保留了原事务,仅在缺号处插入一笔
      <code>"source": "${esc(s.marker)}"</code> 的推算提交。可把该包连同原两份 CSV 放到「来源核查」页复查。</p>`;
    const blob = new Blob([data.package_json], { type: "application/json;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    dl.innerHTML = `<a href="${url}" download="${esc(data.filename)}">下载补齐后的证据包 JSON</a>`;
    dl.style.display = "block";
  } else {
    banner.className = "banner err";
    banner.textContent = "补档失败,不提供下载:" + (data.message || ("HTTP " + httpStatus));
    html += `<p class="muted">原因:${esc(data.reason || data.message || "未知")}${data.code ? `(代码:${esc(data.code)})` : ""}</p>`;
  }
  detail.innerHTML = html;
  document.getElementById("rresult").scrollIntoView({ behavior: "smooth" });
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


# ---- 补档 ----

def _load_json_upload(raw: bytes, what: str):
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise InputError(f"{what}不是合法 JSON: {e}")


@app.post("/api/recover")
async def api_recover(
    evidence: UploadFile = File(...),
    end_snapshot: UploadFile = File(...),
):
    """用缺一笔的证据包 + 末次提交后的全量快照推算补齐包。

    成功:返回补齐后的证据包(可直接下载)+ 摘要;
    失败:说明原因,且不提供下载。
    """
    try:
        data = _load_json_upload(await evidence.read(), "证据包")
        end_data = _load_json_upload(await end_snapshot.read(), "末次快照")
    except InputError as e:
        return JSONResponse(
            {"status": "invalid_input", "message": str(e), "downloadable": False,
             "reason": str(e)}, status_code=400)
    try:
        outcome = recover(data, end_data)
    except InputError as e:  # 证据包结构非法(操作/表名/行结构等)
        return JSONResponse(
            {"status": "invalid_input", "message": str(e), "downloadable": False,
             "reason": str(e)}, status_code=400)
    except EvidenceError as e:  # 结构校验中抛出的证据不足
        return JSONResponse(
            {"status": "invalid_input", "message": str(e), "downloadable": False,
             "reason": str(e)}, status_code=400)
    except RecoveryError as e:
        return JSONResponse(
            {"status": "recovery_failed", "code": e.code, "message": str(e),
             "downloadable": False, "reason": str(e)}, status_code=422)

    package = outcome["package"]
    summary = outcome["summary"]
    return JSONResponse({
        "status": "recovered",
        "downloadable": True,
        "missing_commit": outcome["missing_commit"],
        "summary": summary,
        "message": (
            f"已按首尾快照推算补出提交 {outcome['missing_commit']}"
            f"({summary['marker']}):净变更 {summary['net_changes']} 条"
            f"(运单 {summary['by_table']['waybills']}、签收 {summary['by_table']['receipts']})。"
        ),
        "package": package,
        "package_json": json.dumps(package, ensure_ascii=False, indent=2),
        "filename": f"evidence.recovered.{outcome['missing_commit']}.json",
    })


@app.get("/api/sample/recovery-evidence.json", response_class=PlainTextResponse)
async def sample_recovery_evidence():
    broken, _, _ = build_recovery_sample()
    return PlainTextResponse(
        json.dumps(broken, ensure_ascii=False, indent=2),
        media_type="application/json; charset=utf-8")


@app.get("/api/sample/end-snapshot.json", response_class=PlainTextResponse)
async def sample_end_snapshot():
    _, end_snapshot, _ = build_recovery_sample()
    return PlainTextResponse(
        json.dumps(end_snapshot, ensure_ascii=False, indent=2),
        media_type="application/json; charset=utf-8")
