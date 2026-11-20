# -*- coding: utf-8 -*-
"""
领域规则模板验收工具。

模板是可编辑的 JSON 数据，不是引擎代码；本脚本只做验证，不新增求值逻辑。

规则按 kind 分两类，验收口径分别裁剪（回收等级是配置量，
不主动调节必然恒值，恒值不是无效信号）：
  event  事件型 —— 要求跨文件同时存在命中与不命中的用例
  config 配置/等级型 —— 恒值属正常语义，不要求文件级区分；改用变体用例
         （把 when 改成实测值 / 不可能值）证明规则确实在求值、能翻转

断言：信号名必须来自权威清单、事件型规则可区分、配置型规则变体可翻转、
模板增删改不影响引擎、零网络调用。全程不截屏/录屏/抓大屏。

运行:
  "python" caliper_templates.py --verify
  "python" caliper_templates.py --verify --bad-template
  "python" caliper_templates.py --verify --simulate-network
"""
import argparse
import csv
import json
import os
import socket
import sys
from dataclasses import replace

import numpy as np

import caliper_core
import caliper_rules

ROOT = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
        else os.path.dirname(os.path.abspath(__file__)))  # 打包后 exe 所在目录即数据根目录
OUT_DIR = os.path.join(ROOT, "out")
TPL_DIR = os.path.join(ROOT, "rules", "templates")
INVENTORY_CSV = os.path.join(OUT_DIR, "channels.csv")
DATA_DIR = os.environ.get("CALIPER_DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "realdata"))
# 数据目录：优先环境变量 CALIPER_DATA_DIR，缺省用项目内 realdata/（隐私数据不入库）
# 9 份去重独立样本；文件名本身即工况标签，用作验证素材
FILES = [
    "20260813-Creep_Test.dat", "Creep.dat", "Creep01.dat", "Creep02.dat",
    "P挡原地踩油门.dat", "T1V_299975_20260812_正常行驶+挡位切换+驾驶模式切换.dat",
    "扭矩不响应.dat", "蠕行过路肩测试.dat", "行车中驾驶模式切换.dat",
]


def load_templates(bad=False):
    """扫描模板目录；pending.json 明确 enabled=false，只作清单不参与求值。"""
    out, pending = [], None
    for name in sorted(os.listdir(TPL_DIR)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(TPL_DIR, name)
        doc = json.load(open(path, encoding="utf-8"))
        if doc.get("enabled") is False:
            pending = doc
            continue
        rules = caliper_rules.load_ruleset(path)
        # kind 是模板元数据，引擎 load_ruleset 会忽略它；只用于裁剪验收口径
        kind = {item["name"]: item.get("kind", "event") for item in doc["rules"]}
        if bad and name == "gear_states.json" and rules:
            rules[0] = replace(rules[0], when="NoSuchSignal_X > 1",
                               required_signals=["NoSuchSignal_X"])
        out.append((name, rules, kind))
    return out, pending


def verify(bad_template=False, simulate_network=False):
    templates, pending = load_templates(bad=bad_template)
    if not templates:
        raise RuntimeError("没有可运行模板")

    # 独立来源：权威清单里的全部信号名，用于禁止编造信号
    known = {r["name"] for r in csv.DictReader(open(INVENTORY_CSV, encoding="utf-8-sig"))}
    all_signals = sorted({s for _, rs, _ in templates for r in rs for s in r.required_signals})
    signals_ok = all(s in known for s in all_signals)

    engine_before = {
        "caliper_rules.py": caliper_rules.md5_of(caliper_rules.__file__),
        "caliper_core.py": caliper_rules.md5_of(caliper_core.__file__),
    }

    net_calls = []
    original_connect = socket.socket.connect

    def blocked(self, addr):
        net_calls.append(repr(addr))
        return None

    socket.socket.connect = blocked
    try:
        # 每个文件只加载一次，加载所有模板所需信号；随后在内存里逐模板评估
        measurements = []
        for f in FILES:
            measurements.append((f, caliper_core.load(os.path.join(DATA_DIR, f), signals=all_signals)))

        tpl_report, event_distinguishable, all_evaluable = [], True, True
        for tpl_name, rules, kind in templates:
            per_rule = {r.name: {"hit": [], "no_hit": [], "not_evaluated": []} for r in rules}
            for f, m in measurements:
                res = caliper_rules.evaluate(m, rules)
                for rr in res.results:
                    per_rule[rr.rule_name][rr.status].append(f)
            rules_report = []
            for r in rules:
                d = per_rule[r.name]
                k = kind.get(r.name, "event")
                evaluable = len(d["hit"]) + len(d["no_hit"]) > 0
                all_evaluable &= evaluable
                if k == "config":
                    distinguishable = None  # 恒值配置量：文件级区分不适用
                else:
                    distinguishable = len(d["hit"]) > 0 and len(d["no_hit"]) > 0
                    event_distinguishable &= distinguishable
                rules_report.append({
                    "name": r.name, "when": r.when, "kind": k,
                    "hit_files": d["hit"][:3], "no_hit_files": d["no_hit"][:3],
                    "not_evaluated_files": d["not_evaluated"][:3],
                    "hit_count": len(d["hit"]), "no_hit_count": len(d["no_hit"]),
                    "distinguishable": distinguishable,
                })
            tpl_report.append({"file": tpl_name, "rule_count": len(rules), "rules": rules_report})

        # 配置型规则变体验收：实测值应命中、不可能值应不命中，证明不是恒真占位
        config_variants = {}
        for tpl_name, rules, kind in templates:
            for r in rules:
                if kind.get(r.name) != "config":
                    continue
                sig = r.required_signals[0]
                for f, m in measurements:
                    if sig not in m.signals:
                        continue
                    arr = np.asarray(m.signals[sig], dtype=float)
                    finite = arr[~np.isnan(arr)]
                    if not len(finite):
                        continue
                    val = float(finite[0])
                    hit_rule = replace(r, when="%s == %g" % (sig, val))
                    miss_rule = replace(r, when="%s == 999999" % sig)
                    hs = caliper_rules.evaluate(m, [hit_rule]).results[0]
                    ms = caliper_rules.evaluate(m, [miss_rule]).results[0]
                    config_variants[r.name] = {
                        "signal": sig, "measured_value": val, "file": f,
                        "hit_variant_status": hs.status,
                        "miss_variant_status": ms.status,
                        "flipped": hs.status == "hit" and ms.status == "no_hit",
                    }
                    break

        # 模板增删改：只改数据，引擎代码零改动
        crud_m = caliper_core.load(os.path.join(DATA_DIR, "P挡原地踩油门.dat"), signals=all_signals)
        gear_rules = next(rs for n, rs, _ in templates if n == "gear_states.json")
        base = caliper_rules.evaluate(crud_m, gear_rules)
        added = gear_rules + [replace(gear_rules[0], name="新增副本规则")]
        deleted = gear_rules[:-1]
        changed = [replace(gear_rules[0], when="GrMod_stDrvAct == 999")] + gear_rules[1:]
        r_add = caliper_rules.evaluate(crud_m, added)
        r_del = caliper_rules.evaluate(crud_m, deleted)
        r_chg = caliper_rules.evaluate(crud_m, changed)
        crud = {
            "add": len(r_add.results) == len(base.results) + 1,
            "delete": len(r_del.results) == len(base.results) - 1,
            "edit": (len(r_chg.results[0].intervals) == 0
                     and len(base.results[0].intervals) > 0),
        }
        engine_after = {
            "caliper_rules.py": caliper_rules.md5_of(caliper_rules.__file__),
            "caliper_core.py": caliper_rules.md5_of(caliper_core.__file__),
        }
        if simulate_network:
            socket.socket().connect(("127.0.0.1", 1))

        checks = {
            "signals_from_p0_inventory": signals_ok,
            "event_rules_distinguishable": event_distinguishable,
            "config_rules_variant_flips": len(config_variants) >= 3
                                          and all(v["flipped"] for v in config_variants.values()),
            "all_rules_evaluable": all_evaluable,
            "template_crud_ok": all(crud.values()),
            "engine_unchanged": engine_before == engine_after,
            "pending_documented": bool(pending)
                                  and len(pending.get("pending_domains", [])) == 1
                                  and pending["pending_domains"][0]["domain"] == "故障位"
                                  and len(pending.get("resolved_domains", [])) == 1,
            "no_network_call": len(net_calls) == 0,
        }
        report = {
            "templates": tpl_report,
            "unique_signals_used": all_signals,
            "config_variants": config_variants,
            "template_crud": crud,
            "engine_md5": engine_before,
            "pending_domains": [d["domain"] for d in (pending or {}).get("pending_domains", [])],
            "resolved_domains": [d["domain"] for d in (pending or {}).get("resolved_domains", [])],
            "checks": checks, "passed": all(checks.values()),
            "network_attempts": net_calls,
            "bad_template": bad_template,
        }
        os.makedirs(OUT_DIR, exist_ok=True)
        # 变异测试写独立文件，避免覆盖已通过的正常验收证据
        suffix = "_bad_template" if bad_template else ("_bad_network" if simulate_network else "")
        out = os.path.join(OUT_DIR, "p5_verify%s.json" % suffix)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2, sort_keys=True)
        return all(checks.values()), report, out
    finally:
        socket.socket.connect = original_connect


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--bad-template", action="store_true", help="变异测试：规则引用不存在的信号")
    ap.add_argument("--simulate-network", action="store_true", help="变异测试：模拟连接尝试")
    args = ap.parse_args()
    if not args.verify:
        ap.error("本工具只提供 --verify 程序化验收")

    ok, report, out = verify(args.bad_template, args.simulate_network)
    for k, v in report["checks"].items():
        print("  %-32s %s" % (k, "OK" if v else "FAIL"))
    for t in report["templates"]:
        bad = [r["name"] for r in t["rules"] if r["distinguishable"] is False]
        cfg = [r["name"] for r in t["rules"] if r["kind"] == "config"]
        print("  模板 %-22s 规则 %d | 事件型无区分力: %s | 配置型: %s"
              % (t["file"], t["rule_count"], bad or "无", cfg or "无"))
    for name, v in report["config_variants"].items():
        print("  配置变体 %-16s 实测值=%g hit_variant=%s miss_variant=%s flipped=%s"
              % (name, v["measured_value"], v["hit_variant_status"],
                 v["miss_variant_status"], v["flipped"]))
    print("P5 VERIFY %s | 模板 %d | 信号 %d | 产出 %s"
          % ("PASS" if ok else "FAIL", len(report["templates"]),
             len(report["unique_signals_used"]), out))
    mutation = args.bad_template or args.simulate_network
    if mutation:
        if ok:
            print("REVERT-TO-FAIL FAILED：坏实现没被抓到", file=sys.stderr)
            sys.exit(1)
        print("REVERT-TO-FAIL OK：坏实现被断言抓到")
        return
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
