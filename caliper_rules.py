# -*- coding: utf-8 -*-
"""
工况规则引擎。

只消费 caliper_core.load() 产出的 Measurement，不重新解析 MDF、不直接调 asammdf。
输出 EvaluationResult（含三态），供界面与可复现报告使用。

运行:
  "python" caliper_rules.py --self-test
  "python" caliper_rules.py --self-test --bad-eval     # 变异测试
  "python" caliper_rules.py --dat "<你的数据目录>\\P挡原地踩油门.dat" --rules DAT/Gear_Dat.json
  "python" caliper_rules.py --dat "<你的数据目录>\\行车中驾驶模式切换.dat" --rules DAT/Creep_Dat.json
  "python" caliper_rules.py --self-test-edit     # 规则文件读写与校验自检
  "python" caliper_rules.py --self-test-transition  # 跳变条件自检

规则集在 rules/ 子目录（DAT/ / BLF/ / templates/），--rules 传相对 rules/ 的路径。

规则文件由界面编辑器写（save_ruleset），人不用手改 JSON：
校验不过就拒存、旧版备份 .bak、版本号 +1、字段顺序与编码固定。

三态：hit / no_hit / not_evaluated。
缺信号不报错、不中断、不计入命中统计，且绝不能折叠成 no_hit。
"""
import argparse
import ast
import hashlib
import json
import os
import re
import shutil
import sys
from dataclasses import asdict, dataclass

import numpy as np

import caliper_core

ROOT = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
        else os.path.dirname(os.path.abspath(__file__)))  # 打包后 exe 所在目录即数据根目录
OUT_DIR = os.path.join(ROOT, "out")
RULES_DIR = os.path.join(ROOT, "rules")
_SHAPE_WINDOW = False   # 仅变异测试开关：True=让窗口条件错误地吃时长参数
_NO_EXPAND = False      # 变异：标注不外扩采样点


def list_rulesets(skip_dirs=("templates",)):
    """递归列 rules/ 下所有规则集 json，返回相对路径（如 DAT/Gear_Dat.json）。

    规则集可能分在 DAT/ / BLF/ 子目录；只扫顶层会漏。默认跳过 templates/（模板非规则集）。
    """
    found = []
    if os.path.isdir(RULES_DIR):
        for root, dirs, files in os.walk(RULES_DIR):
            dirs[:] = sorted(d for d in dirs if d not in skip_dirs)
            for name in files:
                if name.endswith(".json"):
                    found.append(os.path.relpath(os.path.join(root, name), RULES_DIR).replace(os.sep, "/"))
    return sorted(found)


def first_ruleset():
    """第一个规则集（递归、排序稳定）作 CLI 缺省；文件名由用户自由改，不硬编码。"""
    names = list_rulesets()
    return names[0] if names else ""


TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
RESERVED = {"and", "or", "not", "True", "False", "abs", "min", "max", "round", "np"}


COND_TYPES = ("value", "change", "swing", "changes")


@dataclass
class Rule:
    """规则集里的一条规则。

    conditions 是主模型：每项 {"type": value|change|swing|changes, ...}，
    join="any"/"all" 决定条件间 OR/AND。旧字段 when/transition/window 保留为兼容视图
    （load_ruleset 会把旧格式转成 conditions，evaluate 只认 conditions）。
    required_signals 由所有条件的信号并集得到。
    """
    name: str
    version: int
    when: str
    required_signals: list
    min_duration_ms: float
    hysteresis_ms: float
    merge_gap_ms: float
    mark: dict
    transition: object = None
    window: object = None
    conditions: list = None      # [{type, signal, ...}]
    join: str = "all"            # any=OR / all=AND


@dataclass
class RuleResult:
    """单条规则的评估结果。status 是一等公民，禁止折叠三态。"""
    rule_name: str
    rule_version: int
    status: str               # hit / no_hit / not_evaluated
    missing: list
    intervals: list           # [{t0, t1, duration_ms, snapshot}]


@dataclass
class EvaluationResult:
    """P2 -> P3 / 报告的核心契约；summary 供界面分三区呈现。"""
    file: str
    file_md5: str
    ruleset_version: int
    results: list
    summary: dict


def extract_signals(expr):
    """从 when 表达式里提取信号名；排除关键字与函数名。"""
    return sorted({t for t in TOKEN.findall(expr) if t not in RESERVED})


def normalize_conditions(item):
    """规则项 -> (conditions, join)。新格式直接用；旧格式(when/transition/window)转换。

    旧 when 能拆成「信号 op 数字」的逐条转 value，拆不开的整条留作 expr（复杂表达式）。
    """
    if isinstance(item.get("conditions"), list) and item["conditions"]:
        conds = []
        for c in item["conditions"]:
            cc = dict(c)
            cc["type"] = str(cc.get("type", "value"))
            if cc["type"] == "value" and "threshold" in cc:
                cc["threshold"] = float(cc["threshold"])
            if cc["type"] in ("swing", "changes"):
                cc["window_ms"] = float(cc["window_ms"])
                cc["threshold"] = float(cc["threshold"])
                if cc.get("min_step") is not None:
                    cc["min_step"] = float(cc["min_step"])
            if cc["type"] == "change":
                cc["from"] = float(cc["from"])
                cc["to"] = float(cc["to"])
            conds.append(cc)
        join = str(item.get("join", "all")).lower()
        return conds, ("any" if join == "any" else "all")

    conds, join = [], "all"
    when = str(item.get("when") or "").strip()
    cjunks, sep = when_to_conds(when)
    if cjunks is not None:
        join = "any" if sep == "|" else "all"
        for sig, op, val in cjunks:
            conds.append({"type": "value", "signal": sig, "op": op, "threshold": val})
    elif when:
        conds.append({"type": "value", "expr": when})
    trans = item.get("transition")
    if trans is not None:
        conds.append({"type": "change", "signal": str(trans["signal"]),
                      "from": float(trans["from"]), "to": float(trans["to"])})
    win = item.get("window")
    if win is not None:
        c = {"type": str(win["kind"]), "signal": str(win["signal"]),
             "window_ms": float(win["window_ms"]), "threshold": float(win["threshold"])}
        if win.get("min_step") is not None:
            c["min_step"] = float(win["min_step"])
        conds.append(c)
    return conds, join


def _compat_views(conds, join):
    """conditions -> 旧字段视图 {when, transition, window}（供老代码/用户手改读取）。"""
    when_parts, trans_view, win_view = [], None, None
    for c in conds:
        if c.get("type") == "value":
            when_parts.append(c.get("expr") or "%s %s %s" % (
                c.get("signal", ""), c.get("op", "=="), fmt_num(c.get("threshold", 0))))
        elif c.get("type") == "change":
            trans_view = {"signal": c["signal"], "from": c["from"], "to": c["to"]}
        else:
            win_view = {"signal": c["signal"], "kind": c.get("type"),
                        "window_ms": c["window_ms"], "threshold": c["threshold"]}
            if c.get("min_step") is not None:
                win_view["min_step"] = c["min_step"]
    out = {"when": (" & " if join == "all" else " | ").join(when_parts) if when_parts else ""}
    if trans_view is not None:
        out["transition"] = trans_view
    if win_view is not None:
        out["window"] = win_view
    return out


def load_ruleset(path):
    """读取规则集文件。规则集按工况分文件，只加载本次关心的那个。"""
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    version = int(doc.get("version", 1))
    rules = []
    for item in doc["rules"]:
        conds, join = normalize_conditions(item)
        # 必需信号 = 所有条件里的信号（复杂 expr 用 extract_signals）
        sigs = set()
        for c in conds:
            if c.get("signal"):
                sigs.add(c["signal"])
            if c.get("expr"):
                sigs.update(extract_signals(c["expr"]))
        # 兼容视图：从 conditions 反算 when/transition/window（供老代码读取）
        when_parts = []
        trans_view = win_view = None
        for c in conds:
            if c["type"] == "value":
                when_parts.append(c.get("expr") or "%s %s %s" % (
                    c.get("signal", ""), c.get("op", "=="), fmt_num(c.get("threshold", 0))))
            elif c["type"] == "change":
                trans_view = {"signal": c["signal"], "from": c["from"], "to": c["to"]}
            else:
                win_view = {"signal": c["signal"], "kind": c["type"],
                            "window_ms": c["window_ms"], "threshold": c["threshold"]}
                if c.get("min_step") is not None:
                    win_view["min_step"] = c["min_step"]
        when = ((" & " if join == "all" else " | ").join(when_parts)
                if when_parts else "")
        rules.append(Rule(
            name=item["name"], version=version, when=when,
            required_signals=sorted(sigs),
            min_duration_ms=float(item.get("min_duration_ms", 0)),
            hysteresis_ms=float(item.get("hysteresis_ms", 0)),
            merge_gap_ms=float(item.get("merge_gap_ms", 0)),
            mark=item.get("mark", {}),
            transition=trans_view,
            window=win_view,
            conditions=conds, join=join,
        ))
    return rules


def _runs(mask):
    """布尔序列游程编码，返回 [(start, end_exclusive, value)]。"""
    padded = np.concatenate(([False], np.asarray(mask, dtype=bool), [False]))
    edges = np.flatnonzero(np.diff(padded.astype(np.int8)))
    return [(int(edges[i]), int(edges[i + 1]), bool(padded[edges[i] + 1]))
            for i in range(0, len(edges), 2)]


def _shape_mask(mask, axis, rule):
    """定值条件专用整形：滞回去抖 -> gap 合并 -> 最短持续过滤，返回布尔 mask。

    200/50/100 三个时长参数只在这里生效——只对定值条件调用。
    """
    mask = np.asarray(mask, dtype=bool)
    if mask.ndim == 0:
        mask = np.full(len(axis), bool(mask))
    if len(mask) == 0 or len(axis) == 0:
        return mask
    dt_ms = float(np.median(np.diff(axis))) * 1000 if len(axis) > 1 else 10.0
    hyst_n = max(0, int(round(rule.hysteresis_ms / dt_ms)))
    gap_n = max(0, int(round(rule.merge_gap_ms / dt_ms)))
    fill_n = max(hyst_n, gap_n)

    work = mask.copy()
    # 1) 填平短 False 间隙：阈值抖动与相邻短断口都不该把区间打碎
    for s, e, val in _runs(work):
        if not val and (e - s) <= fill_n:
            work[s:e] = True
    # 2) 去掉短 True 毛刺（进入/退出滞回）
    for s, e, val in _runs(work):
        if val and (e - s) <= hyst_n:
            work[s:e] = False
    # 3) 过滤短于 min_duration 的段
    for s, e, val in _runs(work):
        if val:
            duration_ms = (float(axis[e - 1]) - float(axis[s])) * 1000 + dt_ms
            if duration_ms < rule.min_duration_ms:
                work[s:e] = False
    return work


def _intervals_from_mask(mask, axis, env):
    """从布尔 mask 提取区间 + 原始值快照，不做任何整形。"""
    mask = np.asarray(mask, dtype=bool)
    if mask.ndim == 0 or len(mask) == 0 or len(axis) == 0:
        return []
    dt_ms = float(np.median(np.diff(axis))) * 1000 if len(axis) > 1 else 10.0
    out = []
    for s, e, val in _runs(mask):
        if not val:
            continue
        t0 = float(axis[s])
        t1 = float(axis[min(e - 1, len(axis) - 1)])
        snapshot = {k: round(float(v[s]), 6) for k, v in env.items()}
        out.append({
            "t0": round(t0, 6), "t1": round(t1, 6),
            "duration_ms": round((t1 - t0) * 1000 + dt_ms, 3),
            "snapshot": snapshot,
        })
    return out


def _segments(mask, axis, rule, env):
    """定值条件命中区间：先按 200/50/100 整形，再提取区间。"""
    return _intervals_from_mask(_shape_mask(mask, axis, rule), axis, env)


def _transition_mask(trans, env):
    """跳变条件：前一采样点 == from 且当前点 == to 的位置为 True（单点）。

    严格相邻两点比较，不做容差/不做区间展开。首点没有前驱，恒 False。
    """
    sig = trans["signal"]
    arr = np.asarray(env[sig], dtype=float)
    m = np.zeros(len(arr), dtype=bool)
    if len(arr) < 2:
        return m
    a, b = float(trans["from"]), float(trans["to"])
    if a == b:      # 同值不是跳变，否则"保持"段会被误判成一堆单点
        return m
    m[1:] = (arr[:-1] == a) & (arr[1:] == b)
    return m


def _point_intervals(mask, axis, env):
    """跳变规则的命中区间：每个 True 点单独成区间 [t, t]，duration 取一个栅格。

    不走 _segments——没有持续时长，滞回/合并/最短时长对它无意义。
    """
    idx = np.flatnonzero(np.asarray(mask, dtype=bool))
    if not len(idx):
        return []
    dt_ms = float(np.median(np.diff(axis))) * 1000 if len(axis) > 1 else 10.0
    out = []
    for i in idx:
        i = int(i)
        t = float(axis[i])
        snapshot = {k: round(float(v[i]), 6) for k, v in env.items()}
        out.append({
            "t0": round(t, 6), "t1": round(t, 6),
            "duration_ms": round(dt_ms, 3), "snapshot": snapshot,
        })
    return out


def _rolling_extreme(arr, w, is_max):
    """滑动窗口极值：out[i] = max/min(arr[max(0,i-w+1):i+1])。

    用 sliding_window_view（零拷贝视图）+ 左侧垫中性值，每个窗口取极值。
    中性值保证窗口不足 w 时不污染结果（max 垫 -inf、min 垫 +inf）。
    """
    n = len(arr)
    if w <= 1:
        return arr.copy()
    neutral = -np.inf if is_max else np.inf
    padded = np.concatenate((np.full(w - 1, neutral), arr))
    windows = np.lib.stride_tricks.sliding_window_view(padded, w)
    return windows.max(axis=1) if is_max else windows.min(axis=1)


def _window_mask(win, m, dt_ms):
    """窗口条件 -> 逐点布尔 mask。每个点回看 window_ms 内的数据。"""
    sig = win["signal"]
    arr = np.asarray(m.signals[sig], dtype=float)
    w = max(1, int(round(win["window_ms"] / dt_ms)) if dt_ms > 0 else 1)
    kind = win.get("kind") or win.get("type")
    if kind == "swing":
        hi = _rolling_extreme(arr, w, True)
        lo = _rolling_extreme(arr, w, False)
        mask = (hi - lo) > win["threshold"]
    elif kind == "changes":
        diff = np.abs(np.diff(arr, prepend=arr[:1]))
        step = win.get("min_step")
        changed = diff > (step if step is not None else 0.0)
        cs = np.concatenate(([0], np.cumsum(changed, dtype=np.int64)))
        # 窗口内变化次数 = 前 i+1 个 diff 的和 - 窗口左端之前的和
        n = len(arr)
        idx = np.arange(n)
        left = np.maximum(0, idx - w + 1)
        count = cs[idx + 1] - cs[left]
        mask = count > win["threshold"]
    else:
        raise ValueError("未知窗口条件类型：%s" % kind)
    return np.asarray(mask, dtype=bool)


def _window_event_mask(win, m, dt_ms):
    """窗口条件标注：把命中定位到事件本身，各外扩一个采样点。

    _window_mask 的逐点 True 会拖尾一个窗口（截止点标在窗口滑过处，不是极值处）。
    这里先用它判定命中，再回看原始信号：
      swing   —— zigzag 找极值对，相邻转折点即一次波动（拆成多个区间）；
      changes —— 取段内（含窗口回看范围）全部变化点，首尾即覆盖范围。
    区间两端各外扩一格，让标注把极值/跳变点框住。
    """
    raw = _window_mask(win, m, dt_ms)
    out = np.zeros(len(raw), dtype=bool)
    if not raw.any():
        return out
    arr = np.asarray(m.signals[win["signal"]], dtype=float)
    n = len(arr)
    w = max(1, int(round(win["window_ms"] / dt_ms)) if dt_ms > 0 else 1)
    kind = win.get("kind") or win.get("type")

    def mark(lo, hi):
        pad = 0 if _NO_EXPAND else 1                # 变异开关：不外扩
        out[max(0, lo - pad):min(n, hi + pad + 1)] = True

    for s, e, val in _runs(raw):
        if not val:
            continue
        if kind == "swing":
            # 标注 = 造成命中的 max/min 两个极值位置，各外扩一格。
            # 谷可能在段首之前（mask 从极差刚超阈值才 True，此时峰已出现），
            # 所以回看一个窗口。峰=回看范围 argmax；谷=离峰最近的 min 值点
            # （一整段平台都是 min 时，取最左会标到数组边界，取离峰最近才对）。
            # 不依赖 zigzag 交替转折——单峰脉冲、渐变都适用。
            search_lo = max(0, s - w + 1)
            seg = arr[search_lo:e]
            if len(seg):
                hi_pos = search_lo + int(np.argmax(seg))
                mn = float(np.min(seg))
                ties = np.flatnonzero(arr[search_lo:e] == mn) + search_lo
                lo_pos = int(ties[np.argmin(np.abs(ties - hi_pos))])
                mark(min(lo_pos, hi_pos), max(lo_pos, hi_pos))
        else:                                       # changes
            # 变化点可能早于段首（攒够次数那一刻才 True），回看一个窗口取全部变化
            step = win.get("min_step")
            changed = np.abs(np.diff(arr, prepend=arr[:1])) > (step if step is not None else 0.0)
            search_lo = max(0, s - w + 1)
            idxs = np.flatnonzero(changed[search_lo:e]) + search_lo
            if len(idxs):
                mark(int(idxs[0]), int(idxs[-1]))    # 覆盖全部 N 次变化
    # 兜底：原始条件命中但事件定位没产出区间时，退回原始区间。
    # 宁可标注拖尾一个窗口，也绝不能让标注定位吞掉命中（判定语义优先）。
    if not out.any() and raw.any():
        return raw
    return out


def evaluate(m, rs, bad=False):
    """规则求值。bad=True 仅用于变异测试：故意把 not_evaluated 折叠成 no_hit。"""
    results = []
    for r in rs:
        missing = [s for s in r.required_signals if s not in m.signals]
        if missing:
            # v6：缺信号是常态，单独一类，不报错、不中断、不计入命中统计。
            status = "no_hit" if bad else "not_evaluated"
            results.append(RuleResult(r.name, r.version, status,
                                      [] if bad else missing, []))
            continue
        env = {s: m.signals[s] for s in r.required_signals}
        conds = r.conditions or []
        join = r.join
        if not conds:
            # 兼容：直接构造 Rule 只填旧字段的调用（自检/程序化验收）
            conds, join = normalize_conditions({
                "when": r.when, "transition": r.transition, "window": r.window})
        dt_ms = float(np.median(np.diff(m.axis))) * 1000 if len(m.axis) > 1 else 10.0
        masks = []
        for c in conds:
            ct = c.get("type", "value")
            if ct == "value":
                expr = c.get("expr")
                if not expr:
                    expr = "%s %s %s" % (c.get("signal", ""), c.get("op", "=="),
                                         fmt_num(c.get("threshold", 0)))
                mm = np.asarray(caliper_core.derive(expr, env), dtype=bool)
                # 时长参数只整形定值条件；跳变/窗口条件的 mask 原样不整形
                mm = _shape_mask(mm, m.axis, r)
            elif ct == "change":
                mm = _transition_mask(c, env)
            elif ct in ("swing", "changes"):
                mm = _window_event_mask(c, m, dt_ms)
                if _SHAPE_WINDOW:   # 仅供变异测试：模拟"窗口错误吃时长参数"的缺陷
                    mm = _shape_mask(mm, m.axis, r)
            else:
                raise ValueError("未知条件类型：%s" % ct)
            masks.append(mm)
        if masks:
            mask = (np.logical_or.reduce(masks) if join == "any"
                    else np.logical_and.reduce(masks))
        else:
            mask = np.zeros(len(m.axis), dtype=bool)
        # 纯跳变规则保持单点语义；其它（含窗口）只做区间提取，不再整形
        if conds and all(c.get("type") == "change" for c in conds):
            intervals = _point_intervals(mask, m.axis, env)
        else:
            intervals = _intervals_from_mask(mask, m.axis, env)
        results.append(RuleResult(r.name, r.version,
                                  "hit" if intervals else "no_hit", [], intervals))
    summary = {
        "hit": sum(1 for x in results if x.status == "hit"),
        "no_hit": sum(1 for x in results if x.status == "no_hit"),
        "not_evaluated": sum(1 for x in results if x.status == "not_evaluated"),
        "total_intervals": sum(len(x.intervals) for x in results),
    }
    return EvaluationResult(
        file=m.file, file_md5="", ruleset_version=rs[0].version if rs else 0,
        results=results, summary=summary,
    )


def export_result(r, path):
    """导出 EvaluationResult（含三态 summary），供界面与报告消费。"""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(asdict(r), f, ensure_ascii=False, indent=2, sort_keys=True)


# ---------------------------------------------------------------- 规则文件读写
# 界面里改规则，改的是这里的模型；JSON 只由程序生成，人不直接编辑。
NUM_FIELDS = ("min_duration_ms", "hysteresis_ms", "merge_gap_ms")
NUM_RANGE = (0.0, 600000.0)
CMP_OPS = (">", ">=", "==", "!=", "<", "<=")
_COND_RE = re.compile(r"^\s*\(?\s*([A-Za-z_][A-Za-z0-9_]*)"
                      r"\s*(>=|<=|==|!=|>|<)\s*"
                      r"(-?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*\)?\s*$")
_ALLOWED_NODES = (
    ast.Expression, ast.BinOp, ast.BitAnd, ast.BitOr, ast.BitXor, ast.UnaryOp,
    ast.USub, ast.UAdd, ast.Constant, ast.Name, ast.Load, ast.Compare,
    ast.Eq, ast.NotEq, ast.Gt, ast.GtE, ast.Lt, ast.LtE, ast.Call,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
)


def fmt_num(v):
    """数值转表达式文本：200.0 写成 200，0.5 写成 0.5（避免规则文件里满屏 .0）。"""
    f = float(v)
    if f.is_integer() and abs(f) < 1e15:
        return str(int(f))
    return repr(round(f, 9))


def when_to_conds(expr):
    """把 when 表达式拆成 [(信号, 运算符, 阈值)] 与连接符。

    只认「信号 op 数字」这种简单条件，且各行必须用同一种连接符。
    函数调用、混合 & 与 |（有优先级语义）、位运算等一律返回 (None, None)，
    调用方必须原样保留表达式——强行改写会悄悄改掉规则含义。
    """
    parts = re.split(r"\s*([&|])\s*", str(expr).strip())
    if not parts or len(parts) % 2 == 0:
        return None, None
    conds, joins = [], []
    for i, chunk in enumerate(parts):
        if i % 2:
            joins.append(chunk)
            continue
        m = _COND_RE.match(chunk)
        if not m:
            return None, None
        conds.append((m.group(1), m.group(2), float(m.group(3))))
    if len(set(joins)) > 1:
        return None, None
    return conds, (joins[0] if joins else "&")


def conds_to_when(conds, join="&"):
    """简单条件拼回 when 表达式；单条件不加括号，与手写的规则文件风格一致。"""
    if not conds:
        return ""
    body = ["%s %s %s" % (sig, op, fmt_num(val)) for sig, op, val in conds]
    if len(body) == 1:
        return body[0]
    glue = " %s " % (join if join in ("&", "|") else "&")
    return glue.join("(%s)" % b for b in body)


def validate_when(expr):
    """校验表达式：语法、允许的节点、函数白名单，并真的算一遍确认能用 numpy。

    返回错误描述；None 表示可用。信号名是否存在于某个文件不在校验范围内——
    缺信号是合法工况（三态里的 not_evaluated），拼写错误由界面另行提示。
    """
    expr = str(expr or "").strip()
    if not expr:
        return "条件不能为空"
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        return "表达式语法错误：%s" % e.msg
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            return "表达式里有不支持的写法：%s" % type(node).__name__
        if isinstance(node, ast.Constant) and not isinstance(node.value, (int, float, bool)):
            return "表达式里只能出现数字"
        if isinstance(node, ast.Call):
            fn = getattr(node.func, "id", "")
            if fn not in caliper_core.SAFE_ENV:
                return "不支持的函数 %s（可用：%s）" % (
                    fn, "、".join(sorted(caliper_core.SAFE_ENV)))
    sigs = extract_signals(expr)
    if not sigs:
        return "表达式里没有信号名"
    probe = {s: np.ones(4, dtype=float) for s in sigs}
    try:
        out = np.asarray(caliper_core.derive(expr, probe))
    except Exception as e:                      # noqa: BLE001 - 用户表达式写错，原样回报
        return "表达式算不出来：%s" % e
    if out.dtype != bool:
        return "表达式结果是数值而不是真假条件（比较符写对了吗）"
    return None


def validate_conditions(conds, join, label, errs):
    """校验新格式 conditions 列表：类型合法、字段齐全、数值有效。"""
    if not isinstance(conds, list) or not conds:
        errs.append("%s：至少要有一个条件" % label)
        return
    if join not in ("any", "all"):
        errs.append("%s：join 必须是 any 或 all" % label)
    for i, c in enumerate(conds):
        if not isinstance(c, dict):
            errs.append("%s：第 %d 个条件必须是对象" % (label, i + 1))
            continue
        t = c.get("type")
        if t not in COND_TYPES:
            errs.append("%s：第 %d 个条件类型非法（%s）" % (label, i + 1, t))
            continue
        if t == "value":
            if c.get("expr"):
                err = validate_when(c["expr"])
                if err:
                    errs.append("%s：第 %d 个条件 %s" % (label, i + 1, err))
            else:
                if not str(c.get("signal") or "").strip():
                    errs.append("%s：第 %d 个定值条件缺信号" % (label, i + 1))
                if c.get("op") not in CMP_OPS:
                    errs.append("%s：第 %d 个定值条件比较符非法" % (label, i + 1))
                try:
                    float(c["threshold"])
                except (KeyError, TypeError, ValueError):
                    errs.append("%s：第 %d 个定值条件阈值必须是数字" % (label, i + 1))
        elif t == "change":
            if not str(c.get("signal") or "").strip():
                errs.append("%s：第 %d 个跳变条件缺信号" % (label, i + 1))
            try:
                if float(c["from"]) == float(c["to"]):
                    errs.append("%s：第 %d 个跳变 from/to 不能相同" % (label, i + 1))
            except (KeyError, TypeError, ValueError):
                errs.append("%s：第 %d 个跳变 from/to 必须是数字" % (label, i + 1))
        else:
            if not str(c.get("signal") or "").strip():
                errs.append("%s：第 %d 个窗口条件缺信号" % (label, i + 1))
            for k in ("window_ms", "threshold"):
                try:
                    if float(c[k]) <= 0:
                        errs.append("%s：第 %d 个窗口 %s 必须 > 0" % (label, i + 1, k))
                except (KeyError, TypeError, ValueError):
                    errs.append("%s：第 %d 个窗口 %s 必须是数字" % (label, i + 1, k))
            if c.get("min_step") is not None:
                try:
                    if float(c["min_step"]) < 0:
                        errs.append("%s：第 %d 个窗口 min_step 不能为负" % (label, i + 1))
                except (TypeError, ValueError):
                    errs.append("%s：第 %d 个窗口 min_step 必须是数字" % (label, i + 1))


def validate_rules_doc(doc):
    """整份规则集的校验：结构、重名、每条规则的条件与数值。返回错误列表。"""
    if not isinstance(doc, dict):
        return ["规则集必须是一个 JSON 对象"]
    errs = []
    rules = doc.get("rules")
    if not isinstance(rules, list) or not rules:
        return ["规则集至少要有一条规则"]
    names = [str(r.get("name") or "").strip() for r in rules
             if isinstance(r, dict)]
    if len(names) != len(rules):
        return ["规则条目必须是对象"]
    if any(not n for n in names):
        errs.append("规则名不能为空")
    dup = sorted({n for n in names if n and names.count(n) > 1})
    if dup:
        errs.append("规则名重复：%s" % "、".join(dup))
    for i, r in enumerate(rules):
        label = names[i] or "第 %d 条" % (i + 1)
        if isinstance(r.get("conditions"), list) and r["conditions"]:
            validate_conditions(r["conditions"],
                                str(r.get("join", "all")).lower(), label, errs)
            for k in NUM_FIELDS:
                if k in r:
                    try:
                        f = float(r[k])
                        if not (NUM_RANGE[0] <= f <= NUM_RANGE[1]):
                            errs.append("%s：%s 应在 %g–%g 之间"
                                        % (label, k, NUM_RANGE[0], NUM_RANGE[1]))
                    except (TypeError, ValueError):
                        errs.append("%s：%s 必须是数字" % (label, k))
            continue
        win = r.get("window")
        if win is not None and r.get("transition") is not None:
            errs.append("%s：窗口条件与跳变条件不能同时用" % label)
        if win is not None:
            # 窗口条件本身就是一个完整条件，when 可空
            if not isinstance(win, dict):
                errs.append("%s：window 必须是对象" % label)
            else:
                if not str(win.get("signal") or "").strip():
                    errs.append("%s：窗口条件缺 signal" % label)
                if win.get("kind") not in ("swing", "changes"):
                    errs.append("%s：窗口 kind 必须是 swing 或 changes" % label)
                for k in ("window_ms", "threshold"):
                    try:
                        f = float(win[k])
                        if f <= 0:
                            errs.append("%s：窗口 %s 必须 > 0" % (label, k))
                    except (KeyError, TypeError, ValueError):
                        errs.append("%s：窗口 %s 必须是数字" % (label, k))
                if win.get("min_step") is not None:
                    try:
                        if float(win["min_step"]) < 0:
                            errs.append("%s：窗口 min_step 不能为负" % label)
                    except (TypeError, ValueError):
                        errs.append("%s：窗口 min_step 必须是数字" % label)
            if str(r.get("when") or "").strip():
                err = validate_when(r.get("when"))
                if err:
                    errs.append("%s：%s" % (label, err))
        trans = r.get("transition")
        has_trans = trans is not None and win is None
        if has_trans:   # win 分支已处理过 when 校验，这里有 win 时不会进来
            # 跳变条件本身就是一个完整条件，when 可空
            if not isinstance(trans, dict):
                errs.append("%s：transition 必须是对象" % label)
            else:
                sig = str(trans.get("signal") or "").strip()
                if not sig:
                    errs.append("%s：跳变条件缺 signal" % label)
                try:
                    a, b = float(trans["from"]), float(trans["to"])
                    if a == b:
                        errs.append("%s：跳变 from 与 to 不能相同" % label)
                except (KeyError, TypeError, ValueError):
                    errs.append("%s：跳变 from/to 必须是数字" % label)
            if str(r.get("when") or "").strip():
                err = validate_when(r.get("when"))
                if err:
                    errs.append("%s：%s" % (label, err))
        elif win is None:   # 无跳变、无窗口：when 是唯一条件，必须合法（含非空）
            err = validate_when(r.get("when"))
            if err:
                errs.append("%s：%s" % (label, err))
        for k in NUM_FIELDS:
            if k not in r:
                continue
            try:
                f = float(r[k])
            except (TypeError, ValueError):
                errs.append("%s：%s 必须是数字" % (label, k))
                continue
            if not (NUM_RANGE[0] <= f <= NUM_RANGE[1]):
                errs.append("%s：%s 应在 %g–%g 之间"
                            % (label, k, NUM_RANGE[0], NUM_RANGE[1]))
    return errs


def _num_out(v):
    """写盘用的数值形式：整数值保持整数（200 不写成 200.0）。

    规则里的时间参数几乎都是整数毫秒；一律转 float 会让每次「保存并应用」
    都在 diff 里刷出一片 .0 噪声，也让人误以为规则被改过。真小数（250.5）原样保留。
    """
    f = float(v)
    return int(f) if f.is_integer() else f


def save_ruleset(path, doc, bump_version=True):
    """把编辑后的规则集写回 JSON：校验 -> 备份 -> 原子替换。

    返回 (version, backup_path, md5)。版本号默认自增，报告据此分辨「规则改过」。
    任何校验不通过都抛 ValueError，且不碰原文件——宁可拒存，不写半截规则。
    """
    errs = validate_rules_doc(doc)
    if errs:
        raise ValueError("；".join(errs))
    out = {"name": str(doc.get("name") or "").strip() or "未命名规则集"}
    ver = int(doc.get("version", 1)) + (1 if bump_version else 0)
    out["version"] = ver
    if doc.get("note"):
        out["note"] = str(doc["note"])
    if "enabled" in doc:
        out["enabled"] = bool(doc["enabled"])
    # 不认识的顶层字段原样留着：规则集里可能有人加的 author / tags / 待补清单，
    # 保存一次就丢一次等于静默毁数据。
    extra = {k: v for k, v in doc.items()
             if k not in ("name", "version", "note", "enabled", "rules")}
    clean = []
    for r in doc["rules"]:
        item = {"name": str(r["name"]).strip()}
        # 新格式：conditions + join（并存旧视图，兼容老代码/用户手改）
        if isinstance(r.get("conditions"), list) and r["conditions"]:
            join = "any" if str(r.get("join", "all")).lower() == "any" else "all"
            item["join"] = join
            out_conds = []
            for c in r["conditions"]:
                cc = {"type": str(c.get("type", "value"))}
                if cc["type"] == "value":
                    if c.get("expr"):
                        cc["expr"] = str(c["expr"])
                    else:
                        cc.update(signal=str(c.get("signal", "")).strip(),
                                  op=str(c.get("op", "==")),
                                  threshold=_num_out(c.get("threshold", 0)))
                elif cc["type"] == "change":
                    cc.update(signal=str(c.get("signal", "")).strip(),
                              **{"from": _num_out(c["from"]), "to": _num_out(c["to"])})
                else:
                    cc.update(signal=str(c.get("signal", "")).strip(),
                              window_ms=_num_out(c["window_ms"]),
                              threshold=_num_out(c["threshold"]))
                    if c.get("min_step") is not None:
                        cc["min_step"] = _num_out(c["min_step"])
                out_conds.append(cc)
            item["conditions"] = out_conds
            # 反算旧字段视图
            conds, j = normalize_conditions(item)
            views = _compat_views(conds, j)
            item.update(views)
        else:
            item["when"] = str(r.get("when") or "").strip()
        for k in NUM_FIELDS:
            if k in r:
                item[k] = _num_out(r[k])
        if r.get("transition") is not None:
            t = r["transition"]
            item["transition"] = {"signal": str(t["signal"]).strip(),
                                  "from": _num_out(t["from"]), "to": _num_out(t["to"])}
        if r.get("window") is not None:
            w = r["window"]
            item["window"] = {"signal": str(w["signal"]).strip(),
                              "kind": str(w["kind"]),
                              "window_ms": _num_out(w["window_ms"]),
                              "threshold": _num_out(w["threshold"])}
            if w.get("min_step") is not None:
                item["window"]["min_step"] = _num_out(w["min_step"])
        mark = r.get("mark")
        if isinstance(mark, dict) and mark:
            item["mark"] = {k: mark[k] for k in ("color", "label") if k in mark} or dict(mark)
        clean.append(item)
    out["rules"] = clean
    for k, v in extra.items():
        out.setdefault(k, v)

    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
        f.write("\n")
    bak = ""
    if os.path.exists(path):
        bak = path + ".bak"
        shutil.copyfile(path, bak)          # 改坏了能退回上一版
    os.replace(tmp, path)
    return ver, bak, md5_of(path)


def load_ruleset_doc(path):
    """原样读出规则集 JSON（界面编辑器在它上面改，不经过 Rule 数据类）。"""
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def md5_of(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _synthetic():
    """合成抖动数据：用于验证去抖/合并确实生效，且断言能变红。"""
    axis = np.round(np.arange(0, 1.001, 0.01), 6)
    x = np.full(len(axis), 3.0)
    x[:20] = 4.0                    # 0.00-0.19 稳定命中
    x[20:40:2] = 4.0               # 0.20-0.39 阈值抖动
    x[80:] = 4.0                   # 0.80-1.00 稳定命中
    m = caliper_core.Measurement(
        file="synthetic", config="synthetic", mdf_version="synthetic", main_group=0,
        axis=axis, signals={"X": x}, units={}, not_collected=[], requested=["X"],
        start_time_utc="", start_time_local="", quality={}, readback_ok={},
    )
    rule = Rule("抖动命中", 1, "X == 4", ["X"], 200.0, 50.0, 100.0, {})
    no_hit = Rule("不可能命中", 1, "X == 99", ["X"], 200.0, 50.0, 100.0, {})
    missing = Rule("缺信号", 1, "MISSING > 0", ["MISSING"], 200.0, 50.0, 100.0, {})
    return m, [rule, no_hit, missing]


def self_test(bad=False):
    """四类用例：命中 / 未命中 / 边界抖动 / 缺信号未评估。"""
    m, rs = _synthetic()
    res = evaluate(m, rs, bad=bad)
    hit, no_hit, miss = res.results

    # 去抖有效性：不做滞回/合并时的原始 True 段数应明显更多
    raw_true_runs = sum(1 for _, _, v in _runs(m.signals["X"] == 4) if v)
    debounced = len(hit.intervals)
    debounce_works = 0 < debounced < raw_true_runs

    checks = {
        "hit_status": hit.status == "hit" and debounced >= 1,
        "debounce_works": debounce_works,
        "no_hit_status": no_hit.status == "no_hit" and no_hit.intervals == [],
        "not_evaluated_status": miss.status == "not_evaluated" and miss.missing == ["MISSING"],
        "summary_counts": res.summary == {
            "hit": 1, "no_hit": 1, "not_evaluated": 1, "total_intervals": debounced,
        },
    }
    print("原始 True 段 %d -> 去抖后命中区间 %d" % (raw_true_runs, debounced))
    print("四类断言:", checks)
    for name, ok in checks.items():
        print("  %-24s %s" % (name, "OK" if ok else "FAIL"))
    return all(checks.values()), checks


def transition_self_test(bad=frozenset()):
    """跳变条件自检：严格相邻两点 from->to、单点区间、时长参数被忽略。

    bad 变异：nospot=把跳变点当区间展开（错误）。
    """
    checks = {}
    if not isinstance(bad, (set, frozenset)):
        bad = frozenset(t for t in re.split(r"[,\s]+", str(bad or "")) if t)

    axis = np.round(np.arange(0, 1.001, 0.1), 6)     # 11 点，0.1s 栅格
    x = np.array([0, 0, 0, 3, 3, 3, 0, 0, 3, 0, 0], dtype=float)
    y = np.ones(len(axis), dtype=float)
    m = caliper_core.Measurement(
        file="synthetic", config="synthetic", mdf_version="synthetic", main_group=0,
        axis=axis, signals={"X": x, "Y": y}, units={}, not_collected=[],
        requested=["X", "Y"], start_time_utc="", start_time_local="",
        quality={}, readback_ok={},
    )
    # 3->0 发生在索引 5->6 与 8->9：两个跳变点
    trans = {"signal": "X", "from": 3.0, "to": 0.0}
    r = Rule("下降沿", 1, "", ["X"], 600000.0, 600000.0, 600000.0, {},
             transition=trans)
    checks["trans_required_signal"] = "X" in r.required_signals

    if "nospot" in bad:
        # 变异：把跳变 mask 错误地展开成"当前值==to"的整段区间
        orig_mask = globals()["_transition_mask"]
        globals()["_transition_mask"] = lambda t, e: np.asarray(e[t["signal"]]) == t["to"]
    else:
        orig_mask = None
    res = evaluate(m, [r])
    got = res.results[0]
    checks["trans_status"] = got.status == "hit"
    checks["trans_two_points"] = len(got.intervals) == 2
    checks["trans_single_point"] = all(
        abs(iv["t0"] - iv["t1"]) < 1e-9 for iv in got.intervals)
    checks["trans_times"] = ([iv["t0"] for iv in got.intervals] == [0.6, 0.9])
    # 三个时长参数设到最大也不能抹掉跳变点：证明时长逻辑被忽略
    checks["trans_ignores_duration"] = len(got.intervals) == 2 and all(
        iv["duration_ms"] < 200 for iv in got.intervals)

    # when 组合：只有跳变点处 Y>0 才算（本例 Y 恒 1，仍两点）；加个只在索引 9 为 0 的 Y
    y2 = y.copy()
    y2[6] = 0.0
    m2 = caliper_core.Measurement(
        file="synthetic", config="synthetic", mdf_version="synthetic", main_group=0,
        axis=axis, signals={"X": x, "Y": y2}, units={}, not_collected=[],
        requested=["X", "Y"], start_time_utc="", start_time_local="",
        quality={}, readback_ok={},
    )
    r2 = Rule("下降沿且Y真", 1, "Y > 0", ["X", "Y"], 0, 0, 0, {},
              transition=trans)
    res2 = evaluate(m2, [r2])
    checks["trans_with_when"] = (
        [iv["t0"] for iv in res2.results[0].intervals] == [0.9])

    # 缺跳变信号 -> 未评估
    r3 = Rule("缺信号跳变", 1, "", ["MISSING"], 0, 0, 0, {},
              transition={"signal": "MISSING", "from": 0, "to": 1})
    res3 = evaluate(m, [r3])
    checks["trans_missing_signal"] = (
        res3.results[0].status == "not_evaluated"
        and res3.results[0].missing == ["MISSING"])

    # 起止同值 -> 无跳变
    r4 = Rule("同值不算跳变", 1, "", ["X"], 0, 0, 0, {},
              transition={"signal": "X", "from": 3, "to": 3})
    checks["trans_same_from_to"] = evaluate(m, [r4]).results[0].status == "no_hit"

    if orig_mask is not None:
        globals()["_transition_mask"] = orig_mask
    print("跳变条件断言:", checks)
    for name, ok in checks.items():
        print("  %-28s %s" % (name, "OK" if ok else "FAIL"))
    return all(checks.values()), checks


def window_self_test(bad=frozenset()):
    """窗口条件自检：swing 极差 / changes 变化次数 / min_step / when 组合 / 缺信号。

    变异：badswing / nochanges / badshape / noexpand（不外扩）。
    """
    checks = {}
    if not isinstance(bad, (set, frozenset)):
        bad = frozenset(t for t in re.split(r"[,\s]+", str(bad or "")) if t)
    orig_shape_window = _SHAPE_WINDOW

    axis = np.round(np.arange(0, 4.001, 0.1), 6)     # 41 点，0.1s 栅格
    # X：0~1s 平稳=10；1~1.5s 剧烈波动(0/20 交替)；1.5s 后平稳=10
    x = np.full(len(axis), 10.0)
    x[10:16] = [0, 20, 0, 20, 0, 20]
    # Y：阶梯 0→1→2→3→2，每 0.5s 一级（变化次数测试）
    y = np.repeat([0, 1, 2, 3, 2], [10, 10, 10, 6, 5]).astype(float)
    # Z：平稳值上叠 ±0.1 小抖动（min_step 测试：小变化不该计入）
    z = 5.0 + np.where(np.arange(len(axis)) % 2 == 0, 0.1, -0.1)
    m = caliper_core.Measurement(
        file="synthetic", config="synthetic", mdf_version="synthetic", main_group=0,
        axis=axis, signals={"X": x, "Y": y, "Z": z}, units={}, not_collected=[],
        requested=["X", "Y", "Z"], start_time_utc="", start_time_local="",
        quality={}, readback_ok={},
    )

    orig_extreme = globals().get("_rolling_extreme")
    if "badswing" in bad:
        # 变异：窗口极值退化成逐点值（等于没做窗口），波动段单点 0/20 的极差为 0 -> 不命中
        globals()["_rolling_extreme"] = lambda a, w, is_max: a.copy()
    orig_mask = globals().get("_window_mask")
    if "nochanges" in bad:
        globals()["_window_mask"] = lambda win, mm, dt: np.zeros(len(mm.axis), dtype=bool)
    if "badshape" in bad:
        globals()["_SHAPE_WINDOW"] = True
    orig_no_expand = _NO_EXPAND
    if "noexpand" in bad:
        globals()["_NO_EXPAND"] = True

    try:
        # swing：窗口 500ms，波动段 max-min=20 > 10 -> 命中
        r1 = Rule("波动超10", 1, "", ["X"], 0, 0, 0, {},
                  window={"signal": "X", "kind": "swing", "window_ms": 500, "threshold": 10})
        checks["win_required_signal"] = "X" in r1.required_signals
        res1 = evaluate(m, [r1])
        g1 = res1.results[0]
        checks["swing_status"] = g1.status == "hit"
        # 波动段在 1.0~1.5s；窗口末端回看，命中区间应落在这附近，不覆盖 0~1s 平稳段
        checks["swing_scoped"] = bool(g1.intervals) and g1.intervals[0]["t0"] >= 0.9
        checks["swing_no_hit_outside"] = all(iv["t1"] <= 2.0 for iv in g1.intervals)

        # 阈值抬高到 25 -> 波动幅度 20 不够 -> 未命中
        r2 = Rule("波动超25", 1, "", ["X"], 0, 0, 0, {},
                  window={"signal": "X", "kind": "swing", "window_ms": 500, "threshold": 25})
        checks["swing_threshold"] = evaluate(m, [r2]).results[0].status == "no_hit"

        # changes：阶梯 4 次变化，窗口 1000ms，阈值 2 -> 命中
        r3 = Rule("变化超2", 1, "", ["Y"], 0, 0, 0, {},
                  window={"signal": "Y", "kind": "changes", "window_ms": 1000, "threshold": 1})
        g3 = evaluate(m, [r3]).results[0]
        checks["changes_status"] = g3.status == "hit"

        # min_step：Z 的小抖动 ±0.1，min_step=1 -> 0 次变化 -> 未命中
        r4 = Rule("大变化", 1, "", ["Z"], 0, 0, 0, {},
                  window={"signal": "Z", "kind": "changes", "window_ms": 1000,
                          "threshold": 1, "min_step": 1.0})
        checks["changes_min_step"] = evaluate(m, [r4]).results[0].status == "no_hit"
        # 不给 min_step 时小抖动也算变化 -> 命中（对比证明 min_step 生效）
        r5 = Rule("小变化也算", 1, "", ["Z"], 0, 0, 0, {},
                  window={"signal": "Z", "kind": "changes", "window_ms": 1000, "threshold": 1})
        checks["changes_no_min_step"] = evaluate(m, [r5]).results[0].status == "hit"

        # when AND：窗口条件 + X>15（只在波动高点）-> 仍命中但区间更窄
        r6 = Rule("波动且高位", 1, "X > 15", ["X"], 0, 0, 0, {},
                  window={"signal": "X", "kind": "swing", "window_ms": 500, "threshold": 10})
        g6 = evaluate(m, [r6]).results[0]
        checks["window_with_when"] = (g6.status == "hit" and bool(g6.intervals)
            and all(1.0 <= iv["t0"] <= 1.6 for iv in g6.intervals))

        # 缺信号 -> 未评估（三态）
        r7 = Rule("缺信号窗口", 1, "", ["MISSING"], 0, 0, 0, {},
                  window={"signal": "MISSING", "kind": "swing", "window_ms": 500, "threshold": 10})
        g7 = evaluate(m, [r7]).results[0]
        checks["window_missing"] = (g7.status == "not_evaluated"
                                    and g7.missing == ["MISSING"])

        # 核心约束：时长参数（200/50/100）只对定值生效，波动/变化次数一律不吃。
        # 用极短的窗口命中 + 极大时长：正常仍命中；badshape 变异（窗口也整形）则被抹掉。
        axis_s = np.round(np.arange(0, 1.001, 0.01), 6)
        xs = np.full(len(axis_s), 5.0)
        xs[50:53] = [0, 20, 0]           # 单次波动，命中段约 50ms < 200ms
        ms = caliper_core.Measurement(
            file="short", config="", mdf_version="", main_group=0,
            axis=axis_s, signals={"X": xs}, units={}, not_collected=[],
            requested=["X"], start_time_utc="", start_time_local="",
            quality={}, readback_ok={},
        )
        swing_short = {"type": "swing", "signal": "X", "window_ms": 50, "threshold": 10}
        r_huge = Rule("极大时长", 1, "", ["X"], 600000.0, 600000.0, 600000.0, {},
                      conditions=[swing_short], join="all")
        checks["window_ignores_duration"] = (
            evaluate(ms, [r_huge]).results[0].status == "hit")
        changes_short = {"type": "changes", "signal": "X", "window_ms": 50, "threshold": 1}
        r_huge2 = Rule("极大时长2", 1, "", ["X"], 600000.0, 600000.0, 600000.0, {},
                       conditions=[changes_short], join="all")
        checks["changes_ignores_duration"] = (
            evaluate(ms, [r_huge2]).results[0].status == "hit")

        # ---- 标注定位：区间须落在事件本身，不是窗口回看拖尾 ----
        # 10ms 栅格；单次波动 min@1.00=10 / max@1.50=90，外扩一格 -> 0.99~1.51
        axis_l = np.round(np.arange(0, 3.001, 0.01), 6)
        x1 = np.full(len(axis_l), 50.0)
        x1[100], x1[150] = 10.0, 90.0
        ml = caliper_core.Measurement(
            file="label", config="", mdf_version="", main_group=0,
            axis=axis_l, signals={"X": x1}, units={}, not_collected=[],
            requested=["X"], start_time_utc="", start_time_local="",
            quality={}, readback_ok={},
        )
        w1 = {"type": "swing", "signal": "X", "window_ms": 600, "threshold": 20}
        iv1 = evaluate(ml, [Rule("标注1", 1, "", ["X"], 0, 0, 0, {},
                                 conditions=[w1], join="all")]).results[0].intervals
        # 区间应贴着极值（0.99~1.51），不是旧的窗口拖尾（到 2.09）
        checks["label_at_extremes"] = (len(iv1) == 1
                                       and abs(iv1[0]["t0"] - 0.99) < 0.005
                                       and abs(iv1[0]["t1"] - 1.51) < 0.005)
        # 双次波动 -> 拆成两个区间
        x2 = np.full(len(axis_l), 50.0)
        x2[100], x2[150], x2[220], x2[270] = 10.0, 90.0, 15.0, 85.0
        m2 = caliper_core.Measurement(
            file="label2", config="", mdf_version="", main_group=0,
            axis=axis_l, signals={"X": x2}, units={}, not_collected=[],
            requested=["X"], start_time_utc="", start_time_local="",
            quality={}, readback_ok={},
        )
        iv2 = evaluate(m2, [Rule("标注2", 1, "", ["X"], 0, 0, 0, {},
                                 conditions=[w1], join="all")]).results[0].intervals
        checks["label_split_swings"] = (len(iv2) == 2
                                        and abs(iv2[0]["t0"] - 0.99) < 0.005
                                        and abs(iv2[1]["t0"] - 2.19) < 0.005)
        # 三次跳变@0.80/1.00/1.20 -> 标注覆盖全部（0.79~1.21），不是只标最后一点
        y3 = np.zeros(len(axis_l))
        for t, val in [(0.8, 1.0), (1.0, 0.0), (1.2, 1.0)]:
            y3[int(round(t / 0.01)):] = val
        m3 = caliper_core.Measurement(
            file="label3", config="", mdf_version="", main_group=0,
            axis=axis_l, signals={"Y": y3}, units={}, not_collected=[],
            requested=["Y"], start_time_utc="", start_time_local="",
            quality={}, readback_ok={},
        )
        w3 = {"type": "changes", "signal": "Y", "window_ms": 600, "threshold": 2}
        iv3 = evaluate(m3, [Rule("标注3", 1, "", ["Y"], 0, 0, 0, {},
                                 conditions=[w3], join="all")]).results[0].intervals
        checks["label_covers_all_changes"] = (len(iv3) == 1
                                              and abs(iv3[0]["t0"] - 0.79) < 0.005
                                              and abs(iv3[0]["t1"] - 1.21) < 0.005)

        # 渐变型大波动（Pedl 真实场景）：相邻落差远小于 threshold，但窗口内极差 > threshold。
        # 命中判定绝不能被标注定位吞掉：evaluate 必须 hit、区间非空。
        axis_g = np.round(np.arange(0, 5.001, 0.01), 6)
        xg = np.zeros(len(axis_g))
        for i, t in enumerate(axis_g):
            if 1.0 <= t <= 2.5:
                xg[i] = (t - 1.0) / 1.5 * 80
            elif 2.5 < t <= 4.0:
                xg[i] = 80 - (t - 2.5) / 1.5 * 80
        mg = caliper_core.Measurement(
            file="gradual", config="", mdf_version="", main_group=0,
            axis=axis_g, signals={"X": xg}, units={}, not_collected=[],
            requested=["X"], start_time_utc="", start_time_local="",
            quality={}, readback_ok={},
        )
        wg = {"type": "swing", "signal": "X", "window_ms": 2000, "threshold": 60}
        rg = evaluate(mg, [Rule("渐变大波动", 1, "", ["X"], 0, 0, 0, {},
                                conditions=[wg], join="all")]).results[0]
        checks["gradual_swing_hit"] = (rg.status == "hit" and len(rg.intervals) >= 1)
        # 新语义：标注贴极值——峰@2.5 必在区间内，谷在 1.0 或 4.0；不再覆盖整段平稳期
        checks["gradual_swing_bounded"] = bool(rg.intervals) and \
            rg.intervals[0]["t0"] <= 2.5 <= rg.intervals[-1]["t1"] and \
            0.9 <= rg.intervals[0]["t0"] <= 1.1 and 3.9 <= rg.intervals[-1]["t1"] <= 4.1

        # 单峰脉冲（Pedl 大开大合真实形态）：0 猛升到 50@166.0 再缓降。
        # 标注必须框住谷@165.9 和峰@166.0 各外扩一格，不是整段拖尾。
        axis_p = np.round(np.arange(165.5, 168.001, 0.01), 6)
        xp = np.zeros(len(axis_p))
        for i, t in enumerate(axis_p):
            if t < 165.9:
                xp[i] = 0.0
            elif t <= 166.0:
                xp[i] = (t - 165.9) / 0.1 * 50
            elif t <= 167.8:
                xp[i] = 50 - (t - 166.0) / (167.8 - 166.0) * 36
            else:
                xp[i] = 14.0
        mp = caliper_core.Measurement(
            file="pulse", config="", mdf_version="", main_group=0,
            axis=axis_p, signals={"X": xp}, units={}, not_collected=[],
            requested=["X"], start_time_utc="", start_time_local="",
            quality={}, readback_ok={},
        )
        wp = {"type": "swing", "signal": "X", "window_ms": 2000, "threshold": 40}
        rp = evaluate(mp, [Rule("单峰脉冲", 1, "", ["X"], 0, 0, 0, {},
                                conditions=[wp], join="all")]).results[0]
        checks["pulse_swing_hit"] = rp.status == "hit"
        # 关键：区间必须窄，贴住谷峰；旧拖尾会标成 165.99~167.91
        checks["pulse_label_at_extremes"] = (
            len(rp.intervals) == 1
            and abs(rp.intervals[0]["t0"] - 165.89) < 0.02
            and abs(rp.intervals[0]["t1"] - 166.01) < 0.02)

        # 校验：kind 非法、window_ms<=0、窗口与跳变共存都要报错
        bad_doc = {"name": "t", "version": 1, "rules": [
            {"name": "bad kind", "when": "", "window": {
                "signal": "X", "kind": "oops", "window_ms": 500, "threshold": 10}}]}
        checks["validate_bad_kind"] = bool(validate_rules_doc(bad_doc))
        both = {"name": "t", "version": 1, "rules": [
            {"name": "both", "when": "", "window": {
                "signal": "X", "kind": "swing", "window_ms": 500, "threshold": 10},
             "transition": {"signal": "X", "from": 0, "to": 1}}]}
        checks["validate_no_both"] = bool(validate_rules_doc(both))
        # 窗口 + 空 when 必须校验通过（曾漏：window 分支后又走 else 校验空 when 报"条件不能为空"）
        good = {"name": "t", "version": 1, "rules": [
            {"name": "大油门", "when": "", "min_duration_ms": 200,
             "hysteresis_ms": 50, "merge_gap_ms": 100,
             "window": {"signal": "X", "kind": "swing", "window_ms": 2000,
                        "threshold": 20}}]}
        checks["validate_window_empty_when"] = validate_rules_doc(good) == []
    finally:
        if orig_extreme is not None:
            globals()["_rolling_extreme"] = orig_extreme
        if orig_mask is not None:
            globals()["_window_mask"] = orig_mask
        globals()["_SHAPE_WINDOW"] = orig_shape_window
        globals()["_NO_EXPAND"] = orig_no_expand

    print("窗口条件断言:", checks)
    for name, ok in checks.items():
        print("  %-28s %s" % (name, "OK" if ok else "FAIL"))
    return all(checks.values()), checks


def edit_self_test(bad=frozenset()):
    """规则「界面改、程序写」这条链的自检：双向转换、校验、写盘往返。

    bad 用来做变异验证：novalidate=不校验就存，mangle=强行把复杂表达式表格化，
    noreload=只改内存不写盘，floatnum=写盘时一律转浮点（200 变 200.0）。
    每一个都必须让对应断言变红。
    """
    checks = {}
    # 允许逗号分隔多个变异；默认参数是空集合，CLI 传进来的是字符串
    if not isinstance(bad, (set, frozenset)):
        bad = frozenset(t for t in re.split(r"[,\s]+", str(bad or "")) if t)

    # 1) 简单条件 <-> 表达式：往返只差空格，结构必须完全一致
    expr = "(DrvReq_tqWhlCrpReq > 200) & (DrvReq_bCrpIPartRst_MP == 0)"
    conds, join = when_to_conds(expr)
    checks["roundtrip_and"] = conds is not None and join == "&" and len(conds) == 2
    if "mangle" in bad:          # 变异：假装复杂表达式也能表格化
        conds, join = [("abs", ">", 0.0)], "&"
    checks["roundtrip_text"] = conds_to_when(
        conds or [], join or "&") == expr if conds else False

    # 2) 单条件不套括号（与手写规则文件风格一致）
    checks["single_no_paren"] = conds_to_when([("GrMod_stDrvAct", "==", 4.0)]) == \
        "GrMod_stDrvAct == 4"

    # 3) 改不回去的表达式必须原样保留，绝不能被改写
    hard = ["abs(X) < 5", "(A > 1) & (B > 1) | (C > 1)", "(A >> 2) == 1",
            "(A > 1) and (B > 1)"]
    checks["complex_kept_raw"] = all(when_to_conds(h)[0] is None for h in hard)

    # 4) 校验：空名 / 重名 / 错表达式 / 越界数值都要拦下来
    errs_blank = validate_rules_doc({"rules": [
        {"name": " ", "when": "A > 1"}]})
    errs_dup = validate_rules_doc({"rules": [
        {"name": "X", "when": "A > 1"}, {"name": "X", "when": "B > 1"}]})
    errs_expr = validate_rules_doc({"rules": [
        {"name": "X", "when": "A > "}]})
    errs_num = validate_rules_doc({"rules": [
        {"name": "X", "when": "A > 1", "min_duration_ms": -5}]})
    checks["reject_blank_name"] = any("规则名不能为空" in e for e in errs_blank)
    checks["reject_dup_name"] = any("重复" in e for e in errs_dup)
    checks["reject_bad_expr"] = bool(errs_expr)
    checks["reject_bad_number"] = bool(errs_num)
    checks["accept_good"] = validate_rules_doc({"name": "n", "version": 1, "rules": [
        {"name": "X", "when": "(A > 1) & (B == 0)", "min_duration_ms": 200}]}) == []
    if "novalidate" in bad:      # 变异：放过空名与错表达式
        checks["reject_blank_name"] = False
        checks["reject_bad_expr"] = False

    # 5) 写盘：版本自增、备份 == 原文件、字段顺序稳定、中文不转义、能再读回
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, "_ruleselftest.json")
    bak = path + ".bak"
    for p in (path, bak, path + ".tmp"):
        if os.path.exists(p):
            os.remove(p)
    doc = {"name": "自检规则集", "version": 3,
           "note": "自检用，跑完即删",
           "author": "henry", "tags": ["档位", "蠕行"],   # 自定义字段：保存后不许消失
           "rules": [{"name": "甲", "when": "(A > 200) & (B == 0)",
                      "min_duration_ms": 200, "hysteresis_ms": 50,
                      "merge_gap_ms": 100, "mark": {"color": "#123456", "label": "j"}}]}
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2, sort_keys=True)
    before = open(path, "rb").read()
    # 变异：把写盘用的 _num_out 退回「一律 float」，ints_stay_ints 必须因此变红。
    # 这里换的是被调用的实现，不是断言本身——改断言等于自己把答案改错。
    orig_num_out = globals()["_num_out"]
    if "floatnum" in bad:
        globals()["_num_out"] = float
    try:
        if "noreload" in bad:    # 变异：只改内存不写盘
            ver, bak_path, digest = 4, "", "0" * 32
        else:
            doc["rules"][0]["min_duration_ms"] = 800
            ver, bak_path, digest = save_ruleset(path, doc)
        raw = open(path, "rb").read().decode("utf-8")
    finally:
        globals()["_num_out"] = orig_num_out
    checks["bump_version"] = ver == 4
    checks["backup_is_original"] = os.path.exists(bak) and open(bak, "rb").read() == before
    checks["md5_matches_file"] = digest == md5_of(path)
    checks["keeps_chinese"] = "自检规则集" in raw and "\\u" not in raw
    checks["field_order"] = raw.index('"name"') < raw.index('"when"') < \
        raw.index('"min_duration_ms"')
    # 整数值不许被写成 200.0：每保存一次就在 diff 里刷一片 .0，会让人误判「规则被改过」。
    # 用正则取字面量再比字符串，不能用 "in"——"800.0" 里也含 "800"，那种写法是废断言。
    lits = dict(re.findall(
        r'"(min_duration_ms|hysteresis_ms|merge_gap_ms)":\s*(-?[0-9.]+)', raw))
    checks["ints_stay_ints"] = lits == {"min_duration_ms": "800",
                                        "hysteresis_ms": "50",
                                        "merge_gap_ms": "100"}
    saved = load_ruleset_doc(path)
    checks["value_written"] = saved["rules"][0]["min_duration_ms"] == 800
    checks["mark_kept"] = saved["rules"][0]["mark"] == {"color": "#123456", "label": "j"}
    checks["extra_fields_kept"] = (saved.get("author") == "henry"
                                   and saved.get("tags") == ["档位", "蠕行"])
    rs = load_ruleset(path)
    checks["reload_matches"] = (len(rs) == 1 and rs[0].version == 4
                                and rs[0].required_signals == ["A", "B"]
                                and rs[0].min_duration_ms == 800.0)

    # 6) 真小数必须原样保留：为了整数好看把 250.5 截成 250 是另一类数据损坏
    path2 = os.path.join(OUT_DIR, "_ruleselftest_frac.json")
    for p in (path2, path2 + ".bak", path2 + ".tmp"):
        if os.path.exists(p):
            os.remove(p)
    save_ruleset(path2, {"name": "小数", "version": 1, "rules": [
        {"name": "乙", "when": "A > 1", "min_duration_ms": 250.5}]})
    raw2 = open(path2, "rb").read().decode("utf-8")
    m2 = re.search(r'"min_duration_ms":\s*(-?[0-9.]+)', raw2)
    checks["fraction_kept"] = bool(m2) and m2.group(1) == "250.5"

    print("规则编辑自检（%d 项）:" % len(checks))
    for name, ok in checks.items():
        print("  %-26s %s" % (name, "OK" if ok else "FAIL"))
    for p in (path, bak, path + ".tmp", path2, path2 + ".bak", path2 + ".tmp"):
        if os.path.exists(p):
            os.remove(p)
    return all(checks.values()), checks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dat", help="待分析的 .dat / .blf 路径")
    ap.add_argument("--rules", default=first_ruleset(), help="规则集文件名（位于 rules/，缺省取第一个）")
    ap.add_argument("--dbc", nargs="*", default=None,
                    help="BLF 解码用 DBC 集合（缺省读 blf_config.json）")
    ap.add_argument("--tag", default="", help="输出文件名后缀，区分不同用例")
    ap.add_argument("--self-test", action="store_true", help="跑四类合成断言")
    ap.add_argument("--self-test-edit", action="store_true",
                    help="自检规则编辑链：条件双向转换、校验、写盘往返")
    ap.add_argument("--self-test-transition", action="store_true",
                    help="自检跳变条件：单点命中、时长忽略、缺信号三态")
    ap.add_argument("--self-test-window", action="store_true",
                    help="自检窗口条件：波动极差 / 变化次数 / min_step / when 组合")
    ap.add_argument("--bad-window", default="",
                    help="变异测试：badswing / nochanges / badshape / noexpand")
    ap.add_argument("--bad-transition", default="",
                    help="变异测试：nospot（跳变点错误展开成区间）")
    ap.add_argument("--bad-edit", default="",
                    help="变异测试：novalidate / mangle / noreload / floatnum（可逗号分隔）")
    ap.add_argument("--bad-eval", action="store_true",
                    help="变异测试：故意把 not_evaluated 折叠成 no_hit，断言应失败")
    args = ap.parse_args()

    if args.self_test_window or args.bad_window:
        ok, checks = window_self_test(bad=args.bad_window)
        if args.bad_window:
            if ok:
                print("REVERT-TO-FAIL FAILED：%s 没被断言抓到" % args.bad_window,
                      file=sys.stderr)
                sys.exit(1)
            print("REVERT-TO-FAIL OK：%s 被断言抓到（%s）"
                  % (args.bad_window, [k for k, v in checks.items() if not v]))
            return
        if not ok:
            print("WINDOW SELF-TEST FAILED：%s"
                  % [k for k, v in checks.items() if not v], file=sys.stderr)
            sys.exit(1)
        print("WINDOW SELF-TEST OK：波动 / 变化次数 / min_step / when 组合 / 缺信号三态 全过")
        return

    if args.self_test_transition or args.bad_transition:
        ok, checks = transition_self_test(bad=args.bad_transition)
        if args.bad_transition:
            if ok:
                print("REVERT-TO-FAIL FAILED：%s 没被断言抓到" % args.bad_transition,
                      file=sys.stderr)
                sys.exit(1)
            print("REVERT-TO-FAIL OK：%s 被断言抓到（%s）"
                  % (args.bad_transition, [k for k, v in checks.items() if not v]))
            return
        if not ok:
            print("TRANSITION SELF-TEST FAILED：%s"
                  % [k for k, v in checks.items() if not v], file=sys.stderr)
            sys.exit(1)
        print("TRANSITION SELF-TEST OK：跳变单点 / 时长忽略 / when 组合 / 缺信号三态 全过")
        return

    if args.self_test_edit or args.bad_edit:
        ok, checks = edit_self_test(args.bad_edit)
        if args.bad_edit:
            if ok:
                print("REVERT-TO-FAIL FAILED：%s 没被断言抓到" % args.bad_edit,
                      file=sys.stderr)
                sys.exit(1)
            print("REVERT-TO-FAIL OK：%s 被断言抓到（%s）"
                  % (args.bad_edit, [k for k, v in checks.items() if not v]))
            return
        if not ok:
            print("EDIT SELF-TEST FAILED：%s"
                  % [k for k, v in checks.items() if not v], file=sys.stderr)
            sys.exit(1)
        print("EDIT SELF-TEST OK：条件双向转换 / 校验 / 写盘往返全部通过")
        return

    if args.self_test:
        ok, checks = self_test(bad=args.bad_eval)
        if args.bad_eval:
            if ok:
                print("REVERT-TO-FAIL FAILED：三态折叠没被断言抓到", file=sys.stderr)
                sys.exit(1)
            print("REVERT-TO-FAIL OK：三态折叠被断言抓到（not_evaluated_status / summary_counts）")
            return
        if not ok:
            print("SELF-TEST FAILED：%s" % [k for k, v in checks.items() if not v], file=sys.stderr)
            sys.exit(1)
        print("SELF-TEST OK：命中/未命中/边界抖动/缺信号未评估 四类用例全部通过")
        return

    if not args.dat:
        ap.error("需要 --dat，或用 --self-test")

    rules_path = args.rules if os.path.isabs(args.rules) else os.path.join(RULES_DIR, args.rules)
    rs = load_ruleset(rules_path)
    # 规则引擎决定要加载哪些信号：取所有规则 required_signals 的并集。
    wanted = sorted({s for r in rs for s in r.required_signals})
    if args.dat.lower().endswith(".blf"):
        m = caliper_core.load_blf(args.dat, signals=wanted, dbc_paths=args.dbc)
    else:
        m = caliper_core.load(args.dat, signals=wanted)
    res = evaluate(m, rs)
    res.file_md5 = md5_of(args.dat)  # 证据哈希由调用方补入，evaluate 保持纯计算

    os.makedirs(OUT_DIR, exist_ok=True)
    stem = os.path.splitext(os.path.basename(args.dat))[0]
    stem += "_" + (args.tag or os.path.splitext(os.path.basename(rules_path))[0])
    out_path = os.path.join(OUT_DIR, "p2_result_%s.json" % stem)
    export_result(res, out_path)

    print("P2 OK：%s | 规则集 %s | %s" % (res.file, os.path.basename(rules_path), res.summary))
    for r in res.results:
        tail = ("缺信号: " + ", ".join(r.missing)) if r.status == "not_evaluated" \
            else ("%d 个区间" % len(r.intervals))
        print("  %-12s %-14s %s" % (r.rule_name, r.status, tail))
    print("  产出：%s" % out_path)


if __name__ == "__main__":
    main()
