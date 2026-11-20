# -*- coding: utf-8 -*-
"""验收专用的规则集夹具：内容内嵌，生成到 out/_verify_rules/。

验收不再依赖 rules/ 目录里的示例文件——用户可随意改名/增删示例，验收不受影响。
规则内容与既有独立证据一致（规则名/版本/信号），保证交叉验证仍成立。
"""
import json
import os

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(ROOT, "out")
FIXTURE_DIR = os.path.join(OUT_DIR, "_verify_rules")


def _rule(name, when, version=1, **extra):
    # version 是 doc 层字段，rule 本身不带（rule_version 取自 doc version）。
    # 若在 rule 里多写 version，RuleSetDialog.collect() 保存时会丢掉它，
    # 导致「打开即脏」「编辑其它规则受影响」两条断言误红。
    r = {"name": name, "when": when,
         "min_duration_ms": 200, "hysteresis_ms": 50, "merge_gap_ms": 100}
    r.update(extra)
    return r


# 规则内容来自既有独立证据反推（name/version/signal），不是猜测。
FIXTURES = {
    # 档位：p2_result_Creep02_gear.json 的 4 条，version=1
    # mark 字段必须与原 gear.json 一致：RuleSetDialog.collect() 会给缺 mark 的规则补 {},
    # 若夹具没 mark，编辑往返会「无改动却变脏」，dirty/others_untouched 断言就红。
    "gear": {"name": "档位工况", "version": 1, "note": "验收夹具：档位",
             "rules": [_rule("P档", "GrMod_stDrvAct == 1", mark={"color": "#5F5E5A", "label": "P"}),
                       _rule("R档", "GrMod_stDrvAct == 2", mark={"color": "#BA7517", "label": "R"}),
                       _rule("N档", "GrMod_stDrvAct == 3", mark={"color": "#185FA5", "label": "N"}),
                       _rule("D档", "GrMod_stDrvAct == 4", mark={"color": "#0F6E56", "label": "D"})]},
    # 蠕行：p2_result_行车中驾驶模式切换_creep.json 只有 1 条 version=2
    "creep": {"name": "蠕行扭矩工况", "version": 2, "note": "验收夹具：蠕行",
              "rules": [_rule("蠕行扭矩请求偏高", "DrvReq_tqWhlCrpReq > 1000", version=2)]},
    # 未评估用例：p2_result_..._creep_gap.json，缺 DrvReq_bCrpIPartRst_MP
    "creep_gap": {"name": "蠕行缺口工况", "version": 1, "note": "验收夹具：缺信号未评估",
                  "rules": [_rule("蠕行请求未被禁止位屏蔽",
                                  "(DrvReq_bCrpIPartRst_MP == 0) & (DrvReq_tqWhlCrpReq > 1000)")]},
    # BLF 参照规则集：p2_result_能耗_L003_blf_gear.json 的档位信号（带 _chN 后缀）
    "blf_gear": {"name": "BLF 档位工况", "version": 1, "note": "验收夹具：BLF 档位",
                 "rules": [_rule("P档_ch0", "VCU_1_G_PRNDGearAct_ch0 == 1"),
                           _rule("R档_ch0", "VCU_1_G_PRNDGearAct_ch0 == 2"),
                           _rule("N档_ch0", "VCU_1_G_PRNDGearAct_ch0 == 3"),
                           _rule("D档_ch0", "VCU_1_G_PRNDGearAct_ch0 == 4")]},
    # BLF 缺信号用例：p2_result_能耗_L003_blf_frre.json
    "blf_frre": {"name": "BLF 缺信号工况", "version": 1, "note": "验收夹具：BLF 缺信号",
                 "rules": [_rule("换挡可用_FR_RE", "HCU_GearShiftAvl_143h_ch0 == 1")]},
}


def paths():
    """生成所有夹具规则集，返回 {逻辑名: 路径}。内容固定 -> 路径固定，P2/md5 可复用。"""
    os.makedirs(FIXTURE_DIR, exist_ok=True)
    out = {}
    for key, doc in FIXTURES.items():
        path = os.path.join(FIXTURE_DIR, "%s.json" % key)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2, sort_keys=True)
        out[key] = path
    return out


# 模块加载即生成：验收里直接用 FIXTURE_PATHS["gear"] 等，不再拼 RULES_DIR。
FIXTURE_PATHS = paths()
