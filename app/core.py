"""核对引擎:根据证据包重放提交,增量维护两个导出视图与目标 CSV 的差异计数。

设计要点
--------
- 导出视图(不物化,只维护差异计数):
    运单导出 = 运单表中 状态 == "已签收" 的行,投影 (单号, 状态, 件数),键为单号;
    签收导出 = 签收表中 单号 属于已签收运单的行,投影 (签收号, 单号, 实收件数),键为签收号。
- 只在起点(起始提交号)和每笔事务整体应用之后比对。
- 不为每次提交复制整表:对每条行变更只更新受影响的键,差异计数 O(1) 增量维护;
  运单签收状态翻转时,只触碰该单号名下的签收行(通过 单号 -> 签收号 索引)。
- 日志缺号(提交号不连续/缺失/超出终止号)或变更前值与当前状态对不上时,
  抛出 EvidenceError,由上层提示"证据不足"并停止核查。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

SIGNED_STATUS = "已签收"

WB_TABLE = "waybills"
RC_TABLE = "receipts"

WB_KEY = "单号"
WB_VALUE_COLS = ("状态", "件数")
RC_KEY = "签收号"
RC_VALUE_COLS = ("单号", "实收件数")

MAX_CHANGES = 50000  # 支持至多 5 万条变更

TABLE_ALIASES = {
    "waybills": WB_TABLE,
    "运单表": WB_TABLE,
    "运单": WB_TABLE,
    "receipts": RC_TABLE,
    "签收表": RC_TABLE,
    "签收": RC_TABLE,
}


class EvidenceError(Exception):
    """证据包无效或前后不一致(证据不足,停止核查)。"""


class InputError(Exception):
    """上传文件格式错误。"""


def norm(v) -> Optional[str]:
    """把 JSON/CSV 里的值归一化为可比较的字符串形式。

    CSV 单元格总是字符串,JSON 里可能是数字;统一成字符串后比较,
    例如 JSON 的 3 与 CSV 的 "3" 视为相等。None 保持 None。
    """
    if v is None:
        return None
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else repr(v)
    return str(v)


def norm_row(row: dict) -> dict:
    return {str(k): norm(v) for k, v in row.items()}


@dataclass
class Change:
    table: str          # WB_TABLE / RC_TABLE
    op: str             # insert / update / delete
    key: str
    before: Optional[dict]
    after: Optional[dict]


@dataclass
class Transaction:
    commit: int
    changes: list


@dataclass
class Evidence:
    start_commit: int
    end_commit: int
    wb_rows: list       # 起点快照:运单表(归一化后的行)
    rc_rows: list       # 起点快照:签收表
    transactions: list  # list[Transaction]
    total_changes: int


def parse_evidence(data) -> Evidence:
    """校验并解析证据包 JSON(已 json.loads 的对象)。"""
    if not isinstance(data, dict):
        raise InputError("证据包顶层必须是 JSON 对象")

    def _int_field(obj, name):
        v = obj.get(name)
        if isinstance(v, bool) or not isinstance(v, int):
            # 容忍字符串形式的数字
            if isinstance(v, str) and v.strip().lstrip("-").isdigit():
                return int(v.strip())
            raise InputError(f"证据包字段 {name} 必须是整数,实际为 {v!r}")
        return v

    start = _int_field(data, "start_commit")
    end = _int_field(data, "end_commit")
    if start > end:
        raise InputError(f"起始提交号 {start} 大于终止提交号 {end}")

    snapshots = data.get("snapshots")
    if not isinstance(snapshots, dict):
        raise InputError("证据包缺少 snapshots 对象")

    def _snap(*names):
        for n in names:
            if n in snapshots:
                rows = snapshots[n]
                if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
                    raise InputError(f"snapshots.{n} 必须是对象数组")
                return [norm_row(r) for r in rows]
        raise InputError(f"snapshots 缺少 {'/'.join(names)} 快照")

    wb_rows = _snap("waybills", "运单表", "运单")
    rc_rows = _snap("receipts", "签收表", "签收")

    txs_raw = data.get("transactions")
    if not isinstance(txs_raw, list):
        raise InputError("证据包缺少 transactions 数组")

    transactions = []
    total = 0
    for i, tx in enumerate(txs_raw):
        if not isinstance(tx, dict):
            raise InputError(f"transactions[{i}] 必须是对象")
        commit = _int_field(tx, "commit")
        changes_raw = tx.get("changes")
        if not isinstance(changes_raw, list):
            raise InputError(f"transactions[{i}].changes 必须是数组")
        changes = []
        for j, ch in enumerate(changes_raw):
            changes.append(_parse_change(ch, f"transactions[{i}].changes[{j}]"))
        total += len(changes)
        if total > MAX_CHANGES:
            raise InputError(f"变更总数超过上限 {MAX_CHANGES} 条")
        transactions.append(Transaction(commit=commit, changes=changes))

    return Evidence(start, end, wb_rows, rc_rows, transactions, total)


def _parse_change(ch, where: str) -> Change:
    if not isinstance(ch, dict):
        raise InputError(f"{where} 必须是对象")
    table = TABLE_ALIASES.get(ch.get("table"))
    if table is None:
        raise InputError(f"{where}.table 无法识别: {ch.get('table')!r}(支持 waybills/运单表、receipts/签收表)")
    op = ch.get("op")
    if op not in ("insert", "update", "delete"):
        raise InputError(f"{where}.op 必须是 insert/update/delete,实际为 {op!r}")
    before = ch.get("before")
    after = ch.get("after")
    key_col = WB_KEY if table == WB_TABLE else RC_KEY

    if op == "insert":
        if before is not None:
            raise EvidenceError(f"{where}: insert 的 before 应为空")
        if not isinstance(after, dict):
            raise EvidenceError(f"{where}: insert 缺少 after 行")
    elif op == "update":
        if not isinstance(before, dict) or not isinstance(after, dict):
            raise EvidenceError(f"{where}: update 需要 before 和 after 行")
    else:  # delete
        if not isinstance(before, dict):
            raise EvidenceError(f"{where}: delete 缺少 before 行")
        if after is not None:
            raise EvidenceError(f"{where}: delete 的 after 应为空")

    before_n = norm_row(before) if isinstance(before, dict) else None
    after_n = norm_row(after) if isinstance(after, dict) else None

    key = ch.get("key")
    key = norm(key) if key is not None else None
    for row, tag in ((before_n, "before"), (after_n, "after")):
        rk = row.get(key_col) if row else None
        if row is not None and rk is None:
            raise EvidenceError(f"{where}: {tag} 行缺少键列 {key_col}")
        if key is None:
            key = rk
        elif rk is not None and rk != key:
            raise EvidenceError(f"{where}: 记录键 {key} 与 {tag} 行的键 {rk} 不一致")
    if key is None:
        raise EvidenceError(f"{where}: 无法确定行键")

    return Change(table=table, op=op, key=key, before=before_n, after=after_n)


class Engine:
    """重放状态机:维护两张表当前状态 + 两个导出视图与目标 CSV 的差异计数。"""

    def __init__(self, tgt_a: dict, tgt_b: dict):
        # tgt_a: 单号 -> (状态, 件数);tgt_b: 签收号 -> (单号, 实收件数)
        self.tgt_a = tgt_a
        self.tgt_b = tgt_b
        self.waybills: dict = {}
        self.receipts: dict = {}
        self.signed: set = set()                 # 已签收运单单号
        self.receipts_by_waybill: dict = {}      # 单号 -> set(签收号)
        self.mism_a = 0
        self.mism_b = 0

    # ---- 快照装载 ----

    def load_snapshot(self, wb_rows, rc_rows):
        for row in wb_rows:
            k = row.get(WB_KEY)
            if k is None:
                raise EvidenceError(f"运单表起点快照存在缺少键列 {WB_KEY} 的行")
            if k in self.waybills:
                raise EvidenceError(f"运单表起点快照键重复: {k}")
            self.waybills[k] = row
            if row.get("状态") == SIGNED_STATUS:
                self.signed.add(k)
        for row in rc_rows:
            k = row.get(RC_KEY)
            if k is None:
                raise EvidenceError(f"签收表起点快照存在缺少键列 {RC_KEY} 的行")
            if k in self.receipts:
                raise EvidenceError(f"签收表起点快照键重复: {k}")
            self.receipts[k] = row
            self.receipts_by_waybill.setdefault(row.get("单号"), set()).add(k)
        self.mism_a = self._full_mism(self.current_view_a(), self.tgt_a)
        self.mism_b = self._full_mism(self.current_view_b(), self.tgt_b)

    # ---- 视图(仅用于初始化与自检,正常重放不调用) ----

    def current_view_a(self) -> dict:
        return {k: (self.waybills[k].get("状态"), self.waybills[k].get("件数"))
                for k in self.signed}

    def current_view_b(self) -> dict:
        return {k: (r.get("单号"), r.get("实收件数"))
                for k, r in self.receipts.items() if r.get("单号") in self.signed}

    @staticmethod
    def _full_mism(cur: dict, tgt: dict) -> int:
        m = 0
        for k, v in cur.items():
            if tgt.get(k) != v:
                m += 1
        for k in tgt:
            if k not in cur:
                m += 1
        return m

    def full_mismatches(self):
        """从头全量重算差异计数(供测试核对增量计数是否正确)。"""
        return (self._full_mism(self.current_view_a(), self.tgt_a),
                self._full_mism(self.current_view_b(), self.tgt_b))

    # ---- 变更应用 ----

    def apply_change(self, ch: Change, where: str):
        store = self.waybills if ch.table == WB_TABLE else self.receipts
        cur = store.get(ch.key)
        if ch.op == "insert":
            if cur is not None:
                raise EvidenceError(f"{where}: insert 前键已存在: {ch.table}/{ch.key}")
        else:
            if cur is None:
                raise EvidenceError(f"{where}: {ch.op} 前键不存在: {ch.table}/{ch.key}")
            if cur != ch.before:
                raise EvidenceError(
                    f"{where}: 前值对不上: {ch.table}/{ch.key} "
                    f"日志前值={ch.before} 当前值={cur}")
        new_row = ch.after if ch.op != "delete" else None
        if ch.table == WB_TABLE:
            self._apply_waybill(ch.key, cur, new_row)
        else:
            self._apply_receipt(ch.key, cur, new_row)

    def _apply_waybill(self, key, old, new):
        old_signed = old is not None and old.get("状态") == SIGNED_STATUS
        new_signed = new is not None and new.get("状态") == SIGNED_STATUS
        old_exp = (old.get("状态"), old.get("件数")) if old_signed else None
        new_exp = (new.get("状态"), new.get("件数")) if new_signed else None
        tgt = self.tgt_a.get(key)
        self.mism_a += (1 if new_exp != tgt else 0) - (1 if old_exp != tgt else 0)

        if old_signed != new_signed:
            # 该单号名下的签收行整体进入/退出签收导出视图
            for rk in self.receipts_by_waybill.get(key, ()):
                rrow = self.receipts[rk]
                rexp = (rrow.get("单号"), rrow.get("实收件数"))
                rtgt = self.tgt_b.get(rk)
                old_exp = rexp if old_signed else None
                new_exp = rexp if new_signed else None
                self.mism_b += (1 if new_exp != rtgt else 0) - (1 if old_exp != rtgt else 0)
            if new_signed:
                self.signed.add(key)
            else:
                self.signed.discard(key)

        if new is None:
            del self.waybills[key]
        else:
            self.waybills[key] = new

    def _apply_receipt(self, key, old, new):
        old_exp = None
        if old is not None and old.get("单号") in self.signed:
            old_exp = (old.get("单号"), old.get("实收件数"))
        new_exp = None
        if new is not None and new.get("单号") in self.signed:
            new_exp = (new.get("单号"), new.get("实收件数"))
        tgt = self.tgt_b.get(key)
        self.mism_b += (1 if new_exp != tgt else 0) - (1 if old_exp != tgt else 0)

        if old is not None:
            s = self.receipts_by_waybill.get(old.get("单号"))
            if s is not None:
                s.discard(key)
        if new is not None:
            self.receipts_by_waybill.setdefault(new.get("单号"), set()).add(key)
            self.receipts[key] = new
        else:
            del self.receipts[key]


# ---- 区间工具 ----

def to_intervals(points) -> list:
    """把离散的吻合提交号合并为闭区间列表 [[a, b], ...]。"""
    ivs = []
    for p in sorted(points):
        if ivs and p == ivs[-1][1] + 1:
            ivs[-1][1] = p
        else:
            ivs.append([p, p])
    return ivs


def intersect_intervals(a: list, b: list) -> list:
    i = j = 0
    out = []
    while i < len(a) and j < len(b):
        lo = max(a[i][0], b[j][0])
        hi = min(a[i][1], b[j][1])
        if lo <= hi:
            out.append([lo, hi])
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return out


def verify(ev: Evidence, tgt_a: dict, tgt_b: dict) -> dict:
    """主流程:装载快照 -> 在起点比对 -> 逐事务重放并在每个提交点比对。"""
    eng = Engine(tgt_a, tgt_b)
    eng.load_snapshot(ev.wb_rows, ev.rc_rows)

    match_a, match_b = [], []
    if eng.mism_a == 0:
        match_a.append(ev.start_commit)
    if eng.mism_b == 0:
        match_b.append(ev.start_commit)

    expected = ev.start_commit + 1
    for tx in ev.transactions:
        if tx.commit != expected:
            raise EvidenceError(
                f"日志缺号或乱序: 期望提交号 {expected}, 实际 {tx.commit}")
        if tx.commit > ev.end_commit:
            raise EvidenceError(
                f"日志缺号: 提交号 {tx.commit} 超出终止提交号 {ev.end_commit}")
        for idx, ch in enumerate(tx.changes):
            eng.apply_change(ch, f"提交 {tx.commit} 第 {idx + 1} 条变更")
        if eng.mism_a == 0:
            match_a.append(tx.commit)
        if eng.mism_b == 0:
            match_b.append(tx.commit)
        expected += 1

    if expected != ev.end_commit + 1:
        raise EvidenceError(
            f"日志缺号: 缺少提交 {expected}..{ev.end_commit} 的记录")

    iv_a = to_intervals(match_a)
    iv_b = to_intervals(match_b)
    return {
        "start_commit": ev.start_commit,
        "end_commit": ev.end_commit,
        "compare_points": ev.end_commit - ev.start_commit + 1,
        "changes_applied": ev.total_changes,
        "snapshot_rows": {"waybills": len(ev.wb_rows), "receipts": len(ev.rc_rows)},
        "waybill_csv": {"intervals": iv_a, "match_commits": len(match_a)},
        "receipt_csv": {"intervals": iv_b, "match_commits": len(match_b)},
        "intersection": intersect_intervals(iv_a, iv_b),
    }


# ---- CSV 读取 ----

def _read_csv_rows(raw: bytes, required_cols, key_col, what: str) -> dict:
    import csv
    import io

    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise InputError(f"{what} 不是有效的 UTF-8 文件: {e}")
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise InputError(f"{what} 为空或缺少表头")
    # 表头允许首尾空格:建立 去空格名 -> 原始名 映射,检查与取数用同一映射
    colmap = {}
    for f in reader.fieldnames:
        colmap.setdefault((f or "").strip(), f)
    missing = [c for c in required_cols if c not in colmap]
    if missing:
        raise InputError(
            f"{what} 缺少必需列: {', '.join(missing)}"
            f"(实际列: {', '.join(colmap)})")
    out = {}
    for ln, row in enumerate(reader, start=2):
        key = (row.get(colmap[key_col]) or "").strip()
        if not key:
            raise InputError(f"{what} 第 {ln} 行缺少键列 {key_col} 的值")
        if key in out:
            raise InputError(f"{what} 第 {ln} 行键 {key} 重复")
        out[key] = tuple((row.get(colmap[c]) or "") for c in required_cols if c != key_col)
    if not out:
        raise InputError(f"{what} 没有任何数据行")
    return out


def load_waybill_csv(raw: bytes) -> dict:
    """运单导出 CSV -> {单号: (状态, 件数)}"""
    return _read_csv_rows(raw, (WB_KEY,) + WB_VALUE_COLS, WB_KEY, "运单 CSV")


def load_receipt_csv(raw: bytes) -> dict:
    """签收导出 CSV -> {签收号: (单号, 实收件数)}"""
    return _read_csv_rows(raw, (RC_KEY,) + RC_VALUE_COLS, RC_KEY, "签收 CSV")
