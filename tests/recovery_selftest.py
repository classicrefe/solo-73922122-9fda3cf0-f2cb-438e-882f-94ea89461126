"""补档自测:端到端补档、逐条核对、失败分支、标记透传、HTTP 接口。

运行:python3 -m tests.selftest   (在仓库根目录,与主自测一起跑)
"""
import copy
import json

from app.core import (
    EvidenceError,
    MARKER,
    load_receipt_csv,
    load_waybill_csv,
    parse_evidence,
    verify,
)
from app.recovery import RecoveryError, recover
from app.sample import build_recovery_sample, build_sample

FAILED = []


def check(name, cond, extra=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  {extra}" if extra else ""))
    if not cond:
        FAILED.append(name)


def expect_recovery_error(name, fn, needle, code=None):
    try:
        fn()
    except RecoveryError as e:
        ok = needle in str(e) and (code is None or e.code == code)
        check(name, ok, f"-> [{e.code}] {e}")
        return
    except Exception as e:  # noqa: BLE001
        check(name, False, f"异常类型不对: {type(e).__name__}: {e}")
        return
    check(name, False, "未抛出 RecoveryError")


def _verify_package(package, wb_csv=None, rc_csv=None):
    _, default_wb, default_rc, _ = build_sample()
    ev = parse_evidence(copy.deepcopy(package))
    return verify(ev,
                  load_waybill_csv((wb_csv or default_wb).encode()),
                  load_receipt_csv((rc_csv or default_rc).encode()))


def t_recover_end_to_end():
    print("== 补档端到端:缺 102 -> 推算补出 -> 重放到末快照")
    broken, end, expected_net = build_recovery_sample()
    out = recover(copy.deepcopy(broken), copy.deepcopy(end))
    pkg = out["package"]
    s = out["summary"]
    check("缺号定位=102", out["missing_commit"] == 102, str(out["missing_commit"]))
    check("净变更条数", s["net_changes"] == expected_net, str(s["net_changes"]))
    check("补齐后重放成功", s["replay_ok"] is True)
    # 原事务原样保留且顺序未变,只多出推算的一笔
    orig = [t for t in broken["transactions"]]
    new = [t for t in pkg["transactions"] if t.get("source") != MARKER]
    check("原事务原样保留", new == orig)
    synth = [t for t in pkg["transactions"] if t.get("source") == MARKER]
    check("恰好插入一笔推算提交", len(synth) == 1 and synth[0]["commit"] == 102,
          str(synth))
    commits = [t["commit"] for t in pkg["transactions"]]
    check("提交号连续", commits == list(range(101, 111)), str(commits))
    # 补齐包能被核查引擎直接消费
    r = _verify_package(pkg)
    check("补齐包可被核查", r["intersection"] == [[100, 100], [102, 102], [106, 106],
                                            [108, 108], [110, 110]], str(r["intersection"]))
    check("推算提交标为快照推算",
          {"commit": 102, "source": MARKER} in r["intersection_marked"],
          str(r["intersection_marked"]))


def t_missing_commit_variants():
    print("== 缺号位置:首笔 / 末笔 / 起点范围内")
    evidence, wb, rc, _ = build_sample()

    for gap, label in ((101, "缺首笔 101"), (110, "缺末笔 110")):
        broken = copy.deepcopy(evidence)
        # 用原样例的真实终点快照(提交 110 后 = 起点状态)
        end_snapshot = {"waybills": copy.deepcopy(copy.deepcopy(broken)["snapshots"]["waybills"]),
                        "receipts": copy.deepcopy(broken["snapshots"]["receipts"])}
        broken["transactions"] = [t for t in broken["transactions"] if t["commit"] != gap]
        try:
            out = recover(broken, end_snapshot)
            check(label, out["missing_commit"] == gap
                  and out["summary"]["replay_ok"], f"-> {out['missing_commit']}")
            _verify_package(out["package"])
        except RecoveryError as e:
            # 缺首/末笔的净变更可能因末快照与起点一致而为空,也可能非空;都算合法
            check(label, False, f"-> {e}")


def t_net_both_tables_and_multi_row():
    print("== 同一笔里两表净变更、单行多次修改逐条核对")
    start_wb = [{"单号": "W1", "状态": "已签收", "件数": 1},
                {"单号": "W2", "状态": "运输中", "件数": 2}]
    tx101 = {"commit": 101, "changes": [
        {"table": "waybills", "op": "update", "key": "W1",
         "before": {"单号": "W1", "状态": "已签收", "件数": 1},
         "after": {"单号": "W1", "状态": "已签收", "件数": 2}},
        {"table": "waybills", "op": "update", "key": "W1",
         "before": {"单号": "W1", "状态": "已签收", "件数": 2},
         "after": {"单号": "W1", "状态": "已签收", "件数": 3}},
        {"table": "receipts", "op": "insert", "key": "R1",
         "before": None, "after": {"签收号": "R1", "单号": "W1", "实收件数": 3}},
    ]}
    tx102 = {"commit": 102, "changes": [
        {"table": "waybills", "op": "update", "key": "W1",
         "before": {"单号": "W1", "状态": "已签收", "件数": 3},
         "after": {"单号": "W1", "状态": "已签收", "件数": 4}},
        {"table": "waybills", "op": "update", "key": "W2",
         "before": {"单号": "W2", "状态": "运输中", "件数": 2},
         "after": {"单号": "W2", "状态": "已签收", "件数": 2}},
        {"table": "receipts", "op": "update", "key": "R1",
         "before": {"签收号": "R1", "单号": "W1", "实收件数": 3},
         "after": {"签收号": "R1", "单号": "W1", "实收件数": 4}},
    ]}
    evidence = {"start_commit": 100, "end_commit": 102,
                "snapshots": {"waybills": start_wb, "receipts": []},
                "transactions": [tx101, tx102]}
    end_snapshot = {"waybills": [
        {"单号": "W1", "状态": "已签收", "件数": 4},
        {"单号": "W2", "状态": "已签收", "件数": 2}],
        "receipts": [{"签收号": "R1", "单号": "W1", "实收件数": 4}]}
    # 缺 102:净变更应为 W1 3->4、W2 运输中->已签收、R1 3->4 = 3 条
    broken = copy.deepcopy(evidence)
    broken["transactions"] = [tx101]
    out = recover(broken, end_snapshot)
    check("两表净变更条数=3", out["summary"]["net_changes"] == 3,
          str(out["summary"]))
    check("按表统计", out["summary"]["by_table"] == {"waybills": 2, "receipts": 1},
          str(out["summary"]["by_table"]))

    # 缺 101(101 里 W1 改了两次):推算只应有一行净变更 W1 1->3,加 R1 新增
    broken2 = copy.deepcopy(evidence)
    broken2["transactions"] = [tx102]
    # 末快照同终点;101 的净变更 = W1 1->3、R1 新增
    out2 = recover(broken2, end_snapshot)
    check("多次修改行合并为一条净变更", out2["summary"]["net_changes"] == 2,
          str(out2["summary"]))
    upd = [c for c in [t for t in out2["package"]["transactions"]
                       if t.get("source") == MARKER][0]["changes"]
           if c["table"] == "waybills"]
    check("净变更合并前后值正确",
          len(upd) == 1 and upd[0]["before"]["件数"] == 1 and upd[0]["after"]["件数"] == 3,
          str(upd))

    # 前推/回推逐条核对:篡改 101 里 W1 第二次修改的 before -> 必须报行值对不上
    bad = copy.deepcopy(evidence)
    bad["transactions"] = [copy.deepcopy(tx101)]   # 缺 102
    bad["transactions"][0]["changes"][1]["before"]["件数"] = 99
    expect_recovery_error(
        "同一笔内多次修改,前值逐条核对",
        lambda: recover(bad, end_snapshot), "前推核对前值", "row_mismatch")

    # 篡改缺号之后日志的 after(回推核对):缺 102,只给 101 与 103
    bad2 = copy.deepcopy(evidence)
    bad2["end_commit"] = 103
    tx103 = {"commit": 103, "changes": [
        {"table": "waybills", "op": "update", "key": "W1",
         "before": {"单号": "W1", "状态": "已签收", "件数": 4},
         "after": {"单号": "W1", "状态": "已签收", "件数": 4}}]}
    bad2["transactions"] = [copy.deepcopy(tx101), tx103]   # 缺 102
    # 末快照中 W1 件数为 4;把 103 日志的 after 篡改为 5 -> 回推时对不上
    tx103["changes"][0]["after"]["件数"] = 5
    tx103["changes"][0]["before"]["件数"] = 5
    expect_recovery_error(
        "缺号之后日志后值逐条核对",
        lambda: recover(bad2, end_snapshot), "回推核对后值", "row_mismatch")


def t_failure_branches():
    print("== 补档失败分支(均不给下载)")
    broken, end, _ = build_recovery_sample()

    # 缺号数 > 1
    two_missing = copy.deepcopy(broken)
    two_missing["transactions"] = [t for t in two_missing["transactions"]
                                   if t["commit"] not in (105, 106)]
    expect_recovery_error("缺两个号", lambda: recover(two_missing, end),
                          "缺号数不为一", "missing_count_not_one")

    # 缺号数 = 0
    full, _, _, _ = build_sample()
    expect_recovery_error("不缺号", lambda: recover(full, end),
                          "缺号数不为一", "missing_count_not_one")

    # 日志重复
    dup = copy.deepcopy(broken)
    dup["transactions"].insert(5, copy.deepcopy(dup["transactions"][0]))
    expect_recovery_error("日志重复", lambda: recover(dup, end),
                          "日志重复", "log_duplicate")

    # 日志乱序(交换两笔,提交号仍齐全)
    swapped = copy.deepcopy(full)
    swapped["transactions"][2], swapped["transactions"][3] = \
        swapped["transactions"][3], swapped["transactions"][2]
    expect_recovery_error("日志乱序", lambda: recover(swapped, end),
                          "日志乱序", "log_out_of_order")

    # 起点快照与缺号前日志的前值对不上(提交 101 的 before 被篡改)
    wrong_before = copy.deepcopy(broken)
    wrong_before["transactions"][0]["changes"][0]["before"]["件数"] = 99
    expect_recovery_error("前推行值对不上", lambda: recover(wrong_before, end),
                          "前推核对前值", "row_mismatch")

    # 末快照与缺号后日志对不上(回推)
    wrong_end = copy.deepcopy(end)
    wrong_end["waybills"][0]["件数"] = 999
    expect_recovery_error("末快照与日志对不上", lambda: recover(broken, wrong_end),
                          "回推核对后值", "row_mismatch")

    # 补齐后超过原变更上限的精确用例见 t_change_limit


def t_noop_missing_commit():
    print("== 缺号前后状态相同 -> 推算为空事务,仍可重放")
    # 缺 103,但 103 只是把 WB0001 件数 3->4 且 105 又改回;若两端都不含 103 的净效果?
    # 构造:起点与末快照完全一致,日志除缺号外均为空操作 -> 净变更为 0。
    wb0 = [{"单号": "W1", "状态": "已签收", "件数": 1}]
    tx = {"table": "waybills", "op": "update", "key": "W1",
          "before": {"单号": "W1", "状态": "已签收", "件数": 1},
          "after": {"单号": "W1", "状态": "已签收", "件数": 1}}
    evidence = {"start_commit": 10, "end_commit": 13,
                "snapshots": {"waybills": copy.deepcopy(wb0), "receipts": []},
                "transactions": [{"commit": 11, "changes": [copy.deepcopy(tx)]},
                                 {"commit": 13, "changes": [copy.deepcopy(tx)]}]}
    end_snapshot = {"waybills": copy.deepcopy(wb0), "receipts": []}
    out = recover(evidence, end_snapshot)
    check("缺 12 定位", out["missing_commit"] == 12, str(out["missing_commit"]))
    check("净变更为空", out["summary"]["net_changes"] == 0, str(out["summary"]))
    synth = [t for t in out["package"]["transactions"] if t.get("source") == MARKER]
    check("空事务仍带标记", len(synth) == 1 and synth[0]["changes"] == [])
    check("空推算仍可重放", out["summary"]["replay_ok"])


def t_evidence_invalid_becomes_input_error():
    print("== 证据包结构非法 -> InputError(与格式错误同一层级)")
    from app.core import InputError
    broken, end, _ = build_recovery_sample()
    bad = copy.deepcopy(broken)
    bad["transactions"][0]["changes"][0]["op"] = "upsert"  # 非法操作
    try:
        recover(bad, end)
        check("非法操作 -> InputError", False, "未抛出异常")
    except InputError as e:
        check("非法操作 -> InputError", "op" in str(e), str(e))
    except Exception as e:  # noqa: BLE001
        check("非法操作 -> InputError", False, f"{type(e).__name__}: {e}")


def t_change_limit():
    print("== 补齐后超出原变更上限 -> 拒绝")
    # 构造原有 49999 条变更,缺号 150 前后两表各差一行 -> 推算净变更 2 条,
    # 补齐后共 50001 条 > 50000。
    wb0 = [{"单号": "W1", "状态": "已签收", "件数": 1},
           {"单号": "W2", "状态": "已签收", "件数": 1}]
    rc0 = [{"签收号": "R1", "单号": "W1", "实收件数": 1}]
    noop = {"table": "waybills", "op": "update", "key": "W1",
            "before": {"单号": "W1", "状态": "已签收", "件数": 1},
            "after": {"单号": "W1", "状态": "已签收", "件数": 1}}
    # start=100,end=200 -> 100 个事务槽,缺 150;其余 99 个各 1 条空操作
    txs = [{"commit": c, "changes": [copy.deepcopy(noop)]}
           for c in range(101, 201) if c != 150]
    cur = sum(len(t["changes"]) for t in txs)
    txs[0]["changes"].extend([copy.deepcopy(noop)] * (49999 - cur))
    assert sum(len(t["changes"]) for t in txs) == 49999
    evidence = {"start_commit": 100, "end_commit": 200,
                "snapshots": {"waybills": copy.deepcopy(wb0),
                              "receipts": copy.deepcopy(rc0)},
                "transactions": txs}
    # 末快照比起点少了 W2 与 R1,且缺号后日志未解释这两处删除:
    # 于是缺号前的状态仍含两行、缺号后的状态不含 -> 推算净变更为 2 条删除。
    end_snapshot = {"waybills": [{"单号": "W1", "状态": "已签收", "件数": 1}],
                    "receipts": []}
    try:
        recover(evidence, end_snapshot)
        check("补齐后超过 50000", False, "未抛出 RecoveryError")
    except RecoveryError as e:
        check("补齐后超过 50000", "超过原变更上限" in str(e)
              and e.code == "change_limit_exceeded", f"-> [{e.code}] {e}")


def t_change_limit_just_under():
    print("== 补齐后恰好 50000 条 -> 允许")
    wb0 = [{"单号": "W1", "状态": "已签收", "件数": 1},
           {"单号": "W2", "状态": "已签收", "件数": 1}]
    noop = {"table": "waybills", "op": "update", "key": "W1",
            "before": {"单号": "W1", "状态": "已签收", "件数": 1},
            "after": {"单号": "W1", "状态": "已签收", "件数": 1}}
    txs = [{"commit": c, "changes": [copy.deepcopy(noop)]}
           for c in range(101, 201) if c != 150]
    cur = sum(len(t["changes"]) for t in txs)
    txs[0]["changes"].extend([copy.deepcopy(noop)] * (49999 - cur))
    evidence = {"start_commit": 100, "end_commit": 200,
                "snapshots": {"waybills": copy.deepcopy(wb0), "receipts": []},
                "transactions": txs}
    end_snapshot = {"waybills": [{"单号": "W1", "状态": "已签收", "件数": 1}],
                    "receipts": []}
    out = recover(evidence, end_snapshot)  # 净变更 1 条删除 -> 共 50000
    check("50000 条边界允许", out["summary"]["total_changes"] == 50000
          and out["summary"]["replay_ok"], str(out["summary"]["total_changes"]))


def t_marker_in_verify():
    print("== 核查页透传「快照推算」标记")
    from app.core import Engine
    broken, end, _ = build_recovery_sample()
    out = recover(broken, end)
    r = _verify_package(out["package"])
    check("交集含推算提交且带标记",
          {"commit": 102, "source": MARKER} in r["intersection_marked"])
    # 普通证据包不应出现标记
    evidence, wb, rc, _ = build_sample()
    r2 = _verify_package(evidence)
    check("原包无标记", r2["intersection_marked"] == [], str(r2["intersection_marked"]))

    # 若推算点不出现在交集里,不应标错别处
    strict_csv = "单号,状态,件数\nWB0001,已签收,3\nWB0003,已签收,2\n"
    # 102 处 WB0002 是运输中,不影响导出;构造只看 100 的签收 CSV?保持原样即可
    r3 = _verify_package(out["package"])
    marked_commits = {m["commit"] for m in r3["intersection_marked"]}
    in_inter = any(lo <= 102 <= hi for lo, hi in r3["intersection"])
    check("标记只在交集内出现", (not in_inter) or 102 in marked_commits)


def t_order_missing_end_snapshot_shapes():
    print("== 末快照结构兼容 + 输入错误")
    broken, end, _ = build_recovery_sample()
    # 外层包一层 snapshots
    wrapped = {"snapshots": copy.deepcopy(end)}
    out = recover(copy.deepcopy(broken), wrapped)
    check("末快照外层 snapshots 兼容", out["summary"]["replay_ok"])
    # 中文表名
    zh = {"运单表": copy.deepcopy(end["waybills"]), "签收表": copy.deepcopy(end["receipts"])}
    out2 = recover(copy.deepcopy(broken), zh)
    check("末快照中文表名兼容", out2["summary"]["replay_ok"])
    # 末快照缺键重复
    dup_end = {"waybills": copy.deepcopy(end["waybills"])
               + [copy.deepcopy(end["waybills"][0])], "receipts": []}
    expect_recovery_error("末快照键重复", lambda: recover(broken, dup_end),
                          "键重复", "snapshot_invalid")


def t_http():
    print("== 补档 HTTP 接口")
    try:
        from fastapi.testclient import TestClient
        from app.main import app
    except Exception as e:  # noqa: BLE001
        print(f"  [SKIP] fastapi 不可用: {e}")
        return
    client = TestClient(app)
    broken, end, _ = build_recovery_sample()

    resp = client.post("/api/recover", files={
        "evidence": ("e.json", json.dumps(broken, ensure_ascii=False).encode()),
        "end_snapshot": ("s.json", json.dumps(end, ensure_ascii=False).encode()),
    })
    body = resp.json()
    check("补档 HTTP 200", resp.status_code == 200, str(resp.status_code))
    check("补档成功且可下载", body.get("status") == "recovered"
          and body.get("downloadable") is True, str(body.get("status")))
    check("返回补齐包", isinstance(body.get("package_json"), str)
          and body["package"]["transactions"][1].get("source") == MARKER)
    pkg = body["package_json"]
    wb = client.get("/api/sample/waybills.csv").text
    rc = client.get("/api/sample/receipts.csv").text
    v = client.post("/api/verify", files={
        "waybill_csv": ("w.csv", wb.encode()), "receipt_csv": ("r.csv", rc.encode()),
        "evidence": ("p.json", pkg.encode())}).json()
    check("补档后重新核查 ok", v["status"] == "ok", str(v.get("status")))
    check("重新核查显示标记",
          {"commit": 102, "source": MARKER} in v["result"]["intersection_marked"])

    # 缺两个号 -> 422 + 不给下载
    two = copy.deepcopy(broken)
    two["transactions"] = [t for t in two["transactions"] if t["commit"] not in (105, 106)]
    resp2 = client.post("/api/recover", files={
        "evidence": ("e.json", json.dumps(two).encode()),
        "end_snapshot": ("s.json", json.dumps(end).encode())})
    b2 = resp2.json()
    check("缺两号 -> 422", resp2.status_code == 422, str(resp2.status_code))
    check("缺两号不给下载", b2.get("downloadable") is False and "package" not in b2,
          str(b2.keys()))
    check("缺两号说明原因", "缺号数不为一" in b2.get("reason", ""), b2.get("reason", ""))

    # 重复日志
    dup = copy.deepcopy(broken)
    dup["transactions"].insert(3, copy.deepcopy(dup["transactions"][0]))
    resp3 = client.post("/api/recover", files={
        "evidence": ("e.json", json.dumps(dup).encode()),
        "end_snapshot": ("s.json", json.dumps(end).encode())})
    check("重复日志 -> 422 且不给下载",
          resp3.status_code == 422 and resp3.json().get("downloadable") is False
          and "日志重复" in resp3.json().get("reason", ""),
          str(resp3.json().get("reason")))

    # 非法 JSON
    resp4 = client.post("/api/recover", files={
        "evidence": ("e.json", b"{not json"),
        "end_snapshot": ("s.json", json.dumps(end).encode())})
    check("非法 JSON -> 400", resp4.status_code == 400
          and resp4.json().get("status") == "invalid_input", str(resp4.status_code))

    # 证据包结构非法(op 不认识) -> 400,不给下载
    bad_struct = copy.deepcopy(broken)
    bad_struct["transactions"][0]["changes"][0]["op"] = "upsert"
    resp5 = client.post("/api/recover", files={
        "evidence": ("e.json", json.dumps(bad_struct).encode()),
        "end_snapshot": ("s.json", json.dumps(end).encode())})
    check("结构非法 -> 400 不给下载",
          resp5.status_code == 400 and resp5.json().get("downloadable") is False,
          str(resp5.status_code))

    # 末快照与日志对不上 -> 422,原因指明行值对不上
    wrong_end = copy.deepcopy(end)
    wrong_end["waybills"][0]["件数"] = 999
    resp6 = client.post("/api/recover", files={
        "evidence": ("e.json", json.dumps(broken).encode()),
        "end_snapshot": ("s.json", json.dumps(wrong_end).encode())})
    check("末快照对不上 -> 422",
          resp6.status_code == 422 and "行值对不上" in resp6.json().get("reason", "")
          and resp6.json().get("downloadable") is False,
          str(resp6.json().get("reason")))

    # 页面与样例
    page = client.get("/")
    check("页面含补档入口", "补档" in page.text and "/api/recover" in page.text)
    check("补档样例可下载",
          client.get("/api/sample/recovery-evidence.json").status_code == 200
          and client.get("/api/sample/end-snapshot.json").status_code == 200)


ALL = [
    t_recover_end_to_end,
    t_missing_commit_variants,
    t_net_both_tables_and_multi_row,
    t_failure_branches,
    t_noop_missing_commit,
    t_evidence_invalid_becomes_input_error,
    t_change_limit,
    t_change_limit_just_under,
    t_marker_in_verify,
    t_order_missing_end_snapshot_shapes,
    t_http,
]


def main():
    for fn in ALL:
        fn()
    print()
    if FAILED:
        print(f"补档自测失败 {len(FAILED)} 项: {', '.join(FAILED)}")
        return 1
    print("补档自测全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
