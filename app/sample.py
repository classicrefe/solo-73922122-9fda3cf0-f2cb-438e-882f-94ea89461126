"""构造一套可手工复核的样例:证据包 JSON + 两份导出 CSV。

场景:提交号 100..110,两份 CSV 都等于起点(提交 100)的导出结果。
各提交后两表的吻合情况(供自测断言):

  提交  运单导出吻合  签收导出吻合  说明
  100   ✓            ✓            起点
  101   ✗            ✗            WB0002 变为已签收,导出视图多出 WB0002/RC0003
  102   ✓            ✓            WB0002 改回运输中
  103   ✗            ✓            WB0001 件数 3->4(只影响运单导出)
  104   ✗            ✗            RC0001 实收件数 3->4
  105   ✓            ✗            WB0001 件数改回 3
  106   ✓            ✓            RC0001 实收件数改回 3
  107   ✗            ✗            新增 WB0009/RC0009
  108   ✓            ✓            删除 RC0009/WB0009
  109   ✗            ✗            WB0003 状态变为异常,导出视图少了 WB0003/RC0002
  110   ✓            ✓            WB0003 状态改回已签收

运单 CSV 吻合点 {100,102,105,106,108,110};签收 CSV 吻合点 {100,102,103,106,108,110};
交集 {100,102,106,108,110}。
"""

WB_ROWS_0 = [
    {"单号": "WB0001", "状态": "已签收", "件数": 3, "目的地": "上海"},
    {"单号": "WB0002", "状态": "运输中", "件数": 5, "目的地": "北京"},
    {"单号": "WB0003", "状态": "已签收", "件数": 2, "目的地": "广州"},
]

RC_ROWS_0 = [
    {"签收号": "RC0001", "单号": "WB0001", "实收件数": 3, "签收人": "张三"},
    {"签收号": "RC0002", "单号": "WB0003", "实收件数": 2, "签收人": "李四"},
    {"签收号": "RC0003", "单号": "WB0002", "实收件数": 5, "签收人": "王五"},
]

WB0009 = {"单号": "WB0009", "状态": "已签收", "件数": 1, "目的地": "杭州"}
RC0009 = {"签收号": "RC0009", "单号": "WB0009", "实收件数": 1, "签收人": "赵六"}


def _upd(table, key, before, **changes):
    after = dict(before)
    after.update(changes)
    return {"table": table, "op": "update", "key": key, "before": before, "after": after}


def build_sample():
    """返回 (evidence_dict, waybill_csv_str, receipt_csv_str, expected_dict)。"""
    wb1, wb2, wb3 = WB_ROWS_0
    rc1, rc2, rc3 = RC_ROWS_0
    transactions = [
        {"commit": 101, "changes": [_upd("waybills", "WB0002", wb2, 状态="已签收")]},
        {"commit": 102, "changes": [_upd("waybills", "WB0002", dict(wb2, 状态="已签收"), 状态="运输中")]},
        {"commit": 103, "changes": [_upd("waybills", "WB0001", wb1, 件数=4)]},
        {"commit": 104, "changes": [_upd("receipts", "RC0001", rc1, 实收件数=4)]},
        {"commit": 105, "changes": [_upd("waybills", "WB0001", dict(wb1, 件数=4), 件数=3)]},
        {"commit": 106, "changes": [_upd("receipts", "RC0001", dict(rc1, 实收件数=4), 实收件数=3)]},
        {"commit": 107, "changes": [
            {"table": "waybills", "op": "insert", "key": "WB0009", "before": None, "after": WB0009},
            {"table": "receipts", "op": "insert", "key": "RC0009", "before": None, "after": RC0009},
        ]},
        {"commit": 108, "changes": [
            {"table": "receipts", "op": "delete", "key": "RC0009", "before": RC0009, "after": None},
            {"table": "waybills", "op": "delete", "key": "WB0009", "before": WB0009, "after": None},
        ]},
        {"commit": 109, "changes": [_upd("waybills", "WB0003", wb3, 状态="异常")]},
        {"commit": 110, "changes": [_upd("waybills", "WB0003", dict(wb3, 状态="异常"), 状态="已签收")]},
    ]
    evidence = {
        "start_commit": 100,
        "end_commit": 110,
        "snapshots": {"waybills": WB_ROWS_0, "receipts": RC_ROWS_0},
        "transactions": transactions,
    }
    waybill_csv = "单号,状态,件数\nWB0001,已签收,3\nWB0003,已签收,2\n"
    receipt_csv = "签收号,单号,实收件数\nRC0001,WB0001,3\nRC0002,WB0003,2\n"
    expected = {
        "waybill_points": [100, 102, 105, 106, 108, 110],
        "receipt_points": [100, 102, 103, 106, 108, 110],
        "intersection": [[100, 100], [102, 102], [106, 106], [108, 108], [110, 110]],
    }
    return evidence, waybill_csv, receipt_csv, expected
