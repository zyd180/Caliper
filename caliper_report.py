# -*- coding: utf-8 -*-
"""
规则集复用与可复现报告。

能力：规则集导出/导入往返、单文件多次套用不同规则集、可复现记录
（数据哈希 / 规则集哈希 / 时点 / 原始值 / 规则版本 / 三态评估状态 / 本地时间）。

运行:
  "python" caliper_report.py --dat "<你的数据目录>\\Creep02.dat" --rules <规则集> --tag <case>
  "python" caliper_report.py --verify --dat ... --rules <规则集> --tag <case>
  "python" caliper_report.py --verify ... --bad-record      # 变异测试
  "python" caliper_report.py --verify ... --bad-ruleset     # 变异测试
  "python" caliper_report.py --verify ... --simulate-network

记录不含时间戳，连跑两次字节一致；全程不截屏/录屏/抓大屏，纯文件与对象断言。
"""
import argparse
import datetime
import json
import os
import socket
import sys
from dataclasses import asdict

import caliper_core
import caliper_rules
import verify_fixtures as _fixtures

ROOT = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
        else os.path.dirname(os.path.abspath(__file__)))  # 打包后 exe 所在目录即数据根目录
OUT_DIR = os.path.join(ROOT, "out")
RULES_DIR = os.path.join(ROOT, "rules")


def first_ruleset():
    """第一个规则集（递归、排序稳定）作 CLI 缺省；复用 caliper_rules 的递归扫描。"""
    names = caliper_rules.list_rulesets()
    return names[0] if names else ""


def export_ruleset(rules, path, drop_last=False):
    """把 Rule 列表导出成规则集文件；只写必要字段，导入时会重新解析 when。

    drop_last 仅用于变异测试：故意丢一条规则，验证往返断言能变红。
    """
    subset = rules[:-1] if drop_last else rules
    doc = {
        "name": "导出规则集",
        "version": subset[0].version if subset else 1,
        "rules": [
            {
                "name": r.name, "when": r.when,
                "min_duration_ms": r.min_duration_ms,
                "hysteresis_ms": r.hysteresis_ms,
                "merge_gap_ms": r.merge_gap_ms,
                "mark": r.mark,
                **({"transition": r.transition} if getattr(r, "transition", None) else {}),
            }
            for r in subset
        ],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2, sort_keys=True)
    return path


def build_record(result, m, dat_path, rules_path, dbc_paths=None):
    """从 EvaluationResult + Measurement 组装可复现记录。不含时间戳，保证可重复。

    dbc_paths 仅 BLF 用例传入：记录里加 dbc_md5 表（MDF 记录不含该键，字节不变）。
    """
    base = None
    if m.start_time_local:
        try:
            base = datetime.datetime.fromisoformat(m.start_time_local)
        except ValueError:
            base = None

    def local_t(t):
        if base is None:
            return None
        return (base + datetime.timedelta(seconds=float(t))).isoformat(timespec="milliseconds")

    rules = []
    for r in result.results:
        intervals = []
        for iv in r.intervals:
            d = dict(iv)
            d["local_t0"] = local_t(iv["t0"])
            d["local_t1"] = local_t(iv["t1"])
            intervals.append(d)
        rules.append({
            "rule_name": r.rule_name,
            "rule_version": r.rule_version,
            "status": r.status,
            "missing": list(r.missing),
            "intervals": intervals,
        })

    rec = {
        "record_version": 1,
        "generator": "caliper_report.py (P4)",
        "data": {
            "file": os.path.basename(dat_path),
            "md5": caliper_rules.md5_of(dat_path),
            "size_bytes": os.path.getsize(dat_path),
            "mdf_version": m.mdf_version,
            "config": m.config,
            "main_group": m.main_group,
            "start_time_utc": m.start_time_utc,
            "start_time_local": m.start_time_local,
            "time_axis_t0": float(m.axis[0]) if len(m.axis) else None,
            "time_axis_t1": float(m.axis[-1]) if len(m.axis) else None,
        },
        "ruleset": {
            "file": os.path.basename(rules_path),
            "md5": caliper_rules.md5_of(rules_path),
            "version": result.ruleset_version,
        },
        "summary": dict(result.summary),
        "rules": rules,
    }
    if dbc_paths:
        rec["data"]["dbc_md5"] = {
            os.path.splitext(os.path.basename(p))[0]: caliper_rules.md5_of(p)
            for p in dbc_paths}
    return rec


def write_json(obj, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, sort_keys=True)


def run(dat, rules, dbc_paths=None):
    """加载 -> 评估 -> 可复现记录。返回 (Measurement, EvaluationResult, record)。"""
    rules_path = rules if os.path.isabs(rules) else os.path.join(RULES_DIR, rules)
    rs = caliper_rules.load_ruleset(rules_path)
    wanted = sorted({s for r in rs for s in r.required_signals})
    if dat.lower().endswith(".blf"):
        m = caliper_core.load_blf(dat, signals=wanted, dbc_paths=dbc_paths)
    else:
        m = caliper_core.load(dat, signals=wanted)
    result = caliper_rules.evaluate(m, rs)
    return m, result, build_record(result, m, dat, rules_path, dbc_paths)


def verify(dat, rules, tag, bad_record=False, bad_ruleset=False,
           simulate_network=False, p3_tag=None, dbc_paths=None):
    """程序化验收：三份独立来源（P2 结果 / P3 验收 / P4 记录）交叉核对。"""
    rules_path = rules if os.path.isabs(rules) else os.path.join(RULES_DIR, rules)
    ruleset = os.path.splitext(os.path.basename(rules_path))[0]
    stem = os.path.splitext(os.path.basename(dat))[0]
    p2_path = os.path.join(OUT_DIR, "p2_result_%s_%s.json" % (stem, ruleset))
    # 输出 tag 与验收报告 tag 解耦：变异测试用新 tag，仍可复用同一份来源。
    p3_path = os.path.join(OUT_DIR, "p3_verify_%s.json" % (p3_tag or tag))
    if not os.path.exists(p2_path) or not os.path.exists(p3_path):
        raise FileNotFoundError("缺少 P2/P3 独立来源：%s / %s" % (p2_path, p3_path))
    p2 = json.load(open(p2_path, encoding="utf-8"))
    p3 = json.load(open(p3_path, encoding="utf-8"))

    net_calls = []
    original_connect = socket.socket.connect

    def blocked(self, addr):
        net_calls.append(repr(addr))
        return None

    socket.socket.connect = blocked
    try:
        m, result, record = run(dat, rules_path, dbc_paths)

        # 规则集导出 -> 导入 -> 再评估，比对完整结果（含三态与区间）
        exported = os.path.join(OUT_DIR, "rules_export_%s.json" % tag)
        rs1 = caliper_rules.load_ruleset(rules_path)
        export_ruleset(rs1, exported, drop_last=bad_ruleset)
        rs2 = caliper_rules.load_ruleset(exported)
        again = caliper_rules.evaluate(m, rs2)
        roundtrip_ok = asdict(again) == asdict(result)

        # 单文件套用另一套规则集，两份记录必须互不影响
        # 用验收夹具里跟当前不同的那套，不硬编码示例文件名
        pool = (["blf_gear", "blf_frre"] if dat.lower().endswith(".blf")
                else ["gear", "creep", "creep_gap"])
        other_key = next((k for k in pool if k != ruleset), pool[0])
        other = _fixtures.FIXTURE_PATHS[other_key]
        _, other_result, other_record = run(dat, other, dbc_paths)
        isolated_ok = (other_record["data"]["md5"] == record["data"]["md5"]
                       and other_record["ruleset"]["file"] != record["ruleset"]["file"])

        if bad_record and record["rules"]:
            record["rules"][0]["status"] = "CORRUPTED"  # 变异测试：篡改三态
        if simulate_network:
            socket.socket().connect(("127.0.0.1", 1))

        # 记录与独立证据逐条核对（status / missing / 区间数 / snapshot 原始值）
        matches_p2 = record["summary"] == p2["summary"]
        if matches_p2 and len(record["rules"]) == len(p2["results"]):
            for a, b in zip(record["rules"], p2["results"]):
                if (a["status"] != b["status"] or a["missing"] != b["missing"]
                        or len(a["intervals"]) != len(b["intervals"])):
                    matches_p2 = False
                    break
                for ia, ib in zip(a["intervals"], b["intervals"]):
                    if ia["snapshot"] != ib["snapshot"] or ia["t0"] != ib["t0"]:
                        matches_p2 = False
                        break
        else:
            matches_p2 = False

        evidence_ok = all(
            set(iv) >= {"t0", "t1", "duration_ms", "snapshot", "local_t0", "local_t1"}
            for r in record["rules"] for iv in r["intervals"]
        ) and len(record["data"]["md5"]) == 32 and len(record["ruleset"]["md5"]) == 32
        stable_ok = build_record(result, m, dat, rules_path, dbc_paths) == record

        checks = {
            "record_matches_p2": matches_p2,
            "record_matches_p3": record["summary"] == p3["summary"],
            "ruleset_roundtrip": roundtrip_ok,
            "multi_ruleset_isolated": isolated_ok,
            "evidence_complete": evidence_ok,
            "record_stable": stable_ok,
            "no_network_call": len(net_calls) == 0,
        }
        os.makedirs(OUT_DIR, exist_ok=True)
        write_json(record, os.path.join(OUT_DIR, "record_%s.json" % tag))
        write_json(other_record, os.path.join(OUT_DIR, "record_%s_second.json" % tag))
        report = {
            "dat": os.path.basename(dat), "ruleset": ruleset,
            "summary": record["summary"],
            "other_ruleset": os.path.basename(other),
            "other_summary": other_result.summary,
            "checks": checks, "passed": all(checks.values()),
            "network_attempts": net_calls,
        }
        out = os.path.join(OUT_DIR, "p4_verify_%s.json" % tag)
        write_json(report, out)
        return all(checks.values()), report, out
    finally:
        socket.socket.connect = original_connect


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dat")
    ap.add_argument("--dbc", nargs="*", default=None,
                    help="BLF 解码用 DBC 集合（缺省读 blf_config.json）")
    ap.add_argument("--rules", default=first_ruleset(), help="规则集文件名（缺省取 rules/ 下第一个）")
    ap.add_argument("--tag", default="case")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--bad-record", action="store_true", help="变异测试：篡改记录里的三态")
    ap.add_argument("--bad-ruleset", action="store_true", help="变异测试：导出时丢一条规则")
    ap.add_argument("--simulate-network", action="store_true", help="变异测试：模拟连接尝试")
    ap.add_argument("--p3-tag", default=None,
                    help="P3 验收报告 tag；变异测试输出新 tag 时仍指向原用例")
    args = ap.parse_args()

    if not args.dat:
        ap.error("需要 --dat")
    if args.verify:
        ok, report, out = verify(args.dat, args.rules, args.tag,
                                 args.bad_record, args.bad_ruleset,
                                 args.simulate_network, args.p3_tag, args.dbc)
        for k, v in report["checks"].items():
            print("  %-26s %s" % (k, "OK" if v else "FAIL"))
        print("P4 VERIFY %s | %s | 产出 %s" % ("PASS" if ok else "FAIL", report["summary"], out))
        mutation = args.bad_record or args.bad_ruleset or args.simulate_network
        if mutation:
            if ok:
                print("REVERT-TO-FAIL FAILED：坏实现没被抓到", file=sys.stderr)
                sys.exit(1)
            print("REVERT-TO-FAIL OK：坏实现被断言抓到")
            return
        if not ok:
            sys.exit(1)
        return

    _, _, record = run(args.dat, args.rules, args.dbc)
    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, "record_%s.json" % args.tag)
    write_json(record, out)
    print("P4 记录已生成：%s | %s" % (out, record["summary"]))


if __name__ == "__main__":
    main()
