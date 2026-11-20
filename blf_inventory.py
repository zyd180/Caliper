# -*- coding: utf-8 -*-
"""BLF 通道清单与自适应归属 CLI。

解析/扫描/归属/信号清单的实现都在 caliper_core（单一实现，GUI 与 CLI 共用）；
本文件只做离线 CLI：自检、落盘、变异。DBC 集合由用户手选（--dbc 或 blf_config.json）。

运行:
  "python" blf_inventory.py --blf <file.blf> --dbc <a.dbc> <b.dbc> ...
变异:
  --bad-map        反转通道优先级，主导性/解码自检必须变红
  --drop-known X   从信号清单抹掉一个已知信号，全集核对必须变红
自检通过才落盘 out/blf_inventory.json / .csv；失败不写任何产出。
字节稳定性看 content_sha256 与 csv（json 含 generated_at，与 MDF 清单口径一致）。
"""
import argparse
import csv
import datetime
import hashlib
import json
import os
import sys
import time

import caliper_core

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(ROOT, "out")
CONFIG = os.path.join(ROOT, "DBC", "blf_config.json")


def md5_of(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def self_check(scan, dbs, res, sigs, drop_known=None, bad_map=False):
    """独立探针 + 全集核对 + 主导性 + 解码抽查。"""
    checks = {}
    names = {s["name"] for s in sigs}
    if drop_known:
        names.discard(drop_known)
    by_key = {d["key"]: d for d in dbs}
    probes = []
    for ch in res["channels"]:
        head = res["priority"][ch][0]
        uids = [i for i in by_key[head]["unique"] if (ch, i) in scan["stats"]]
        if not uids:
            continue
        top = max(uids, key=lambda i: scan["stats"][(ch, i)]["count"])
        probes.append("%s_ch%d" % (by_key[head]["by_id"][top].signals[0].name, ch))
    checks["probes_present"] = bool(probes) and all(p in names for p in probes)
    expected = set()
    for ck, key in res["resolution"].items():
        ch_s, id_s = ck.split(":")
        for sig in by_key[key]["by_id"][int(id_s, 16)].signals:
            expected.add("%s_ch%s" % (sig.name, ch_s))
    checks["signals_complete"] = names == expected
    dom = {}
    for ch in res["channels"]:
        tot = sum(v for k, v in res["uniq_frames"].items() if k.startswith("%d:" % ch))
        head_v = res["uniq_frames"].get("%d:%s" % (ch, res["priority"][ch][0]), 0)
        dom[ch] = (head_v / tot) if tot else 0.0
    checks["dominance"] = all(v > 0.5 for v in dom.values()) if dom else False
    checks["decode_spot"] = True
    for ch in res["channels"]:
        head = res["priority"][ch][0]
        uids = [i for i in by_key[head]["unique"] if (ch, i) in scan["stats"]]
        if not uids:
            checks["decode_spot"] = False
            continue
        top = max(uids, key=lambda i: scan["stats"][(ch, i)]["count"])
        try:
            dec = by_key[head]["by_id"][top].decode(scan["stats"][(ch, top)]["sample"])
            ok = len(dec) > 0
            for d in dec.values():
                float(getattr(d, "phys_value", getattr(d, "raw_value", 0)))
            checks["decode_spot"] &= ok
        except Exception:
            checks["decode_spot"] = False
    checks["no_bad_map"] = not bad_map
    return checks, probes, dom


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--blf", required=True)
    ap.add_argument("--dbc", nargs="*", default=None)
    ap.add_argument("--config", default=CONFIG)
    ap.add_argument("--bad-map", action="store_true")
    ap.add_argument("--drop-known", default=None)
    args = ap.parse_args()

    dbc_paths = args.dbc
    if not dbc_paths and os.path.exists(args.config):
        with open(args.config, encoding="utf-8") as f:
            dbc_paths = json.load(f).get("dbc_paths")
        if dbc_paths:
            dbc_paths = [caliper_core.resolve_dbc_path(p) for p in dbc_paths]
    if not dbc_paths:
        sys.exit("ERROR: 需要 --dbc 或 DBC/blf_config.json 里的 dbc_paths（DBC 由用户手选）")

    dbs = caliper_core.blf_load_dbcs(dbc_paths)
    t0 = time.time()

    def prog(n, total):
        print("  扫描 %d / %s 帧 (%.0fs)" % (n, total or "?", time.time() - t0))

    scan = caliper_core.blf_scan(args.blf, progress=prog)
    res = caliper_core.blf_resolve(scan, dbs, bad_map=args.bad_map)
    sigs = caliper_core.blf_signals(scan, dbs, res)
    checks, probes, dom = self_check(scan, dbs, res, sigs,
                                     drop_known=args.drop_known, bad_map=args.bad_map)
    print("通道优先级:", {ch: [k.split("version5.0_")[-1][:5] for k in v]
                          for ch, v in res["priority"].items()})
    print("主导性:", {k: round(v, 3) for k, v in dom.items()})
    print("探针:", probes)
    print("歧义 ID %d 个 / 未解析 ID %d 个 / 信号 %d 条"
          % (len(res["ambiguous"]), len(res["unresolved"]), len(sigs)))
    for k, v in checks.items():
        print("  %-16s %s" % (k, "OK" if v else "FAIL"))
    if not all(checks.values()):
        if args.bad_map or args.drop_known:
            print("REVERT-TO-FAIL OK：坏映射/缺信号被自检抓到")
            return
        print("SELF-CHECK FAILED", file=sys.stderr)
        sys.exit(1)
    if args.bad_map or args.drop_known:
        print("REVERT-TO-FAIL FAILED：变异没被自检抓到", file=sys.stderr)
        sys.exit(1)

    utc = datetime.datetime.fromtimestamp(scan["t0"], datetime.timezone.utc)
    result = {
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "generator": "blf_inventory.py (B0)",
        "blf": os.path.abspath(args.blf),
        "blf_md5": md5_of(args.blf),
        "size_bytes": os.path.getsize(args.blf),
        "frames": scan["frames"],
        "objects": scan["objects"],
        "span_s": round(scan["t1"] - scan["t0"], 3),
        "start_time_utc": utc.isoformat(timespec="seconds"),
        "start_time_local": utc.astimezone(caliper_core.LOCAL_TZ).isoformat(timespec="seconds"),
        "dbcs": [{"key": d["key"], "path": d["path"], "md5": md5_of(d["path"]),
                  "messages": len(d["db"].frames),
                  "signals": sum(len(f.signals) for f in d["db"].frames)} for d in dbs],
        "channels": {str(ch): {
            "frames": sum(s["count"] for (c, _), s in scan["stats"].items() if c == ch),
            "fd": sum(s["fd"] for (c, _), s in scan["stats"].items() if c == ch),
            "priority": res["priority"][ch],
            "raster_ms": {("0x%X" % i): round(1000 * sorted(s["diffs"])[len(s["diffs"]) // 2], 4)
                          for (c, i), s in sorted(scan["stats"].items())
                          if c == ch and s["diffs"]},
        } for ch in res["channels"]},
        "uniq_frames": res["uniq_frames"],
        "resolution": res["resolution"],
        "ambiguous_ids": ["0x%X" % i for i in res["ambiguous"]],
        "unresolved_ids": [[ch, "0x%X" % i] for ch, i in res["unresolved"]],
        "signals": sigs,
        "self_check": {"checks": checks, "probes": probes,
                       "dominance": dom, "passed": True,
                       "drop_known_simulation": args.drop_known,
                       "bad_map_simulation": args.bad_map},
    }
    payload = {k: v for k, v in result.items() if k != "generated_at"}
    result["content_sha256"] = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "blf_inventory.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    with open(os.path.join(OUT_DIR, "blf_inventory.csv"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["name", "base", "ch", "id", "dbc", "unit", "bits", "has_values"])
        for s in sigs:
            w.writerow([s["name"], s["base"], s["ch"], s["id"], s["dbc"],
                        s["unit"], s["bits"], int("values" in s)])
    print("SELF-CHECK PASS | 信号 %d | content_sha256=%s"
          % (len(sigs), result["content_sha256"][:16]))
    print("产出：out/blf_inventory.json / out/blf_inventory.csv")


if __name__ == "__main__":
    main()