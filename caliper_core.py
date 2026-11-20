# -*- coding: utf-8 -*-
"""
数据内核，同时作为 CLI 使用。

能力：读取 -> 统一时基 -> 多栅格对齐(last-value-hold) -> 计算信号(公式/位)
      -> NaN/丢帧合理性检查 -> 时区统一。不做规则求值。
两类数据源：MDF（.dat / .mf4，经 asammdf，load()）与 .blf（CAN/LIN，凭 DBC 解码，
load_blf() / BlfSession 单遍流式扫描并缓存报文，勾选信号零重扫）。

数据契约：
  load(path, signals=None) -> Measurement      # 规则引擎只消费这个对象，不重新解析 MDF
  report(m: Measurement) -> dict               # 证据报告，纯计算，不读文件
  run_measurement(path, wanted)                # 薄封装，兼容既有 CLI 与回归证据

运行:
  "python" caliper_core.py --self-test
  "python" caliper_core.py --self-test --bad-align   # 变异测试
  "python" caliper_core.py --self-test-blf           # BLF 内核自检（合成 BLF，不需真实文件）
  "python" caliper_core.py "<你的数据目录>\\Creep02.dat"

关键约定:
1. 主组定位消费权威清单 out/channels.json，不重复实现一套判据；
   运行时不 import 离线工具 inventory.py。
2. 布尔/枚举/状态位一律 last-value-hold，绝不线性插值 —— 插值会造出
   根本不存在的中间枚举值。数值信号也统一用 hold，避免引入虚假样本。
3. 规则引用的信号允许在该文件不存在（三态语义）：记入 not_collected，不报错。
4. 表达式只来自本地规则文件，不接外部输入；eval 清空 builtins 并限白名单。
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import sys
from array import array
from dataclasses import dataclass
from decimal import Decimal

import numpy as np
from asammdf import MDF

ROOT = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
        else os.path.dirname(os.path.abspath(__file__)))  # 打包后 exe 所在目录即数据根目录
OUT_DIR = os.path.join(ROOT, "out")
INVENTORY = os.path.join(OUT_DIR, "channels.json")
DEFAULT_SIGNALS = [
    "GrMod_stDrvAct", "AccP_percAccrPedl", "ESC_stEPB",
    "VehMotRA_bCrpTqInh", "VeMod_bDrvrInput",
]
LOCAL_TZ = datetime.timezone(datetime.timedelta(hours=8))  # 实测 start_time 为 UTC，本地 +8
SAFE_ENV = {"np": np, "abs": abs, "min": min, "max": max, "round": round}
ALIAS_RE = re.compile(r"\\XCP.*$")


def norm(name):
    """去掉 INCA 的 \\XCP: 1 等事件后缀，得到归一化信号名。"""
    return ALIAS_RE.sub("", name).strip()


@dataclass
class Measurement:
    """内核 -> 规则引擎的核心数据契约。纯数据，不塞行为。

    除契约字段外附带 mdf_version / requested / readback_ok 等加载元数据，
    规则引擎只消费 axis / signals / units / not_collected。
    """
    file: str
    config: str
    mdf_version: str
    main_group: int
    axis: np.ndarray          # 统一时基（秒），保留原始非等间隔
    signals: dict             # name -> ndarray，已对齐到 axis
    units: dict
    not_collected: list       # 本次未采集的信号，是「未评估」的输入
    requested: list
    start_time_utc: str
    start_time_local: str
    quality: dict
    readback_ok: dict         # 加载时独立回读结果，仅作证据


def align_to_axis(ts, values, axis, hold=True):
    """把一条信号对齐到统一时间轴。

    hold=True  用 last-value-hold：取 <= 目标时刻的最近样本；轴起点早于首个样本时为 NaN。
    hold=False 线性插值，仅用于变异测试证明断言有区分力，正式路径禁用。
    """
    ts = np.asarray(ts, dtype=float)
    values = np.asarray(values, dtype=float)
    axis = np.asarray(axis, dtype=float)
    if hold:
        idx = np.clip(np.searchsorted(ts, axis, side="right") - 1, 0, len(ts) - 1)
        out = values[idx].copy()
        out[axis < ts[0]] = np.nan
        return out
    return np.interp(axis, ts, values)


def derive(expr, env):
    """计算信号：受限表达式求值，公式与位运算都走这里（P2 规则表达式复用此函数）。"""
    scope = dict(SAFE_ENV)
    scope.update(env)
    return eval(expr, {"__builtins__": {}}, scope)  # noqa: S307 - 表达式只来自本地规则文件


def quality(axis, signals):
    """丢帧与 NaN 检查：间隔超过 3 倍中位栅格记为一次丢帧。"""
    dt = np.diff(np.asarray(axis, dtype=float))
    med = float(np.median(dt)) if len(dt) else 0.0
    gaps = [
        {"t0": float(axis[i]), "t1": float(axis[i + 1]), "dt_ms": round(float(dt[i]) * 1000, 3)}
        for i in range(len(dt)) if med > 0 and dt[i] > 3 * med
    ]
    nan_counts = {}
    for name, v in signals.items():
        arr = np.asarray(v, dtype=float)
        nan_counts[name] = int(np.count_nonzero(np.isnan(arr)))
    return {
        "sample_count": len(axis),
        "raster_median_ms": round(med * 1000, 4),
        "raster_min_ms": round(float(dt.min()) * 1000, 4) if len(dt) else None,
        "raster_max_ms": round(float(dt.max()) * 1000, 4) if len(dt) else None,
        "gap_count": len(gaps),
        "gaps": gaps[:20],
        "nan_counts": nan_counts,
    }


def to_local(dt_utc):
    """MDF start_time 是 UTC，界面时点必须转本地，否则与试验记录对不上。"""
    if dt_utc is None:
        return None
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=datetime.timezone.utc)
    return dt_utc.astimezone(LOCAL_TZ).isoformat(timespec="seconds")


def locate(name):
    """从 P0 权威清单取主组/配置；找不到则由调用方回退到 scan()。"""
    with open(INVENTORY, encoding="utf-8") as f:
        inv = json.load(f)
    for item in inv["files"]:
        if item["file"] == name:
            return item
    raise KeyError("P0 清单里没有该文件：%s" % name)


def scan(path):
    """运行时扫描任意 MDF，不依赖 P0 清单。

    主组判据：通道数最多的组（实测 5 组类 109 通道 / 121 组类 259 通道，
    远大于标定常量小组）。返回与清单项兼容的 entry，外加全部可用信号列表，
    供 GUI 列出信号树并按 (group,index) 精确取数，不靠 channels_db 的名字索引。
    """
    m = MDF(path)
    try:
        groups = m.groups
        gi = max(range(len(groups)), key=lambda i: len(groups[i].channels))
        signals = []
        for ci, ch in enumerate(groups[gi].channels):
            raw = ch.name
            if raw == "time":
                continue
            signals.append({
                "name": norm(raw), "raw_name": raw, "unit": unit_of(ch),
                "group": gi, "index": ci,
            })
        return {
            "file": os.path.basename(path),
            "config": "5-group" if len(groups) == 5 else "121-group",
            "mdf_version": m.version,
            "main_group": gi,
            "group_count": len(groups),
            "signals": signals,
        }
    finally:
        m.close()   # 异常路径也必须关文件，否则临时探针文件删不掉


def unit_of(ch):
    """单位在 conversion.unit，channel.unit 实测全空。"""
    conv = getattr(ch, "conversion", None)
    return (getattr(conv, "unit", "") or "") if conv else ""


def load(path, signals=None, signal_map=None):
    """唯一加载入口：读文件 -> 统一时基 -> 对齐 -> 质量检查 -> Measurement。

    只做加载，不生成报告。规则引擎直接消费返回对象，避免重新解析 MDF。
    signal_map 为 {name: (group,index)}，由 GUI 的 scan() 提供，用于精确取数；
    不传时退回 channels_db 名字查找（保持既有行为不变）。
    """
    wanted = list(signals) if signals is not None else list(DEFAULT_SIGNALS)
    name = os.path.basename(path)
    try:
        entry = locate(name)
    except KeyError:
        # 任意未登记的测量文件也能打开：运行时扫描，不要求先跑 inventory.py
        entry = scan(path)
    main_group = entry["main_group"]
    m = MDF(path)

    # 只取主组里存在的信号；缺的不报错，记为 not_collected（v6 三态语义）。
    found, missing, units = {}, [], {}
    for sig_name in wanted:
        if signal_map and sig_name in signal_map:
            g, i = signal_map[sig_name]
        else:
            loc = m.channels_db.get(sig_name) or m.channels_db.get(sig_name + "\\XCP: 1")
            if not loc:
                missing.append(sig_name)
                continue
            g, i = loc[0]
        if g != main_group:
            missing.append(sig_name + "(不在主组)")
            continue
        found[sig_name] = (g, i)
        units[sig_name] = unit_of(m.groups[g].channels[i])
    if not found:
        # 规则集引用的信号一个都没采集：不报错，返回空 Measurement，
        # 由规则引擎判定所有规则为「未评估」（三态语义贯穿到内核）。
        utc, local = str(m.start_time), to_local(m.start_time)
        m.close()
        return Measurement(
            file=name, config=entry["config"], mdf_version=entry["mdf_version"],
            main_group=main_group, axis=np.array([], dtype=float),
            signals={}, units={}, not_collected=list(wanted), requested=wanted,
            start_time_utc=utc, start_time_local=local,
            quality=quality(np.array([], dtype=float), {}), readback_ok={},
        )

    # 统一时基 = 主组第一条信号的原始时间戳（保留非等间隔原貌，不强制等栅格）。
    first = m.get(group=found[list(found)[0]][0], index=found[list(found)[0]][1])
    axis = np.asarray(first.timestamps, dtype=float)
    aligned, raw_head = {}, {}
    for sig_name, (g, i) in found.items():
        s = m.get(group=g, index=i)
        vals = np.asarray(s.samples, dtype=float)
        aligned[sig_name] = align_to_axis(s.timestamps, vals, axis, hold=True)
        raw_head[sig_name] = [round(float(x), 6) for x in vals[:5]]

    # 独立回读：重新从文件取一次，核对前 5 个值，抓「读错文件/组/列」这类错误。
    readback_ok = {}
    for sig_name, (g, i) in found.items():
        again = np.asarray(m.get(group=g, index=i).samples, dtype=float)[:5]
        readback_ok[sig_name] = bool(np.allclose(again, np.asarray(raw_head[sig_name], dtype=float)))

    meas = Measurement(
        file=name, config=entry["config"], mdf_version=m.version,
        main_group=main_group, axis=axis, signals=aligned, units=units,
        not_collected=missing, requested=wanted,
        start_time_utc=str(m.start_time), start_time_local=to_local(m.start_time),
        quality=quality(axis, aligned), readback_ok=readback_ok,
    )
    m.close()
    return meas


def report(m):
    """从 Measurement 生成证据报告。纯计算，不读文件、不 import 离线工具。"""
    derived = {}
    if "AccP_percAccrPedl" in m.signals:
        derived["AccP_x2"] = derive("AccP_percAccrPedl * 2", m.signals)
    if "GrMod_stDrvAct" in m.signals:
        derived["Gear_low2bit"] = derive(
            "GrMod_stDrvAct & 3",
            {"GrMod_stDrvAct": np.asarray(m.signals["GrMod_stDrvAct"], dtype=np.int64)},
        )
    probe_axis = m.axis[::5][:10]
    probe = {}
    if "GrMod_stDrvAct" in m.signals:
        probe = {
            "axis": [round(float(t), 6) for t in probe_axis],
            "values": [round(float(x), 6) for x in
                       align_to_axis(m.axis, m.signals["GrMod_stDrvAct"], probe_axis, hold=True)],
        }
    return {
        "file": m.file,
        "config": m.config,
        "mdf_version": m.mdf_version,
        "main_group": m.main_group,
        "start_time_utc": m.start_time_utc,
        "start_time_local": m.start_time_local,
        "time_axis": {"t0": float(m.axis[0]), "t1": float(m.axis[-1]),
                      "count": len(m.axis), "source": "主组 time 通道，保留原始非等间隔"},
        "signals_requested": list(m.requested),
        "signals_aligned": sorted(m.signals),
        "signals_not_collected": m.not_collected,
        "raw_head": {k: [round(float(x), 6) for x in v[:5]] for k, v in m.signals.items()},
        "readback_ok": m.readback_ok,
        "quality": m.quality,
        "derived": {
            k: {"head": [round(float(x), 6) for x in v[:5]], "count": len(v)}
            for k, v in derived.items()
        },
        "probe_hold": probe,
    }


def run_measurement(path, wanted):
    """薄封装，兼容 P1 既有 CLI 与回归证据；新代码请直接用 load() + report()。"""
    return report(load(path, wanted))


# ---------------------------------------------------------------- BLF 分支
# 解码链：python-can 流式读帧 + canmatrix 解 DBC（asammdf 8.1.2 核心无 BLF 读取器）。
# 契约不变：产出仍是 Measurement；信号名 = DBC 名 + "_ch<通道>"（双通道都列）。
# 无效码保留原值；值表只供显示层枚举映射。
DBC_DIR = os.path.join(ROOT, "DBC")                    # DBC 文件与配置的统一存放目录
BLF_CONFIG = os.path.join(DBC_DIR, "blf_config.json")
BLF_DIFF_CAP = 2000
BLF_XCHECK_FRAMES = 32      # 向量化快路径每键抽样与 canmatrix 逐值核对的帧数
BLF_CHUNK = 1 << 16         # 原始 payload 分块累积帧数（批量 join 比逐帧 extend 快）


def resolve_dbc_path(entry):
    """配置项 -> 实际 DBC 完整路径。

    新配置只存文件名（如 A.dbc）-> DBC/A.dbc；旧配置存完整路径则原样保留（兼容迁移期）。
    """
    if os.path.isabs(entry):
        return entry
    return os.path.join(DBC_DIR, entry)


def blf_dbc_paths(dbc_paths=None):
    if dbc_paths:
        return list(dbc_paths)
    if os.path.exists(BLF_CONFIG):
        with open(BLF_CONFIG, encoding="utf-8") as f:
            paths = json.load(f).get("dbc_paths")
        if paths:
            return [resolve_dbc_path(p) for p in paths]
    raise ValueError("BLF 需要 DBC 集合：传 --dbc 或在 DBC/blf_config.json 里配置 dbc_paths")


def _fix_dbc_text(s):
    """canmatrix 用 latin-1 读 DBC 的 CM_，GBK 中文被解成 ×ÝÏò 这种乱码；修回 GBK。

    实测 loadp(encoding=...) 对 canmatrix 0.9x 无效，只能事后按字节还原。
    纯 ASCII（GBK 兼容）解完不变；本就正常的中文 encode latin-1 会失败，走 except 原样返回。
    修不了也不抛异常——描述只是展示用。
    """
    if not s:
        return ""
    try:
        return s.encode("latin-1").decode("gbk")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


def blf_load_dbcs(paths):
    import canmatrix.formats as cf
    out = []
    for p in paths:
        db = list(cf.loadp(p).values())[0]
        out.append({
            "key": os.path.splitext(os.path.basename(p))[0],
            "path": os.path.abspath(p),
            "db": db,
            "ids": {f.arbitration_id.id for f in db.frames},
            "by_id": {f.arbitration_id.id: f for f in db.frames},
        })
    keys = [d["key"] for d in out]
    if len(set(keys)) != len(keys):
        raise ValueError("DBC 文件名（去扩展名）重复，无法作为键：%s" % keys)
    for d in out:
        d["unique"] = (d["ids"] - set().union(*(o["ids"] for o in out if o is not d))
                       if len(out) > 1 else set(d["ids"]))
    return out


class _RawAcc:
    """单遍扫描时的紧凑累积器：payload 分块进 bytearray、时刻进 array('d')。

    逐帧 extend 大 bytearray 会反复 realloc 拷贝（实测比批量 join 慢），故先攒
    BLF_CHUNK 帧再一次性 extend；定长 DLC 键不存逐帧长度，省一份内存。
    """
    __slots__ = ("buf", "parts", "ts", "dlc", "width", "uniform")

    def __init__(self, first_len):
        self.buf = bytearray()
        self.parts = []
        self.ts = array("d")
        self.dlc = None
        self.width = first_len
        self.uniform = True

    def add(self, data, timestamp):
        self.parts.append(data)
        if len(self.parts) >= BLF_CHUNK:
            self.buf.extend(b"".join(self.parts))
            self.parts.clear()
        self.ts.append(timestamp)
        length = len(data)
        if length != self.width:
            if self.uniform:
                self.uniform = False
                self.dlc = array("B", [self.width]) * (len(self.ts) - 1)
            if length > self.width:
                self.width = length
            self.dlc.append(length)

    def flush(self):
        if self.parts:
            self.buf.extend(b"".join(self.parts))
            self.parts.clear()


def _blf_pack_raw(a):
    a.flush()
    n = len(a.ts)
    ts = np.frombuffer(a.ts, dtype=np.float64)
    flat = np.frombuffer(a.buf, dtype=np.uint8)
    if a.uniform:
        dlc = np.full(n, a.width, dtype=np.uint8)
        pay = flat.reshape(n, a.width) if n else np.zeros((0, a.width), dtype=np.uint8)
    else:
        dlc = np.frombuffer(a.dlc, dtype=np.uint8)
        pay = np.zeros((n, a.width), dtype=np.uint8)
        off = 0
        for i in range(n):                      # 变长 DLC：补齐到该键最大宽度
            length = int(dlc[i])
            pay[i, :length] = flat[off:off + length]
            off += length
    return {"ts": ts, "pay": pay, "dlc": dlc, "n": n}


def blf_scan(path, progress=None, collect=None):
    """流式单遍：(通道,ID) 计数/FD/首末时刻/间隔样本/首帧数据。不整文件进内存。

    collect 给 ID 集合时，同一遍顺带收下这些报文的原始时刻+payload（供零重扫解码）；
    不给则与原来完全一致，只出统计。
    """
    import can
    r = can.BLFReader(path)
    total = getattr(r, "object_count", None)
    stats, n, t0, t1 = {}, 0, None, None
    acc = {} if collect is not None else None
    for msg in r:
        n += 1
        key = (msg.channel, msg.arbitration_id)
        s = stats.get(key)
        if s is None:
            s = stats[key] = {"count": 0, "fd": 0, "first": msg.timestamp,
                              "last": msg.timestamp, "diffs": [],
                              "sample": bytes(msg.data), "sample_fd": bool(msg.is_fd)}
        else:
            if len(s["diffs"]) < BLF_DIFF_CAP:
                s["diffs"].append(msg.timestamp - s["last"])
            s["last"] = msg.timestamp
        s["count"] += 1
        s["fd"] += 1 if msg.is_fd else 0
        if acc is not None and msg.arbitration_id in collect:
            a = acc.get(key)
            if a is None:
                a = acc[key] = _RawAcc(len(msg.data))
            a.add(msg.data, msg.timestamp)
        if t0 is None:
            t0 = msg.timestamp
        t1 = msg.timestamp
        if progress and n % 2000000 == 0:
            progress(n, total)
    out = {"stats": stats, "frames": n, "t0": t0, "t1": t1, "objects": total}
    if acc is not None:
        out["raw"] = {k: _blf_pack_raw(a) for k, a in acc.items()}
        acc.clear()
    return out


def blf_resolve(scan, dbs, bad_map=False, priority_override=None):
    """自适应归属：通道优先级 = 唯一 ID 帧数主导；共有 ID 归优先级里头一个定义者。"""
    uniq_frames, cover = {}, {}
    for (ch, i), s in scan["stats"].items():
        for d in dbs:
            if i in d["unique"]:
                uniq_frames[(ch, d["key"])] = uniq_frames.get((ch, d["key"]), 0) + s["count"]
            if i in d["ids"]:
                cover[(ch, d["key"])] = cover.get((ch, d["key"]), 0) + s["count"]
    channels = sorted({ch for ch, _ in scan["stats"]})
    priority = {}
    for ch in channels:
        order = sorted((d["key"] for d in dbs),
                       key=lambda k: (-uniq_frames.get((ch, k), 0),
                                      -cover.get((ch, k), 0), k))
        if bad_map:
            order = order[::-1]
        if priority_override and str(ch) in priority_override:
            want = priority_override[str(ch)]
            order = [k for k in want if k in order] + [k for k in order if k not in want]
        priority[ch] = order
    by_key = {d["key"]: d for d in dbs}
    resolution, unresolved = {}, []
    for (ch, i) in sorted(scan["stats"]):
        owner = next((d["key"] for d in dbs if i in d["unique"]), None)
        if owner is None:
            owner = next((k for k in priority[ch] if i in by_key[k]["ids"]), None)
        if owner is None:
            unresolved.append([ch, i])
        else:
            resolution["%d:0x%X" % (ch, i)] = owner
    ambiguous = sorted(i for i in set().union(*(d["ids"] for d in dbs))
                       if sum(i in d["ids"] for d in dbs) > 1)
    return {"channels": channels, "priority": priority,
            "uniq_frames": {"%d:%s" % (ch, k): v for (ch, k), v in uniq_frames.items()},
            "resolution": resolution, "unresolved": unresolved, "ambiguous": ambiguous}


def blf_signals(scan, dbs, res):
    by_key = {d["key"]: d for d in dbs}
    sigs = []
    for ck, key in sorted(res["resolution"].items()):
        ch_s, id_s = ck.split(":")
        ch = int(ch_s)
        for sig in by_key[key]["by_id"][int(id_s, 16)].signals:
            entry = {"name": "%s_ch%d" % (sig.name, ch), "base": sig.name,
                     "ch": ch, "id": id_s, "dbc": key,
                     "unit": sig.unit or "", "bits": int(sig.size),
                     "desc": _fix_dbc_text(sig.comment or "").strip()}  # DBC CM_ 描述
            if sig.values:
                entry["values"] = {str(k): v for k, v in sig.values.items()}
            sigs.append(entry)
    return sigs


def _blf_stamp(path, dbc_paths):
    """会话复用指纹：BLF 的 mtime/size + 各 DBC 内容 md5（DBC 改了必须重扫）。"""
    st = os.stat(path)
    digests = []
    for p in dbc_paths:
        with open(p, "rb") as f:
            digests.append(hashlib.md5(f.read()).hexdigest())
    return (st.st_mtime_ns, st.st_size, tuple(digests))


def _blf_sig_plan(sig, frame_bits):
    """向量化提取资格 + 位布局/缩放参数；不合格返回 None（交回 canmatrix 逐帧解码）。

    实测本项目 4 个 DBC 共 2733 个信号全为 Motorola/无符号/整数，快路径可全覆盖；
    小端/浮点/有符号/超 64 位/越界/多路复用一个都不放过，一律回退。
    """
    if sig is None or sig.is_little_endian or sig.is_float or sig.is_signed:
        return None
    if getattr(sig, "multiplex", None) is not None:
        return None
    size, start = int(sig.size), int(sig.start_bit)
    if size < 1 or size > 64 or start < 0 or start + size > frame_bits:
        return None
    factor, offset = sig.factor, sig.offset
    if isinstance(factor, Decimal) and isinstance(offset, Decimal):
        k = max(-factor.as_tuple().exponent, -offset.as_tuple().exponent, 0)
        scale = 10 ** k
        num_f, num_o = int(factor * scale), int(offset * scale)
        # canmatrix 的 Decimal 乘加在 28 位有效数字内精确；超出就交回它，避免语义分叉
        if ((1 << size) - 1) * abs(num_f) + abs(num_o) >= 10 ** 28:
            return None
    else:
        scale, num_f, num_o = None, float(factor), float(offset)
    return {"start": start, "size": size, "F": num_f, "O": num_o, "scale": scale}


def _blf_raw_words(words, start, size):
    """Motorola 批提取：words 是 (n, W/8) 大端 uint64 字视图，start 为 canmatrix 内部
    MSB-first 线性位号（DBC @0 起始位经 loader 换算后即此值，不是 DBC 原值）。"""
    wi, off = start // 64, start % 64
    mask = np.uint64((1 << size) - 1) if size < 64 else np.uint64(0xFFFFFFFFFFFFFFFF)
    if off + size <= 64:
        return (words[:, wi] >> np.uint64(64 - off - size)) & mask
    take = 64 - off                      # 跨 64 位字：本字取低位、下一字取高位拼接
    hi = words[:, wi] & np.uint64((1 << take) - 1)
    lo = words[:, wi + 1] >> np.uint64(64 - (size - take))
    return (hi << np.uint64(size - take)) | lo


def _blf_phys(raw, plan):
    """与 canmatrix raw2phys 逐值同值：Decimal 精确乘加后用 int/int 的正确舍入除法。
    不能先把 factor 舍入成 float64 再乘——那会在阈值边界上翻判定。"""
    if plan["scale"] is None:
        return raw.astype(float) * plan["F"] + plan["O"]
    bound = ((1 << plan["size"]) - 1) * abs(plan["F"]) + abs(plan["O"])
    if bound < (1 << 63):
        num = raw.astype(np.int64) * plan["F"] + plan["O"]
        if plan["scale"] == 1:
            return num.astype(float)      # int64->float64 本身就是正确舍入
        return np.array([v / plan["scale"] for v in num.tolist()], dtype=float)
    return np.array([(int(v) * plan["F"] + plan["O"]) / plan["scale"]
                     for v in raw.tolist()], dtype=float)


def _blf_extract_vec(pay, size, plans, ts, bad=frozenset()):
    """整列 numpy 位运算 + 精确缩放。返回 base -> (ts, phys, raw, 行号)。"""
    n = pay.shape[0]
    if size % 8 == 0 and pay.shape[1] == size and pay.flags.c_contiguous:
        words = pay.view(">u8")           # 定长且整字节：零拷贝直接视图
    else:
        width = ((size + 7) // 8) * 8
        buf = np.zeros((n, width), dtype=np.uint8)
        buf[:, :size] = pay[:, :size]
        words = buf.view(">u8")
    rows = np.arange(n)
    out = {}
    for base, plan in plans.items():
        raw = _blf_raw_words(words, plan["start"], plan["size"])
        if "vecshift" in bad:
            raw = raw >> np.uint64(1)    # 变异：故意错一位，交叉核对应抓到
        out[base] = (ts, _blf_phys(raw, plan), raw, rows)
    return out


def _blf_extract_cm(frame, pay, dlc, ts, bases):
    """回退路径：canmatrix 逐帧解码（小端/浮点/有符号/多路复用/变长 DLC）。
    不用 db.decode：canmatrix 1.2 的 _frames_dict_id_extend 是类级共享缓存，
    多 DBC 同 ID 时先解码者会污染后解码者；直接用自己 by_id 的 Frame.decode。"""
    acc = {b: ([], [], [], []) for b in bases}
    for i in range(pay.shape[0]):
        dec = frame.decode(bytes(pay[i, :int(dlc[i])]))
        for b in bases:
            d = dec.get(b)
            if d is None:
                continue
            t, v, r, rows = acc[b]
            t.append(float(ts[i]))
            v.append(float(getattr(d, "phys_value", getattr(d, "raw_value", float("nan")))))
            r.append(int(getattr(d, "raw_value", 0)))
            rows.append(i)
    return {b: (np.asarray(t, dtype=float), np.asarray(v, dtype=float),
                np.asarray(r, dtype=np.int64), np.asarray(rows, dtype=np.int64))
            for b, (t, v, r, rows) in acc.items()}


def _blf_xcheck(frame, pay, dlc, plans, got, key):
    """快路径安全网：每键抽 32 帧用 canmatrix 重解，逐值不等即报错，绝不静默分叉。"""
    n = pay.shape[0]
    if not n:
        return
    idx = np.unique(np.linspace(0, n - 1, min(n, BLF_XCHECK_FRAMES)).astype(int))
    for i in idx:
        dec = frame.decode(bytes(pay[i, :int(dlc[i])]))
        for base in plans:
            d = dec.get(base)
            if d is None:
                continue
            ref = float(getattr(d, "phys_value", getattr(d, "raw_value", float("nan"))))
            val = float(got[base][1][i])
            if not (val == ref or (np.isnan(val) and np.isnan(ref))):
                raise RuntimeError(
                    "BLF 向量化提取与 canmatrix 不一致：%d:0x%X %s 第%d帧 %r != %r"
                    % (key[0], key[1], base, int(i), val, ref))


class BlfSession:
    """一次流式扫描（顺带收集原始报文）后会话常驻：后续勾选/改映射零重扫。

    缓存的是原始报文而非解码结果，每次 load 都重新提取+对齐；判定语义与 load_blf
    完全一致，规则评估仍由 caliper_rules 做。DBC 集合或文件变了才重扫（reusable 判定）。
    """

    def __init__(self, path, dbc_paths=None, channel_priority=None,
                 progress=None, bad=frozenset()):
        self.path = os.path.abspath(path)
        self.bad = set(bad)
        self.dbc_paths = [os.path.abspath(p) for p in blf_dbc_paths(dbc_paths)]
        self.priority = dict(channel_priority or {})
        self.dbs = blf_load_dbcs(self.dbc_paths)
        ids = set().union(*(d["ids"] for d in self.dbs)) if self.dbs else set()
        self.scan = blf_scan(path, progress=progress, collect=ids)
        self._stamp = _blf_stamp(self.path, self.dbc_paths)
        self._resolve()

    def _resolve(self):
        self.res = blf_resolve(self.scan, self.dbs, bad_map="map" in self.bad,
                               priority_override=self.priority)
        self.index = {s["name"]: s for s in blf_signals(self.scan, self.dbs, self.res)}

    def entry(self):
        return {"signals": blf_signals(self.scan, self.dbs, self.res),
                "res": self.res, "frames": self.scan["frames"], "session": self}

    def reusable(self, path, dbc_paths):
        return (os.path.abspath(path) == self.path
                and sorted(os.path.abspath(p) for p in dbc_paths) == sorted(self.dbc_paths)
                and _blf_stamp(self.path, self.dbc_paths) == self._stamp)

    def remap(self, channel_priority=None, bad=None):
        """只改通道优先级/变异开关：重算归属与信号索引，不重扫文件。"""
        if bad is not None:
            self.bad = set(bad)
        if channel_priority is not None:
            self.priority = dict(channel_priority)
        self._resolve()
        return self.entry()

    def load(self, signals=None):
        return _blf_measurement(self, signals)

    def extract_signal(self, name, axis):
        """只解码一条信号并 hold 对齐到给定 axis（增量勾选路径：零重扫、不碰其它信号）。

        复用 load() 的同一套位提取 / 32 帧交叉核对 / 首 5 帧回读，数值与全量逐值一致。
        返回 (values, unit, readback_ok)；信号没录到返回 None。
        调用前须确认该报文帧数 <= len(axis)：更密的信号会改变时基，必须回退全量 load。
        """
        e = self.index.get(name)
        if e is None:
            return None
        key = (e["ch"], int(e["id"], 16))
        r = (self.scan.get("raw") or {}).get(key)
        if r is None or not r["n"]:
            return None
        base = e["base"]
        frame = {d["key"]: d for d in self.dbs}[
            self.res["resolution"]["%d:0x%X" % key]]["by_id"][key[1]]
        ts, pay, dlc = r["ts"], r["pay"], r["dlc"]

        # 与 _blf_measurement 同一条快/慢路径判据：合格走向量化，否则 canmatrix 逐帧
        plan = _blf_sig_plan(frame.signal_by_name(base), frame.size * 8)
        plans = {base: plan} if plan is not None else None
        if plans and bool(np.all(dlc == frame.size)):
            got = _blf_extract_vec(pay, frame.size, plans, ts, self.bad)
            _blf_xcheck(frame, pay, dlc, plans, got, key)
        else:
            got = _blf_extract_cm(frame, pay, dlc, ts, [base])
        t, v, _raw, rows = got[base]
        values = align_to_axis(np.asarray(t), np.asarray(v), axis,
                               hold="align" not in self.bad)

        ok = True                      # 首 5 帧回读，独立核对（与全量路径同口径）
        for j in range(min(5, len(rows))):
            i = int(rows[j])
            d = frame.decode(bytes(pay[i, :int(dlc[i])])).get(base)
            if d is None:
                ok = False
                break
            v2 = float(getattr(d, "phys_value", getattr(d, "raw_value", float("nan"))))
            ok &= abs(v2 - float(v[j])) < 1e-9
        return values, e.get("unit", ""), bool(ok)

    def frame_count(self, name):
        """信号所属报文的帧数；增量路径据此判断会不会改变时基。"""
        e = self.index.get(name)
        if e is None:
            return 0
        key = (e["ch"], int(e["id"], 16))
        return int((self.scan.get("stats") or {}).get(key, {}).get("count", 0))


def _blf_measurement(sess, signals):
    """从会话缓存提取 wanted -> 统一时基 -> hold 对齐 -> Measurement（零重扫）。"""
    bad, scan, res, index = sess.bad, sess.scan, sess.res, sess.index
    by_key = {d["key"]: d for d in sess.dbs}
    wanted = list(signals) if signals is not None else []
    need, missing = {}, []
    for name in wanted:
        e = index.get(name)
        if e is None:
            missing.append(name)
            continue
        need.setdefault((e["ch"], int(e["id"], 16)), set()).add(e["base"])

    utc = datetime.datetime.fromtimestamp(scan["t0"], datetime.timezone.utc)
    if not need:
        return Measurement(
            file=os.path.basename(sess.path), config="blf-%dch" % len(res["channels"]),
            mdf_version="BLF", main_group=-1, axis=np.array([], dtype=float),
            signals={}, units={}, not_collected=list(wanted), requested=wanted,
            start_time_utc=utc.isoformat(timespec="seconds"),
            start_time_local=to_local(utc),
            quality=quality(np.array([], dtype=float), {}), readback_ok={})

    raw = scan.get("raw") or {}
    series, rawhead, ctr_seq, ctr_sig = {}, {}, {}, {}
    for key, bases in need.items():
        frame = by_key[res["resolution"]["%d:0x%X" % key]]["by_id"][key[1]]
        cs = next((s for s in frame.signals if "RollgCntr" in s.name), None)
        ctr_sig[key] = cs
        r = raw.get(key)
        if r is None or not r["n"]:
            continue
        ts, pay, dlc = r["ts"], r["pay"], r["dlc"]
        names = sorted(set(bases) | ({cs.name} if cs is not None else set()))
        plans = {}
        vec_ok = not (frame.is_multiplexed or frame.is_complex_multiplexed
                      or frame.is_pdu_container)
        for base in names:
            plan = _blf_sig_plan(frame.signal_by_name(base), frame.size * 8)
            plans[base] = plan
            vec_ok = vec_ok and plan is not None
        vec_ok = vec_ok and bool(np.all(dlc == frame.size))
        if vec_ok:
            got = _blf_extract_vec(pay, frame.size, plans, ts, bad)
            _blf_xcheck(frame, pay, dlc, plans, got, key)
        else:
            got = _blf_extract_cm(frame, pay, dlc, ts, names)
        for base in sorted(bases):
            t, v, _rv, rows = got[base]
            name = base if "suffix" in bad else "%s_ch%d" % (base, key[0])
            series[name] = (t, v)
            head = rawhead.setdefault(name, [])
            for j in range(min(5, len(rows))):
                i = int(rows[j])
                head.append((key[0], key[1], base, float(v[j]),
                             bytes(pay[i, :int(dlc[i])])))
        if cs is not None:
            ctr_seq[key] = (got[cs.name][2], int(cs.size))

    axis_key = max(need, key=lambda k: scan["stats"][k]["count"])
    axis_base = sorted(need[axis_key])[0]
    axis_name = axis_base if "suffix" in bad else "%s_ch%d" % (axis_base, axis_key[0])
    axis = np.array(series[axis_name][0], dtype=float)   # 拷贝：与会话缓存隔离，外部改轴不污染缓存
    aligned, units = {}, {}
    for name, (ts, vs) in series.items():
        aligned[name] = align_to_axis(np.asarray(ts), np.asarray(vs), axis,
                                      hold="align" not in bad)
        units[name] = index.get(name, {}).get("unit", "")
    readback_ok = {}
    for name, heads in rawhead.items():
        ok = True
        for ch, i, base, v, data in heads:
            owner = res["resolution"].get("%d:0x%X" % (ch, i))
            if owner is None:
                ok = False
                break
            dec = by_key[owner]["by_id"][i].decode(data)
            d = dec.get(base)
            v2 = float(getattr(d, "phys_value", getattr(d, "raw_value", float("nan"))))
            ok &= abs(v2 - v) < 1e-9
        readback_ok[name] = bool(ok)
    q = quality(axis, aligned)
    drops = {}
    for key, (vals, bits) in ctr_seq.items():
        # 模数取观测最大值+1：实测 UAES 计数器 0..14 回绕（mod 15），不是 2^bits；
        # 满量程计数器 max=2^bits-1 时该式同样给出 2^bits。
        if not len(vals):
            continue
        mod = int(vals.max()) + 1
        n_drop = int(np.count_nonzero((vals[:-1] + 1) % mod != vals[1:]))
        cname = "%s_ch%d" % (ctr_sig[key].name, key[0])
        drops[cname] = n_drop
    q["counter_drops"] = drops
    return Measurement(
        file=os.path.basename(sess.path), config="blf-%dch" % len(res["channels"]),
        mdf_version="BLF", main_group=-1, axis=axis, signals=aligned, units=units,
        not_collected=missing, requested=wanted,
        start_time_utc=utc.isoformat(timespec="seconds"), start_time_local=to_local(utc),
        quality=q, readback_ok=readback_ok)


def load_blf(path, signals=None, dbc_paths=None, channel_priority=None,
             progress=None, bad=frozenset()):
    """BLF 加载入口（单遍）：扫描+收集原始报文一遍 -> 只提取 wanted -> hold 对齐。

    bad 仅变异测试：align=插值替 hold，map=反转通道优先级，suffix=丢掉通道后缀，
    vecshift=向量化位提取故意错一位（交叉核对应报错）。
    """
    return BlfSession(path, dbc_paths, channel_priority, progress, bad).load(signals)


def self_test_blf(bad=frozenset()):
    """合成 BLF 独立真值：双 DBC 同 ID 不同 factor（考通道归属）、双通道同 ID 不同值
    （考后缀分离）、计数器跳变（考丢帧）、缺信号（考三态）。BLFWriter 造文件。

    另含 Motorola（@0+）报文考向量化快路径：16 位大端、12 位非对齐 + 0.1 factor、
    64 位满量程、4 位计数器；以及会话复用/改映射零重扫断言。
    bad：align/map/suffix/vecshift（位提取错一位）/nocache（丢会话重扫）。
    """
    import can
    bad = set(bad)
    d1 = os.path.join(OUT_DIR, "_blf_selftest1.dbc")
    d2 = os.path.join(OUT_DIR, "_blf_selftest2.dbc")
    d3 = os.path.join(OUT_DIR, "_blf_selftest3.dbc")
    blf = os.path.join(OUT_DIR, "_blf_selftest.blf")
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(d1, "w", encoding="utf-8") as f:
        f.write('VERSION ""\nNS_ :\nBS_:\nBU_: TEST\n'
                'BO_ 256 A: 8 TEST\n'
                ' SG_ X : 0|8@1+ (1,0) [0|255] "" TEST\n'
                ' SG_ RollgCntr1 : 8|4@1+ (1,0) [0|15] "" TEST\n'
                'BO_ 512 B: 8 TEST\n'
                ' SG_ Y : 0|16@1+ (0.1,0) [0|6553.5] "V" TEST\n'
                'BO_ 768 W: 8 TEST\n'
                ' SG_ W : 0|8@1+ (1,0) [0|255] "" TEST\n')
    with open(d2, "w", encoding="utf-8") as f:
        f.write('VERSION ""\nNS_ :\nBS_:\nBU_: TEST\n'
                'BO_ 256 A: 8 TEST\n'
                ' SG_ X : 0|8@1+ (10,0) [0|2550] "" TEST\n'
                ' SG_ RollgCntr1 : 8|4@1+ (1,0) [0|15] "" TEST\n'
                'BO_ 512 B: 8 TEST\n'
                ' SG_ Y : 0|16@1+ (0.1,0) [0|6553.5] "V" TEST\n'
                'BO_ 1024 V: 8 TEST\n'
                ' SG_ V : 0|8@1+ (1,0) [0|255] "" TEST\n')
    # Motorola 专用 DBC：ID 与 d1/d2 不重叠，不干扰既有归属断言
    with open(d3, "w", encoding="utf-8") as f:
        f.write('VERSION ""\nNS_ :\nBS_:\nBU_: TEST\n'
                'BO_ 1280 M: 8 TEST\n'
                ' SG_ MBig : 7|16@0+ (1,0) [0|65535] "" TEST\n'
                ' SG_ MScale : 23|12@0+ (0.1,0) [0|409.5] "V" TEST\n'
                ' SG_ RollgCntrM : 39|4@0+ (1,0) [0|15] "" TEST\n'
                'BO_ 1536 Q: 8 TEST\n'
                ' SG_ M64 : 7|64@0+ (1,0) [0|18446744073709551615] "" TEST\n')
    for p in (blf,):
        if os.path.exists(p):
            os.remove(p)
    w = can.BLFWriter(blf)
    xs = {0: [1, 1, 4, 4, 2], 1: [3, 3, 3, 1, 1]}
    ctr = {0: [0, 1, 2, 4, 5], 1: [0, 1, 2, 3, 4]}   # ch0 跳变一次 -> 丢帧 1
    ys = {0: [100, 100, 200, 200, 300], 1: [50, 50, 50, 60, 60]}
    for i in range(5):
        t = i * 0.01
        for ch in (0, 1):
            da = bytes([xs[ch][i], ctr[ch][i]]) + b"\x00" * 6
            w.on_message_received(can.Message(
                timestamp=t, arbitration_id=0x100, data=da,
                is_extended_id=False, channel=ch, is_fd=True))
            db_ = int(ys[ch][i]).to_bytes(2, "little") + b"\x00" * 6
            w.on_message_received(can.Message(
                timestamp=t + 0.005, arbitration_id=0x200, data=db_,
                is_extended_id=False, channel=ch, is_fd=True))
    # 唯一 ID 锚点：0x300 只在 ch0、0x400 只在 ch1 -> 自适应优先级 ch0=d1, ch1=d2
    w.on_message_received(can.Message(timestamp=0.0, arbitration_id=0x300,
                                      data=b"\x01" + b"\x00" * 7,
                                      is_extended_id=False, channel=0, is_fd=True))
    w.on_message_received(can.Message(timestamp=0.0, arbitration_id=0x400,
                                      data=b"\x01" + b"\x00" * 7,
                                      is_extended_id=False, channel=1, is_fd=True))
    # Motorola 真值（手算，独立于 canmatrix）：MBig=bytes0-1 大端；
    # MScale=byte2<<4 | byte3>>4，phys=raw*0.1；RollgCntrM=byte4>>4
    mbig = [0x0102, 0x0102, 0xABCD, 0xABCD, 0x0000]
    mscale = [10, 10, 4095, 4095, 0]
    mctr = [0, 1, 2, 4, 5]                      # 跳变一次 -> 丢帧 1
    mpay = []
    for i in range(5):
        b = bytearray(8)
        b[0] = (mbig[i] >> 8) & 0xFF
        b[1] = mbig[i] & 0xFF
        b[2] = (mscale[i] >> 4) & 0xFF
        b[3] = (mscale[i] & 0x0F) << 4
        b[4] = mctr[i] << 4
        mpay.append(bytes(b))
        w.on_message_received(can.Message(timestamp=i * 0.01, arbitration_id=0x500,
                                          data=mpay[-1], is_extended_id=False,
                                          channel=0, is_fd=True))
    m64 = [1, 2 ** 32 + 3, 2 ** 64 - 1, 2 ** 63, 7]
    qpay = [int(v).to_bytes(8, "big") for v in m64]
    for i in range(5):
        w.on_message_received(can.Message(timestamp=i * 0.01 + 0.005,
                                          arbitration_id=0x600, data=qpay[i],
                                          is_extended_id=False, channel=1, is_fd=True))
    w.stop()

    # 统计流式遍数：会话复用/改映射必须零重扫
    streams = []
    orig_reader = can.BLFReader

    class CountingReader(orig_reader):
        def __init__(self, *a, **k):
            streams.append(1)
            super().__init__(*a, **k)

    can.BLFReader = CountingReader
    vec_calls, cm_calls = [], []
    orig_vec, orig_cm = _blf_extract_vec, _blf_extract_cm

    def spy_vec(*a, **k):
        vec_calls.append(1)
        return orig_vec(*a, **k)

    def spy_cm(*a, **k):
        cm_calls.append(1)
        return orig_cm(*a, **k)

    globals()["_blf_extract_vec"], globals()["_blf_extract_cm"] = spy_vec, spy_cm
    try:
        m = load_blf(blf, signals=["X_ch0", "X_ch1", "Y_ch0", "RollgCntr1_ch0", "Z_ch0"],
                     dbc_paths=[d1, d2], bad=bad)
        g = lambda n: m.signals.get(n, np.array([]))
        checks = {}
        checks["axis_from_densest"] = len(m.axis) == 5 and abs(m.axis[2] - 0.02) < 1e-9
        checks["hold_values_ch0"] = np.array_equal(g("X_ch0"), [1, 1, 4, 4, 2])
        checks["map_channel_factor"] = np.array_equal(g("X_ch1"), [30, 30, 30, 10, 10])
        # Y 的首帧晚于轴起点：hold 语义下轴首点必须是 NaN（不插值、不借值）
        checks["factor_and_hold_nan"] = np.array_equal(
            g("Y_ch0"), [np.nan, 10.0, 10.0, 20.0, 20.0], equal_nan=True)
        checks["counter_drop_detected"] = m.quality["counter_drops"].get("RollgCntr1_ch0") == 1
        checks["missing_not_collected"] = m.not_collected == ["Z_ch0"]
        checks["readback_ok"] = all(m.readback_ok.values()) and bool(m.readback_ok)
        checks["local_tz"] = m.start_time_local.endswith("+08:00")

        # ---- Motorola 快路径：必须真的走向量化，且与 canmatrix 全帧逐值相等 ----
        vec_calls.clear()
        cm_calls.clear()
        try:
            m2 = load_blf(blf, signals=["MBig_ch0", "MScale_ch0", "RollgCntrM_ch0", "M64_ch1"],
                          dbc_paths=[d1, d2, d3], bad=bad)
        except RuntimeError as e:
            if "vecshift" in bad and "向量化" in str(e):
                return False, {"vec_xcheck_caught": True}
            raise
        g2 = lambda n: m2.signals.get(n, np.array([]))
        checks["vec_path_used"] = bool(vec_calls) and not cm_calls
        checks["motorola_big_endian"] = np.array_equal(
            g2("MBig_ch0"), [258, 258, 43981, 43981, 0])
        checks["motorola_12bit_scale"] = np.array_equal(
            g2("MScale_ch0"), [1.0, 1.0, 409.5, 409.5, 0.0])
        checks["motorola_64bit"] = np.array_equal(
            g2("M64_ch1"),
            [np.nan, 1.0, 4294967299.0, 1.8446744073709552e19, 9.223372036854776e18],
            equal_nan=True)
        checks["motorola_counter_drop"] = m2.quality["counter_drops"].get("RollgCntrM_ch0") == 1
        checks["motorola_readback"] = all(m2.readback_ok.values()) and bool(m2.readback_ok)
        db3 = blf_load_dbcs([d3])[0]["by_id"]
        for base, fid, pays, ts in (("MBig", 0x500, mpay, [i * 0.01 for i in range(5)]),
                                    ("MScale", 0x500, mpay, [i * 0.01 for i in range(5)]),
                                    ("M64", 0x600, qpay, [i * 0.01 + 0.005 for i in range(5)])):
            vals = [float(db3[fid].decode(p)[base].phys_value) for p in pays]
            ref = align_to_axis(np.asarray(ts), np.asarray(vals), m2.axis)
            checks["vec_equals_canmatrix_" + base] = np.array_equal(
                g2(base + ("_ch0" if fid == 0x500 else "_ch1")), ref, equal_nan=True)

        # ---- 会话复用：增量勾选 / 改通道映射都不许重扫 ----
        n0 = len(streams)
        sess = BlfSession(blf, dbc_paths=[d1, d2, d3])
        n_open = len(streams)
        a = sess.load(["X_ch0", "Y_ch0"])
        if "nocache" in bad:
            sess = BlfSession(blf, dbc_paths=[d1, d2, d3])   # 变异：丢会话，重新流式扫描
        b = sess.load(["X_ch0", "Y_ch0", "MBig_ch0"])
        checks["session_open_single_pass"] = n_open - n0 == 1
        checks["session_no_restream"] = len(streams) == n_open
        ga = lambda mm, n: mm.signals.get(n, np.array([]))
        checks["session_incremental_equal"] = (
            np.array_equal(ga(a, "X_ch0"), ga(b, "X_ch0"), equal_nan=True)
            and np.array_equal(ga(a, "Y_ch0"), ga(b, "Y_ch0"), equal_nan=True)
            and np.array_equal(a.axis, b.axis))
        fresh = load_blf(blf, signals=["X_ch0", "Y_ch0", "MBig_ch0"], dbc_paths=[d1, d2, d3])
        checks["session_matches_fresh"] = (
            np.array_equal(fresh.axis, b.axis)
            and all(np.array_equal(fresh.signals[n], b.signals[n], equal_nan=True)
                    for n in fresh.signals))
        n_before = len(streams)
        sess.remap(channel_priority={"0": ["_blf_selftest2"]})
        c = sess.load(["X_ch0"])
        checks["remap_no_restream"] = len(streams) == n_before
        checks["remap_changes_owner"] = np.array_equal(ga(c, "X_ch0"), [10, 10, 40, 40, 20])
        checks["session_reusable"] = sess.reusable(blf, [d1, d2, d3]) and not sess.reusable(
            blf, [d1, d2])
    finally:
        can.BLFReader = orig_reader
        globals()["_blf_extract_vec"], globals()["_blf_extract_cm"] = orig_vec, orig_cm
    for p in (d1, d2, d3, blf):
        if os.path.exists(p):
            os.remove(p)
    return all(checks.values()), checks


def self_test(bad=False):
    """合成用例：硬编码期望值（独立来源），断言能真正变红。

    枚举信号在 0/0.05/0.1s 取 1/4/2，对齐到 10ms 轴。
    hold 正确结果只能出现 1、4、2；线性插值会产生 1.6、2.2 等不存在的中间状态。
    """
    axis = np.round(np.arange(0, 0.101, 0.01), 6)
    ts = np.array([0.0, 0.05, 0.1])
    vals = np.array([1.0, 4.0, 2.0])
    expected = np.array([1, 1, 1, 1, 1, 4, 4, 4, 4, 4, 2], dtype=float)
    got = align_to_axis(ts, vals, axis, hold=not bad)
    linear = align_to_axis(ts, vals, axis, hold=False)
    ok = bool(np.array_equal(got, expected))
    discriminating = bool(np.any(np.abs(linear - np.round(linear)) > 1e-9))
    return ok, discriminating, got, linear


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file", nargs="?", help="待分析的 MDF (.dat/.mf4) 路径")
    ap.add_argument("--signals", default=",".join(DEFAULT_SIGNALS))
    ap.add_argument("--tag", default="", help="输出文件名后缀，区分不同用例，避免互相覆盖")
    ap.add_argument("--self-test", action="store_true", help="跑合成断言")
    ap.add_argument("--bad-align", action="store_true",
                    help="变异测试：故意用线性插值代替 last-value-hold，断言应失败")
    ap.add_argument("--self-test-blf", action="store_true", help="跑合成 BLF 真值断言")
    ap.add_argument("--bad-blf", default="",
                    help="变异测试：align（插值替 hold）/ map（反转通道优先级）/ suffix（丢通道后缀）"
                         "/ vecshift（位提取错一位）/ nocache（丢会话重扫）")
    args = ap.parse_args()

    if args.self_test_blf or args.bad_blf:
        bad = {t.strip() for t in args.bad_blf.split(",") if t.strip()}
        unknown = bad - {"align", "map", "suffix", "vecshift", "nocache"}
        if unknown:
            ap.error("--bad-blf 只能取 align / map / suffix / vecshift / nocache，收到：%s"
                     % ",".join(sorted(unknown)))
        ok, checks = self_test_blf(bad)
        for k, v in checks.items():
            print("  %-26s %s" % (k, "OK" if v else "FAIL"))
        if bad:
            if ok:
                print("REVERT-TO-FAIL FAILED：BLF 坏实现没被抓到", file=sys.stderr)
                sys.exit(1)
            print("REVERT-TO-FAIL OK：BLF 坏实现被断言抓到（%s）" % ",".join(sorted(bad)))
            return
        if not ok:
            print("SELF-TEST-BLF FAILED：%s"
                  % [k for k, v in checks.items() if not v], file=sys.stderr)
            sys.exit(1)
        print("SELF-TEST-BLF OK：合成 BLF 真值（归属/后缀/factor/计数器/三态/回读/时区）全过")
        return

    if args.self_test:
        ok, discriminating, got, linear = self_test(bad=args.bad_align)
        print("hold 对齐结果:", got.tolist())
        print("线性插值结果:", [round(float(x), 3) for x in linear])
        print("断言有区分力(坏版出现非整数中间值):", discriminating)
        if args.bad_align:
            if ok:
                print("REVERT-TO-FAIL FAILED：坏实现下断言仍然通过", file=sys.stderr)
                sys.exit(1)
            print("REVERT-TO-FAIL OK：坏实现被断言抓到")
            return
        if not ok or not discriminating:
            print("SELF-TEST FAILED：hold=%s discriminating=%s" % (ok, discriminating), file=sys.stderr)
            sys.exit(1)
        print("SELF-TEST OK：枚举信号 last-value-hold 对齐通过，且断言能区分坏实现")
        return

    if not args.file:
        ap.error("需要 MDF (.dat/.mf4) 路径，或用 --self-test")

    wanted = [s.strip() for s in args.signals.split(",") if s.strip()]
    result = run_measurement(args.file, wanted)
    os.makedirs(OUT_DIR, exist_ok=True)
    stem = os.path.splitext(os.path.basename(args.file))[0]
    stem = stem + ("_" + args.tag if args.tag else "")
    out_path = os.path.join(OUT_DIR, "p1_check_%s.json" % stem)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, sort_keys=True)

    q = result["quality"]
    print("P1 OK：%s | %s | 主组 g%d | %d 样本 | 栅格中位 %.3fms | 丢帧 %d"
          % (result["file"], result["config"], result["main_group"],
             q["sample_count"], q["raster_median_ms"], q["gap_count"]))
    print("  起始 UTC %s -> 本地 %s" % (result["start_time_utc"], result["start_time_local"]))
    print("  已对齐信号 %s" % ", ".join(result["signals_aligned"]))
    print("  本次未采集 %s" % (", ".join(result["signals_not_collected"]) or "无"))
    print("  独立回读全部通过：%s" % all(result["readback_ok"].values()))
    print("  计算信号：%s" % (", ".join(result["derived"]) or "无"))
    print("  产出：%s" % out_path)


if __name__ == "__main__":
    main()
