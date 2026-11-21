"""自测:核心场景、证据不足分支、增量计数一致性、5 万条变更性能、HTTP 接口。

运行:python3 -m tests.selftest   (在仓库根目录)
"""
import json
import random
import sys
import time

from app.core import (
    EvidenceError,
    InputError,
    load_receipt_csv,
    load_waybill_csv,
    parse_evidence,
    verify,
)
from app.sample import build_sample

FAILED = []


def check(name, cond, extra=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  {extra}" if extra else ""))
    if not cond:
        FAILED.append(name)


def expect_evidence_error(name, fn, needle):
    try:
        fn()
    except EvidenceError as e:
        check(name, needle in str(e), f"-> {e}")
        return
    except Exception as e:  # noqa: BLE001
        check(name, False, f"异常类型不对: {type(e).__name__}: {e}")
        return
    check(name, False, "未抛出 EvidenceError")


def run_verify(evidence_dict, wb_csv, rc_csv):
    ev = parse_evidence(json.loads(json.dumps(evidence_dict)))
    return verify(ev, load_waybill_csv(wb_csv.encode()), load_receipt_csv(rc_csv.encode()))


def t_sample():
    print("== 样例场景(提交 100..110)")
    from app.core import to_intervals
    evidence, wb_csv, rc_csv, expected = build_sample()
    r = run_verify(evidence, wb_csv, rc_csv)
    check("运单 CSV 吻合区间",
          r["waybill_csv"]["intervals"] == to_intervals(expected["waybill_points"]),
          str(r["waybill_csv"]["intervals"]))
    check("签收 CSV 吻合区间",
          r["receipt_csv"]["intervals"] == to_intervals(expected["receipt_points"]),
          str(r["receipt_csv"]["intervals"]))
    check("交集", r["intersection"] == expected["intersection"], str(r["intersection"]))
    check("变更条数", r["changes_applied"] == 12, str(r["changes_applied"]))


def t_counter_consistency():
    print("== 增量计数 vs 全量重算(逐提交核对)")
    from app.core import Engine, parse_evidence as pe
    evidence, wb_csv, rc_csv, _ = build_sample()
    ev = pe(json.loads(json.dumps(evidence)))
    eng = Engine(load_waybill_csv(wb_csv.encode()), load_receipt_csv(rc_csv.encode()))
    eng.load_snapshot(ev.wb_rows, ev.rc_rows)
    ok = (eng.mism_a, eng.mism_b) == eng.full_mismatches()
    for tx in ev.transactions:
        for i, ch in enumerate(tx.changes):
            eng.apply_change(ch, f"t{tx.commit}#{i}")
        ok = ok and (eng.mism_a, eng.mism_b) == eng.full_mismatches()
    check("每个提交后增量计数与全量重算一致", ok)


def t_no_common_source():
    print("== 交集为空 -> 所查范围内没有共同来源")
    evidence = {
        "start_commit": 200, "end_commit": 201,
        "snapshots": {
            "waybills": [{"单号": "W1", "状态": "已签收", "件数": 3}],
            "receipts": [{"签收号": "R1", "单号": "W1", "实收件数": 3}],
        },
        "transactions": [{"commit": 201, "changes": [
            {"table": "waybills", "op": "update", "key": "W1",
             "before": {"单号": "W1", "状态": "已签收", "件数": 3},
             "after": {"单号": "W1", "状态": "已签收", "件数": 4}},
            {"table": "receipts", "op": "update", "key": "R1",
             "before": {"签收号": "R1", "单号": "W1", "实收件数": 3},
             "after": {"签收号": "R1", "单号": "W1", "实收件数": 4}},
        ]}],
    }
    # 运单 CSV 只在 200 吻合;签收 CSV 只在 201 吻合
    r = run_verify(evidence, "单号,状态,件数\nW1,已签收,3\n", "签收号,单号,实收件数\nR1,W1,4\n")
    check("运单吻合点=[200]", r["waybill_csv"]["intervals"] == [[200, 200]], str(r["waybill_csv"]["intervals"]))
    check("签收吻合点=[201]", r["receipt_csv"]["intervals"] == [[201, 201]], str(r["receipt_csv"]["intervals"]))
    check("交集为空", r["intersection"] == [])


def t_evidence_errors():
    print("== 证据不足分支")
    evidence, wb_csv, rc_csv, _ = build_sample()

    gap = json.loads(json.dumps(evidence))
    gap["transactions"] = [t for t in gap["transactions"] if t["commit"] != 105]
    expect_evidence_error("日志缺号(中间缺 105)",
                          lambda: run_verify(gap, wb_csv, rc_csv), "日志缺号")

    trunc = json.loads(json.dumps(evidence))
    trunc["transactions"] = trunc["transactions"][:-2]  # 只到 108,end 仍为 110
    expect_evidence_error("日志缺号(尾部缺失)",
                          lambda: run_verify(trunc, wb_csv, rc_csv), "日志缺号")

    bad_before = json.loads(json.dumps(evidence))
    bad_before["transactions"][2]["changes"][0]["before"]["件数"] = 99  # 提交 103
    expect_evidence_error("前值对不上",
                          lambda: run_verify(bad_before, wb_csv, rc_csv), "前值对不上")

    beyond = json.loads(json.dumps(evidence))
    beyond["end_commit"] = 109  # 日志含 110,超出终止号
    expect_evidence_error("提交号超出终止号",
                          lambda: run_verify(beyond, wb_csv, rc_csv), "超出终止提交号")


def t_receipt_retarget():
    print("== 签收行改挂单号 + 运单状态翻转(多签收行)")
    evidence = {
        "start_commit": 300, "end_commit": 303,
        "snapshots": {
            "waybills": [{"单号": "A", "状态": "已签收", "件数": 1},
                         {"单号": "B", "状态": "运输中", "件数": 2}],
            "receipts": [{"签收号": "R1", "单号": "A", "实收件数": 1},
                         {"签收号": "R2", "单号": "A", "实收件数": 1},
                         {"签收号": "R3", "单号": "B", "实收件数": 2}],
        },
        "transactions": [
            {"commit": 301, "changes": [  # R3 改挂到已签收的 A -> 签收导出多出 R3
                {"table": "receipts", "op": "update", "key": "R3",
                 "before": {"签收号": "R3", "单号": "B", "实收件数": 2},
                 "after": {"签收号": "R3", "单号": "A", "实收件数": 2}}]},
            {"commit": 302, "changes": [  # B 变为已签收,但 B 名下已无签收行
                {"table": "waybills", "op": "update", "key": "B",
                 "before": {"单号": "B", "状态": "运输中", "件数": 2},
                 "after": {"单号": "B", "状态": "已签收", "件数": 2}}]},
            {"commit": 303, "changes": [  # 删除 R3 -> 签收导出恢复吻合
                {"table": "receipts", "op": "delete", "key": "R3",
                 "before": {"签收号": "R3", "单号": "A", "实收件数": 2}, "after": None}]},
        ],
    }
    r = run_verify(evidence, "单号,状态,件数\nA,已签收,1\n",
                   "签收号,单号,实收件数\nR1,A,1\nR2,A,1\n")
    check("运单吻合区间", r["waybill_csv"]["intervals"] == [[300, 301]],
          str(r["waybill_csv"]["intervals"]))
    check("签收吻合区间", r["receipt_csv"]["intervals"] == [[300, 300], [303, 303]],
          str(r["receipt_csv"]["intervals"]))
    check("交集=[300]", r["intersection"] == [[300, 300]], str(r["intersection"]))


def t_csv_validation():
    print("== CSV 输入校验")
    try:
        load_waybill_csv("单号,状态\nW1,已签收\n".encode())
        check("缺列报错", False)
    except InputError as e:
        check("缺列报错", "件数" in str(e), str(e))
    try:
        load_waybill_csv("单号,状态,件数\nW1,已签收,1\nW1,已签收,2\n".encode())
        check("重复键报错", False)
    except InputError as e:
        check("重复键报错", "重复" in str(e), str(e))
    d = load_waybill_csv("单号,状态,件数\nW1,已签收,3\n".encode())
    check("正常解析", d == {"W1": ("已签收", "3")}, str(d))
    d2 = load_waybill_csv(" 单号 ,状态,件数\nW1,已签收,3\n".encode())
    check("表头带空格可解析", d2 == {"W1": ("已签收", "3")}, str(d2))
    d3 = load_waybill_csv("单号,状态,件数\n".encode() and "单号,状态,件数\nW1,已签收,3\n".encode())
    check("BOM 头可解析", load_waybill_csv("单号,状态,件数\nW1,已签收,3\n".encode("utf-8-sig")) == d3)


def t_perf_50k():
    print("== 5 万条变更性能与一致性")
    rng = random.Random(42)
    n_wb, n_rc = 5000, 5000
    wb0 = {f"W{i:05d}": {"单号": f"W{i:05d}", "状态": "已签收" if i % 2 == 0 else "运输中",
                         "件数": rng.randint(1, 9)} for i in range(n_wb)}
    rc0 = {f"R{i:05d}": {"签收号": f"R{i:05d}", "单号": f"W{(i * 7) % n_wb:05d}",
                         "实收件数": rng.randint(1, 9)} for i in range(n_rc)}
    # 工作副本:生成变更日志的同时推进,保证 before 值与重放一致
    wb = {k: dict(v) for k, v in wb0.items()}
    rc = {k: dict(v) for k, v in rc0.items()}
    n_tx, per_tx = 500, 100  # 共 50000 条变更
    transactions = []
    for c in range(1, n_tx + 1):
        changes = []
        for _ in range(per_tx):
            if rng.random() < 0.5:
                k = f"W{rng.randrange(n_wb):05d}"
                before = dict(wb[k])
                after = dict(before)
                if rng.random() < 0.3:
                    after["状态"] = "运输中" if before["状态"] == "已签收" else "已签收"
                else:
                    after["件数"] = rng.randint(1, 9)
                wb[k] = after
                changes.append({"table": "waybills", "op": "update", "key": k,
                                "before": before, "after": after})
            else:
                k = f"R{rng.randrange(n_rc):05d}"
                before = dict(rc[k])
                after = dict(before)
                after["实收件数"] = rng.randint(1, 9)
                rc[k] = after
                changes.append({"table": "receipts", "op": "update", "key": k,
                                "before": before, "after": after})
        transactions.append({"commit": 1000 + c, "changes": changes})
    evidence = {"start_commit": 1000, "end_commit": 1000 + n_tx,
                "snapshots": {"waybills": list(wb0.values()), "receipts": list(rc0.values())},
                "transactions": transactions}

    t0 = time.perf_counter()
    ev = parse_evidence(json.loads(json.dumps(evidence)))
    # 目标取终点状态:最后一个比对点必吻合
    tgt_a = {k: (r["状态"], str(r["件数"])) for k, r in wb.items() if r["状态"] == "已签收"}
    tgt_b = {k: (r["单号"], str(r["实收件数"])) for k, r in rc.items()
             if r["单号"] in tgt_a}
    r = verify(ev, tgt_a, tgt_b)
    dt = time.perf_counter() - t0
    check("变更条数=50000", r["changes_applied"] == 50000, str(r["changes_applied"]))
    check("终点吻合(目标=终点导出)",
          r["waybill_csv"]["intervals"][-1][1] == 1000 + n_tx
          and r["receipt_csv"]["intervals"][-1][1] == 1000 + n_tx,
          f"运单末区间={r['waybill_csv']['intervals'][-1]} 签收末区间={r['receipt_csv']['intervals'][-1]}")
    print(f"  耗时 {dt:.2f}s(解析+重放 50000 条变更, 快照 10000 行)")
    check("性能可接受(<20s)", dt < 20, f"{dt:.2f}s")


def t_http():
    print("== HTTP 接口(TestClient)")
    try:
        from fastapi.testclient import TestClient
        from app.main import app
    except Exception as e:  # noqa: BLE001
        print(f"  [SKIP] fastapi 不可用: {e}")
        return
    client = TestClient(app)
    evidence, wb_csv, rc_csv, expected = build_sample()
    files = {
        "waybill_csv": ("waybills.csv", wb_csv.encode(), "text/csv"),
        "receipt_csv": ("receipts.csv", rc_csv.encode(), "text/csv"),
        "evidence": ("evidence.json", json.dumps(evidence, ensure_ascii=False).encode(),
                     "application/json"),
    }
    resp = client.post("/api/verify", files=files)
    check("样例核查 HTTP 200", resp.status_code == 200, str(resp.status_code))
    body = resp.json()
    check("样例核查结论 ok", body.get("status") == "ok", str(body.get("status")))
    check("样例交集正确", body["result"]["intersection"] == expected["intersection"],
          str(body["result"]["intersection"]))

    bad = json.loads(json.dumps(evidence))
    bad["transactions"] = [t for t in bad["transactions"] if t["commit"] != 107]
    files2 = dict(files)
    files2["evidence"] = ("evidence.json", json.dumps(bad, ensure_ascii=False).encode(),
                          "application/json")
    body2 = client.post("/api/verify", files=files2).json()
    check("缺号 -> insufficient_evidence", body2.get("status") == "insufficient_evidence",
          str(body2.get("status")))

    files3 = dict(files)
    files3["waybill_csv"] = ("waybills.csv", "单号,状态\nW1,已签收\n".encode(), "text/csv")
    resp3 = client.post("/api/verify", files=files3)
    check("坏 CSV -> 400 invalid_input",
          resp3.status_code == 400 and resp3.json().get("status") == "invalid_input",
          f"{resp3.status_code} {resp3.json().get('status')}")

    resp4 = client.get("/")
    check("操作页可访问", resp4.status_code == 200 and "货运数仓来源核对" in resp4.text)
    resp5 = client.get("/api/sample/evidence.json")
    check("样例证据包可下载", resp5.status_code == 200
          and resp5.json()["start_commit"] == 100)


def main():
    t_sample()
    t_counter_consistency()
    t_no_common_source()
    t_evidence_errors()
    t_receipt_retarget()
    t_csv_validation()
    t_perf_50k()
    t_http()
    print()
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {', '.join(FAILED)}")
        sys.exit(1)
    print("全部自测通过。")


if __name__ == "__main__":
    main()
