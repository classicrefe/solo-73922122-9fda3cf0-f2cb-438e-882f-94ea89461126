"""补档:缺一笔事务日志时,用首尾快照和其余日志推算并生成可重放的补齐包。

流程
----
1. 校验原证据包:提交号必须严格递增(重复/乱序直接拒绝),且
   ``start_commit+1 .. end_commit`` 中恰好缺一个提交号。
2. 前推:从起点快照出发,顺序应用缺号之前的日志,逐条核对每条变更的
   ``before``(同一笔里同一行改过多次也逐条核对),得到缺号前的两表状态。
3. 回推:从末次提交后的全量快照出发,逆序反做缺号之后的日志,逐条核对每条
   变更的 ``after``(同样逐条),得到缺号后的两表状态。
4. 比较缺号前后两表状态,把运单表和签收表的**净变更**合并成同一笔事务,
   插入原日志中,提交号标为 ``source = "快照推算"``。
5. 用补齐后的包从起点快照完整重放一遍,必须恰好落到末次提交后的全量快照。
   任何一步对不上、缺号数不为一、日志重复/乱序、补齐后超过变更上限,
   都抛 RecoveryError,由上层说明原因且不提供下载。
"""
from __future__ import annotations

import copy
from typing import Optional

from .core import (
    MAX_CHANGES,
    MARKER,
    RC_KEY,
    RC_TABLE,
    WB_KEY,
    WB_TABLE,
    EvidenceError,
    InputError,
    norm,
    norm_row,
    parse_evidence,
)


class RecoveryError(Exception):
    """补档失败(原因明确,不生成下载)。"""

    def __init__(self, message: str, code: str = "recovery_failed"):
        super().__init__(message)
        self.code = code


# ---- 原始行状态(保留原 JSON 值类型,便于原样写回补齐包) ----

_WB_NAMES = ("waybills", "运单表", "运单")
_RC_NAMES = ("receipts", "签收表", "签收")


def _pick_rows(holder: dict, names, what: str) -> list:
    for n in names:
        if n in holder:
            rows = holder[n]
            if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
                raise InputError(f"{what} 的 {n} 必须是对象数组")
            return rows
    raise InputError(f"{what} 缺少 {'/'.join(names)}")


def extract_end_snapshot(obj) -> tuple:
    """从上传的末快照 JSON 中取出 (运单行, 签收行)。

    支持 ``{"waybills": [...], "receipts": [...]}``、外面再包一层
    ``snapshots`` 以及中文表名。
    """
    if not isinstance(obj, dict):
        raise InputError("末次快照顶层必须是 JSON 对象")
    holder = obj
    inner = obj.get("snapshots")
    if isinstance(inner, dict):
        holder = inner
    wb = _pick_rows(holder, _WB_NAMES, "末次快照")
    rc = _pick_rows(holder, _RC_NAMES, "末次快照")
    return wb, rc


def _start_snapshot_rows(data: dict) -> tuple:
    snapshots = data.get("snapshots")
    if not isinstance(snapshots, dict):
        raise InputError("证据包缺少 snapshots 对象")
    wb = _pick_rows(snapshots, _WB_NAMES, "证据包起点快照")
    rc = _pick_rows(snapshots, _RC_NAMES, "证据包起点快照")
    return wb, rc


class RawState:
    """两张表的原始行状态:键归一化为字符串,值保留上传时的 JSON 结构。"""

    def __init__(self):
        self.tables = {WB_TABLE: {}, RC_TABLE: {}}

    def load(self, table: str, rows: list, key_col: str, label: str):
        store = self.tables[table]
        for row in rows:
            key = norm(row.get(key_col))
            if key is None:
                raise RecoveryError(
                    f"{label}存在缺少键列 {key_col} 的行", "snapshot_invalid")
            if key in store:
                raise RecoveryError(f"{label}键重复: {key}", "snapshot_invalid")
            store[key] = copy.deepcopy(row)

    def get(self, table: str, key: str):
        return self.tables[table].get(key)


# ---- 日志顺序与缺号 ----

def _check_order(parsed_txs, start: int, end: int) -> int:
    """校验提交号严格递增且都在范围内,返回唯一的缺号。"""
    seen_set = set()
    prev = None
    for tx in parsed_txs:
        c = tx.commit
        if c in seen_set:
            raise RecoveryError(
                f"日志重复: 提交号 {c} 出现多次", "log_duplicate")
        if c <= start or c > end:
            raise RecoveryError(
                f"日志提交号越界: {c} 不在 {start + 1}..{end} 范围内",
                "log_out_of_range")
        if prev is not None and c < prev:
            raise RecoveryError(
                f"日志乱序: 提交号 {c} 出现在 {prev} 之后", "log_out_of_order")
        seen_set.add(c)
        prev = c

    span = end - start
    missing_count = span - len(seen_set)
    if missing_count == 0:
        raise RecoveryError(
            "缺号数不为一: 日志已覆盖全部提交号,没有可补的缺号",
            "missing_count_not_one")
    if missing_count != 1:
        detail = ""
        if span <= 200000:  # 范围可控时列出缺号,便于说明原因
            missing = [c for c in range(start + 1, end + 1) if c not in seen_set]
            detail = ":" + "、".join(str(c) for c in missing[:20])
            if len(missing) > 20:
                detail += " …"
        raise RecoveryError(
            f"缺号数不为一: 缺失 {missing_count} 个提交号{detail}",
            "missing_count_not_one")
    # 恰好缺一个:用求和差直接定位,避免为大范围构造集合
    total = (start + 1 + end) * span // 2
    gap = total - sum(seen_set)
    if gap < start + 1 or gap > end or gap in seen_set:
        # 理论不可达;防御非法数字
        raise RecoveryError("缺号定位失败,证据包提交号异常", "missing_count_not_one")
    return gap


# ---- 前推 / 回推 / 重放 ----

def _row_norm(row) -> Optional[dict]:
    return norm_row(row) if row is not None else None


def _apply_forward(state: RawState, ch, raw_ch: dict, where: str):
    """按日志正向应用一条变更,并核对 before。"""
    store = state.tables[ch.table]
    key = ch.key
    cur = store.get(key)
    cur_n = _row_norm(cur)
    if ch.op == "insert":
        if cur is not None:
            raise RecoveryError(
                f"{where}: 前推核对前值,行值对不上(insert 前键已存在 "
                f"{ch.table}/{key})", "row_mismatch")
        store[key] = copy.deepcopy(raw_ch.get("after"))
    else:
        if cur is None:
            raise RecoveryError(
                f"{where}: 前推核对前值,行值对不上({ch.op} 前键不存在 "
                f"{ch.table}/{key})", "row_mismatch")
        if cur_n != ch.before:
            raise RecoveryError(
                f"{where}: 前推核对前值,行值对不上 {ch.table}/{key}: "
                f"日志前值={ch.before} 推算值={cur_n}", "row_mismatch")
        if ch.op == "delete":
            del store[key]
        else:
            store[key] = copy.deepcopy(raw_ch.get("after"))


def _apply_backward(state: RawState, ch, raw_ch: dict, where: str):
    """逆序反做一条变更,并核对 after。"""
    store = state.tables[ch.table]
    key = ch.key
    cur = store.get(key)
    cur_n = _row_norm(cur)
    if ch.op == "insert":
        if cur is None or cur_n != ch.after:
            raise RecoveryError(
                f"{where}: 回推核对后值,行值对不上 {ch.table}/{key}: "
                f"日志后值={ch.after} 推算值={cur_n}", "row_mismatch")
        del store[key]
    elif ch.op == "update":
        if cur is None or cur_n != ch.after:
            raise RecoveryError(
                f"{where}: 回推核对后值,行值对不上 {ch.table}/{key}: "
                f"日志后值={ch.after} 推算值={cur_n}", "row_mismatch")
        store[key] = copy.deepcopy(raw_ch.get("before"))
    else:  # delete 的反向:当前必须不存在,再插回 before 行
        if cur is not None:
            raise RecoveryError(
                f"{where}: 回推核对后值,行值对不上 {ch.table}/{key}: "
                f"该行在缺号后日志中已删除,但末快照中仍存在", "row_mismatch")
        store[key] = copy.deepcopy(raw_ch.get("before"))


def _net_changes(before: RawState, after: RawState) -> list:
    """两表净变更;每行至多一条,运单在前、签收在后,键排序保证可重复。"""
    changes = []
    for table, key_col in ((WB_TABLE, WB_KEY), (RC_TABLE, RC_KEY)):
        b = before.tables[table]
        a = after.tables[table]
        for key in sorted(set(b) | set(a)):
            br, ar = b.get(key), a.get(key)
            if br is None:
                changes.append({"table": table, "op": "insert", "key": key,
                                "before": None, "after": copy.deepcopy(ar)})
            elif ar is None:
                changes.append({"table": table, "op": "delete", "key": key,
                                "before": copy.deepcopy(br), "after": None})
            elif norm_row(br) != norm_row(ar):
                changes.append({"table": table, "op": "update", "key": key,
                                "before": copy.deepcopy(br),
                                "after": copy.deepcopy(ar)})
    return changes


def _replay_to_end(start_wb, start_rc, transactions, end_wb, end_rc):
    """把给定事务序列从起点快照完整重放,必须恰好落到末快照。"""
    state = RawState()
    state.load(WB_TABLE, start_wb, WB_KEY, "起点快照")
    state.load(RC_TABLE, start_rc, RC_KEY, "起点快照")

    end = RawState()
    end.load(WB_TABLE, end_wb, WB_KEY, "末次快照")
    end.load(RC_TABLE, end_rc, RC_KEY, "末次快照")

    for tx in transactions:
        raw_txs = tx.get("changes") if isinstance(tx, dict) else None
        if not isinstance(raw_txs, list):
            raise RecoveryError("补齐包事务结构异常", "replay_failed")
        for idx, raw_ch in enumerate(raw_txs):
            where = f"提交 {tx.get('commit')} 第 {idx + 1} 条变更"
            ch = _parse_one_change(raw_ch, where)
            _apply_forward(state, ch, raw_ch, where)

    diffs = []
    for table, key_col in ((WB_TABLE, WB_KEY), (RC_TABLE, RC_KEY)):
        cur, want = state.tables[table], end.tables[table]
        for key in sorted(set(cur) | set(want)):
            if _row_norm(cur.get(key)) != _row_norm(want.get(key)):
                diffs.append(f"{table}/{key}")
    if diffs:
        more = "" if len(diffs) <= 5 else f" 等 {len(diffs)} 处"
        raise RecoveryError(
            "补齐后无法完整重放到末快照: 行值对不上 " + "、".join(diffs[:5]) + more,
            "replay_failed")


def _parse_one_change(raw_ch, where):
    """复用 core 的变更解析规则(表名/操作/键/行结构)。"""
    from .core import _parse_change  # 延迟导入,避免公开 API 面扩大
    return _parse_change(raw_ch, where)


# ---- 主入口 ----

def recover(evidence_data: dict, end_snapshot_data: dict) -> dict:
    """根据缺一笔的证据包与末快照生成补齐包。

    返回 ``{"package": ..., "missing_commit": ..., "summary": {...}}``;
    失败抛 RecoveryError(说明原因)或 InputError(上传格式错误)。
    证据包内部结构非法(core.EvidenceError)统一转为 InputError,
    与 parse_evidence 的格式错误同一层级。
    """
    try:
        return _recover(evidence_data, end_snapshot_data)
    except EvidenceError as e:
        raise InputError(str(e)) from e


def _recover(evidence_data: dict, end_snapshot_data: dict) -> dict:
    ev = parse_evidence(evidence_data)  # 结构校验 + 归一化
    raw_txs = evidence_data["transactions"]
    if len(raw_txs) != len(ev.transactions):  # 防御
        raise InputError("证据包 transactions 解析不一致")

    end_wb, end_rc = extract_end_snapshot(end_snapshot_data)
    start_wb, start_rc = _start_snapshot_rows(evidence_data)

    # 1. 顺序 + 缺号
    gap = _check_order(ev.transactions, ev.start_commit, ev.end_commit)

    # 2. 前推:起点快照 + 缺号之前日志
    before = RawState()
    before.load(WB_TABLE, start_wb, WB_KEY, "起点快照")
    before.load(RC_TABLE, start_rc, RC_KEY, "起点快照")
    pre = [(p, r) for p, r in zip(ev.transactions, raw_txs) if p.commit < gap]
    for ptx, rtx in pre:
        raw_changes = rtx.get("changes") or []
        for idx, (pch, rch) in enumerate(zip(ptx.changes, raw_changes)):
            _apply_forward(before, pch, rch,
                           f"提交 {ptx.commit} 第 {idx + 1} 条变更")

    # 3. 回推:末快照 + 缺号之后日志(逆序)
    after = RawState()
    after.load(WB_TABLE, end_wb, WB_KEY, "末次快照")
    after.load(RC_TABLE, end_rc, RC_KEY, "末次快照")
    post = [(p, r) for p, r in zip(ev.transactions, raw_txs) if p.commit > gap]
    for ptx, rtx in reversed(post):
        raw_changes = rtx.get("changes") or []
        pairs = list(zip(ptx.changes, raw_changes))
        for idx, (pch, rch) in reversed(list(enumerate(pairs))):
            _apply_backward(after, pch, rch,
                            f"提交 {ptx.commit} 第 {idx + 1} 条变更")

    # 4. 净变更 = 缺号后的状态 - 缺号前的状态
    net = _net_changes(before, after)
    total_after = ev.total_changes + len(net)
    if total_after > MAX_CHANGES:
        raise RecoveryError(
            f"补齐后变更总数 {total_after} 超过原变更上限 {MAX_CHANGES} 条"
            f"(原有 {ev.total_changes} 条,推算新增 {len(net)} 条)",
            "change_limit_exceeded")

    # 5. 组装补齐包:保留原事务原样,把推算的一笔插到正确位置
    package = copy.deepcopy(evidence_data)
    synth = {"commit": gap, "source": MARKER, "changes": net}
    insert_at = sum(1 for p in ev.transactions if p.commit < gap)
    package["transactions"] = (copy.deepcopy(raw_txs[:insert_at])
                               + [synth]
                               + copy.deepcopy(raw_txs[insert_at:]))

    # 6. 用补齐后的包完整重放到末快照(最终把关)
    _replay_to_end(package["snapshots"].get("waybills", start_wb),
                   package["snapshots"].get("receipts", start_rc),
                   package["transactions"], end_wb, end_rc)

    by_table = {WB_TABLE: 0, RC_TABLE: 0}
    by_op = {"insert": 0, "update": 0, "delete": 0}
    for ch in net:
        by_table[ch["table"]] += 1
        by_op[ch["op"]] += 1
    summary = {
        "missing_commit": gap,
        "marker": MARKER,
        "net_changes": len(net),
        "by_table": by_table,
        "by_op": by_op,
        "original_changes": ev.total_changes,
        "total_changes": total_after,
        "max_changes": MAX_CHANGES,
        "replay_ok": True,
        "end_snapshot_rows": {"waybills": len(end_wb), "receipts": len(end_rc)},
    }
    return {"package": package, "missing_commit": gap, "summary": summary}
