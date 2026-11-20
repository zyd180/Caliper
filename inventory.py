# -*- coding: utf-8 -*-
"""
通道清单生成器（MDF/INCA 测量文件）。

用本机 asammdf 8.1.2 读取 INCA MDF3 测量文件，产出：
  out/channels.json    权威通道清单（结构化，可独立复核）
  out/channels.csv     同一清单的扁平可读版（utf-8-sig，Excel 可直接打开）
  out/gear_rules.json  档位工况规则基线样例

运行:      "python" inventory.py
变异测试:  "python" inventory.py --drop-known GrMod_stDrvAct
           （模拟清单漏列一个已知信号，脚本必须非零退出并列出缺哪个）

关键约定（都是实测踩过的坑，勿改）:
1. 清单以 groups/channels 实际遍历为权威来源，不用 channels_db 的别名索引当清单。
   channels_db 里同一通道有多个别名 key，且索引可能缺项。
2. 别名归一化只去掉 "\\XCP..." 事件后缀；数组下标 [n] 保留，那是不同通道。
3. 单位在 conversion.unit，channel.unit 实测全空，必须读转换块。
4. 自检按实验配置分类：蠕行专属信号只在 5 组类文件里要求。121 组类文件按需采集，
   缺这些量属正常，不能判为失败（三态语义）。
5. 自检通过后才落盘，避免坏结果覆盖已有交付物。
"""
import argparse
import csv
import datetime
import hashlib
import json
import os
import re
import statistics
import sys

from asammdf import MDF

DATA_DIR = os.environ.get("CALIPER_DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "realdata"))
# 数据目录：优先环境变量 CALIPER_DATA_DIR，缺省用项目内 realdata/（隐私数据不入库）
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")

# 自检基准：硬编码的独立常量，与解析结果不同源。缺项时这条断言能真正变红，
# 不是"两边同一个集合自己比自己"的废断言。
UNIVERSAL = [
    "GrMod_stDrvAct", "AccP_percAccrPedl", "ESC_stEPB",
    "VehMotRA_bCrpTqInh", "VeMod_bDrvrInput",
]
CREEP_ONLY = [
    "DrvReq_tqCrpReqMerge_MP", "ER_CrpTqReq_GearR",
    "TOnD_CrpTqReq_tiCrpStuckDly",
]
ALIAS_RE = re.compile(r"\\XCP.*$")


def md5_of(path):
    """整文件 md5，用于精确去重（不靠文件名或大小猜）。"""
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def norm(name):
    """去掉 INCA 的 \\XCP: 1 等事件后缀，得到规则使用的归一化信号名。"""
    return ALIAS_RE.sub("", name).strip()


def unit_of(ch):
    """单位在转换块里，channel.unit 实测全空。"""
    conv = getattr(ch, "conversion", None)
    return (getattr(conv, "unit", "") or "") if conv else ""


def conv_type(ch):
    conv = getattr(ch, "conversion", None)
    return getattr(conv, "conversion_type", None) if conv else None


def inspect(path):
    """解析单个 MDF：文件级元数据 + 逐通道记录 + 档位信号独立复核。"""
    m = MDF(path)
    groups = m.groups
    # 主组 = 通道数最多的组。实测 5 组类为 109 通道、121 组类为 259 通道，
    # 远大于其余小组，判据稳定且不用硬编码组索引。
    gi_main = max(range(len(groups)), key=lambda i: len(groups[i].channels))
    main_ch = len(groups[gi_main].channels)
    sig = m.get(group=gi_main, index=1) if main_ch > 1 else None
    n_samples = len(sig.samples) if sig is not None else 0

    # 栅格取主组时间轴前 200 个间隔的中位数；实测并非严格等间隔（0.0098~0.0103s）。
    raster_ms = None
    if sig is not None and len(sig.timestamps) > 3:
        ts = sig.timestamps[:201]
        diffs = [float(b - a) for a, b in zip(ts[:-1], ts[1:])]
        raster_ms = round(statistics.median(diffs) * 1000, 4)

    records = []
    for gi, g in enumerate(groups):
        for ci, ch in enumerate(g.channels):
            raw = ch.name
            if raw == "time":
                continue  # time 是各组主通道，不是观测量
            records.append({
                "group": gi,
                "index": ci,
                "name": norm(raw),
                "raw_name": raw,
                "unit": unit_of(ch),
                "conversion_type": conv_type(ch),
                "is_main_group": gi == gi_main,
            })

    info = {
        "file": os.path.basename(path),
        "md5": md5_of(path),
        "size_bytes": os.path.getsize(path),
        "mtime": datetime.datetime.fromtimestamp(os.path.getmtime(path)).isoformat(timespec="seconds"),
        "mdf_version": m.version,
        "start_time_utc": str(m.start_time),
        "group_count": len(groups),
        "config": "5-group" if len(groups) == 5 else "121-group",
        "main_group": gi_main,
        "main_group_channels": main_ch,
        "main_group_samples": n_samples,
        "raster_ms": raster_ms,
        "channel_count": len(records),
        "unique_signal_names": len({r["name"] for r in records}),
        "channels": records,
    }

    # 独立复核档位信号：必须可定位，且取值落在 {1,2,3,4}。
    gear = m.channels_db.get("GrMod_stDrvAct") or m.channels_db.get("GrMod_stDrvAct\\XCP: 1")
    if gear:
        g0, i0 = gear[0]
        vals = m.get(group=g0, index=i0).samples
        info["gear_check"] = {
            "group": g0, "index": i0, "samples": len(vals),
            "values": sorted({round(float(x), 6) for x in vals.tolist()})[:8],
        }
    else:
        info["gear_check"] = None
    m.close()
    return info


def gear_rules():
    """P0 规则基线样例：只产出数据文件，不实现求值引擎（那是 P2）。"""
    spec = [
        ("P档", 1, "#5F5E5A", "P"),
        ("R档", 2, "#BA7517", "R"),
        ("N档", 3, "#185FA5", "N"),
        ("D档", 4, "#0F6E56", "D"),
    ]
    return {
        "name": "档位工况",
        "version": 1,
        "note": "P0 基线样例。阈值属于每条规则；规则引用的信号允许在文件中缺失（v6 三态）。",
        "rules": [
            {
                "name": n, "when": "GrMod_stDrvAct == %d" % v,
                "min_duration_ms": 200, "hysteresis_ms": 50, "merge_gap_ms": 100,
                "mark": {"color": c, "label": lab},
            }
            for n, v, c, lab in spec
        ],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--drop-known", default=None,
                    help="变异测试：模拟清单漏列一个已知信号，验证自检能报错")
    args = ap.parse_args()

    files = sorted(e.path for e in os.scandir(DATA_DIR)
                   if e.is_file() and e.name.lower().endswith(".dat"))
    if not files:
        sys.exit("ERROR: 未找到 .dat 文件：" + DATA_DIR)

    # 按整文件 md5 去重，保留排序最靠前的文件名作为代表。
    by_hash, dup = {}, {}
    for p in files:
        h = md5_of(p)
        if h in by_hash:
            dup.setdefault(h, [by_hash[h]]).append(os.path.basename(p))
        else:
            by_hash[h] = os.path.basename(p)
    reps = sorted(by_hash.values())
    print("文件总数 %d，去重后独立样本 %d" % (len(files), len(reps)))
    for h, names in dup.items():
        print("  重复组 md5=%s : %s" % (h[:8], " ≡ ".join(names)))

    result = {
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "data_dir": DATA_DIR,
        "generator": "inventory.py (P0)",
        "self_check": {
            "universal_signals": UNIVERSAL,
            "creep_only_signals": CREEP_ONLY,
            "note": "蠕行专属信号只对 5-group 类文件要求；121-group 缺这些量属正常。",
            "drop_known_simulation": args.drop_known,
            "passed": False,
        },
        "duplicates": {"md5=" + h: names for h, names in dup.items()},
        "files": [],
    }

    failures = []
    for name in reps:
        info = inspect(os.path.join(DATA_DIR, name))
        names = {r["name"] for r in info["channels"]}
        if args.drop_known:
            names.discard(args.drop_known)  # 只影响自检输入，不改动真实清单
        expected = set(UNIVERSAL)
        if info["config"] == "5-group":
            expected |= set(CREEP_ONLY)
        if not expected:
            sys.exit("ERROR: 自检基准为空，这条断言会是废断言")
        missing = sorted(expected - names)
        if missing:
            failures.append((name, missing))
        if not info["gear_check"]:
            failures.append((name, ["GrMod_stDrvAct 未能定位"]))
        elif not set(info["gear_check"]["values"]).issubset({1.0, 2.0, 3.0, 4.0}):
            failures.append((name, ["GrMod_stDrvAct 取值超出 {1,2,3,4}"]))
        result["files"].append(info)
        print("  %-40s %-10s groups=%3d channels=%4d unique=%4d main=g%-3d raster=%sms gear=%s"
              % (name[:40], info["config"], info["group_count"], info["channel_count"],
                 info["unique_signal_names"], info["main_group"], info["raster_ms"],
                 info["gear_check"]["values"] if info["gear_check"] else "MISSING"))

    if failures:
        for name, miss in failures:
            print("SELF-CHECK FAIL %s -> 缺: %s" % (name, ", ".join(miss)), file=sys.stderr)
        sys.exit(1)  # 非零退出，且不写任何产出文件

    result["self_check"]["passed"] = True
    # 稳定内容哈希：排除 generated_at。数据不变时两次运行必须一致，
    # 这样"连跑两次结果一致"能用哈希独立复核，而不是靠肉眼看文件。
    payload = {k: v for k, v in result.items() if k != "generated_at"}
    result["content_sha256"] = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    os.makedirs(OUT_DIR, exist_ok=True)

    with open(os.path.join(OUT_DIR, "channels.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    with open(os.path.join(OUT_DIR, "channels.csv"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["file", "config", "group", "index", "name", "raw_name",
                    "unit", "conversion_type", "is_main_group"])
        for info in result["files"]:
            for r in info["channels"]:
                w.writerow([info["file"], info["config"], r["group"], r["index"],
                            r["name"], r["raw_name"], r["unit"],
                            r["conversion_type"], int(r["is_main_group"])])

    with open(os.path.join(OUT_DIR, "gear_rules.json"), "w", encoding="utf-8") as f:
        json.dump(gear_rules(), f, ensure_ascii=False, indent=2)

    total = sum(i["channel_count"] for i in result["files"])
    print("SELF-CHECK PASS：%d 份独立样本，共 %d 条通道记录" % (len(result["files"]), total))
    print("content_sha256=%s（数据内容哈希，两次运行应一致）" % result["content_sha256"])
    print("产出：out/channels.json / out/channels.csv / out/gear_rules.json")


if __name__ == "__main__":
    main()
