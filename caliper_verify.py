# -*- coding: utf-8 -*-
"""Caliper 程序化验收套件（offscreen，绝不截屏/录屏/抓大屏）。

从 caliper_gui.py 拆出，纯搬运不改逻辑。依赖方向：本模块 -> caliper_gui（验收用业务类/常量）；
caliper_gui 只在 main() 的验收分支里延迟 import 本模块，GUI 启动不加载这里。
命令行入口仍是 `python caliper_gui.py --verify*`，由 caliper_gui.main() 转发到 run_cli_verify()。
"""
import argparse
import csv
import json
import math
import os
import re
import shutil
import socket
import sys

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtTest, QtWidgets

import caliper_gui
from caliper_gui import (
    AddSignalsDialog, BlfMapDialog, CURSOR2_COLOR, CURVE_COLORS, ConditionDialog, DATA_DIR,
    MainWindow, MdaViewBox, MultiRulesDialog, NO_RULES_LABEL, OUT_DIR, ROOT,
    PER_AXIS_LABEL, RULES_DIR, RULE_CURVE_MIN_DIST, RULE_MARK_COLORS,
    RULE_NAME_ROLE, RULE_PAIR_MIN_DIST, RelativeTimeAxis, RuleSetDialog,
    SHARED_AXIS_LABEL, SIGNAL_DESC, USER_ROLE, Y_AXIS_MIN_WIDTH,
    _color_dist, _enum_suffix, _fmt_value, _header_snapshot, _near, apply_theme,
)
import caliper_core
import caliper_rules
import caliper_report
import caliper_templates
import verify_fixtures as _fixtures

def verify(dat, rules, tag, bad_render=False, simulate_network=False, bad_nev=False):
    """程序化验收：不截屏、不录屏、不抓大屏，只检查 Qt 对象与标记数量。"""
    rules_path = rules if os.path.isabs(rules) else os.path.join(RULES_DIR, rules)
    ruleset = os.path.splitext(os.path.basename(rules_path))[0]
    stem = os.path.splitext(os.path.basename(dat))[0]
    p2_path = os.path.join(OUT_DIR, "p2_result_%s_%s.json" % (stem, ruleset))
    if not os.path.exists(p2_path):
        raise FileNotFoundError("缺少独立 P2 清单，无法做交叉核对：" + p2_path)
    with open(p2_path, encoding="utf-8") as f:
        p2 = json.load(f)

    net_calls = []
    original_connect = socket.socket.connect

    def blocked(self, addr):
        net_calls.append(repr(addr))
        return None

    socket.socket.connect = blocked
    try:
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        w = MainWindow()
        w.open_file(dat, auto=False)
        w.load_rules(rules_path)
        result = w.run_analysis()
        if bad_render and w.regions:
            w.plot.removeItem(w.regions.pop())
        if bad_nev:                      # 变异：把「未评估」写成「0 命中」，三态红线应被抓
            nev = w.rule_tree.topLevelItem(2)
            for i in range(nev.childCount()):
                nev.child(i).setText(0, "0 命中")
        clicked = w.click_first_hit()
        if simulate_network:
            socket.socket().connect(("127.0.0.1", 1))

        p2_total = p2["summary"]["total_intervals"]
        s = result.summary
        checks = {
            "regions_match_p2": len(w.regions) == p2_total,
            "markers_match_p2": w.marker_count == p2_total,
            "summary_matches_p2": s == p2["summary"],
            "three_state_partition": [
                w.rule_tree.topLevelItem(i).childCount() for i in range(3)
            ] == [s["hit"], s["no_hit"], s["not_evaluated"]],
            # 未评估组必须先有它该有的条数，再逐条核对文案：
            # 空集合上的 all() 恒真，不先比条数就等于没断言。
            "not_evaluated_distinct": (
                w.rule_tree.topLevelItem(2).childCount() == s["not_evaluated"]
                and all("未评估" in w.rule_tree.topLevelItem(2).child(i).text(0)
                        and "0 命中" not in w.rule_tree.topLevelItem(2).child(i).text(0)
                        for i in range(s["not_evaluated"]))
            ),
            # 按用例裁剪：无命中区间的用例不该要求「点击回跳」，否则是空断言
            "click_jump": (clicked and w.last_jump is not None
                           and abs(w.cursor.value() - w.last_jump[0]) < 1e-6)
                          if p2_total > 0 else (not clicked and w.last_jump is None),
            "local_time_shown": w.local_time_text.startswith("本地时间：2026-")
                                if p2_total > 0 else (w.local_time_text == ""),
            "no_network_call": len(net_calls) == 0,
        }
        report = {
            "dat": os.path.basename(dat), "ruleset": ruleset,
            "p2_total_intervals": p2_total, "gui_region_count": len(w.regions),
            "gui_marker_count": w.marker_count, "summary": s,
            "checks": checks, "passed": all(checks.values()),
            "bad_render": bad_render, "network_attempts": net_calls,
        }
        os.makedirs(OUT_DIR, exist_ok=True)
        out = os.path.join(OUT_DIR, "p3_verify_%s.json" % tag)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2, sort_keys=True)
        return all(checks.values()), report, out
    finally:
        socket.socket.connect = original_connect


def verify_add_signals(dat=None, bad=frozenset()):
    """新验收：任意未登记文件可打开、信号可勾选增删、工况标记不受影响。

    dat 缺省用 Creep02.dat；换数据集时先用 caliper_rules.py 生成同名独立清单（硬依赖）。
    变异 nodesc=描述映射读不到（降级为空），描述断言必须变红。
    """
    src = dat or os.path.join(DATA_DIR, "Creep02.dat")
    probe = os.path.join(OUT_DIR, "scan_probe.dat")   # 故意不在权威清单里
    shutil.copyfile(src, probe)
    global SIGNAL_DESC
    orig_desc = SIGNAL_DESC
    if "nodesc" in bad:
        SIGNAL_DESC = {}                              # 变异：JSON 缺失/损坏时的降级态
    try:
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        w = MainWindow()
        w.open_file(probe)                            # auto=True：拖放即出曲线
        # 默认不加载规则，这里显式加载夹具档位规则，用于核对「信号增删不影响区间数」
        w.load_rules(_fixtures.FIXTURE_PATHS["gear"])
        w.run_analysis()
        all_signals = len(w.signal_map)
        tree_items = w.signal_tree.topLevelItemCount()   # 必须核对树，不能只查 map
        base_curves = w.curve_count
        base_regions = len(w.regions)

        extras = [n for n in sorted(w.signal_map) if n not in w.checked][:3]
        w.set_checked(extras, True)
        c3 = w.curve_count
        w.set_checked([extras[0]], False)
        c2 = w.curve_count
        regions_after = len(w.regions)

        w.search.setText("AccP")
        visible = sum(1 for i in range(w.signal_tree.topLevelItemCount())
                      if not w.signal_tree.topLevelItem(i).isHidden())
        w.search.setText("")
        visible_all = sum(1 for i in range(w.signal_tree.topLevelItemCount())
                          if not w.signal_tree.topLevelItem(i).isHidden())

        stem = os.path.splitext(os.path.basename(src))[0]
        p2_path = os.path.join(OUT_DIR, "p2_result_%s_gear.json" % stem)
        if not os.path.exists(p2_path):
            raise FileNotFoundError(
                "缺少独立 P2 清单：%s（先跑 caliper_rules.py --dat … --rules <规则集>）" % p2_path)
        with open(p2_path, encoding="utf-8") as f:
            p2_total = json.load(f)["summary"]["total_intervals"]

        checks = {
            "unregistered_file_opened": w.scan_info is not None and w.m is not None,
            # 清单必须比「只有规则所需信号」多——v9 缺口就是只列规则那 1 个信号；
            # 用相对判据代替硬编码 >100，换小文件数据集时断言仍有区分力
            "all_signals_listed": all_signals > len(w.rule_signals) and tree_items == all_signals,
            "default_curve_shown": base_curves >= 1,   # 显式 load_rules 后自动补规则信号
            "three_signals_added": c3 == base_curves + 3,
            "one_signal_removed": c2 == c3 - 1,
            "regions_unchanged": regions_after == base_regions == p2_total,
            "search_filters": 0 < visible < visible_all,
            "templates_in_rulesets": any("templates/" in d for d in w.rule_paths),
        }

        # 添加信号对话框的描述列：取自 JSON、无描述留空、中文搜描述、选中仍返回纯信号名
        probe_names = ["AccP_percAccrPedl", "Brake_percBrkPedl", "NO_DESC_SIGNAL_XYZ"]
        dlg = AddSignalsDialog(probe_names, w, desc=SIGNAL_DESC)
        col1 = {dlg.list.topLevelItem(i).text(0): dlg.list.topLevelItem(i).text(1)
                for i in range(dlg.list.topLevelItemCount())}
        checks["desc_column_from_json"] = (
            col1.get("AccP_percAccrPedl") == SIGNAL_DESC.get("AccP_percAccrPedl")
            and col1.get("Brake_percBrkPedl") == SIGNAL_DESC.get("Brake_percBrkPedl"))
        checks["desc_missing_is_blank"] = col1.get("NO_DESC_SIGNAL_XYZ") == ""
        # 描述列悬停显示完整描述：每行 tooltip(1) == 该信号完整描述（与 SignalPicker 一致）
        checks["desc_tooltip"] = all(
            dlg.list.topLevelItem(i).toolTip(1) == SIGNAL_DESC.get(
                dlg.list.topLevelItem(i).text(0), "")
            for i in range(dlg.list.topLevelItemCount())) and any(
                dlg.list.topLevelItem(i).toolTip(1)
                for i in range(dlg.list.topLevelItemCount()))  # 防"空==空"假绿
        # 搜中文描述：只留下描述命中的信号
        dlg._filter("制动踏板")
        hits = [dlg.list.topLevelItem(i).text(0)
                for i in range(dlg.list.topLevelItemCount())
                if not dlg.list.topLevelItem(i).isHidden()]
        checks["search_matches_desc"] = hits == ["Brake_percBrkPedl"]
        dlg._filter("")
        # 勾选返回纯信号名（第 0 列的 UserRole），不带描述
        dlg.list.topLevelItem(0).setCheckState(0, QtCore.Qt.CheckState.Checked)
        checks["select_returns_plain_name"] = dlg.selected_names() == ["AccP_percAccrPedl"]
        dlg.close()

        report = {
            "probe": os.path.basename(probe), "signals_available": all_signals,
            "tree_items": tree_items,
            "curves_initial": base_curves, "curves_after_add": c3,
            "curves_after_remove": c2, "regions": regions_after,
            "search_visible": visible, "search_cleared_visible": visible_all,
            "checks": checks, "passed": all(checks.values()), "bad": sorted(bad),
        }
        # 换数据集时报告名带数据名，不覆盖默认用例的基线证据
        suffix = ("_bad_" + "_".join(sorted(bad))) if bad else ("" if dat is None else "_" + stem)
        out = os.path.join(OUT_DIR, "p3_verify_add_signals%s.json" % suffix)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2, sort_keys=True)
        return all(checks.values()), report, out
    finally:
        SIGNAL_DESC = orig_desc
        if os.path.exists(probe):
            os.remove(probe)


def verify_dbc_reselect(tag="dbc_reselect", bad=frozenset()):
    """验收「重选 DBC 集合」：下拉框选项与写回的通道映射都必须跟着新 DBC 变。

    曾在重选后只更新 new_dbc_paths、不刷新下拉框，确定后写回旧 DBC 名 -> 映射失效。
    变异 stale=重选不刷新下拉框（复现旧缺陷），两条断言必须变红。
    """
    checks = {}
    orig_get = QtWidgets.QFileDialog.getOpenFileNames
    old_keys = ["OLD_A", "OLD_B"]
    new_paths = [os.path.join(OUT_DIR, "NEW_A.dbc"),
                 os.path.join(OUT_DIR, "NEW_B.dbc")]
    new_keys = ["NEW_A", "NEW_B"]
    priority = {"1": ["OLD_B"]}             # 旧映射指向 OLD_B（新集合里不存在）
    if "stale" in bad:
        # 变异：重选只存路径，不刷新下拉框 -> 复现旧缺陷
        def stale_reselect(self):
            self.new_dbc_paths = new_paths
        orig_reselect = BlfMapDialog._reselect
        BlfMapDialog._reselect = stale_reselect
    else:
        orig_reselect = None

    def fake_get(*args, **kwargs):
        return new_paths, ""

    QtWidgets.QFileDialog.getOpenFileNames = staticmethod(fake_get)
    try:
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        dlg = BlfMapDialog(["1", "2"], priority, old_keys)
        checks["initial_old_keys"] = all(
            [dlg.combos[ch].itemText(i) for i in range(dlg.combos[ch].count())] == old_keys
            for ch in dlg.combos)
        dlg._reselect()
        got_keys = [[dlg.combos[ch].itemText(i) for i in range(dlg.combos[ch].count())]
                    for ch in dlg.combos]
        checks["combos_refreshed"] = all(k == new_keys for k in got_keys)
        checks["stale_priority_dropped"] = all(
            v in new_keys for v in
            [names[0] for names in dlg.priority_override().values()])
        checks["new_paths_stored"] = dlg.new_dbc_paths == new_paths
        dlg.close()
    finally:
        QtWidgets.QFileDialog.getOpenFileNames = orig_get
        if orig_reselect is not None:
            BlfMapDialog._reselect = orig_reselect

    report = {"checks": checks, "passed": all(checks.values()), "bad": sorted(bad),
              "old_keys": old_keys, "new_keys": new_keys}
    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, "p3_verify_%s.json" % tag)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, sort_keys=True)
    return report["passed"], report, out


def verify_mf4(tag="mf4", bad=frozenset()):
    """验收 MDF 4.x (.MF4) 文件适配：内核 scan/load + GUI 打开/勾选/出曲线。

    用真实样本 realdata/Recorder_*.MF4，不合成。断言覆盖：版本识别为 4.x、
    信号可读、时区、信号树、曲线绘制。变异 nover=把 mdf_version 读错，
    版本断言必须变红。
    """
    dat = os.path.join(DATA_DIR, "Recorder_2026-07-29_09-59-14.MF4")
    if not os.path.exists(dat):
        raise FileNotFoundError("MF4 验收样本缺失：" + dat)
    checks = {}
    orig_scan = caliper_core.scan
    if "nover" in bad:
        def bad_scan(p):
            e = orig_scan(p)
            e["mdf_version"] = "3.00"       # 变异：版本读错
            return e
        caliper_core.scan = bad_scan
    try:
        # 内核层
        entry = caliper_core.scan(dat)
        checks["mf4_version_4x"] = str(entry["mdf_version"]).startswith("4.")
        m = caliper_core.load(dat, signals=[s["name"] for s in entry["signals"][:3]])
        checks["mf4_load_signals"] = len(m.signals) >= 1
        checks["mf4_axis_nonempty"] = len(m.axis) > 1000
        checks["mf4_timezone"] = m.start_time_local.endswith("+08:00")

        # GUI 层
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        w = MainWindow()
        w.open_file(dat, auto=False)
        app.processEvents()
        checks["gui_opened"] = len(w.signal_map) == len(entry["signals"])
        checks["gui_tree_items"] = w.signal_tree.topLevelItemCount() == len(entry["signals"])
        sig = "EM2_tqMotReq"
        checks["gui_signal_present"] = sig in w.signal_map
        if sig in w.signal_map:
            w.set_checked([sig], True)
            app.processEvents()
            checks["gui_curve_drawn"] = w.curve_count >= 1
        else:
            checks["gui_curve_drawn"] = False

        # 规则集验证：加载按 MF4 实际信号写的规则集，6 条应全部命中、无未评估
        rules_path = os.path.join(ROOT, "rules", "MF4", "MF4_Recorder.json")
        w.load_rules(rules_path)
        w.run_analysis()
        app.processEvents()
        res = w.result
        checks["mf4_rules_all_hit"] = (res is not None and len(res.results) == 6
                                       and all(r.status == "hit" for r in res.results))
        checks["mf4_rules_no_not_eval"] = (res is not None
                                           and not any(r.status == "not_evaluated"
                                                       for r in res.results))
        checks["mf4_rules_regions"] = len(w.regions) > 0

        # 观测值（不断言严格递增：这份 MF4 原始时间戳本身有回退，属数据特征）
        d = np.diff(m.axis)
        neg_dt = int((d < 0).sum())
    finally:
        caliper_core.scan = orig_scan

    n_hit = sum(1 for r in res.results if r.status == "hit") if res else 0
    report = {"checks": checks, "passed": all(checks.values()), "bad": sorted(bad),
              "mdf_version": str(entry["mdf_version"]), "signals": len(w.signal_map),
              "points": int(len(m.axis)), "negative_dt": neg_dt,
              "rules_hit": n_hit, "regions": len(w.regions)}
    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, "p3_verify_%s.json" % tag)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, sort_keys=True)
    return report["passed"], report, out


def verify_dbcdir(tag="dbcdir", bad=frozenset()):
    """验收 DBC 统一目录：配置在 DBC/、只存文件名、选 DBC 复制去重、读取拼完整路径。

    全部在临时目录里做，不污染真实 DBC/。变异 noresolve=读取时不拼 DBC 目录，
    "解析出完整路径"断言必须变红。
    """
    import tempfile
    checks = {}
    orig_dir = caliper_core.DBC_DIR
    orig_cfg = caliper_core.BLF_CONFIG
    orig_resolve = caliper_core.resolve_dbc_path
    if "noresolve" in bad:
        caliper_core.resolve_dbc_path = lambda entry: entry   # 变异：不拼目录

    tmp = tempfile.mkdtemp(prefix="dbc_verify_")
    try:
        # 1. 真实配置的位置与内容形态（只读，不污染）
        checks["config_in_dbcdir"] = os.path.normcase(orig_cfg) == os.path.normcase(
            os.path.join(orig_dir, "blf_config.json"))
        real_cfg = json.load(open(orig_cfg, encoding="utf-8"))
        stored = real_cfg.get("dbc_paths", [])
        checks["config_stores_names"] = bool(stored) and all(
            not os.path.isabs(p) for p in stored)
        checks["resolve_to_fullpath"] = all(
            os.path.isabs(caliper_core.resolve_dbc_path(p))
            and os.path.exists(caliper_core.resolve_dbc_path(p))
            for p in stored)

        # 2. 复制去重：临时当 DBC_DIR，放两个源 DBC，测复制 + 同名跳过
        caliper_core.DBC_DIR = tmp
        caliper_core.BLF_CONFIG = os.path.join(tmp, "blf_config.json")
        src_dir = tempfile.mkdtemp(prefix="dbc_src_")
        try:
            a = os.path.join(src_dir, "A.dbc")
            b = os.path.join(src_dir, "B.dbc")
            with open(a, "w", encoding="utf-8") as f:
                f.write("DBC A content")
            with open(b, "w", encoding="utf-8") as f:
                f.write("DBC B content")
            names = MainWindow._stage_dbcs([a, b])
            checks["stage_copies"] = names == ["A.dbc", "B.dbc"] and all(
                os.path.exists(os.path.join(tmp, n)) for n in names)
            # 同名跳过：改源文件，再 stage，目标不应被覆盖
            with open(a, "w", encoding="utf-8") as f:
                f.write("CHANGED")
            MainWindow._stage_dbcs([a])
            kept = open(os.path.join(tmp, "A.dbc"), encoding="utf-8").read()
            checks["stage_skip_existing"] = kept == "DBC A content"
        finally:
            shutil.rmtree(src_dir, ignore_errors=True)
    finally:
        caliper_core.DBC_DIR = orig_dir
        caliper_core.BLF_CONFIG = orig_cfg
        caliper_core.resolve_dbc_path = orig_resolve
        shutil.rmtree(tmp, ignore_errors=True)

    report = {"checks": checks, "passed": all(checks.values()), "bad": sorted(bad),
              "config": os.path.relpath(orig_cfg), "dbc_count": len(stored)}
    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, "p3_verify_%s.json" % tag)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, sort_keys=True)
    return report["passed"], report, out


def verify_poem(tag="poem", bad=frozenset()):
    """验收顶栏诗词标语：来自诗词表、长度不超限、每次启动换一句。

    QSettings 记索引轮换，连开两个窗口应拿到不同诗句。
    变异 fixed=把标语写死成固定串（复现"每次都一样"的缺陷），轮换断言必须变红。
    """
    checks = {}
    orig_pick = caliper_gui.pick_poem
    if "fixed" in bad:
        caliper_gui.pick_poem = lambda: "固定标语"   # 变异：每次都一样
    try:
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        poems = caliper_gui.POEMS
        w1 = MainWindow()
        w2 = MainWindow()
        t1, t2 = w1.brand_title.text(), w2.brand_title.text()
        checks["poem_from_list"] = t1 in poems and t2 in poems
        checks["poem_rotates"] = t1 != t2
        checks["poem_short_enough"] = all(len(p) <= 8 for p in poems)
        checks["poem_no_old_title"] = "VCU 测量数据" not in (t1 + t2)
    finally:
        caliper_gui.pick_poem = orig_pick

    report = {"checks": checks, "passed": all(checks.values()), "bad": sorted(bad),
              "first": t1, "second": t2, "poem_count": len(poems)}
    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, "p3_verify_%s.json" % tag)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, sort_keys=True)
    return report["passed"], report, out


def verify_export(tag="export", bad=frozenset()):
    """验收「GUI 导出可复现记录」：与 CLI 同链字节一致、三态不折叠、前提不满足不落盘。

    独立来源是 caliper_report.run 的 CLI 记录（在注入变异之前先取好）。
    bad 供变异验证：fold=导出时把 not_evaluated 折叠成 no_hit，nowrite=只算不写盘。
    全程 offscreen 程序化断言，不截屏；临时 JSON 跑完即删。
    """
    bad = set(bad)
    dat1 = os.path.join(DATA_DIR, "Creep02.dat")
    dat2 = os.path.join(DATA_DIR, "行车中驾驶模式切换.dat")
    gear_path = _fixtures.FIXTURE_PATHS["gear"]
    gap_path = _fixtures.FIXTURE_PATHS["creep_gap"]
    hit_json = os.path.join(OUT_DIR, "_export_verify_hit.json")
    nev_json = os.path.join(OUT_DIR, "_export_verify_nev.json")
    guard_json = os.path.join(OUT_DIR, "_export_verify_guard.json")

    def wipe():
        for p in (hit_json, nev_json, guard_json):
            if os.path.exists(p):
                os.remove(p)

    wipe()
    net_calls = []
    original_connect = socket.socket.connect

    def blocked(self, addr):
        net_calls.append(repr(addr))
        return None

    socket.socket.connect = blocked
    orig_build = caliper_report.build_record
    orig_write = caliper_report.write_json
    try:
        # CLI 参照必须在变异注入前生成，否则参照本身也被污染，断言就成了自己比自己
        _, _, cli_hit = caliper_report.run(dat1, gear_path)
        _, _, cli_nev = caliper_report.run(dat2, gap_path)

        if "fold" in bad:      # 变异：导出链把未评估折叠成未命中（换实现，不改断言）
            def _fold(result, m, dat, rules):
                rec = orig_build(result, m, dat, rules)
                for r in rec["rules"]:
                    if r["status"] == "not_evaluated":
                        r["status"] = "no_hit"
                        r["missing"] = []
                rec["summary"] = {
                    "hit": sum(1 for r in rec["rules"] if r["status"] == "hit"),
                    "no_hit": sum(1 for r in rec["rules"] if r["status"] == "no_hit"),
                    "not_evaluated": sum(1 for r in rec["rules"] if r["status"] == "not_evaluated"),
                    "total_intervals": sum(len(r["intervals"]) for r in rec["rules"]),
                }
                return rec
            caliper_report.build_record = _fold
        if "nowrite" in bad:   # 变异：只算记录不写盘
            caliper_report.write_json = lambda obj, path: None

        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        w = MainWindow()
        checks = {}

        def rec_matches_result(rec, result):
            if rec is None or rec["summary"] != result.summary:
                return False
            if len(rec["rules"]) != len(result.results):
                return False
            for a, b in zip(rec["rules"], result.results):
                if (a["status"] != b.status or a["missing"] != b.missing
                        or len(a["intervals"]) != len(b.intervals)):
                    return False
                for ia, ib in zip(a["intervals"], b.intervals):
                    if (ia["t0"] != ib["t0"] or ia["t1"] != ib["t1"]
                            or ia["snapshot"] != ib["snapshot"]):
                        return False
            return True

        # 用例 1：命中用例（Creep02 + gear，4 区间）
        w.open_file(dat1, auto=False)
        w.load_rules(gear_path)
        w.run_analysis()
        rec1 = w.export_record(hit_json)
        checks["hit_returns_record"] = rec1 is not None
        checks["hit_matches_ui"] = rec_matches_result(rec1, w.result)
        checks["hit_matches_cli"] = rec1 == cli_hit
        disk1 = json.load(open(hit_json, encoding="utf-8")) if os.path.exists(hit_json) else None
        checks["hit_file_written"] = disk1 is not None and disk1 == rec1
        checks["md5_fields"] = (rec1 is not None
                                and rec1["data"]["md5"] == caliper_rules.md5_of(dat1)
                                and rec1["ruleset"]["md5"] == caliper_rules.md5_of(gear_path))

        # 用例 2：未评估用例（121 组 + creep_gap）——三态红线延伸到导出链
        w.open_file(dat2, auto=False)
        w.load_rules(gap_path)
        w.run_analysis()
        rec2 = w.export_record(nev_json)
        checks["nev_preserved"] = (rec2 is not None
                                   and rec2["summary"]["not_evaluated"] == 1
                                   and rec2["rules"][0]["status"] == "not_evaluated"
                                   and rec2["rules"][0]["missing"] == ["DrvReq_bCrpIPartRst_MP"])
        checks["nev_matches_cli"] = rec2 == cli_nev
        disk2 = json.load(open(nev_json, encoding="utf-8")) if os.path.exists(nev_json) else None
        checks["nev_file_written"] = disk2 is not None and disk2 == rec2

        # 守卫：没文件 / 没加载规则 → 拒绝导出，也不落半截记录
        w2 = MainWindow()
        checks["no_file_rejected"] = (w2.export_record(guard_json) is None
                                      and not os.path.exists(guard_json))
        w2.open_file(dat1, auto=False)
        w2.run_analysis()
        checks["no_rules_rejected"] = (w2.export_record(guard_json) is None
                                       and not os.path.exists(guard_json))
        checks["no_network_call"] = len(net_calls) == 0
        w.close()
        w2.close()
    finally:
        caliper_report.build_record = orig_build
        caliper_report.write_json = orig_write
        socket.socket.connect = original_connect
        wipe()

    report = {"checks": checks, "passed": all(checks.values()), "bad": sorted(bad),
              "hit_summary": cli_hit["summary"], "nev_summary": cli_nev["summary"]}
    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, "p3_verify_%s.json" % tag)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, sort_keys=True)
    return report["passed"], report, out


def verify_new_ruleset(tag="new_ruleset", bad=frozenset()):
    """验收「新建空白规则集」：真实信号骨架、版本 1、不覆盖已有文件、限 rules/ 目录、
    建完自动选中并重算、编辑器能接着改。临时文件跑完即删，不碰真实规则集。

    bad 供变异验证：overwrite=跳过已存在检查（会覆盖旧文件），
    bump=新文件也自增版本号（1 变 2）。两者都必须让对应断言变红。
    """
    bad = set(bad)
    tmp = os.path.join(RULES_DIR, "_new_verify.json")
    bak = tmp + ".bak"
    outside = os.path.join(OUT_DIR, "_new_outside.json")
    nofile = os.path.join(RULES_DIR, "_new_nofile.json")

    def wipe():
        for p in (tmp, bak, tmp + ".tmp", outside, nofile):
            if os.path.exists(p):
                os.remove(p)

    wipe()
    net_calls = []
    original_connect = socket.socket.connect

    def blocked(self, addr):
        net_calls.append(repr(addr))
        return None

    socket.socket.connect = blocked
    try:
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        w = MainWindow()
        w.open_file(os.path.join(DATA_DIR, "Creep02.dat"), auto=False)
        w.run_analysis()
        checks = {}
        created, _msg = w.create_ruleset(tmp, bad=bad)
        checks["created"] = created is not None and os.path.exists(tmp)
        doc = json.load(open(tmp, encoding="utf-8"))
        checks["version_is_one"] = doc["version"] == 1
        checks["valid_doc"] = caliper_rules.validate_rules_doc(doc) == []
        rs = caliper_rules.load_ruleset(tmp)
        sig = sorted(w.signal_map)[0]
        checks["skeleton_real_signal"] = rs[0].required_signals == [sig] and sig in w.signal_map
        # 默认空画布，w.m 未加载任何信号；验收要显式取数再评估骨架规则
        m_tmp = caliper_core.load(w.dat_path, signals=[sig], signal_map=w.signal_map)
        res = caliper_rules.evaluate(m_tmp, rs)
        checks["engine_evaluable"] = res.results[0].status in ("hit", "no_hit")

        w.after_rules_saved(created)
        checks["dropdown_selected"] = (w.rules_path is not None
                                       and os.path.basename(w.rules_path) == "_new_verify.json")
        engine = caliper_rules.evaluate(w.m, caliper_rules.load_ruleset(created))
        checks["ui_recomputed"] = w.result is not None and w.result.summary == engine.summary

        dlg = RuleSetDialog(created, signals=sorted(w.signal_map))
        checks["editor_opens_clean"] = dlg.table.rowCount() == 1 and dlg.is_dirty() is False
        v_before = dlg.saved_version
        dlg.set_cell(0, dlg.COL_MIN, "300")
        saved = dlg.save()
        doc2 = json.load(open(tmp, encoding="utf-8"))
        # 版本断言用相对增量：bump 变异只该打红 version_is_one，不连带误伤这条
        checks["edit_save_chain"] = (saved is True
                                     and doc2["version"] == v_before + 1
                                     and doc2["rules"][0]["min_duration_ms"] == 300
                                     and os.path.exists(bak))

        # 已存在：拒绝覆盖，旧文件一个字节都不动
        before = caliper_rules.md5_of(tmp)
        again, msg2 = w.create_ruleset(tmp, bad=bad)
        checks["no_overwrite"] = (again is None and "已存在" in msg2
                                  and caliper_rules.md5_of(tmp) == before)
        # rules/ 之外：拒绝（下拉只扫 rules/，存别处等于悄悄丢失）
        p_out, _msg_out = w.create_ruleset(outside)
        checks["outside_rules_rejected"] = p_out is None and not os.path.exists(outside)
        # 没打开测量文件：拒绝（骨架必须用真实信号名，不许编信号）
        w2 = MainWindow()
        p_nf, _msg_nf = w2.create_ruleset(nofile)
        checks["no_signals_rejected"] = p_nf is None and not os.path.exists(nofile)
        checks["no_network_call"] = len(net_calls) == 0
        w.close()
        w2.close()
    finally:
        socket.socket.connect = original_connect
        wipe()

    report = {"checks": checks, "passed": all(checks.values()), "bad": sorted(bad),
              "skeleton_signal": sig,
              "ruleset": "_new_verify.json（临时副本，跑完即删）"}
    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, "p3_verify_%s.json" % tag)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, sort_keys=True)
    return report["passed"], report, out


def verify_enum(tag="enum", bad=frozenset()):
    """验收「枚举文字标签」：只加在显示层读数上，数值仍在、引擎与快照零改动。

    bad 供变异验证：nolabel=映射失效（标签消失），wrongmap=把 4 错标成 N档。
    两者都必须让对应断言变红；数据数值类断言在变异下仍应绿（标签不许碰数据）。
    """
    bad = set(bad)
    orig_labels = caliper_gui.ENUM_LABELS
    net_calls = []
    original_connect = socket.socket.connect

    def blocked(self, addr):
        net_calls.append(repr(addr))
        return None

    socket.socket.connect = blocked
    try:
        if "nolabel" in bad:     # 变异：显示层映射被清空（换实现，不改断言）
            caliper_gui.ENUM_LABELS = {}
        if "wrongmap" in bad:    # 变异：档位 4 错标成 N档
            wrong = dict(orig_labels["GrMod_stDrvAct"])
            wrong[4.0] = "N档"
            caliper_gui.ENUM_LABELS = {"GrMod_stDrvAct": wrong}

        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        w = MainWindow()
        w.open_file(os.path.join(DATA_DIR, "Creep02.dat"), auto=False)
        w.load_rules(_fixtures.FIXTURE_PATHS["gear"])
        w.run_analysis()
        w.set_checked(["GrMod_stDrvAct", "AccP_percAccrPedl"], True)
        w.set_per_axis(True)
        app.processEvents()

        gear = np.asarray(w.m.signals["GrMod_stDrvAct"], dtype=float)
        at1 = np.flatnonzero(gear == 1)
        at4 = np.flatnonzero(gear == 4)
        checks = {"samples_exist": len(at1) > 0 and len(at4) > 0}

        def value_text(name):
            for i in range(w.value_tree.topLevelItemCount()):
                it = w.value_tree.topLevelItem(i)
                if it.text(0) == name:
                    return it.text(1)
            return None

        def line_text():
            return next(l1 for n, l1, _l2 in w.cursors
                        if n == "GrMod_stDrvAct").label.toPlainText()

        if checks["samples_exist"]:
            w._set_cursor(float(w.m.axis[int(at4[0])]))
            app.processEvents()
            checks["value_label_d"] = value_text("GrMod_stDrvAct") == "4 (D档)"
            checks["line_label_d"] = line_text().startswith("4 (D档)")
            w._set_cursor(float(w.m.axis[int(at1[0])]))
            app.processEvents()
            checks["value_label_p"] = value_text("GrMod_stDrvAct") == "1 (P档)"
            checks["line_label_p"] = line_text().startswith("1 (P档)")
            v_acc = value_text("AccP_percAccrPedl")
            checks["non_enum_unlabeled"] = v_acc is not None and "档" not in v_acc
            w.set_per_axis(False)
            w._set_cursor(float(w.m.axis[int(at4[0])]))
            app.processEvents()
            checks["shared_value_label"] = value_text("GrMod_stDrvAct") == "4 (D档)"
        else:
            for k in ("value_label_d", "line_label_d", "value_label_p",
                      "line_label_p", "non_enum_unlabeled", "shared_value_label"):
                checks[k] = False

        # 标签只许进显示层：信号数组与命中快照必须仍是纯数值
        checks["data_still_numeric"] = (
            all(np.issubdtype(np.asarray(a).dtype, np.number)
                for a in w.m.signals.values())
            and all(isinstance(v, (int, float))
                    for r in w.result.results for iv in r.intervals
                    for v in iv["snapshot"].values()))
        checks["no_network_call"] = len(net_calls) == 0
        w.close()
    finally:
        caliper_gui.ENUM_LABELS = orig_labels
        socket.socket.connect = original_connect

    report = {"checks": checks, "passed": all(checks.values()), "bad": sorted(bad),
              "dat": "Creep02.dat",
              "mapping": {n: {str(int(k)): v for k, v in m.items()}
                          for n, m in orig_labels.items()}}
    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, "p3_verify_%s.json" % tag)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, sort_keys=True)
    return report["passed"], report, out


def verify_rule_marks(tag="rule_marks", bad=frozenset()):
    """验收「逐规则彩色标注 + 勾选显隐」：规则色板与曲线色板独立且色差达标、
    区间带/标记点按规则着色、右栏复选框逐规则显隐（不重建树）、重绘后状态保持。

    bad 供变异验证：nocolor=所有规则同色，curvepalette=规则色退回曲线色板，
    notoggle=勾选不生效。三者都必须让对应断言变红。
    """
    bad = set(bad)
    dat = os.path.join(DATA_DIR, "Creep02.dat")
    gear_path = _fixtures.FIXTURE_PATHS["gear"]
    p2_path = os.path.join(OUT_DIR, "p2_result_Creep02_gear.json")
    if not os.path.exists(p2_path):
        raise FileNotFoundError("缺少独立 P2 清单：" + p2_path)
    with open(p2_path, encoding="utf-8") as f:
        p2_total = json.load(f)["summary"]["total_intervals"]

    orig_colors = MainWindow._rule_colors
    orig_apply = MainWindow._apply_rule_mark_visibility
    net_calls = []
    original_connect = socket.socket.connect

    def blocked(self, addr):
        net_calls.append(repr(addr))
        return None

    socket.socket.connect = blocked
    try:
        if "nocolor" in bad:       # 变异：所有规则涂同一个色（换实现，不改断言）
            MainWindow._rule_colors = lambda self, hits: {
                r.rule_name: "#4E6E23" for r in hits}
        if "curvepalette" in bad:  # 变异：规则色退回曲线色板（撞色缺陷）
            MainWindow._rule_colors = lambda self, hits: {
                r.rule_name: CURVE_COLORS[i % len(CURVE_COLORS)]
                for i, r in enumerate(hits)}
        if "notoggle" in bad:      # 变异：勾选状态不落到图形项
            MainWindow._apply_rule_mark_visibility = lambda self: None

        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        w = MainWindow()
        w.open_file(dat, auto=False)
        w.load_rules(gear_path)
        w.run_analysis()
        hits = [r for r in w.result.results if r.status == "hit" and r.intervals]
        colors = w._rule_colors(hits)
        checks = {"hit_rules_ge2": len(hits) >= 2}
        names = list(colors)
        checks["colors_distinct"] = all(
            _color_dist(colors[a], colors[b]) >= RULE_PAIR_MIN_DIST
            for i, a in enumerate(names) for b in names[i + 1:])
        checks["no_curve_collision"] = all(
            _color_dist(c, cc) >= RULE_CURVE_MIN_DIST
            for c in colors.values() for cc in CURVE_COLORS)
        checks["palette_independent"] = not (set(RULE_MARK_COLORS) & set(CURVE_COLORS))

        def same_color(actual, expect):
            return actual.name().lower() == QtGui.QColor(expect).name().lower()

        checks["regions_use_rule_color"] = bool(w.rule_regions) and all(
            same_color(reg.lines[0].pen.color(), colors[n])
            and same_color(reg.brush.color(), colors[n])
            for n, regs in w.rule_regions.items() for reg in regs)
        checks["markers_use_rule_color"] = bool(w.rule_markers) and all(
            same_color(sc.opts["brush"].color(), colors[n])
            for n, scs in w.rule_markers.items() for sc in scs)
        checks["counts_match_p2"] = (len(w.rule_markers) == len(hits)
                                     and len(w.regions) == p2_total
                                     and w.marker_count == p2_total)

        def child_of(name):
            for i in range(w.rule_tree.topLevelItemCount()):
                top = w.rule_tree.topLevelItem(i)
                for j in range(top.childCount()):
                    c = top.child(j)
                    if c.data(0, RULE_NAME_ROLE) == name:
                        return c
            return None

        checkable = QtCore.Qt.ItemFlag.ItemIsUserCheckable
        checked = QtCore.Qt.CheckState.Checked
        unchecked = QtCore.Qt.CheckState.Unchecked
        checks["checkbox_on_hits"] = all(
            child_of(r.rule_name) is not None
            and bool(child_of(r.rule_name).flags() & checkable)
            and child_of(r.rule_name).checkState(0) == checked for r in hits)
        no_hits = [r for r in w.result.results if r.status == "no_hit"]
        checks["no_checkbox_on_no_hit"] = bool(no_hits) and all(
            child_of(r.rule_name) is not None
            and not bool(child_of(r.rule_name).flags() & checkable) for r in no_hits)

        target = hits[-1].rule_name
        tops_before = [w.rule_tree.topLevelItem(i)
                       for i in range(w.rule_tree.topLevelItemCount())]
        child_of(target).setCheckState(0, unchecked)
        checks["toggle_hides"] = all(
            not it.isVisible() for it in w.rule_regions[target] + w.rule_markers[target])
        checks["others_visible"] = all(
            it.isVisible() for n in names if n != target
            for it in w.rule_regions[n] + w.rule_markers[n])
        checks["tree_not_rebuilt"] = [
            w.rule_tree.topLevelItem(i)
            for i in range(w.rule_tree.topLevelItemCount())] == tops_before
        checks["counts_unchanged"] = (len(w.regions) == p2_total
                                      and w.marker_count == p2_total)
        child_of(target).setCheckState(0, checked)
        checks["toggle_restores"] = all(
            it.isVisible() for it in w.rule_regions[target] + w.rule_markers[target])

        child_of(target).setCheckState(0, unchecked)
        w.render()
        checks["hidden_survives_rerender"] = all(
            not it.isVisible() for it in w.rule_regions[target] + w.rule_markers[target])
        checks["checkbox_state_kept"] = child_of(target).checkState(0) == unchecked
        child_of(target).setCheckState(0, checked)

        w.set_checked(["AccP_percAccrPedl", "ESC_stEPB"], True)
        w.set_per_axis(True)
        n_axes = len(w.axis_plots)
        checks["per_axis_counts"] = (n_axes >= 2
                                     and len(w.regions) == p2_total * n_axes
                                     and w.marker_count == p2_total * n_axes)
        checks["per_axis_colors"] = all(
            same_color(reg.lines[0].pen.color(), colors[n])
            for n, regs in w.rule_regions.items() for reg in regs)
        child_of(target).setCheckState(0, unchecked)
        checks["per_axis_toggle_hides"] = all(
            not it.isVisible() for it in w.rule_regions[target] + w.rule_markers[target])
        checks["no_network_call"] = len(net_calls) == 0
        w.close()
    finally:
        MainWindow._rule_colors = orig_colors
        MainWindow._apply_rule_mark_visibility = orig_apply
        socket.socket.connect = original_connect

    report = {"checks": checks, "passed": all(checks.values()), "bad": sorted(bad),
              "dat": os.path.basename(dat), "rule_colors": colors,
              "p2_total_intervals": p2_total}
    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, "p3_verify_%s.json" % tag)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, sort_keys=True)
    return report["passed"], report, out


def verify_panels(tag="panels", bad=frozenset()):
    """验收「左栏已删 + 右栏可收起 + 右栏多 Tab」：左栏 UI 不存在、signal_tree
    作为隐藏状态载体仍在；两个 Tab 齐全、信号值 Tab 行与曲线一致、右栏按钮真实
    收起/展开（走 toggled 信号）、收起状态跨重绘保持、Tab 切换与读数更新。

    bad 供变异验证：nocollapse=按钮不收起栏，novalues=信号值 Tab 不刷新，
    noinset=收起后不留内缩带（拉手压图），noselection=行选择机制没装。
    四者都必须让对应断言变红。
    """
    bad = set(bad)
    orig_right = MainWindow.toggle_right_panel
    orig_vals = MainWindow._refresh_value_tab
    orig_margins = MainWindow._update_mid_margins
    orig_sel = MainWindow._setup_value_selection
    net_calls = []
    original_connect = socket.socket.connect

    def blocked(self, addr):
        net_calls.append(repr(addr))
        return None

    socket.socket.connect = blocked
    try:
        if "nocollapse" in bad:    # 变异：按钮接了信号但不收起（换实现，不改断言）
            MainWindow.toggle_right_panel = lambda self, on: None
        if "novalues" in bad:      # 变异：信号值 Tab 永不刷新
            MainWindow._refresh_value_tab = lambda self: None
        if "noinset" in bad:       # 变异：收起后不留内缩带，拉手压到示波器上
            MainWindow._update_mid_margins = lambda self: None
        if "noselection" in bad:   # 变异：行选择机制没装（Ctrl 多选/全选失效）
            MainWindow._setup_value_selection = lambda self: None

        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        w = MainWindow()
        w.resize(1200, 700)
        w.show()                   # offscreen：只在内存里，不上屏
        app.processEvents()
        w.open_file(os.path.join(DATA_DIR, "Creep02.dat"), auto=False)
        w.load_rules(_fixtures.FIXTURE_PATHS["gear"])
        w.run_analysis()
        # load_rules 不自动改勾选：显式勾两条，保证 Tab 有多行可供多选/全选断言
        w.set_checked(["GrMod_stDrvAct", "AccP_percAccrPedl"], True)
        app.processEvents()
        names = sorted(n for n in w.checked if n in w.m.signals)
        total = w.result.summary["total_intervals"]
        checks = {}
        checks["tabs_present"] = (w.right_tabs.count() == 2
                                  and w.right_tabs.tabText(0) == "工况评估"
                                  and w.right_tabs.tabText(1) == "选中信号")
        checks["default_tab_eval"] = w.right_tabs.currentIndex() == 0
        # 状态载体必须保留
        checks["left_panel_removed"] = (
            not hasattr(w, "btn_left_panel")
            and w.left_panel is not None and not w.left_panel.isVisible()
            and w.left_panel.parent() is None)   # 未挂进 splitter/布局
        checks["signal_tree_kept_hidden"] = (
            hasattr(w, "signal_tree") and w.signal_tree is not None
            and w.signal_tree.topLevelItemCount() > 0)
        checks["handle_arrows_expanded"] = w.btn_right_panel.text() == "▶"
        mw, mh = w._mid_widget.width(), w._mid_widget.height()
        # 右栏拉手归右栏、贴内侧边缘，绝不落在示波器上
        checks["handles_inside_panels"] = (
            w.btn_right_panel.parent() is w.right_panel
            and w.btn_right_panel.x() == 2
            and abs(w.btn_right_panel.y() - (w.right_panel.height() - 64) // 2) <= 1)
        r_left = w.btn_right_panel.mapToGlobal(QtCore.QPoint(0, 0)).x()
        m_right = w._mid_widget.mapToGlobal(QtCore.QPoint(mw, 0)).x()
        checks["handles_not_on_plot"] = r_left >= m_right
        checks["plot_flush_when_expanded"] = (
            w._mid_layout.getContentsMargins() == (0, 0, 0, 0))
        rows = [w.value_tree.topLevelItem(i)
                for i in range(w.value_tree.topLevelItemCount())]
        checks["value_rows_match_curves"] = (
            [it.text(0) for it in rows] == names
            and w.curve_count == len([n for n in names if n not in w.hidden_signals]))
        checks["value_units"] = bool(rows) and all(
            it.text(4) == (w.units.get(it.text(0)) or "").strip() for it in rows)
        checks["values_dash_before_cursor"] = (bool(rows) and w.cursor_x is None
                                               and all(it.text(1) == "—" and it.text(2) == "—"
                                                       and it.text(3) == "—" for it in rows))

        # 走真实按钮信号收起/展开右栏，而不是直接 setVisible
        checks["curves_unaffected"] = (w.curve_count == len(names)
                                       and len(w.regions) == total)
        tree_id, tabs_id = id(w.rule_tree), id(w.right_tabs)
        w.btn_right_panel.setChecked(False)
        app.processEvents()
        checks["right_collapsed"] = not w.right_panel.isVisible()
        w.btn_right_panel.setChecked(True)
        app.processEvents()
        checks["right_restored"] = (w.right_panel.isVisible()
                                    and id(w.rule_tree) == tree_id
                                    and id(w.right_tabs) == tabs_id)
        w.btn_right_panel.setChecked(False)
        w.set_checked(["AccP_percAccrPedl"], True)   # 触发全量重绘
        app.processEvents()
        checks["collapsed_survives_render"] = not w.right_panel.isVisible()
        checks["handle_arrows_collapsed"] = w.btn_right_panel.text() == "◀"
        # 收起后右栏隐藏，拉手改挂曲线区并跟到新边缘
        mw2 = w._mid_widget.width()
        checks["handles_follow_collapse"] = (mw2 > mw
                                             and w.btn_right_panel.parent() is w._mid_widget
                                             and w.btn_right_panel.isVisible()
                                             and w.btn_right_panel.x() == mw2 - 20)
        # 收起后示波器右侧内缩 24px 给拉手留位；左侧无栏不内缩
        checks["plot_inset_when_collapsed"] = (
            w._mid_layout.getContentsMargins() == (0, 0, 24, 0))
        hr_left = w.btn_right_panel.mapToGlobal(QtCore.QPoint(0, 0)).x()
        pr_right = w._plot_panel.mapToGlobal(
            QtCore.QPoint(w._plot_panel.width(), 0)).x()
        checks["collapsed_handles_clear_of_plot"] = hr_left >= pr_right
        w.btn_right_panel.setChecked(True)
        app.processEvents()
        checks["handle_arrows_restored"] = (w.btn_right_panel.text() == "▶"
                                            and w.btn_right_panel.parent() is w.right_panel)
        checks["plot_flush_after_restore"] = (
            w._mid_layout.getContentsMargins() == (0, 0, 0, 0))

        w.right_tabs.setCurrentIndex(1)
        app.processEvents()
        checks["tab_switch_values"] = (w.value_tree.isVisible()
                                       and not w.rule_tree.isVisible())
        arrs = {n: np.asarray(w.m.signals[n], dtype=float) for n in names}
        sample = next((i for i in range(len(w.m.axis))
                       if all(math.isfinite(a[i]) for a in arrs.values())), 0)
        w._set_cursor(float(w.m.axis[sample]))
        app.processEvents()
        checks["values_update_on_cursor"] = bool(names) and all(
            value_row_text(w, n) == _fmt_value(arrs[n][sample]) + _enum_suffix(n, arrs[n][sample])
            for n in names)
        # 行选择：单击单选 / Ctrl+点击加选 / Ctrl+A 全选；标尺移动与行重建不丢选中态
        sel_names = sorted(n for n in w.checked if n in w.m.signals)
        checks["selection_mode"] = (
            w.value_tree.selectionMode()
            == QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection
            and w.value_tree.selectionBehavior()
            == QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        vp = w.value_tree.viewport()
        rect0 = w.value_tree.visualItemRect(w.value_tree.topLevelItem(0))
        QtTest.QTest.mouseClick(vp, QtCore.Qt.MouseButton.LeftButton, pos=rect0.center())
        checks["click_selects_single"] = [
            it.text(0) for it in w.value_tree.selectedItems()] == [sel_names[0]]
        rect1 = w.value_tree.visualItemRect(w.value_tree.topLevelItem(1))
        QtTest.QTest.mouseClick(vp, QtCore.Qt.MouseButton.LeftButton,
                                QtCore.Qt.KeyboardModifier.ControlModifier,
                                pos=rect1.center())
        checks["ctrl_click_multiselect"] = sorted(
            it.text(0) for it in w.value_tree.selectedItems()) == sorted(sel_names[:2])
        w.value_tree.setFocus()
        QtTest.QTest.keyClick(w.value_tree, QtCore.Qt.Key.Key_A,
                              QtCore.Qt.KeyboardModifier.ControlModifier)
        checks["ctrl_a_select_all"] = (len(sel_names) >= 2
                                       and len(w.value_tree.selectedItems()) == len(sel_names))
        w._set_cursor(float(w.m.axis[sample]))
        checks["selection_survives_cursor"] = (
            len(w.value_tree.selectedItems()) == len(sel_names))
        QtTest.QTest.mouseClick(vp, QtCore.Qt.MouseButton.LeftButton, pos=rect0.center())
        w.set_checked(["ESC_stEPB"], True)   # 触发行重建
        checks["selection_survives_rebuild"] = [
            it.text(0) for it in w.value_tree.selectedItems()] == [sel_names[0]]
        w.right_tabs.setCurrentIndex(0)
        app.processEvents()
        checks["tab_switch_back"] = w.rule_tree.isVisible()
        checks["no_network_call"] = len(net_calls) == 0
        w.close()
    finally:
        MainWindow.toggle_right_panel = orig_right
        MainWindow._refresh_value_tab = orig_vals
        MainWindow._update_mid_margins = orig_margins
        MainWindow._setup_value_selection = orig_sel
        socket.socket.connect = original_connect

    report = {"checks": checks, "passed": all(checks.values()), "bad": sorted(bad),
              "dat": "Creep02.dat"}
    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, "p3_verify_%s.json" % tag)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, sort_keys=True)
    return report["passed"], report, out


def value_row_text(w, name):
    for i in range(w.value_tree.topLevelItemCount()):
        it = w.value_tree.topLevelItem(i)
        if it.text(0) == name:
            return it.text(1)
    return None


def verify_selops(tag="selops", bad=frozenset()):
    """验收「选中信号」操作批：移除/solo/隐藏/复制/统计/属性/CSV 导出（全时程+视野）/
    曲线样式三型与线宽颜色/Y 自适应复用/采样点显示。全部走真实方法链路。

    bad 供变异验证（换实现不改断言）：noremove / nosolo / nohide / nocopy /
    badstats（mean/std 互换）/ nostyle / nosamples / noexport。
    """
    bad = set(bad)
    dat = os.path.join(DATA_DIR, "Creep02.dat")
    gear_path = _fixtures.FIXTURE_PATHS["gear"]
    tmp_csv = os.path.join(OUT_DIR, "_selops_verify.csv")
    meths = ("sel_remove", "sel_solo", "sel_hide", "sel_copy",
             "stats_rows", "apply_curve_style", "sel_samples", "export_csv")
    orig = {m: getattr(MainWindow, m) for m in meths}
    net_calls = []
    original_connect = socket.socket.connect

    def blocked(self, addr):
        net_calls.append(repr(addr))
        return None

    socket.socket.connect = blocked
    try:
        if "noremove" in bad:
            MainWindow.sel_remove = lambda self: None
        if "nosolo" in bad:
            MainWindow.sel_solo = lambda self: None
        if "nohide" in bad:
            MainWindow.sel_hide = lambda self: None
        if "nocopy" in bad:
            MainWindow.sel_copy = lambda self: None
        if "badstats" in bad:
            MainWindow.stats_rows = lambda self, names: [
                dict(r, mean=r["max"], std=r["min"])
                for r in orig["stats_rows"](self, names)]
        if "nostyle" in bad:
            MainWindow.apply_curve_style = lambda self, names, style: None
        if "nosamples" in bad:
            MainWindow.sel_samples = lambda self, on: None
        if "noexport" in bad:
            MainWindow.export_csv = lambda self, names, path, view_range=None: 0

        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        w = MainWindow()
        w.resize(1200, 700)
        w.show()
        app.processEvents()
        w.open_file(dat, auto=False)
        w.load_rules(gear_path)
        w.run_analysis()
        three = ["AccP_percAccrPedl", "ESC_stEPB", "GrMod_stDrvAct"]
        w.set_checked(three, True)
        app.processEvents()
        total = w.result.summary["total_intervals"]
        checks = {}

        def select(ns):
            w.value_tree.clearSelection()
            for i in range(w.value_tree.topLevelItemCount()):
                it = w.value_tree.topLevelItem(i)
                if it.text(0) in ns:
                    it.setSelected(True)

        def tree_checked(name):
            for i in range(w.signal_tree.topLevelItemCount()):
                it = w.signal_tree.topLevelItem(i)
                if it.text(0) == name:
                    return it.checkState(0) == QtCore.Qt.CheckState.Checked
            return None

        def curve_items():
            return [it for it in w.plot.getPlotItem().listDataItems()
                    if isinstance(it, pg.PlotDataItem)]

        # 移除：取消勾选、左栏同步、区间带不受影响
        select(three[:2])
        w.sel_remove()
        checks["remove_unchecks"] = (w.checked == {three[2]}
                                     and not tree_checked(three[0])
                                     and tree_checked(three[2]))
        checks["remove_keeps_regions"] = (w.curve_count == 1
                                          and len(w.regions) == total)
        w.set_checked(three, True)
        # 仅显示选中：隐藏其他，勾选与值列不动（不是移除）
        select([three[0]])
        w.sel_solo()
        checks["solo_hides_others"] = (
            w.curve_count == 1
            and w.checked == set(three)
            and w.value_tree.topLevelItemCount() == 3
            and w.hidden_signals == set(three[1:]))
        select(three)
        w.sel_show()
        checks["solo_restore_via_show"] = (w.curve_count == 3
                                           and not w.hidden_signals)
        # 隐藏：曲线藏、值列与勾选保留
        select([three[0]])
        w.sel_hide()
        checks["hide_curves_only"] = (w.curve_count == 2
                                      and w.value_tree.topLevelItemCount() == 3
                                      and w.hidden_signals == {three[0]}
                                      and tree_checked(three[0])
                                      and len(w.regions) == total)
        w.sel_show()
        checks["show_restores"] = (w.curve_count == 3 and not w.hidden_signals)
        # 复制为表格
        select(three[:2])
        w._set_cursor(float(w.m.axis[len(w.m.axis) // 2]))
        w.sel_copy()
        clip = QtWidgets.QApplication.clipboard().text().splitlines()
        row0 = [w.value_tree.topLevelItem(i) for i in range(3)
                if w.value_tree.topLevelItem(i).text(0) == three[0]][0]
        checks["copy_tsv"] = (len(clip) == 3 and clip[0] == "信号\tC1\tC2\tΔ\t单位"
                              and clip[1] == "\t".join(row0.text(c) for c in range(5)))
        # 统计：与 numpy 独立重算逐字段相等
        rows = w.stats_rows(three[:2])
        ok_stats = True
        for r in rows:
            a = np.asarray(w.m.signals[r["name"]], dtype=float)
            fin = a[np.isfinite(a)]
            ok_stats &= (abs(r["min"] - fin.min()) < 1e-9
                         and abs(r["max"] - fin.max()) < 1e-9
                         and abs(r["mean"] - fin.mean()) < 1e-9
                         and abs(r["std"] - fin.std()) < 1e-9
                         and r["nan"] == int(np.count_nonzero(np.isnan(a)))
                         and r["count"] == a.size)
        checks["stats_match_numpy"] = bool(ok_stats)
        # 属性：与扫描/quality 字段对应
        pr = w.props_rows([three[2]])[0]
        checks["props_match_scan"] = (
            pr["unit"] == (w.units.get(three[2]) or "")
            and (pr["group"], pr["index"]) == w.signal_map[three[2]]
            and pr["samples"] == len(w.m.axis)
            and pr["nan"] == w.m.quality["nan_counts"][three[2]])
        # CSV 全时程：回读逐值相等
        if os.path.exists(tmp_csv):
            os.remove(tmp_csv)
        n = w.export_csv(three[:2], tmp_csv)
        ok_full = n == len(w.m.axis) and os.path.exists(tmp_csv)
        if ok_full:
            with open(tmp_csv, encoding="utf-8-sig", newline="") as f:
                rd = list(csv.reader(f))
            ok_full = rd[0] == ["time_s"] + three[:2] and len(rd) - 1 == n
            for i, row in enumerate(rd[1:]):
                ok_full &= abs(float(row[0]) - float(w.m.axis[i])) < 1e-9
                for c, nm in enumerate(three[:2]):
                    v = float(w.m.signals[nm][i])
                    ok_full &= (row[c + 1] == "" if math.isnan(v)
                                else abs(float(row[c + 1]) - v) < 1e-9)
        checks["export_full_matches"] = bool(ok_full)
        # CSV 视野切片：行数与边界正确
        lo = float(w.m.axis[100])
        hi = float(w.m.axis[500])
        w._active_plot().setXRange(lo, hi, padding=0)
        vr = tuple(w._active_plot().getViewBox().viewRange()[0])
        n2 = w.export_csv(three[:2], tmp_csv, vr)
        expect = int(np.count_nonzero((w.m.axis >= vr[0]) & (w.m.axis <= vr[1])))
        ok_view = n2 == expect and 0 < n2 < n
        if ok_view:
            with open(tmp_csv, encoding="utf-8-sig", newline="") as f:
                rd = list(csv.reader(f))
            ts = [float(r[0]) for r in rd[1:]]
            ok_view = len(ts) == n2 and ts[0] >= vr[0] and ts[-1] <= vr[1]
        checks["export_view_slice"] = bool(ok_view)
        # 曲线样式：阶梯 / 散点 / 恢复默认
        w.apply_curve_style([three[0]], {"kind": "step", "width": 3.0, "color": "#123456"})
        it0 = curve_items()[0]
        checks["style_step_width_color"] = (
            it0.opts.get("stepMode") == "left"
            and abs(it0.opts["pen"].widthF() - 3.0) < 1e-9
            and it0.opts["pen"].color().name() == "#123456")
        w.apply_curve_style([three[0]], {"kind": "scatter"})
        it0 = curve_items()[0]
        pen0 = it0.opts["pen"]
        checks["style_scatter_no_pen"] = (
            (pen0 is None or pen0.style() == QtCore.Qt.PenStyle.NoPen)
            and it0.opts["symbol"] == "o")
        w.apply_curve_style([three[0]], None)
        it0 = curve_items()[0]
        checks["style_reset_default"] = (
            it0.opts["pen"] is not None
            and abs(it0.opts["pen"].widthF() - 1.8) < 1e-9
            and it0.opts["pen"].color().name() == QtGui.QColor(w.curve_colors[three[0]]).name())
        # 采样点：每一个采样点都画，关掉即移除
        select([three[0]])
        w.sel_samples(True)
        scs = w.sample_items.get(three[0], [])
        checks["samples_all_points"] = (len(scs) == 1
                                        and len(scs[0].data) == len(w.m.axis))
        w.sel_samples(False)
        checks["samples_off_removed"] = not w.sample_items
        # Y 自适应复用：分轴手动量程后对选中轴自适应
        w.set_per_axis(True)
        w.set_y_range(-5.0, 5.0, name=three[0])
        select([three[0]])
        w.sel_auto_y()
        vb0 = dict(w.axis_plots)[three[0]].getViewBox()
        checks["auto_y_reuses_existing"] = bool(vb0.autoRangeEnabled()[1])
        w.set_per_axis(False)
        checks["menu_wired"] = (w.value_tree.contextMenuPolicy()
                                == QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        checks["no_network_call"] = len(net_calls) == 0
        w.close()
    finally:
        for m in meths:
            setattr(MainWindow, m, orig[m])
        socket.socket.connect = original_connect
        if os.path.exists(tmp_csv):
            os.remove(tmp_csv)

    report = {"checks": checks, "passed": all(checks.values()), "bad": sorted(bad),
              "dat": os.path.basename(dat)}
    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, "p3_verify_%s.json" % tag)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, sort_keys=True)
    return report["passed"], report, out


def _curves_in_view(win):
    """画出来的曲线是否真看得见：X/Y 既要包住数据，也不能宽到把数据压成一条线。

    只判「包住」不够：标尺参与量程时视野会反馈放大到 ±1e9，epoch 时基的数据
    虽然落在里面，却只占右边 0.0002%，肉眼仍等于没曲线。故加跨度比上限。
    """
    if win.m is None or not len(win.m.axis):
        return False
    a0, a1 = float(win.m.axis[0]), float(win.m.axis[-1])
    if win.axis_plots:
        groups = [(name, pi) for name, pi in win.axis_plots]
    else:
        listed = [n for n in sorted(win.checked)
                  if n in win.m.signals and n not in win.hidden_signals]
        groups = [(listed, win.plot.getPlotItem())]
    for names, pi in groups:
        xr, yr = pi.getViewBox().viewRange()
        if not (xr[0] <= a0 and a1 <= xr[1]):
            return False
        if a1 > a0 and (xr[1] - xr[0]) > 4.0 * (a1 - a0):
            return False
        vals = []
        for n in ([names] if isinstance(names, str) else names):
            v = win.m.signals.get(n)
            if v is not None:
                vals.append(v[np.isfinite(v)])
        fin = np.concatenate(vals) if vals else np.array([])
        if len(fin):
            lo, hi = float(fin.min()), float(fin.max())
            if not (yr[0] <= lo and hi <= yr[1]):
                return False
            if hi > lo and (yr[1] - yr[0]) > 4.0 * (hi - lo):
                return False
    return True


def verify_blf(dat, dbc_paths, rules, tag, bad=frozenset()):
    """验收 BLF GUI 链路：扫描建树 / wanted 加载 / 区间与 P2 交叉 / 三态 / 值Tab / 标尺。

    同步模式走与后台线程相同的语义方法（_blf_scan_sync / 会话 load / _on_blf_*）。
    另考性能语义：打开只流式一遍、勾选新信号与改通道映射零重扫且旧值逐值不变。
    还考「曲线真的看得见」：数据段必须落在 X/Y 视野内（标尺若参与自动量程，
    其 boundingRect 随视野反馈放大，epoch 时基的 BLF 曲线会整段跑出视野）。
    bad：noscan=扫描结果不建树，noeval=加载后不评估，nocache=每次加载丢会话，
    nobounds=竖直标尺重新参与自动量程。（incremental 自身变异：noincr / nodesc）
    """
    import can
    bad = set(bad)
    rules_path = rules if os.path.isabs(rules) else os.path.join(RULES_DIR, rules)
    ruleset = os.path.splitext(os.path.basename(rules_path))[0]
    stem = os.path.splitext(os.path.basename(dat))[0]
    p2_path = os.path.join(OUT_DIR, "p2_result_%s_%s.json" % (stem, ruleset))
    if not os.path.exists(p2_path):
        raise FileNotFoundError("缺少独立 P2 清单：%s（先跑 caliper_rules.py --dat … --dbc …）" % p2_path)
    with open(p2_path, encoding="utf-8") as f:
        p2 = json.load(f)
    orig_scanned = MainWindow._on_blf_scanned
    orig_loaded = MainWindow._on_blf_loaded
    net_calls = []
    original_connect = socket.socket.connect

    def blocked(self, addr):
        net_calls.append(repr(addr))
        return None

    socket.socket.connect = blocked
    streams = []
    orig_reader = can.BLFReader
    orig_vb_additem = pg.ViewBox.addItem

    class CountingReader(orig_reader):
        def __init__(self, *a, **k):
            streams.append(1)
            super().__init__(*a, **k)

    can.BLFReader = CountingReader
    try:
        if "noscan" in bad:
            MainWindow._on_blf_scanned = lambda self, entry, token, auto=True: None
        if "nobounds" in bad:
            # 变异：让竖直标尺重新参与自动量程（真缺陷注入，不改断言）
            def _add_ruler_with_bounds(self, item, ignoreBounds=False):
                if isinstance(item, pg.InfiniteLine) and item.angle == 90:
                    ignoreBounds = False
                return orig_vb_additem(self, item, ignoreBounds)

            pg.ViewBox.addItem = _add_ruler_with_bounds
        if "noeval" in bad:
            def _noeval(self, m, token):
                self.m = m
                self.rule_marks = {}
                self.result = None
                self.render()
            MainWindow._on_blf_loaded = _noeval
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        w = MainWindow()
        w._blf_sync = True
        w._blf_bad = set(bad)
        w._blf_dbc_override = list(dbc_paths)
        w.open_file(dat)
        w.load_rules(rules_path)
        w.run_analysis()
        checks = {}
        checks["blf_opened"] = w.m is not None and w.scan_info is not None
        n_sig = len(w.scan_info["signals"]) if w.scan_info else 0
        checks["all_signals_listed"] = (
            n_sig > 1000 and w.signal_tree.topLevelItemCount() == n_sig)
        total = p2["summary"]["total_intervals"]
        checks["regions_match_p2"] = len(w.regions) == total
        checks["markers_match_p2"] = w.marker_count == total
        checks["summary_matches_p2"] = (w.result is not None
                                        and w.result.summary == p2["summary"])
        checks["three_state_partition"] = (
            w.result is not None and [
                w.rule_tree.topLevelItem(i).childCount() for i in range(3)
            ] == [w.result.summary["hit"], w.result.summary["no_hit"],
                  w.result.summary["not_evaluated"]])
        listed = sorted(n for n in w.checked if n in w.m.signals)
        checks["value_rows_match"] = (
            w.value_tree.topLevelItemCount() == len(listed))
        sample = len(w.m.axis) // 2
        w._set_cursor(float(w.m.axis[sample]))
        checks["cursor_readout"] = bool(listed) and all(
            value_row_text(w, n) == _fmt_value(w.m.signals[n][sample])
            + _enum_suffix(n, w.m.signals[n][sample]) for n in listed)
        checks["legend_removed"] = getattr(w, "curve_label", None) is None
        checks["blf_curves_in_view"] = _curves_in_view(w)
        # ---- 性能语义：单遍打开 + 勾选/改映射零重扫，旧值逐值不变 ----
        n_open = len(streams)
        checks["blf_single_pass_open"] = n_open == 1
        checks["blf_session_kept"] = w.blf_session is not None
        before = {n: w.m.signals[n].copy() for n in w.m.signals} if w.m else {}
        axis_before = w.m.axis.copy() if w.m is not None else np.array([])
        sess = w.blf_session
        # 追加的信号必须不改变「最密报文即时基」这条原有语义，才能逐值比对旧序列
        extra = None
        if sess is not None and len(axis_before):
            stats = sess.scan["stats"]
            extra = next((n for n in sorted(w.signal_map)
                          if n not in w.checked
                          and stats[w.signal_map[n]]["count"] < len(axis_before)), None)
        if extra is not None:
            w.checked.add(extra)
            w.run_analysis()
            checks["blf_toggle_no_restream"] = (len(streams) == n_open
                                                and extra in w.m.signals
                                                and np.array_equal(w.m.axis, axis_before))
            checks["blf_toggle_keeps_values"] = bool(before) and all(
                np.array_equal(before[n], w.m.signals[n], equal_nan=True) for n in before)
            checks["blf_toggle_curves_in_view"] = _curves_in_view(w)
        else:
            checks["blf_toggle_no_restream"] = False
            checks["blf_toggle_keeps_values"] = False
            checks["blf_toggle_curves_in_view"] = False
        # 分轴模式（每信号一个子图、独立 Y、X 联动）同样必须看得见
        w.per_axis = True
        w.render()
        checks["blf_per_axis_curves_in_view"] = _curves_in_view(w)
        w.per_axis = False
        w.render()
        n_before = len(streams)
        keys = [os.path.splitext(os.path.basename(p))[0] for p in dbc_paths]
        w.blf_priority = {"0": [keys[-1]] + keys[:-1]}
        w.open_blf(w.dat_path)
        checks["blf_remap_no_restream"] = len(streams) == n_before
        # 三态演示：FR_RE 网段未录 -> 未评估 + 缺信号，不是 0 命中
        w2 = MainWindow()
        w2._blf_sync = True
        w2._blf_bad = set(bad)
        w2._blf_dbc_override = list(dbc_paths)
        w2.open_file(dat, auto=False)
        w2.load_rules(_fixtures.FIXTURE_PATHS["blf_frre"])
        w2.run_analysis()
        checks["blf_second_window_single_pass"] = len(streams) == n_before + 1
        nev = w2.result.summary["not_evaluated"] if w2.result else 0
        checks["frre_not_evaluated"] = (
            nev == 1 and w2.rule_tree.topLevelItem(2).childCount() == 1
            and "未评估" in w2.rule_tree.topLevelItem(2).child(0).text(0)
            and "HCU_GearShiftAvl_143h_ch0" in w2.rule_tree.topLevelItem(2).child(0).toolTip(0))
        checks["no_network_call"] = len(net_calls) == 0
        w.close()
        w2.close()
    finally:
        MainWindow._on_blf_scanned = orig_scanned
        MainWindow._on_blf_loaded = orig_loaded
        socket.socket.connect = original_connect
        can.BLFReader = orig_reader
        pg.ViewBox.addItem = orig_vb_additem
    report = {"checks": checks, "passed": all(checks.values()), "bad": sorted(bad),
              "dat": os.path.basename(dat), "ruleset": ruleset,
              "summary": w.result.summary if w.result else None,
              "p2_total_intervals": total}
    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, "p3_verify_%s.json" % tag)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, sort_keys=True)
    return report["passed"], report, out


def _own_scale_ok(m, name, vb, tol=0.06, max_ratio=1.5):
    """该轴 Y 量程是否只贴着这条信号自己的数据（证明各轴独立、不共用一把尺）。

    判据两条：① 包住自己的数据（留 6% 容差给像素取整）；② 宽度不超过自身跨度的 1.5 倍。
    第 ② 条是关键：若所有轴共用同一把尺，窄量程信号所在的轴会被撑到全局包络，比值必然超标。
    常量信号（跨度 0）返回 None，不参与判定——它的自动量程是环绕式补偿出来的。
    """
    vals = np.asarray(m.signals[name], dtype=float)
    lo, hi = float(np.nanmin(vals)), float(np.nanmax(vals))
    span = hi - lo
    if not math.isfinite(span) or span <= 0:
        return None
    r = vb.viewRange()[1]
    covers = r[0] <= lo + tol * span and r[1] >= hi - tol * span
    return bool(covers and (r[1] - r[0]) <= span * max_ratio)


def verify_rule_edit(tag="rule_edit", bad=frozenset()):
    """验收「界面改规则、程序写 JSON」这条链，全程 offscreen 程序化断言。

    覆盖：载入不改内容 / 改数值 / 新增 / 删除 / 拒存不改文件 / 复杂表达式原样保留 /
    保存后界面立刻按新规则重算。临时规则集放在 rules/ 下（界面的下拉只扫那里），
    跑完即删，不碰真实规则文件。
    """
    checks = {}
    tmp = os.path.join(RULES_DIR, "_edit_verify.json")
    bak = tmp + ".bak"
    lean_path = os.path.join(RULES_DIR, "_edit_verify_lean.json")
    cleanup = (tmp, bak, tmp + ".tmp", lean_path)

    def wipe():
        for p in cleanup:
            if os.path.exists(p):
                os.remove(p)

    wipe()
    shutil.copyfile(_fixtures.FIXTURE_PATHS["gear"], tmp)
    base_bytes = open(tmp, "rb").read()
    base = json.loads(base_bytes.decode("utf-8"))
    n0 = len(base["rules"])
    os.makedirs(OUT_DIR, exist_ok=True)

    # 变异只改实现，不动断言：否则等于测试自己把答案改对，证明不了任何事。
    if "mangle" in bad:
        _orig = caliper_rules.when_to_conds

        def _loose(expr):
            conds, join = _orig(expr)
            if conds is None:          # 缺陷：把复杂表达式硬拆成第一个条件，其余丢掉
                head = re.split(r"\s*[&|]\s*", str(expr).strip())[0]
                m = caliper_rules._COND_RE.match(head)
                if m:
                    return [(m.group(1), m.group(2), float(m.group(3)))], "&"
            return conds, join

        caliper_rules.when_to_conds = _loose
    if "noapply" in bad:
        def _half_apply(self, path):   # 缺陷：只刷新下拉，忘了按新规则重算
            self.refresh_rules()
            self.rules_path = path
            self.rule_scope_label.setText(os.path.basename(path))

        MainWindow.after_rules_saved = _half_apply

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    apply_theme(app)
    try:
        # ---- 1) 载入即所见：表格内容必须与文件一致，不能一打开就走样 ----
        dlg = RuleSetDialog(tmp, signals=[])
        checks["rows_loaded"] = dlg.table.rowCount() == n0 == 4
        checks["conds_preserved"] = all(
            dlg.when_at(r) == base["rules"][r]["when"] for r in range(n0))
        checks["summary_plain"] = all(
            dlg.table.item(r, dlg.COL_WHEN).text() == base["rules"][r]["when"]
            for r in range(n0))
        checks["focus_rule_row"] = (dlg.select_rule("N档") == 2
                                    and dlg.table.currentRow() == 2)
        checks["focus_missing_row"] = dlg.select_rule("不存在的规则") == -1
        checks["dirty_clean_at_open"] = dlg.is_dirty() is False
        not_ruleset = False
        try:
            RuleSetDialog(os.path.join(RULES_DIR, "templates", "pending.json"), signals=[])
        except ValueError:
            not_ruleset = True
        checks["non_ruleset_rejected"] = not_ruleset
        probe = RuleSetDialog(tmp, signals=[])
        probe.set_cell(0, probe.COL_MIN, "999")
        checks["dirty_tracks_edit"] = probe.is_dirty() is True
        # 最简规则（无 mark、无三个数值字段）打开后不能算「改过」
        lean = os.path.join(RULES_DIR, "_edit_verify_lean.json")
        with open(lean, "w", encoding="utf-8", newline="\n") as f:
            json.dump({"name": "最简", "version": 1,
                       "rules": [{"name": "只有条件", "when": "A > 1"}]},
                      f, ensure_ascii=False, indent=2)
        try:
            checks["dirty_lean_rule"] = RuleSetDialog(lean, signals=[]).is_dirty() is False
        finally:
            os.remove(lean)

        # ---- 2) 改一个数值 -> 写盘：只有这一条变，其余原样，旧版备份 ----
        dlg.set_cell(3, dlg.COL_MIN, "800")
        checks["save_ok"] = dlg.save() is True
        doc2 = json.load(open(tmp, encoding="utf-8"))
        checks["value_written"] = doc2["rules"][3]["min_duration_ms"] == 800
        checks["version_bumped"] = doc2["version"] == base["version"] + 1
        checks["others_untouched"] = doc2["rules"][:3] == base["rules"][:3]
        checks["backup_is_original"] = (os.path.exists(bak)
                                       and open(bak, "rb").read() == base_bytes)
        checks["engine_accepts"] = [r.when for r in caliper_rules.load_ruleset(tmp)] == \
            [r["when"] for r in doc2["rules"]]

        # ---- 3) 新增一条：条件由对话框拼出，保存后引擎能直接评估 ----
        cd = ConditionDialog(
            "(GrMod_stDrvAct == 4) & (AccP_percAccrPedl < 1)", "滑行",
            ["GrMod_stDrvAct", "AccP_percAccrPedl"])
        checks["cond_generated"] = cd.when() == \
            "(GrMod_stDrvAct == 4) & (AccP_percAccrPedl < 1)"
        row = dlg.add_rule("滑行", cd.when())
        dlg.set_cell(row, dlg.COL_MIN, "500")
        checks["add_saved"] = dlg.save() is True
        doc3 = json.load(open(tmp, encoding="utf-8"))
        checks["rule_added"] = (len(doc3["rules"]) == n0 + 1
                                and doc3["rules"][-1]["name"] == "滑行")
        rs3 = caliper_rules.load_ruleset(tmp)
        checks["added_evaluable"] = (
            rs3[-1].required_signals == ["AccP_percAccrPedl", "GrMod_stDrvAct"]
            and rs3[-1].min_duration_ms == 500.0)

        # ---- 4) 删除一条 ----
        dlg.table.selectRow(0)
        dlg.remove_selected()
        checks["delete_saved"] = dlg.save() is True
        doc4 = json.load(open(tmp, encoding="utf-8"))
        checks["rule_removed"] = (len(doc4["rules"]) == n0
                                  and all(r["name"] != "P档" for r in doc4["rules"]))

        # ---- 5) 非法输入：拒存，且一个字节都不碰文件 ----
        digest_before = caliper_rules.md5_of(tmp)
        bad_dlg = RuleSetDialog(tmp, signals=[])
        bad_dlg.set_cell(0, bad_dlg.COL_NAME, "   ")
        checks["blank_rejected"] = (bad_dlg.save() is False
                                    and "规则名不能为空" in bad_dlg.msg.text())
        checks["file_untouched"] = caliper_rules.md5_of(tmp) == digest_before

        # 错误表达式在写入前就被拦下（走 set_when 的校验）
        rejected = False
        try:
            bad_dlg.set_when(0, "GrMod_stDrvAct ===")
        except ValueError:
            rejected = True
        checks["bad_expr_rejected"] = rejected and caliper_rules.md5_of(tmp) == digest_before

        # ---- 6) 复杂表达式：高级模式已取消；旧规则里的复杂 when 作为 expr 条件原样保留 ----
        hard = "abs(AccP_percAccrPedl) < 5"
        cd2 = ConditionDialog(hard, "复杂", ["AccP_percAccrPedl"])
        checks["complex_no_advanced"] = not hasattr(cd2, "chk_adv")
        checks["complex_as_expr"] = any(
            c.get("type") == "value" and c.get("expr") == hard
            for c in cd2.conditions_data())
        cd2.close()
        bad_dlg.set_when(0, hard)
        bad_dlg.set_cell(0, bad_dlg.COL_NAME, "复杂")
        checks["complex_saved"] = bad_dlg.save() is True
        doc6 = json.load(open(tmp, encoding="utf-8"))
        checks["complex_intact"] = doc6["rules"][0]["when"] == hard

        # ---- 7) 界面联动：保存后下拉仍选中它，并立刻按新规则重算 ----
        w = MainWindow()
        w.resize(1200, 700)
        w.show()
        w.open_file(os.path.join(DATA_DIR, "Creep02.dat"), auto=False)
        w.load_rules(tmp)
        w.run_analysis()
        dlg4 = RuleSetDialog(tmp, signals=sorted(w.signal_map))
        dlg4.set_when(0, "GrMod_stDrvAct == 4")     # 把「复杂」那条改成 D 档
        dlg4.set_cell(0, dlg4.COL_NAME, "复杂")
        checks["ui_save"] = dlg4.save() is True
        w.after_rules_saved(tmp)
        checks["edit_entry_present"] = (
            w.btn_edit_rules.text() == "编辑规则"
            and w.rule_tree.contextMenuPolicy()
            == QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        checks["ui_selected"] = (w.rules_path is not None
                                 and os.path.basename(w.rules_path) == "_edit_verify.json")
        engine = caliper_rules.evaluate(w.m, caliper_rules.load_ruleset(tmp))
        first_ui = w.result.results[0] if w.result and w.result.results else None
        checks["ui_recomputed"] = (first_ui is not None
                                   and first_ui.rule_name == engine.results[0].rule_name
                                   and first_ui.intervals == engine.results[0].intervals
                                   and first_ui.status == "hit")
        checks["ui_matches_engine"] = w.result.summary == engine.summary
        checks["ui_new_signal_checked"] = "GrMod_stDrvAct" in w.checked
        checks["ui_rule_shown_in_panel"] = any(
            "复杂" in w.rule_tree.topLevelItem(i).child(j).text(0)
            for i in range(w.rule_tree.topLevelItemCount())
            for j in range(w.rule_tree.topLevelItem(i).childCount()))

        # ---- 8) 子目录里的规则集（templates/）同样能改，下拉定位要正确 ----
        tpl = os.path.join(RULES_DIR, "templates", "_edit_verify_tpl.json")
        shutil.copyfile(os.path.join(RULES_DIR, "templates", "gear_states.json"), tpl)
        try:
            dt = RuleSetDialog(tpl, signals=[])
            dt.set_cell(0, dt.COL_MIN, "300")
            checks["template_saved"] = dt.save() is True
            checks["template_value"] = json.load(
                open(tpl, encoding="utf-8"))["rules"][0]["min_duration_ms"] == 300
            w.after_rules_saved(tpl)
            checks["template_selected"] = (w.rules_path is not None
                                           and w.rules_path.endswith(
                                               os.path.join("templates", "_edit_verify_tpl.json")))
        finally:
            for fp in (tpl, tpl + ".bak", tpl + ".tmp"):
                if os.path.exists(fp):
                    os.remove(fp)

        w.close()
    finally:
        wipe()

    report = {
        "checks": checks, "passed": all(checks.values()), "bad": sorted(bad),
        "ruleset": "夹具档位规则的临时副本（out/_edit_verify.json，跑完即删）",
        "version": {"base": base["version"],
                    "after_first_save": base["version"] + 1},
        "rule_count": {"base": n0},
    }
    out = os.path.join(OUT_DIR, "p3_verify_%s.json" % tag)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, sort_keys=True)
    return report["passed"], report, out


def verify_blf_incremental(tag="blf_incremental", bad=frozenset()):
    """验收「勾一个加一个」的增量路径：合成 BLF，不读真实文件，秒级完成。

    硬断言对象身份与解码次数——只比数值相等抓不到「全量重算后恰好同值」的假实现。
    """
    import can
    checks = {}
    d1 = os.path.join(OUT_DIR, "_incr.dbc")
    blf = os.path.join(OUT_DIR, "_incr.blf")
    os.makedirs(OUT_DIR, exist_ok=True)
    # 用 GBK 写：真实 DBC 就是 GBK，canmatrix 按 latin-1 读成乱码，验证修复链
    with open(d1, "w", encoding="gbk") as f:
        f.write('VERSION ""\nNS_ :\nBS_:\nBU_: TEST\n'
                'BO_ 256 A: 8 TEST\n SG_ X : 0|8@1+ (1,0) [0|255] "" TEST\n'
                'BO_ 512 B: 8 TEST\n SG_ Y : 0|8@1+ (1,0) [0|255] "" TEST\n'
                'BO_ 1024 V: 8 TEST\n SG_ V : 0|8@1+ (1,0) [0|255] "" TEST\n'
                'CM_ SG_ 256 X "X信号中文描述";\n')  # DBC comment：验证 BLF 描述来源
    if os.path.exists(blf):
        os.remove(blf)
    w = can.BLFWriter(blf)
    # 0x100 五帧作初始时基；0x400 三帧可选；0x200 十帧更密（加它必须回退全量）
    for fid, n, val in ((0x100, 5, 7), (0x400, 3, 11)):
        for i in range(n):
            w.on_message_received(can.Message(
                timestamp=i * 0.01, arbitration_id=fid,
                data=bytes([val + i]) + b"\x00" * 7,
                is_extended_id=False, channel=0, is_fd=True))
    for i in range(10):
        w.on_message_received(can.Message(
            timestamp=i * 0.005, arbitration_id=0x200,
            data=bytes([50 + i]) + b"\x00" * 7,
            is_extended_id=False, channel=0, is_fd=True))
    w.stop()

    orig_incr = MainWindow._blf_incremental
    orig_extract = caliper_core.BlfSession.extract_signal
    orig_load = caliper_core.BlfSession.load
    orig_blf_signals = caliper_core.blf_signals
    if "nodesc" in bad:
        # 变异：BLF 信号不带 desc（等价于没读 DBC comment），描述断言必须变红
        def _bad_signals(scan, dbs, res):
            out = orig_blf_signals(scan, dbs, res)
            for s in out:
                s["desc"] = ""
            return out
        caliper_core.blf_signals = _bad_signals
    extract_calls = []
    full_calls = []
    streams = []
    orig_reader = can.BLFReader

    class CountingReader(orig_reader):
        def __init__(self, *a, **k):
            streams.append(1)
            super().__init__(*a, **k)

    def counting_extract(self, *a, **k):
        extract_calls.append(1)
        return orig_extract(self, *a, **k)

    def counting_load(self, *a, **k):
        full_calls.append(1)
        return orig_load(self, *a, **k)

    if "noincr" in bad:
        MainWindow._blf_incremental = lambda self, sess, wanted: False   # 变异：强制全量
    caliper_core.BlfSession.extract_signal = counting_extract
    caliper_core.BlfSession.load = counting_load
    can.BLFReader = CountingReader
    w = None
    try:
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        sess = caliper_core.BlfSession(blf, dbc_paths=[d1])
        w = MainWindow()
        w.dat_path = blf
        w.blf_dbc_paths = [d1]
        w.blf_session = sess
        w.checked = {"X_ch0"}
        w.m = sess.load(["X_ch0"])
        # 不调 run_analysis()：BLF 分支会起 Worker 异步 load，计数窗口会串进噪声。
        # 这里只验增量决策与其产物，异步路径由 --verify-blf 全量验收覆盖。
        extract_calls.clear()
        full_calls.clear()
        x_id = id(w.m.signals["X_ch0"])
        axis_id = id(w.m.axis)
        n_open = len(streams)

        # 直接调增量判据：验证「只解码新增 + 旧数组身份不变 + 数值与全量相等」
        w.checked.add("V_ch0")
        wanted = sorted(w.rule_signals | w.checked)
        incr_ok = w._blf_incremental(sess, wanted)
        checks["added_signal_present"] = incr_ok and "V_ch0" in w.m.signals
        checks["only_one_decode"] = len(extract_calls) == 1 and not full_calls
        checks["no_restream"] = len(streams) == n_open
        checks["old_array_identity"] = id(w.m.signals["X_ch0"]) == x_id
        checks["axis_identity"] = id(w.m.axis) == axis_id
        # 变异下 noincr 强制全量，w.m 里不会有新信号；逐值核对只在增量成功时要求
        fresh = sess.load(["X_ch0", "V_ch0"])
        checks["matches_full_load"] = (not incr_ok or np.array_equal(
            w.m.signals["V_ch0"], fresh.signals["V_ch0"], equal_nan=True))

        # BLF 信号描述来自 DBC 的 CM_ comment（带 _ch0 后缀的界面名做键）
        entry = sess.entry()
        descs = {s["name"]: s.get("desc", "") for s in entry["signals"]}
        checks["blf_desc_from_dbc_comment"] = descs.get("X_ch0") == "X信号中文描述"
        checks["blf_desc_empty_without_comment"] = descs.get("Y_ch0") == ""

        extract_calls.clear()
        full_calls.clear()
        w.checked.add("Y_ch0")            # 更密报文会改变时基（10 帧 > 5）-> 必须回退全量
        wanted = sorted(w.rule_signals | w.checked)
        # 更密时判据必须拒绝增量（返回 False），由调用方走全量
        checks["denser_falls_back"] = (not w._blf_incremental(sess, wanted)
                                       and not extract_calls)
    finally:
        MainWindow._blf_incremental = orig_incr
        caliper_core.BlfSession.extract_signal = orig_extract
        caliper_core.BlfSession.load = orig_load
        caliper_core.blf_signals = orig_blf_signals
        can.BLFReader = orig_reader
        if w is not None:
            w.close()
        for p in (d1, blf):
            if os.path.exists(p):
                os.remove(p)
    report = {"checks": checks, "passed": all(checks.values()), "bad": sorted(bad)}
    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, "p3_verify_%s.json" % tag)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, sort_keys=True)
    return report["passed"], report, out


def verify_startup(tag="startup", bad=frozenset()):
    """验收启动 preflight 的递归判空与自愈：不碰真实 rules/，全用临时目录。

    - 子目录结构（DAT/ BLF/ templates/）下 find_rules 能递归找到，preflight 不判空；
    - 真空目录时 seed_rules_if_empty 自动建 templates/ 并写一条合法测试规则；
    - 种子规则能被 caliper_rules 加载（真合法，不是占位垃圾）。
    """
    import shutil
    import tempfile
    import start as start_mod

    checks = {}
    do_seed = (lambda base: True) if "noseed" in bad else start_mod.seed_rules_if_empty
    tmp = tempfile.mkdtemp(prefix="wb_startup_")
    try:
        # 场景一：模拟用户整理后的子目录结构（顶层无 json）
        sub = os.path.join(tmp, "rules")
        os.makedirs(os.path.join(sub, "DAT"))
        os.makedirs(os.path.join(sub, "BLF"))
        os.makedirs(os.path.join(sub, "templates"))
        with open(os.path.join(sub, "DAT", "Gear_Dat.json"), "w", encoding="utf-8") as f:
            json.dump({"name": "档位", "version": 1, "rules": []}, f)
        with open(os.path.join(sub, "BLF", "Gear_Blf.json"), "w", encoding="utf-8") as f:
            json.dump({"name": "BLF档位", "version": 1, "rules": []}, f)
        with open(os.path.join(sub, "templates", "tpl.json"), "w", encoding="utf-8") as f:
            json.dump({"name": "模板", "version": 1, "enabled": False, "rules": []}, f)
        found = start_mod.find_rules(sub, skip_dirs=("templates",))
        checks["recursive_find"] = found == ["BLF/Gear_Blf.json", "DAT/Gear_Dat.json"]
        checks["no_false_empty"] = bool(start_mod.find_rules(sub, skip_dirs=()))  # 子目录有规则->非空

        # 场景二：真空目录 -> 自愈生成种子
        empty = os.path.join(tmp, "empty_rules")
        os.makedirs(empty, exist_ok=True)
        checks["empty_before_seed"] = not start_mod.find_rules(empty, skip_dirs=())
        checks["seed_creates_file"] = do_seed(empty) is True
        checks["seed_idempotent"] = do_seed(empty) is False  # 已有->不重复生成
        seed = os.path.join(empty, "templates", "seed_rule.json")
        checks["seed_written"] = os.path.isfile(seed)
        if checks["seed_written"]:
            doc = json.load(open(seed, encoding="utf-8"))
            checks["seed_has_rules"] = bool(doc.get("rules")) and "name" in doc
        # 种子规则能被引擎加载（说明是真合法规则集）
        checks["seed_loadable"] = (checks["seed_written"]
                                   and caliper_rules.load_ruleset_doc(seed).get("rules") is not None)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    report = {"checks": checks, "passed": all(checks.values()), "bad": sorted(bad)}
    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, "p3_verify_%s.json" % tag)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, sort_keys=True)
    return report["passed"], report, out


def verify_blf_switch(tag="blf_switch", bad=frozenset()):
    """验收：连续加载两个 BLF，示波器数据要跟着换。

    根因是异步 QThread worker 没 parent、单引用被换文件覆盖后中途被 GC 销毁，
    导致第二个 BLF 加载中断、停在旧图。这里用同步模式过完整数据链验证「第二个
    BLF 真的替换了数据」；异步崩溃→正常的证据在 HANDOFF 里（探针实测）。
    """
    import can
    checks = {}
    dbc = os.path.join(OUT_DIR, "_switch.dbc")
    b1 = os.path.join(OUT_DIR, "_switch1.blf")
    b2 = os.path.join(OUT_DIR, "_switch2.blf")
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(dbc, "w", encoding="gbk") as f:
        f.write('VERSION ""\nNS_ :\nBS_:\nBU_: TEST\n'
                'BO_ 256 A: 8 TEST\n SG_ X : 0|8@1+ (1,0) [0|255] "" TEST\n')

    def make_blf(path, base, n, dt):
        if os.path.exists(path):
            os.remove(path)
        wr = can.BLFWriter(path)
        for i in range(n):
            wr.on_message_received(can.Message(
                timestamp=i * dt, arbitration_id=0x100,
                data=bytes([base + i]) + b"\x00" * 7,
                is_extended_id=False, channel=0, is_fd=True))
        wr.stop()

    make_blf(b1, 7, 5, 0.01)       # X_ch0: 7..11，5 帧
    make_blf(b2, 90, 8, 0.05)      # X_ch0: 90..97，8 帧（明显不同）

    orig_scan = MainWindow._blf_scan_sync
    orig_run = MainWindow.run_analysis
    if "nostrict" in bad:
        # 变异：第二次分析直接跳过（模拟 bug 表现：第二个 BLF 不重绘，停在旧图）
        calls = [0]
        def _bad_run(self):
            calls[0] += 1
            if calls[0] >= 2:
                return self.result
            return orig_run(self)
        MainWindow.run_analysis = _bad_run
    w = None
    try:
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        w = MainWindow()
        w._blf_sync = True          # 同步走完整链，避开脚本环境 QThread 事件循环不稳
        w._blf_dbc_override = [dbc]

        def load(path):
            w.open_file(path, auto=False)
            app.processEvents()
            w.rule_signals = {"X_ch0"}
            w.checked = {"X_ch0"}
            w.run_analysis()
            app.processEvents()
            return w.m

        m1 = load(b1)
        x1 = float(m1.signals["X_ch0"][0]) if m1 is not None and len(m1.axis) else None
        n1 = len(m1.axis) if m1 is not None else 0
        m2 = load(b2)
        x2 = float(m2.signals["X_ch0"][0]) if m2 is not None and len(m2.axis) else None
        n2 = len(m2.axis) if m2 is not None else 0
        checks["first_blf_loaded"] = (x1 == 7.0 and n1 == 5)
        checks["second_blf_replaces"] = (x2 == 90.0 and n2 == 8)  # 数据真的换了，不是停在旧图
        checks["path_updated"] = os.path.basename(str(w.dat_path)) == "_switch2.blf"
        w.close()
    finally:
        MainWindow._blf_scan_sync = orig_scan
        MainWindow.run_analysis = orig_run
        if w is not None and w._blf_worker is not None:
            w._blf_worker.requestInterruption()
        for p in (dbc, b1, b2):
            if os.path.exists(p):
                os.remove(p)
    report = {"checks": checks, "passed": all(checks.values()), "bad": sorted(bad)}
    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, "p3_verify_%s.json" % tag)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, sort_keys=True)
    return report["passed"], report, out


def verify_cursor_init(tag="cursor_init", bad=frozenset()):
    """验收：未拖过标尺时它必须落在数据视野内（BLF epoch 时基的回归）。

    用合成 epoch 数据。变异 noinit 强制摆到 0（复现 BLF 看不到标尺）。
    """
    checks = {}
    orig_place = MainWindow._place_cursor_at_data_mid
    if "noinit" in bad:
        MainWindow._place_cursor_at_data_mid = lambda self: self.cursor.setValue(0.0)

    w = None
    try:
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        w = MainWindow()
        t0 = 1787226338.25
        axis = t0 + np.arange(60) * 0.1
        w.m = caliper_core.Measurement(
            file="synthetic.blf", config="blf-1ch", mdf_version="BLF", main_group=-1,
            axis=axis, signals={"A": np.arange(60, dtype=float)}, units={},
            not_collected=[], requested=["A"], start_time_utc="", start_time_local="",
            quality={}, readback_ok={})
        w.checked = {"A"}
        w.result = None
        w.rules_path = None
        w.render()
        app.processEvents()
        vb = w.plot.getPlotItem().getViewBox()
        lo, hi = vb.viewRange()[0]
        cur = float(w.cursor.value())
        checks["cursor_in_view"] = lo <= cur <= hi
        checks["cursor_near_data_mid"] = abs(cur - float(axis[20])) < 1e-6
        checks["cursor_x_still_none"] = w.cursor_x is None
        # 未动标尺：值列仍显示 "—"，不能被初始定位带出读数
        checks["no_readout_before_drag"] = all(
            w.value_tree.topLevelItem(i).text(1) == "—"
            for i in range(w.value_tree.topLevelItemCount()))
        # 拖动仍然正常：走 sigPositionChanged 全链路，cursor_x 被设置
        w.cursor.setValue(float(axis[25]))   # 与初始 1/3 位置不同，确保真的触发移动
        checks["drag_still_works"] = (w.cursor_x is not None
                                      and abs(w.cursor_x - float(axis[25])) < 1e-6)
    finally:
        MainWindow._place_cursor_at_data_mid = orig_place
        if w is not None:
            w.close()
    report = {"checks": checks, "passed": all(checks.values()), "bad": sorted(bad)}
    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, "p3_verify_%s.json" % tag)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, sort_keys=True)
    return report["passed"], report, out


def verify_cursor2(tag="cursor2", bad=frozenset()):
    """验收双标尺：黄线独立定位 + 值列 C1/C2/Δ（合成数据，程序化，不截屏）。

    变异 noc2 让右键不定位黄线；baddhead 让分轴线头显示 Δ；badhover 让黄线 hover 变细变亮；whitebg 让线头标签回到白底。断言必须变红。
    """
    checks = {}
    orig_set2 = MainWindow._set_cursor2
    orig_init = MainWindow.__init__
    if "badhover" in bad or "whitebg" in bad:
        # 变异：构造后注入旧缺陷——badhover 细金线；whitebg 把线头标签改回白底
        def _bad_init(self, *a, **k):
            orig_init(self, *a, **k)
            if "badhover" in bad:
                self.cursor2.setHoverPen(pg.mkPen("#DAA520", width=2))
            if "whitebg" in bad:
                for line in [self.cursor, self.cursor2] + [l for pair in self.cursors for l in pair[1:]]:
                    if line.label is not None:
                        line.label.fill = pg.mkBrush(255, 255, 255, 225)
        MainWindow.__init__ = _bad_init
    if "noc2" in bad:
        MainWindow._set_cursor2 = lambda self, x: None
    orig_refresh = MainWindow._refresh_value_tab
    orig_readout = MainWindow._refresh_cursor_readout
    if "baddhead" in bad:
        # 变异：注入旧缺陷——分轴 cursor2 线头改回显示 Δ，新断言必须打红
        def _bad_readout(self):
            orig_readout(self)
            for _n, _l1, l2 in self.cursors:
                if l2.label is not None and self.cursor_x is not None and self.cursor2_x is not None:
                    v1 = self._value_at(_n, self.cursor_x)
                    v2 = self._value_at(_n, self.cursor2_x)
                    l2.label.setText("%+.4g" % (v2 - v1))
        MainWindow._refresh_cursor_readout = _bad_readout

    w = None
    try:
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        w = MainWindow()
        t0 = 1787226338.25
        axis = t0 + np.arange(60) * 0.1
        vals = np.arange(60, dtype=float) * 2.0        # 斜率 2，便于核对 Δ
        w.m = caliper_core.Measurement(
            file="synthetic.blf", config="", mdf_version="BLF", main_group=-1,
            axis=axis, signals={"A": vals}, units={"A": "V"},
            not_collected=[], requested=["A"], start_time_utc="",
            start_time_local="2026-08-13T14:25:38+08:00", quality={}, readback_ok={})
        w.checked = {"A"}
        w.units = {"A": "V"}       # 正常打开文件时由扫描结果填充，合成场景手动给
        w.result = None
        w.rules_path = None
        w.render()
        app.processEvents()

        checks["two_lines_distinct"] = (w.cursor is not w.cursor2
                                        and w.cursor2 is not None)
        checks["cursor2_is_amber"] = (
            w.cursor2.pen.color().name().lower() == CURSOR2_COLOR.lower())

        # 两条标尺 hover 都要真加粗（整数线宽）+ 颜色加深，不能一条加重一条变淡
        def _luma(hex_color):
            c = QtGui.QColor(hex_color)
            return 0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()

        def _hover_ok(line):
            # 读线的实际 pen，不读模块常量——否则变异改了线也打不红断言
            base = line.pen.color().name()
            hover = line.hoverPen.color().name()
            return (line.hoverPen.width() > line.pen.width()
                    and _luma(hover) < _luma(base))  # hover 更暗=加重

        checks["both_hover_thicken"] = (
            w.cursor.hoverPen.width() > w.cursor.pen.width()
            and w.cursor2.hoverPen.width() > w.cursor2.pen.width())
        checks["both_hover_darken"] = _hover_ok(w.cursor) and _hover_ok(w.cursor2)
        checks["initial_separated"] = abs(float(w.cursor2.value())
                                          - float(w.cursor.value())) > 1e-6

        x1, x2 = float(axis[10]), float(axis[40])
        w._set_cursor(x1)
        w._set_cursor2(x2)
        app.processEvents()
        checks["c1_position"] = abs(w.cursor_x - x1) < 1e-9
        checks["c2_position"] = (w.cursor2_x is not None
                                 and abs(w.cursor2_x - x2) < 1e-9)
        checks["independent_move"] = (abs(float(w.cursor.value()) - x1) < 1e-9
                                      and abs(float(w.cursor2.value()) - x2) < 1e-9)

        item = w.value_tree.topLevelItem(0)
        v1, v2 = float(vals[10]), float(vals[40])
        checks["value_c1"] = item.text(1) == _fmt_value(v1)
        checks["value_c2"] = item.text(2) == _fmt_value(v2)
        delta_text = "%+.4g" % (v2 - v1)
        checks["value_delta"] = item.text(3) == delta_text
        checks["delta_no_unit"] = ("V" not in item.text(3))
        checks["units_in_col4"] = item.text(4) == "V"
        checks["readout_shows_both"] = ("t1 =" in w.cursor_label.text()
                                        and "t2 =" in w.cursor_label.text())
        # Δt 必须是 x2-x1（相对量），不能过 _rel_time 再减一次 origin
        dt_text = "%+.3f s" % (x2 - x1)
        checks["readout_delta_t"] = "Δt = " + dt_text in w.cursor_label.text()
        # Δt 不能被 SI origin 污染：直接用 x2-x1，不是 _rel_time(x2-x1)
        wrong_dt = "%+.3f s" % ((x2 - x1) - t0)
        checks["delta_t_no_origin"] = wrong_dt not in w.cursor_label.text()

        # 分轴线头：cursor2 那条标当前值，不能标 Δ（Δ 只在右栏值列）
        w.set_per_axis(True)
        app.processEvents()
        want2 = _fmt_value(v2) + " V"       # 该信号在 x2 的当前值（带单位）
        checks["line_head_c2_is_value"] = bool(w.cursors) and all(
            l2.label is not None and l2.label.toPlainText() == want2
            for _n, _l1, l2 in w.cursors)
        checks["line_head_c2_not_delta"] = all(
            l2.label is None or l2.label.toPlainText() != delta_text
            for _n, _l1, l2 in w.cursors)
        # 线头标签背景透明：cursor1/cursor2 与所有分轴标尺的 label 都不该有白底
        all_labels = [w.cursor.label, w.cursor2.label] + [
            line.label for pair in w.cursors for line in pair[1:]]
        checks["line_head_no_bg"] = bool(all_labels) and all(
            lab is None or lab.fill.style() == QtCore.Qt.BrushStyle.NoBrush
            for lab in all_labels)
        w.set_per_axis(False)
        app.processEvents()

        # 只定位 cursor2 时：C1/Δ 显示 —，C2 仍给值
        w.cursor_x = None
        w._refresh_cursor_readout()
        app.processEvents()
        checks["dt_dash_when_c1_unset"] = "Δt = —" in w.cursor_label.text()
        checks["c1_unset_shows_dash"] = (item.text(1) == "—" and item.text(3) == "—"
                                         and item.text(2) != "—")
    finally:
        MainWindow._set_cursor2 = orig_set2
        MainWindow._refresh_value_tab = orig_refresh
        MainWindow._refresh_cursor_readout = orig_readout
        MainWindow.__init__ = orig_init
        if w is not None:
            w.close()
    report = {"checks": checks, "passed": all(checks.values()), "bad": sorted(bad)}
    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, "p3_verify_%s.json" % tag)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, sort_keys=True)
    return report["passed"], report, out


def verify_cursor_dclick(tag="cursor_dclick", bad=frozenset()):
    """验收：左键双击把标尺定位到点击位置（合成数据，程序化事件，不截屏）。

    变异 nodouble 让双击不再发信号，断言必须变红。
    """
    checks = {}
    orig_dclick = MdaViewBox.mouseDoubleClickEvent
    orig_click = MdaViewBox.mouseClickEvent
    if "nodouble" in bad:
        # 变异：双击不定位（真缺陷注入，不改断言）
        MdaViewBox.mouseDoubleClickEvent = lambda self, ev: ev.accept()
    if "menuconflict" in bad:
        # 变异：右键回到 pyqtgraph 原生「单击立即弹菜单」，双击会被菜单抢走
        MdaViewBox.mouseClickEvent = pg.ViewBox.mouseClickEvent

    w = None
    try:
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        w = MainWindow()
        t0 = 1787226338.25
        axis = t0 + np.arange(60) * 0.1
        w.m = caliper_core.Measurement(
            file="synthetic.blf", config="blf-1ch", mdf_version="BLF", main_group=-1,
            axis=axis, signals={"A": np.arange(60, dtype=float)}, units={},
            not_collected=[], requested=["A"], start_time_utc="", start_time_local="",
            quality={}, readback_ok={})
        w.checked = {"A"}
        w.result = None
        w.rules_path = None
        w.render()
        app.processEvents()

        vb = w.plot.getPlotItem().getViewBox()
        view_before = vb.viewRange()
        # 目标数据 X：轴中偏右，模拟在图上双击这个位置
        target = float(axis[40])

        class _DClickEv:
            def __init__(self, button):
                self._button = button
                self.accepted = False

            def button(self):
                return self._button

            def pos(self):
                # 数据坐标 -> ViewBox 局部坐标，真实 mouseDoubleClickEvent 会反算回来
                return vb.childGroup.mapToParent(pg.Point(target, 0.0))

            def accept(self):
                self.accepted = True

            def ignore(self):
                self.accepted = False

        ev = _DClickEv(QtCore.Qt.MouseButton.LeftButton)
        vb.mouseDoubleClickEvent(ev)
        app.processEvents()
        checks["left_dclick_accepts"] = ev.accepted
        checks["cursor_at_click"] = (w.cursor_x is not None
                                     and abs(w.cursor_x - target) < 1e-3)
        checks["cursor_line_moved"] = abs(float(w.cursor.value()) - target) < 1e-3
        checks["readout_refreshed"] = ("t1 =" in w.cursor_label.text()
                                       and "%.3f s" % w._rel_time(target) in w.cursor_label.text())
        checks["no_rect_zoom"] = vb.viewRange() == view_before   # 双击不能误触发框选

        # 分轴：双击子图同样定位（重建后确认接线没丢）
        w.set_per_axis(True)
        app.processEvents()
        sub_vb = w.axis_plots[0][1].getViewBox()
        target2 = float(axis[15])
        ev2 = _DClickEv(QtCore.Qt.MouseButton.LeftButton)
        ev2.pos = lambda: sub_vb.childGroup.mapToParent(pg.Point(target2, 0.0))
        sub_vb.mouseDoubleClickEvent(ev2)
        app.processEvents()
        checks["per_axis_dclick"] = (w.cursor_x is not None
                                     and abs(w.cursor_x - target2) < 1e-3)
        # 右键双击定位 cursor2（黄线），不碰 cursor1
        before_x = w.cursor_x
        ev3 = _DClickEv(QtCore.Qt.MouseButton.RightButton)
        vb.mouseDoubleClickEvent(ev3)
        app.processEvents()
        checks["right_dclick_moves_c2"] = (w.cursor2_x is not None
                                           and abs(w.cursor2_x - target) < 1e-3)
        checks["right_dclick_keeps_c1"] = w.cursor_x == before_x

        # 真实场景事件序列：右键双击时菜单不能弹（否则菜单抢焦点，用户以为无效）。
        # 直接调 mouseDoubleClickEvent 抓不到这个缺陷——必须走 QGraphicsScene 序列。
        w.set_per_axis(False)
        app.processEvents()
        vb2 = w.plot.getPlotItem().getViewBox()
        menu_calls = []
        orig_raise = vb2.raiseContextMenu
        vb2.raiseContextMenu = lambda ev: (menu_calls.append(1), orig_raise(ev))
        scene = w.plot.scene()
        target3 = float(axis[8])
        sp = vb2.mapViewToScene(pg.Point(target3, 0.0))

        def _send(kind, button):
            e = QtWidgets.QGraphicsSceneMouseEvent(kind)
            e.setButton(button)
            e.setButtons(button if kind != QtWidgets.QGraphicsSceneMouseEvent.GraphicsSceneMouseRelease
                         else QtCore.Qt.MouseButton.NoButton)
            e.setPos(sp)
            e.setScenePos(sp)
            e.setScreenPos(QtCore.QPoint(100, 100))
            QtWidgets.QApplication.sendEvent(scene, e)
            app.processEvents()

        RB = QtCore.Qt.MouseButton.RightButton
        SM = QtWidgets.QGraphicsSceneMouseEvent
        for kind in (SM.GraphicsSceneMousePress, SM.GraphicsSceneMouseRelease,
                     SM.GraphicsSceneMouseDoubleClick, SM.GraphicsSceneMouseRelease):
            _send(kind, RB)
        checks["right_dclick_no_menu"] = not menu_calls
        checks["right_dclick_real_moves_c2"] = (w.cursor2_x is not None
                                                and abs(w.cursor2_x - target3) < 1e-3)
        # 定时器必须覆盖系统双击间隔，否则慢双击仍被菜单抢走
        checks["menu_delay_covers_dclick"] = (
            vb2._menu_timer.interval() >= QtWidgets.QApplication.instance().doubleClickInterval())
        # 右键单击（未触发双击）后，定时器到期仍要弹菜单——不能把单击功能弄丢
        menu_calls.clear()
        _send(SM.GraphicsSceneMousePress, RB)
        _send(SM.GraphicsSceneMouseRelease, RB)
        vb2._menu_timer.timeout.emit()   # 快进定时器，避免真等 580ms
        app.processEvents()
        checks["single_click_still_menu"] = bool(menu_calls)
        vb2.raiseContextMenu = orig_raise
    finally:
        MdaViewBox.mouseDoubleClickEvent = orig_dclick
        MdaViewBox.mouseClickEvent = orig_click
        if w is not None:
            w.close()
    report = {"checks": checks, "passed": all(checks.values()), "bad": sorted(bad)}
    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, "p3_verify_%s.json" % tag)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, sort_keys=True)
    return report["passed"], report, out


def verify_mouse(tag="mouse", bad=frozenset()):
    """验收示波器鼠标语义：左键框选、中键左右拖、滚轮禁用。程序化，不模拟真实点击。

    直接检查 ViewBox 配置与事件方法分流；变异 nowheel / nopan / norect 各自打红对应项。
    """
    checks = {}
    orig_wheel = MdaViewBox.wheelEvent
    orig_drag = MdaViewBox.mouseDragEvent
    if "nowheel" in bad:
        MdaViewBox.wheelEvent = pg.ViewBox.wheelEvent      # 变异：恢复滚轮缩放
    if "nopan" in bad:
        # 变异：中键不再切 PanMode，落回父类 RectMode -> 中键也变成画框
        def _nopan(self, ev, axis=None):
            pg.ViewBox.mouseDragEvent(self, ev, axis)
        MdaViewBox.mouseDragEvent = _nopan
    orig_cinit = MdaViewBox.__init__
    if "norect" in bad:
        # 变异：构造后把左键模式切回 PanMode（真缺陷注入，不改断言）
        def _norect_init(self, *a, **k):
            orig_cinit(self, *a, **k)
            self.setMouseMode(pg.ViewBox.PanMode)

        MdaViewBox.__init__ = _norect_init

    w = None
    try:
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        w = MainWindow()
        vb = w.plot.getPlotItem().getViewBox()
        checks["custom_viewbox"] = isinstance(vb, MdaViewBox)
        checks["left_rect_mode"] = vb.state["mouseMode"] == pg.ViewBox.RectMode

        class _WheelEv:
            def __init__(self):
                self.ignored = False

            def delta(self):
                return 120                 # 正常会放大 1.02^120 ≈ 10 倍

            def pos(self):
                return QtCore.QPointF(10, 10)

            def ignore(self):
                self.ignored = True

            def accept(self):
                pass

        before = vb.viewRange()
        ev = _WheelEv()
        vb.wheelEvent(ev)
        app.processEvents()
        checks["wheel_disabled"] = ev.ignored and vb.viewRange() == before

        # 中键拖动：真实调一次 mouseDragEvent，确认期间 Y 被锁、结束后状态还原
        class _DragEv:
            def __init__(self, button, finish=True):
                self._button = button
                self._finish = finish

            def button(self):
                return self._button

            def pos(self):
                return pg.Point(120, 40)

            def lastPos(self):
                return pg.Point(60, 40)

            def isFinish(self):
                return self._finish

            def buttonDownPos(self, b):
                return pg.Point(60, 40)

            def accept(self):
                pass

        mode_before = vb.state["mouseMode"]
        y_before = vb.viewRange()[1]
        vb.mouseDragEvent(_DragEv(QtCore.Qt.MouseButton.MiddleButton))
        checks["middle_restores_mode"] = vb.state["mouseMode"] == mode_before
        checks["middle_restores_y_enable"] = bool(vb.state["mouseEnabled"][1])
        checks["middle_y_unchanged"] = vb.viewRange()[1] == y_before
    finally:
        MdaViewBox.wheelEvent = orig_wheel
        MdaViewBox.mouseDragEvent = orig_drag
        MdaViewBox.__init__ = orig_cinit
        if w is not None:
            w.close()
    report = {"checks": checks, "passed": all(checks.values()), "bad": sorted(bad)}
    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, "p3_verify_%s.json" % tag)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, sort_keys=True)
    return report["passed"], report, out


def verify_reltime(tag="reltime", bad=frozenset()):
    """验收 X 轴相对时间：合成 epoch 时基，不依赖真实 BLF 文件。

    两层都要验：刻度与标尺读数从首帧起算（可读），而 m.axis 仍是 epoch 原值（判定不受影响）。
    """
    checks = {}
    orig_set_origin = RelativeTimeAxis.set_origin
    orig_init = RelativeTimeAxis.__init__
    if "nooffset" in bad:
        # 变异：不设零点，刻度退回 1.8e9（真缺陷注入，不改断言）
        RelativeTimeAxis.set_origin = lambda self, t0: None
    if "noscalefix" in bad:
        # 变异：恢复 pyqtgraph 的自动 SI 前缀 -> label 出现 "(x1e+09)"（真实缺陷复现）
        def _with_si(self, *args, **kwargs):
            orig_init(self, *args, **kwargs)
            self.enableAutoSIPrefix(True)

        RelativeTimeAxis.__init__ = _with_si
    w = None
    try:
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        w = MainWindow()
        t0 = 1787226338.25
        n = 60
        axis = t0 + np.arange(n) * 0.1
        m = caliper_core.Measurement(
            file="synthetic.blf", config="blf-1ch", mdf_version="BLF", main_group=-1,
            axis=axis, signals={"A": np.arange(n, dtype=float)}, units={"A": "V"},
            not_collected=[], requested=["A"],
            start_time_utc="2026-08-13T06:25:38+00:00",
            start_time_local="2026-08-13T14:25:38+08:00",
            quality={}, readback_ok={})
        w.m = m
        w.checked = {"A"}
        w.rules_path = None
        w.result = None
        w.render()
        checks["origin_is_first_frame"] = abs(w._x_origin - t0) < 1e-6
        xaxis = w.plot.getPlotItem().getAxis("bottom")
        # 直接比对「相对值」应有的刻度文字：只查 "178" 不在串里是废断言，
        # pyqtgraph 把 1.8e9 排成科学计数法时根本不含 178。
        # 期望值必须绕开子类（否则 origin 会被减两次），直接调父类格式化。
        ticks = xaxis.tickStrings([t0, t0 + 10.0, t0 + 20.0], 1.0, 10.0)
        expect = pg.AxisItem.tickStrings(xaxis, [0.0, 10.0, 20.0], 1.0, 10.0)
        checks["ticks_are_relative"] = ticks == expect
        # 真实绘制路径的坑：pyqtgraph 按 epoch 范围自动加 SI 前缀，label 变成
        # "时间 / s (x1e+09)"、scale=1e9 把相对秒又乘回去 —— 单测 tickStrings 抓不到。
        app.processEvents()
        label_text = xaxis.label.text if hasattr(xaxis.label, "text") else str(xaxis.labelText)
        checks["no_si_prefix"] = not xaxis.autoSIPrefix and "e+" not in str(label_text).lower()
        checks["scale_is_one"] = abs(xaxis.autoSIPrefixScale - 1.0) < 1e-12
        checks["axis_data_unchanged"] = abs(float(w.m.axis[0]) - t0) < 1e-6
        w._set_cursor(t0 + 5.0)
        checks["cursor_t_relative"] = "5.000 s" in w.cursor_label.text()
        w.set_per_axis(True)
        app.processEvents()
        bottoms = [pi.getAxis("bottom") for _, pi in w.axis_plots
                   if pi.getAxis("bottom").style["showValues"]]
        checks["per_axis_relative"] = bool(bottoms) and all(
            isinstance(a, RelativeTimeAxis) and abs(a.origin - t0) < 1e-6 for a in bottoms)
    finally:
        RelativeTimeAxis.set_origin = orig_set_origin
        RelativeTimeAxis.__init__ = orig_init
        if w is not None:
            w.close()
    report = {"checks": checks, "passed": all(checks.values()), "bad": sorted(bad)}
    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, "p3_verify_%s.json" % tag)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, sort_keys=True)
    return report["passed"], report, out


def verify_next_segment(tag="next_segment", bad=frozenset()):
    """子规则右键「下一段」验收：逐段游标状态机 + 右键菜单含该项。

    D档在 Creep02+gear 下有 2 段：首次跳第 1 段、再点第 2 段、末段再点循环回第 1 段。
    变异 alwaysfirst=游标不推进；nowrap=末段不循环；alwaysjump=左键守卫失效；
    noreset=左键重进规则不重置游标（本次 bug）。
    """
    checks = {}
    orig_next = MainWindow.next_segment
    orig_click = MainWindow.on_item_clicked
    if "noreset" in bad:
        # 变异：左键重新进入规则时不重置游标（就是本次修复的 bug）
        def _bad_click(self, item, column):
            rule_name = item.data(0, RULE_NAME_ROLE)
            if rule_name and self._clicked_rule == rule_name:
                return
            self._clicked_rule = rule_name
            data = item.data(0, USER_ROLE)
            if data:
                self._jump_to_interval(float(data[0]), float(data[1]))
        MainWindow.on_item_clicked = _bad_click
    if "alwaysjump" in bad:
        # 变异：去掉「已激活再点无动作」守卫，每次左键都跳第一段
        def _bad_click(self, item, column):
            data = item.data(0, USER_ROLE)
            if data:
                self._jump_to_interval(float(data[0]), float(data[1]))
        MainWindow.on_item_clicked = _bad_click
    if "nowrap" in bad:
        # 变异：末段后再点无动作、不循环（旧行为），循环断言必须变红
        def _bad_next(self, rule_name):
            if not rule_name or self.result is None:
                return False
            rr = next((r for r in self.result.results if r.rule_name == rule_name), None)
            if rr is None or not rr.intervals:
                return False
            ivs = rr.intervals
            idx = self._seg_nav.get(rule_name, -1)
            nxt = 0 if idx < 0 else idx + 1
            if nxt >= len(ivs):
                return False              # 变异：不循环，末段直接返回
            iv = ivs[nxt]
            self._seg_nav[rule_name] = nxt
            self._jump_to_interval(float(iv["t0"]), float(iv["t1"]))
            return True
        MainWindow.next_segment = _bad_next
    if "alwaysfirst" in bad:
        def _bad_next(self, rule_name):
            if not rule_name or self.result is None:
                return False
            rr = next((r for r in self.result.results if r.rule_name == rule_name), None)
            if not rr or not rr.intervals:
                return False
            iv = rr.intervals[0]         # 变异：永远第一段，不推进游标
            self._jump_to_interval(float(iv["t0"]), float(iv["t1"]))
            return True
        MainWindow.next_segment = _bad_next

    w = None
    try:
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        w = MainWindow()
        w.open_file(os.path.join(DATA_DIR, "Creep02.dat"), auto=False)
        w.load_rules(_fixtures.FIXTURE_PATHS["gear"])
        w.run_analysis()
        app.processEvents()
        d = next((r for r in w.result.results if len(r.intervals) >= 2), None)
        checks["has_multi_segment_rule"] = d is not None
        if d is None:
            raise RuntimeError("没有含 2 段以上的规则，验收数据集失效")
        name = d.rule_name
        t0s = [round(float(iv["t0"]), 6) for iv in d.intervals]

        # 首次点 -> 第一段
        ok1 = w.next_segment(name)
        app.processEvents()
        checks["first_click_first_segment"] = (
            ok1 and abs(w.cursor.value() - t0s[0]) < 1e-6
            and w._seg_nav.get(name) == 0)
        # 再点 -> 第二段
        ok2 = w.next_segment(name)
        app.processEvents()
        checks["second_click_next_segment"] = (
            ok2 and abs(w.cursor.value() - t0s[1]) < 1e-6
            and w._seg_nav.get(name) == 1)
        # 末段再点 -> 循环回第一段
        ok3 = w.next_segment(name)
        app.processEvents()
        checks["last_click_wraps_to_first"] = (
            ok3 and abs(w.cursor.value() - t0s[0]) < 1e-6
            and w._seg_nav.get(name) == 0)
        # 再走一整轮回到第一段之后，紧接着再点应又回到第二段（循环连续有效）
        w.next_segment(name)
        app.processEvents()
        checks["wrap_keeps_cycling"] = (
            abs(w.cursor.value() - t0s[1]) < 1e-6 and w._seg_nav.get(name) == 1)

        # 右键菜单有「下一段」，且对无段规则禁用
        captured = {}
        orig_menu_cls = QtWidgets.QMenu

        class CapMenu(orig_menu_cls):
            def exec(self, *a, **k):
                captured["acts"] = [(x.text(), x.isEnabled()) for x in self.actions()]
                return None

        QtWidgets.QMenu = CapMenu
        try:
            # 找到该子规则的树项，模拟右键它
            target = None
            for i in range(w.rule_tree.topLevelItemCount()):
                top = w.rule_tree.topLevelItem(i)
                for j in range(top.childCount()):
                    ch = top.child(j)
                    if ch.data(0, RULE_NAME_ROLE) == name:
                        target = ch
            rect = w.rule_tree.visualItemRect(target)
            w.on_rule_context_menu(rect.center())
        finally:
            QtWidgets.QMenu = orig_menu_cls
        acts = dict(captured.get("acts", []))
        checks["context_menu_has_next"] = ("下一段" in acts and acts["下一段"] is True)

        # 换文件/重算后游标应失效：直接调 next 又回到第一段
        w._seg_nav = {}
        w.next_segment(name)
        app.processEvents()
        checks["reset_returns_first"] = abs(w.cursor.value() - t0s[0]) < 1e-6

        # 左键点击：首次跳第一段，同一条已选中高亮时再点无动作，点另一条才跳
        def find_item(rule):
            for i in range(w.rule_tree.topLevelItemCount()):
                top = w.rule_tree.topLevelItem(i)
                for j in range(top.childCount()):
                    ch = top.child(j)
                    if ch.data(0, RULE_NAME_ROLE) == rule:
                        return ch
            return None

        w._clicked_rule = None
        item_a = find_item(name)
        w.on_item_clicked(item_a, 0)
        app.processEvents()
        checks["left_click_first_jumps"] = (
            abs(w.cursor.value() - t0s[0]) < 1e-6 and w._clicked_rule == name)
        # 已激活态：把光标移开，再点同一条应无动作（光标不动）
        w._set_cursor(float(w.m.axis[len(w.m.axis) // 2]))
        app.processEvents()
        moved = w.cursor.value()
        w.on_item_clicked(item_a, 0)
        app.processEvents()
        checks["left_click_selected_no_action"] = abs(w.cursor.value() - moved) < 1e-9
        # 点另一条规则：应切换激活并跳转
        other = next((r for r in w.result.results
                      if r.rule_name != name and r.intervals), None)
        if other:
            item_b = find_item(other.rule_name)
            w.on_item_clicked(item_b, 0)
            app.processEvents()
            checks["left_click_other_switches"] = (
                w._clicked_rule == other.rule_name
                and abs(w.cursor.value() - float(other.intervals[0]["t0"])) < 1e-6)
        else:
            checks["left_click_other_switches"] = True

        # 复现 bug：推进到第 3 段 -> 左键切到别的规则 -> 点回这条 -> 下一段应是第 2 段，不是第 4 段。
        # 造一条 4 段合成规则，避免依赖数据集里恰好有 4 段的规则。
        probe = "游标重置探针"
        probe_t0s = [1.0, 2.0, 3.0, 4.0]
        probe_result = caliper_rules.RuleResult(
            probe, 1, "hit", [],
            [{"t0": t, "t1": t + 0.5, "duration_ms": 500, "snapshot": {}}
             for t in probe_t0s])
        w.result.results.append(probe_result)
        # 在树里加两个可点条目（探针 + 另一条 B），带 rule_name / 第一段区间
        hit_top = next((w.rule_tree.topLevelItem(i)
                        for i in range(w.rule_tree.topLevelItemCount())
                        if w.rule_tree.topLevelItem(i).text(0) == "命中"), None)
        def add_item(label, t0):
            it = QtWidgets.QTreeWidgetItem([label, "4 段" if label == probe else "1 段"])
            it.setData(0, RULE_NAME_ROLE, label)
            it.setData(0, USER_ROLE, [t0, t0 + 0.5])
            hit_top.addChild(it)
            return it
        it_probe = add_item(probe, probe_t0s[0])
        it_b = add_item("另一规则B", 10.0)
        w.result.results.append(caliper_rules.RuleResult(
            "另一规则B", 1, "hit", [],
            [{"t0": 10.0, "t1": 10.5, "duration_ms": 500, "snapshot": {}}]))

        w._clicked_rule = None
        w._seg_nav = {}
        w.on_item_clicked(it_probe, 0)         # 左键点探针 -> 第 1 段, 游标=0
        w.next_segment(probe)                  # -> 第 2 段
        w.next_segment(probe)                  # -> 第 3 段, 游标=2
        checks["bugsetup_at_third"] = (w._seg_nav.get(probe) == 2)
        w.on_item_clicked(it_b, 0)             # 左键切到 B
        w.on_item_clicked(it_probe, 0)         # 点回探针 -> 第 1 段, 游标应重置
        checks["reenter_resets_cursor"] = (w._seg_nav.get(probe) == 0)
        w.next_segment(probe)                  # 关键：应是第 2 段, 不是第 4 段
        checks["next_after_reenter_is_second"] = (
            abs(w.cursor.value() - probe_t0s[1]) < 1e-6
            and w._seg_nav.get(probe) == 1)
        w.close()
    finally:
        MainWindow.next_segment = orig_next
        MainWindow.on_item_clicked = orig_click
    report = {"checks": checks, "passed": all(checks.values()), "bad": sorted(bad),
              "rule": name if checks.get("has_multi_segment_rule") else None,
              "segment_t0s": t0s if checks.get("has_multi_segment_rule") else []}
    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, "p3_verify_%s.json" % tag)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, sort_keys=True)
    return report["passed"], report, out


def verify_transition(tag="transition", bad=frozenset()):
    """跳变条件验收：引擎单点判定 + 时长忽略 + 缺信号三态 + 新 UI 行往返。

    变异 noignore 让引擎错误套用时长过滤；nochange 让 UI 跳变行不落盘。断言必须变红。
    """
    checks = {}
    orig_points = caliper_rules._point_intervals
    if "noignore" in bad:
        def _bad_points(mask, axis, env):
            dummy = caliper_rules.Rule("bad", 1, "", [], 600000.0, 0, 0, {})
            return caliper_rules._segments(mask, axis, dummy, env)
        caliper_rules._point_intervals = _bad_points
    orig_norm = caliper_rules.normalize_conditions
    if "nochange" in bad:
        # 变异：转换时丢掉 change 条件
        def _bad_norm(item):
            if isinstance(item.get("conditions"), list):
                item = dict(item)
                item["conditions"] = [c for c in item["conditions"] if c.get("type") != "change"]
            return orig_norm(item)
        caliper_rules.normalize_conditions = _bad_norm

    tmp = None
    try:
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        axis = np.round(np.arange(0, 1.001, 0.1), 6)
        x = np.array([0, 0, 0, 3, 3, 3, 0, 0, 3, 0, 0], dtype=float)
        y = np.ones(len(axis), dtype=float)
        y[6] = 0.0
        m = caliper_core.Measurement(
            file="synthetic", config="synthetic", mdf_version="synthetic", main_group=0,
            axis=axis, signals={"X": x, "Y": y}, units={}, not_collected=[],
            requested=["X", "Y"], start_time_utc="", start_time_local="",
            quality={}, readback_ok={},
        )
        # 引擎：三个时长设最大，仍命中共 2 个单点
        change_c = {"type": "change", "signal": "X", "from": 3.0, "to": 0.0}
        r = caliper_rules.Rule("下降沿", 1, "", ["X"], 600000.0, 600000.0,
                           600000.0, {}, conditions=[change_c], join="all")
        got = caliper_rules.evaluate(m, [r]).results[0]
        checks["trans_hit"] = got.status == "hit"
        checks["trans_single_points"] = (
            [iv["t0"] for iv in got.intervals] == [0.6, 0.9]
            and all(iv["t0"] == iv["t1"] for iv in got.intervals))
        checks["trans_duration_ignored"] = len(got.intervals) == 2
        # 与定值条件 AND：只有 0.9 那个点 Y>0
        r2 = caliper_rules.Rule("下降沿且Y", 1, "", ["X", "Y"], 0, 0, 0, {},
                            conditions=[change_c,
                                        {"type": "value", "signal": "Y", "op": ">", "threshold": 0}],
                            join="all")
        checks["trans_with_value"] = (
            [iv["t0"] for iv in caliper_rules.evaluate(m, [r2]).results[0].intervals] == [0.9])
        # 缺信号 -> 未评估
        r3 = caliper_rules.Rule("缺信号", 1, "", ["MISSING"], 0, 0, 0, {},
                            conditions=[{"type": "change", "signal": "MISSING",
                                         "from": 0, "to": 1}], join="all")
        res3 = caliper_rules.evaluate(m, [r3]).results[0]
        checks["trans_missing"] = (res3.status == "not_evaluated"
                                   and res3.missing == ["MISSING"])

        # 新 UI 往返：跳变行 + 定值行混合，保存后写盘、读回
        tmp = os.path.join(OUT_DIR, "_verify_transition.json")
        base = {"name": "跳变验收", "version": 1, "rules": [
            {"name": "普通规则", "when": "X > 100", "min_duration_ms": 200,
             "hysteresis_ms": 50, "merge_gap_ms": 100, "mark": {"label": "old"}}]}
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(base, f, ensure_ascii=False)
        dlg = RuleSetDialog(tmp, signals=["X", "Y"])
        orig_exec = ConditionDialog.exec

        def fake_exec(self):
            self.rows = [{"type": "value", "signal": "X", "op": ">", "threshold": 100},
                         {"type": "change", "signal": "X", "from": 3, "to": 0}]
            self.cmb_join.setCurrentIndex(1)   # 全部
            self.table.setRowCount(0)
            for _ in self.rows:
                self._append_row()
            self._validate()
            return QtWidgets.QDialog.DialogCode.Accepted

        ConditionDialog.exec = fake_exec
        try:
            checks["trans_ok_enabled"] = True
            dlg.edit_condition(0)
            conds = dlg.conditions_at(0) or []
            checks["trans_editor_rows"] = (
                len(conds) == 2
                and any(c.get("type") == "change" and c.get("signal") == "X" for c in conds))
            checks["trans_no_duration_for_change"] = "—" in dlg.table.item(0, dlg.COL_MIN).text() \
                if not any(c.get("type") == "value" for c in conds) else True
            checks["trans_save"] = dlg.save() is True
            saved = json.load(open(tmp, encoding="utf-8"))["rules"][0]
            checks["trans_saved"] = any(
                c.get("type") == "change" for c in saved.get("conditions", []))
            # 旧格式规则加载仍能得到 change 条件
            checks["trans_old_format_load"] = any(
                c.get("type") == "change" for c in caliper_rules.load_ruleset(tmp)[0].conditions)
        finally:
            ConditionDialog.exec = orig_exec
            dlg.close()
    finally:
        caliper_rules._point_intervals = orig_points
        caliper_rules.normalize_conditions = orig_norm
        if tmp and os.path.exists(tmp):
            os.remove(tmp)
    report = {"checks": checks, "passed": all(checks.values()), "bad": sorted(bad)}
    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, "p3_verify_%s.json" % tag)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, sort_keys=True)
    return report["passed"], report, out


def verify_window(tag="window", bad=frozenset()):
    """窗口条件验收：引擎波动/变化 + 新 UI 四类型行、任一/全部合并、往返写盘。

    变异 nowin：归一化时丢掉窗口条件，写盘断言必须变红。
    """
    checks = {}
    orig_norm = caliper_rules.normalize_conditions
    if "nowin" in bad:
        def _bad_norm(item):
            if isinstance(item.get("conditions"), list):
                item = dict(item)
                item["conditions"] = [c for c in item["conditions"]
                                      if c.get("type") not in ("swing", "changes")]
            return orig_norm(item)
        caliper_rules.normalize_conditions = _bad_norm
    tmp = None
    try:
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        axis = np.round(np.arange(0, 4.001, 0.1), 6)
        x = np.full(len(axis), 5.0)
        x[10:16] = [0, 20, 0, 20, 0, 20]      # 窗口内 max-min=20
        x[20:30] = 20.0                       # 持续 1s 高位，供 min_duration=200ms 的定值命中
        z = 5.0 + np.where(np.arange(len(axis)) % 2 == 0, 0.1, -0.1)
        y = np.full(len(axis), 5.0)          # 供 UI 往返的 swing Y 条件用
        m = caliper_core.Measurement(
            file="synthetic", config="", mdf_version="", main_group=0,
            axis=axis, signals={"X": x, "Y": y, "Z": z}, units={}, not_collected=[],
            requested=["X", "Y", "Z"], start_time_utc="", start_time_local="",
            quality={}, readback_ok={},
        )
        swing = {"type": "swing", "signal": "X", "window_ms": 500, "threshold": 10}
        changes = {"type": "changes", "signal": "Z", "window_ms": 1000,
                   "threshold": 1, "min_step": 1.0}
        r1 = caliper_rules.Rule("波动", 1, "", ["X"], 0, 0, 0, {},
                            conditions=[swing], join="all")
        checks["swing_hit"] = caliper_rules.evaluate(m, [r1]).results[0].status == "hit"
        r2 = caliper_rules.Rule("大变化", 1, "", ["Z"], 0, 0, 0, {},
                            conditions=[changes], join="all")
        checks["changes_min_step"] = caliper_rules.evaluate(m, [r2]).results[0].status == "no_hit"
        # join=any：波动或定值任一命中；join=all：两者都要满足
        combo = [swing, {"type": "value", "signal": "X", "op": ">", "threshold": 100}]
        any_res = caliper_rules.evaluate(m, [caliper_rules.Rule(
            "any", 1, "", ["X"], 0, 0, 0, {}, conditions=combo, join="any")]).results[0]
        all_res = caliper_rules.evaluate(m, [caliper_rules.Rule(
            "all", 1, "", ["X"], 0, 0, 0, {}, conditions=combo, join="all")]).results[0]
        checks["join_any_vs_all"] = (any_res.status == "hit" and all_res.status == "no_hit")
        # 缺信号 -> 未评估
        r3 = caliper_rules.Rule("缺", 1, "", ["MISSING"], 0, 0, 0, {},
                            conditions=[{"type": "swing", "signal": "MISSING",
                                         "window_ms": 500, "threshold": 10}], join="all")
        g3 = caliper_rules.evaluate(m, [r3]).results[0]
        checks["window_missing"] = g3.status == "not_evaluated" and g3.missing == ["MISSING"]

        # 新 UI：四类型行编辑，任一/全部，往返写盘
        tmp = os.path.join(OUT_DIR, "_verify_window.json")
        base = {"name": "窗口验收", "version": 1, "rules": [
            {"name": "普通规则", "when": "X > 100", "min_duration_ms": 200,
             "hysteresis_ms": 50, "merge_gap_ms": 100, "mark": {"label": "old"}}]}
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(base, f, ensure_ascii=False)
        dlg = RuleSetDialog(tmp, signals=["X", "Y", "Z"])
        orig_exec = ConditionDialog.exec

        def fake_exec(self):
            self.rows = [
                {"type": "value", "signal": "X", "op": ">", "threshold": 10},
                {"type": "change", "signal": "X", "from": 1, "to": 2},
                {"type": "swing", "signal": "Y", "window_ms": 1000, "threshold": 5},
                {"type": "changes", "signal": "Z", "window_ms": 1000,
                 "threshold": 3, "min_step": 0.5}]
            self.cmb_join.setCurrentIndex(0)   # 任一
            self.table.setRowCount(0)
            for _ in self.rows:
                self._append_row()
            self._validate()
            self._sync_all = lambda: None   # 测试直接注入 rows，不让控件值回写覆盖
            return QtWidgets.QDialog.DialogCode.Accepted

        ConditionDialog.exec = fake_exec
        try:
            cd = ConditionDialog("", "探针", ["X", "Y", "Z"])
            checks["grid_four_types"] = len(ConditionDialog.TYPE_KEYS) == 4
            checks["grid_no_advanced"] = not hasattr(cd, "chk_adv") and not hasattr(cd, "raw")
            # 布局：四种判据控件都不能被列宽截断（曾因 ResizeToContents 算不准而显示不全）
            cd.rows = [
                {"type": "value", "signal": "X", "op": "==", "threshold": 0},
                {"type": "change", "signal": "X", "from": 1, "to": 2},
                {"type": "swing", "signal": "Y", "window_ms": 1000, "threshold": 5},
                {"type": "changes", "signal": "Z", "window_ms": 1000,
                 "threshold": 3, "min_step": 0.5}]
            cd.table.setRowCount(0)
            for _ in cd.rows:
                cd._append_row()
            cd.show()
            app.processEvents()
            w_type, w_crit = cd.table.columnWidth(0), cd.table.columnWidth(2)
            checks["grid_col_widths"] = (
                w_type >= max(cd.table.cellWidget(r, 0).width()
                              for r in range(cd.table.rowCount()))
                and all(cd.table.cellWidget(r, 2).layout().sizeHint().width() <= w_crit
                        for r in range(cd.table.rowCount())))
            # 真实填值路径：改判据输入框 -> _sync_row 要写回正确字段（嵌套布局后 findChildren 顺序）
            host = cd.table.cellWidget(3, 2)      # changes 行：窗口ms / 阈值 / 最小变化
            eds = host.findChildren(QtWidgets.QLineEdit)
            eds[0].setText("2500"); eds[1].setText("7"); eds[2].setText("0.9")
            cd._sync_row(3)
            r3 = cd.rows[3]
            checks["grid_field_sync"] = (r3.get("window_ms") == "2500"
                                         and r3.get("threshold") == "7"
                                         and r3.get("min_step") == "0.9")
            cd.close()
            dlg.edit_condition(0)
            conds = dlg.conditions_at(0) or []
            checks["grid_four_rows"] = len(conds) == 4 and {c.get("type") for c in conds} == \
                {"value", "change", "swing", "changes"}
            checks["grid_join_any"] = dlg.join_at(0) == "any"
            checks["grid_no_duration_nonvalue"] = "—" in dlg.table.item(0, dlg.COL_MIN).text() \
                if not any(c.get("type") == "value" for c in conds) else True
            checks["grid_save"] = dlg.save() is True
            saved = json.load(open(tmp, encoding="utf-8"))["rules"][0]
            checks["grid_saved"] = (len(saved.get("conditions", [])) == 4
                                    and saved.get("join") == "any")
            # 读回：load_ruleset 拿到 4 个条件、join=any
            rs = caliper_rules.load_ruleset(tmp)[0]
            checks["grid_reload"] = len(rs.conditions) == 4 and rs.join == "any"
        finally:
            ConditionDialog.exec = orig_exec
            dlg.close()
    finally:
        caliper_rules.normalize_conditions = orig_norm
        if tmp and os.path.exists(tmp):
            os.remove(tmp)
    report = {"checks": checks, "passed": all(checks.values()), "bad": sorted(bad)}
    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, "p3_verify_%s.json" % tag)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, sort_keys=True)
    return report["passed"], report, out


def verify_workflow(tag="workflow", bad=frozenset()):
    """验收三项新工作流：默认空画布 / 右键添加信号 / 多规则集一起评估。

    用真实小 MDF + 现成规则文件，秒级。变异 nodefault / noadd / single 各自打红对应族。
    """
    checks = {}
    orig_add = MainWindow.sel_add_signals
    orig_multi = MainWindow.choose_multi_rules
    orig_open = MainWindow.open_file
    orig_attach_menu = MainWindow._attach_menu
    orig_show_note = MultiRulesDialog._show_note
    orig_addaction = QtWidgets.QMenu.addAction
    if "nomenuaxis" in bad:
        # 变异：右栏菜单不加分轴项（只拦对象形式的 addAction(QAction)，
        # 示波器菜单 addAction(字符串) 不受影响），value_menu_has_per_axis 必须变红
        def _bad_addaction(self, *a, **k):
            if a and isinstance(a[0], QtGui.QAction) and a[0].text() == PER_AXIS_LABEL:
                return None
            return orig_addaction(self, *a, **k)
        QtWidgets.QMenu.addAction = _bad_addaction
    if "nomenuadd" in bad:
        # 变异：示波器右键菜单不加「添加信号…」，两条菜单断言必须变红
        def _bad_attach(self, vb, name=None):
            orig_attach_menu(self, vb, name)
            m = vb.menu
            if m:
                for a in m.actions():
                    if "添加信号" in a.text():
                        m.removeAction(a)
        MainWindow._attach_menu = _bad_attach
    if "nonote" in bad:
        MultiRulesDialog._show_note = lambda self, current, _previous=None: self.note.clear()
    if "nodefault" in bad:
        # 变异：恢复旧行为——打开无规则文件时自动勾选第一个信号
        def _bad_open(self, path, auto=True):
            r = orig_open(self, path, auto=auto)
            if self.signal_map and not self.rule_signals:
                self.checked = {sorted(self.signal_map)[0]}
            return r
        MainWindow.open_file = _bad_open
    if "noadd" in bad:
        MainWindow.sel_add_signals = lambda self: []       # 变异：添加信号无效
    if "single" in bad:
        # 变异：多规则选择永远只返回单条，合并评估能力失效
        def _single(self):
            paths = self.rules_paths
            if paths and len(paths) > 1:
                self.rules_paths = paths[:1]
                self.rules_path = paths[0]
            return self.rules_paths
        MainWindow.choose_multi_rules = _single
    orig_export = MainWindow.export_record
    orig_msg = QtWidgets.QMessageBox.information
    QtWidgets.QMessageBox.information = lambda *a, **k: None   # 验收中不弹模态框

    w = None
    try:
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        w = MainWindow()
        w._extra_rule_dir = _fixtures.FIXTURE_DIR   # 让夹具规则集出现在「选择规则集」列表
        dat = os.path.join(DATA_DIR, "Creep02.dat")
        w.open_file(dat, auto=False)
        app.processEvents()
        # 没规则时默认空画布
        # 规则入口：下拉框已删，按钮名改为「选择规则集」
        checks["rule_entry_ui"] = (not hasattr(w, "cmb_rules")
                                   and w.btn_multi_rules.text() == "选择规则集"
                                   and w.btn_multi_rules.parent() is not None)
        checks["default_empty_no_rules"] = (w.rules_path is None and not w.checked
                                            and not w.m)
        w.run_analysis()
        app.processEvents()
        checks["empty_canvas_no_curves"] = (w.m is not None and w.curve_count == 0
                                            and w.result is None)

        # 添加信号对话框：验证可选列表与批量勾选（直接注入选择，避免弹模态框）
        avail = sorted(n for n in w.signal_map if n not in w.checked)
        want = avail[:2]
        seen = {}
        orig_exec = AddSignalsDialog.exec

        def fake_exec(self):
            seen["available"] = [self.list.topLevelItem(i).text(0)
                                 for i in range(self.list.topLevelItemCount())]
            for i in range(self.list.topLevelItemCount()):
                it = self.list.topLevelItem(i)
                if it.text(0) in want:
                    it.setCheckState(0, QtCore.Qt.CheckState.Checked)
            return QtWidgets.QDialog.DialogCode.Accepted

        AddSignalsDialog.exec = fake_exec
        added = w.sel_add_signals()
        AddSignalsDialog.exec = orig_exec
        checks["add_dialog_lists_unchecked"] = bool(seen.get("available")) and all(
            n in seen["available"] for n in want)
        checks["add_signals_batch"] = set(added) == set(want) and set(want) <= w.checked
        app.processEvents()
        checks["added_curves_visible"] = w.curve_count >= len(want)

        # 右键菜单里确实有「添加信号…」：把 QMenu.exec 换掉捕获动作文本
        # （PySide6 类方法 patch 对实例 C++ 绑定无效，改为 patch 实例 exec）
        acts = []
        menu_acts = []
        orig_menu_cls = QtWidgets.QMenu

        class CapturingMenu(orig_menu_cls):
            def exec(self, *a, **k):
                for action in self.actions():
                    acts.append(action.text())
                    menu_acts.append(action)
                return None

        QtWidgets.QMenu = CapturingMenu
        try:
            w.on_value_context_menu(QtCore.QPoint(0, 0))
        finally:
            QtWidgets.QMenu = orig_menu_cls
        checks["context_menu_has_add"] = any("添加信号" in t for t in acts)
        # 右栏「选中信号」右键也有分轴项，且复用示波器同一个 act_per_axis（勾选态天然同步）
        checks["value_menu_has_per_axis"] = any(
            a is w.act_per_axis and a.text() == PER_AXIS_LABEL for a in menu_acts)

        # 示波器右键菜单（共用轴 + 分轴子图）也有「添加信号…」，复用同一入口
        vb_menu = w.plot.getPlotItem().getViewBox().menu
        shared_acts = [a.text() for a in vb_menu.actions()] if vb_menu else []
        checks["plot_menu_has_add"] = any("添加信号" in t for t in shared_acts)
        w.set_per_axis(True)
        app.processEvents()
        sub_menu = w.axis_plots[0][1].getViewBox().menu if w.axis_plots else None
        sub_acts = [a.text() for a in sub_menu.actions()] if sub_menu else []
        checks["subplot_menu_has_add"] = any("添加信号" in t for t in sub_acts)
        w.set_per_axis(False)
        app.processEvents()

        # 多规则集：选两条规则，合并评估的结果条数 = 各自之和
        g_path = _fixtures.FIXTURE_PATHS["gear"]
        c_path = _fixtures.FIXTURE_PATHS["creep"]
        if os.path.exists(g_path) and os.path.exists(c_path):
            w.rule_paths = {}   # refresh_rules 填充显示名->路径
            w.refresh_rules()
            paths = [g_path, c_path]
            orig_exec2 = MultiRulesDialog.exec

            dlg_seen = {}

            def fake_multi(self):
                dlg_seen["inst"] = self
                for i in range(self.list.count()):
                    it = self.list.item(i)
                    if it.data(QtCore.Qt.ItemDataRole.UserRole) in paths:
                        it.setCheckState(QtCore.Qt.CheckState.Checked)
                    else:
                        it.setCheckState(QtCore.Qt.CheckState.Unchecked)
                # 选中 gear，触发备注显示
                gear_item = next(it for it in (self.list.item(i) for i in range(self.list.count()))
                                 if it.data(QtCore.Qt.ItemDataRole.UserRole) == g_path)
                self.list.setCurrentItem(gear_item)
                self._show_note(gear_item)
                return QtWidgets.QDialog.DialogCode.Accepted

            MultiRulesDialog.exec = fake_multi
            got = w.choose_multi_rules()
            MultiRulesDialog.exec = orig_exec2
            gear_note = (caliper_rules.load_ruleset_doc(g_path).get("note") or "").strip()
            checks["dialog_note_shows_selected"] = (
                gear_note in dlg_seen["inst"].note.toPlainText()) if gear_note else False
            # 子规则显示：切到 creep（3 条规则、when 含 >），详情里要有规则名和转义后的条件
            dlg = dlg_seen["inst"]
            creep_item = next(it for it in (dlg.list.item(i) for i in range(dlg.list.count()))
                              if it.data(QtCore.Qt.ItemDataRole.UserRole) == c_path)
            dlg._show_note(creep_item)
            shown = dlg.note.toPlainText()
            creep_rules = caliper_rules.load_ruleset_doc(c_path).get("rules") or []
            checks["dialog_subrules_listed"] = bool(creep_rules) and all(
                r.get("name", "") in shown for r in creep_rules) and "子规则" in shown
            checks["dialog_subrule_when_escaped"] = (
                "DrvReq_tqWhlCrpReq > 1000" in shown)   # > 没被当 HTML 吞掉
            checks["multi_rules_selected"] = set(got or []) == set(paths)
            checks["multi_rules_scope_label"] = "2 个规则集" in w.rule_scope_label.text()
            app.processEvents()
            rs1 = caliper_rules.load_ruleset(g_path)
            rs2 = caliper_rules.load_ruleset(c_path)
            checks["multi_rules_merged_count"] = (
                w.result is not None
                and len(w.result.results) == len(rs1) + len(rs2))
            checks["multi_rules_marks_merged"] = (
                set(w.rule_marks) == {r.name for r in rs1 + rs2})
            # 多规则下导出记录应拒绝，避免只写主规则 md5 的误导记录
            w.m = w.m  # noqa
            rec = w.export_record(os.path.join(OUT_DIR, "_should_not_exist.json"))
            checks["multi_export_refused"] = rec is None
        else:
            checks["multi_rules_selected"] = False
            checks["multi_rules_scope_label"] = False
            checks["multi_rules_merged_count"] = False
            checks["multi_rules_marks_merged"] = False
            checks["multi_export_refused"] = False
    finally:
        MainWindow.sel_add_signals = orig_add
        MainWindow.choose_multi_rules = orig_multi
        MainWindow.open_file = orig_open
        MainWindow.export_record = orig_export
        MainWindow._attach_menu = orig_attach_menu
        MultiRulesDialog._show_note = orig_show_note
        QtWidgets.QMenu.addAction = orig_addaction
        QtWidgets.QMessageBox.information = orig_msg
        if w is not None:
            w.close()
    report = {"checks": checks, "passed": all(checks.values()), "bad": sorted(bad)}
    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, "p3_verify_%s.json" % tag)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, sort_keys=True)
    return report["passed"], report, out


def verify_ui(tag="ui", bad_axis=False, bad_y_share=False, bad_menu=False,
              bad=frozenset(), native=False, dat=None):
    """验收界面要求：默认不加载规则、Y 量程可手动/自适应、表头自适应、每信号一个 Y 轴、
    X 轴落到最下方、Y 标尺统宽左对齐、竖直标尺可拖动读数。

    全程 offscreen + 直接读 Qt 对象状态，不截屏、不录屏、不抓大屏。
    bad 里的名字（xaxis / yalign / cursor）用于注入对应缺陷，证明断言能变红。
    """
    bad = set(bad)
    manual = (-4321.0, 1234.0)     # 故意选一个自动量程绝不会算出来的区间
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    w = MainWindow()
    w.resize(1200, 700)
    if native:
        # 用系统真实字体（中文有字宽差异），但窗口绝不投到屏幕，也就不存在抓屏
        w.setAttribute(QtCore.Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    w.show()                       # offscreen 平台：窗口只在内存里，不投到屏幕
    app.processEvents()

    dat = dat or os.path.join(DATA_DIR, "Creep02.dat")
    w.open_file(dat, auto=False)
    w.run_analysis()
    app.processEvents()

    vb = w.plot.getPlotItem().getViewBox()
    checks = {}
    checks["default_no_rules"] = (w.rules_path is None and not w.rules_paths)
    checks["no_eval_without_rules"] = (w.result is None and not w.regions
                                       and w.marker_count == 0)
    hint = w.rule_tree.topLevelItem(0).text(0) if w.rule_tree.topLevelItemCount() else ""
    checks["rule_panel_says_not_loaded"] = (w.rule_tree.topLevelItemCount() == 1
                                            and "未加载规则集" in hint
                                            and "0 命中" not in hint)
    checks["default_empty_canvas"] = w.curve_count == 0   # 没规则时默认空画布

    checks["y_manual_applied"] = (w.set_y_range(*manual)
                                  and _near(vb.viewRange()[1], manual))

    extra = [n for n in sorted(w.signal_map) if n not in w.checked][:1]
    curves_before = w.curve_count
    w.set_checked(extra, True)     # 触发 plot.clear + 全量重绘
    app.processEvents()
    curves_after = w.curve_count
    checks["y_manual_survives_rerender"] = (curves_after == curves_before + 1
                                            and w.auto_y is False
                                            and _near(vb.viewRange()[1], manual))

    w.enable_auto_y()
    w.render()
    app.processEvents()
    auto_rng = vb.viewRange()[1]
    checks["y_auto_reenabled"] = (w.auto_y is True and w.y_range is None
                                  and bool(vb.autoRangeEnabled()[1])
                                  and not _near(auto_rng, manual))

    checks["y_rejects_inverted"] = (w.set_y_range(10.0, 10.0) is False
                                    and w.auto_y is True)   # 非法量程不得改动视图

    app.processEvents()
    left = _header_snapshot(w.signal_tree)
    right = _header_snapshot(w.rule_tree)
    # 左栏已删：signal_tree 隐藏，表头宽度不可测，只保留 resize 模式检查（状态载体仍需正确配置）
    checks["left_header_adaptive"] = (
        left["mode0"] == "Stretch" and left["mode1"] == "ResizeToContents")
    checks["right_header_adaptive"] = (
        right["mode0"] == "Stretch" and 90 <= right["w1"] <= 260
        and right["w0"] >= right["title0_px"]
        and abs(right["w0"] + right["w1"] - right["viewport"]) <= 4)

    # 载入规则后明细列内容变长（"4 个区间""缺：..."），表头仍不得挤掉标题
    w.load_rules(_fixtures.FIXTURE_PATHS["gear"])
    w.run_analysis()
    app.processEvents()
    left2 = _header_snapshot(w.signal_tree)
    right2 = _header_snapshot(w.rule_tree)
    # 左栏已删：signal_tree 是隐藏状态载体，表头宽度不再可见，只测可见的 rule_tree
    checks["headers_adaptive_with_rules"] = (
        right2["w0"] >= right2["title0_px"]
        and 90 <= right2["w1"] <= 260
        and abs(right2["w0"] + right2["w1"] - right2["viewport"]) <= 4)

    # ---- 分轴模式：每个信号一个 Y 轴 ----
    need = 3
    more = [n for n in sorted(w.signal_map) if n not in w.checked][:max(0, need - w.curve_count)]
    if more:
        w.set_checked(more, True)
    app.processEvents()
    total = w.result.summary["total_intervals"]
    n_curves = w.curve_count
    shared_regions = len(w.regions)
    xc = 0.5 * (float(w.m.axis[0]) + float(w.m.axis[-1]))

    checks["shared_menu_has_per_axis"] = (w.act_per_axis.text() == PER_AXIS_LABEL
                                          and w.act_per_axis.isCheckable()
                                          and not w.act_per_axis.isChecked())
    # 走真实的右键菜单动作切换，而不是直接调 set_per_axis：证明菜单确实连着功能
    if bad_menu:                       # 变异：把菜单动作的连接拆掉（典型「加了菜单没接功能」）
        w.act_per_axis.toggled.disconnect()
        w.act_per_axis.toggled.connect(lambda *_: None)
    w.act_per_axis.setChecked(True)
    app.processEvents()
    checks["per_axis_menu_action"] = (w.per_axis and w.plot_host.currentIndex() == 1)
    if bad_axis and w.axis_plots:      # 变异：少建一个子图（等价于把某条信号画进了别人的轴）
        w.axis_plots.pop()
        if w.cursors:
            w.cursors.pop()            # 标尺跟着少一条，避免变异把验收本身搞崩
    axes = list(w.axis_plots)
    per_axis_regions = len(w.regions)
    per_axis_markers = w.marker_count

    sub_menu = [a.text() for a in axes[0][1].getViewBox().menu.actions()] if axes else []
    checks["per_axis_subplot_menu"] = ("修改 Y 轴量程…" in sub_menu
                                       and "Y 轴量程自适应" in sub_menu
                                       and SHARED_AXIS_LABEL in sub_menu)
    checks["per_axis_switched"] = (w.per_axis and w.plot_host.currentIndex() == 1)
    checks["per_axis_visible_widget"] = (w.plot_host.currentWidget() is w.stack)
    # 标记点按命中规则分组（逐规则着色/显隐），采样点开关会给该子图额外加一个 scatter
    n_hit_rules = sum(1 for r in w.result.results if r.status == "hit" and r.intervals)
    checks["per_axis_items_per_plot"] = bool(axes) and all(
        sum(isinstance(it, pg.PlotDataItem) for it in pi.listDataItems()) == 1
        and sum(isinstance(it, pg.ScatterPlotItem) for it in pi.listDataItems())
        == ((n_hit_rules if total else 0) + (1 if n in w.sample_points_on else 0))
        for n, pi in axes)      # 每个子图只画自己那一条曲线（外加各规则标记点/采样点）
    checks["per_axis_axes_match_curves"] = (len(axes) == n_curves and n_curves >= 2
                                            and all(isinstance(pi, pg.PlotItem) for _, pi in axes))
    checks["per_axis_distinct_viewboxes"] = (len({id(pi.getViewBox()) for _, pi in axes}) == len(axes)
                                             and len(axes) >= 2)
    if bad_y_share and axes:           # 变异：把所有轴都改成全局包络（即共用一把尺）
        glo = min(pi.getViewBox().viewRange()[1][0] for _, pi in axes)
        ghi = max(pi.getViewBox().viewRange()[1][1] for _, pi in axes)
        for _, pi in axes:
            pi.getViewBox().setYRange(glo, ghi, padding=0)
    scaled = [(n, _own_scale_ok(w.m, n, pi.getViewBox())) for n, pi in axes]
    scaled = [(n, ok) for n, ok in scaled if ok is not None]
    checks["per_axis_own_scale"] = len(scaled) >= 2 and all(ok for _, ok in scaled)

    if axes:
        axes[0][1].setXRange(xc - 1.0, xc + 1.0, padding=0)
    app.processEvents()
    widths = [abs(float(pi.getViewBox().viewRange()[0][1] - pi.getViewBox().viewRange()[0][0]))
              for _, pi in axes]
    checks["per_axis_x_linked"] = bool(widths) and all(abs(wd - 2.0) <= 0.2 for wd in widths)

    checks["per_axis_regions_scaled"] = (w.region_intervals == total
                                        and per_axis_regions == total * len(axes)
                                        and per_axis_markers == total * len(axes))
    # 每个子图的标题必须是它自己那条信号（不能所有子图套同一个名字），且两两不同
    titles = {n: (pi.titleLabel.text if getattr(pi, "titleLabel", None) is not None else "")
              for n, pi in axes}
    checks["per_axis_label_per_signal"] = (
        bool(axes) and all(w._axis_label(n) in t for n, t in titles.items())
        and len(set(titles.values())) == len(titles))

    # ---- X 轴只在最下方标刻度 / Y 标尺统宽左对齐 / 竖直标尺可拖动读数 ----
    if "xaxis" in bad:      # 变异：时间轴退回最上面那个子图（改之前的样子）
        axes[0][1].setLabel("bottom", "时间 / s")
        axes[0][1].getAxis("bottom").setStyle(showValues=True)
        axes[-1][1].setLabel("bottom", "")
        axes[-1][1].getAxis("bottom").setStyle(showValues=False)
    bottoms = [pi.getAxis("bottom") for _, pi in axes]
    checks["per_axis_x_axis_on_bottom"] = (
        bool(bottoms)
        and bool(bottoms[-1].style["showValues"]) and bottoms[-1].labelText == "时间 / s"
        and all((not b.style["showValues"]) and b.labelText == "" for b in bottoms[:-1]))

    # 造一支量纲极大的轴：不统宽时它的刻度文本会把那个子图撑宽、左边界错开
    wide_name = axes[0][0] if axes else ""
    w.set_y_range(-2e6, 2e6, name=wide_name)
    app.processEvents()
    if "yalign" in bad:     # 变异：退回「各按自己刻度自动宽度」
        for _, pi in axes:
            pi.getAxis("left").setWidth(None)
        app.processEvents()
    ywidths = [float(pi.getAxis("left").fixedWidth or 0.0) for _, pi in axes]
    vbx = [round(pi.getViewBox().scenePos().x(), 1) for _, pi in axes]
    checks["per_axis_y_axes_aligned"] = (bool(ywidths)
                                        and min(ywidths) >= Y_AXIS_MIN_WIDTH
                                        and len(set(ywidths)) == 1
                                        and len(set(vbx)) == 1)
    w.enable_auto_y(wide_name)   # 复原，避免影响后面的逐轴量程断言
    app.processEvents()

    arrs = {n: np.asarray(w.m.signals[n], dtype=float) for n, _ in axes}
    sample = next((i for i in range(len(w.m.axis))
                   if all(math.isfinite(a[i]) for a in arrs.values())), 0) \
        if arrs else 0
    t_cur = float(w.m.axis[sample])
    w.cursor.setValue(t_cur)     # 等价于拖标尺：走 sigPositionChanged 全链路
    app.processEvents()
    if "cursor" in bad:          # 变异：标尺各走各的、读数不刷新
        for _, line1, _line2 in w.cursors:
            w._syncing_cursor = True
            line1.setValue(0.0)
            w._syncing_cursor = False
        w.cursor_label.setText("")
        for i in range(w.value_tree.topLevelItemCount()):
            w.value_tree.topLevelItem(i).setText(1, "—")   # 读数消失，证明断言不是恒真
        app.processEvents()
    checks["cursor_movable"] = (w.cursor.movable is True
                                and bool(w.cursors)
                                and all(l1.movable for _, l1, _l2 in w.cursors)
                                and w.cursor2.movable is True)
    checks["cursor_syncs_all_axes"] = (
        abs(w.cursor.value() - t_cur) < 1e-9
        and all(abs(l1.value() - t_cur) < 1e-9 for _, l1, _l2 in w.cursors))
    # 信号值已挪到右栏「信号值」Tab：每行的值列 == 该时刻实测值（含枚举标签）
    def value_row(n):
        for i in range(w.value_tree.topLevelItemCount()):
            it = w.value_tree.topLevelItem(i)
            if it.text(0) == n:
                return it
        return None

    checks["cursor_readout_values"] = bool(arrs) and all(
        value_row(n) is not None
        and value_row(n).text(1) == _fmt_value(arrs[n][sample]) + _enum_suffix(n, arrs[n][sample])
        for n in arrs)
    # 底部图例已整体删除：读数只在右栏「选中信号」Tab，示波器下方不再有任何图示行
    checks["legend_removed"] = getattr(w, "curve_label", None) is None
    checks["cursor_value_at_line"] = bool(w.cursors) and all(
        line1.label is not None
        and line1.label.toPlainText().split(" ")[0] == _fmt_value(arrs[n][sample])
        for n, line1, _l2 in w.cursors)
    checks["cursor_local_time"] = w.local_time_text.startswith("本地时间：2026-")

    name0 = axes[0][0] if axes else ""
    ok_manual = w.set_y_range(*manual, name=name0) if axes else False
    app.processEvents()
    r0 = axes[0][1].getViewBox().viewRange()[1] if axes else [0.0, 0.0]
    others = [pi.getViewBox().viewRange()[1] for _, pi in axes[1:]]
    checks["per_axis_manual_isolated"] = (ok_manual and name0 in w.axis_ranges
                                         and _near(r0, manual)
                                         and all(not _near(r, manual) for r in others))

    more2 = [n for n in sorted(w.signal_map) if n not in w.checked][:1]
    if more2:
        w.set_checked(more2, True)
    app.processEvents()
    curves_now = w.curve_count
    axes2 = dict(w.axis_plots)
    checks["per_axis_manual_survives"] = (curves_now == n_curves + len(more2)
                                          and name0 in axes2
                                          and _near(axes2[name0].getViewBox().viewRange()[1], manual))
    checks["cursor_survives_rerender"] = (
        w.cursor_x is not None and abs(w.cursor_x - t_cur) < 1e-9
        and bool(w.cursors) and all(abs(l1.value() - t_cur) < 1e-9 for _, l1, _l2 in w.cursors))

    w.set_per_axis(False)
    app.processEvents()
    checks["shared_axis_restored"] = (not w.per_axis and w.plot_host.currentIndex() == 0
                                      and w.curve_count == curves_now
                                      and len(w.regions) == total
                                      and w.marker_count == total)
    checks["shared_regions_unchanged"] = shared_regions == total

    report = {
        "dat": os.path.basename(dat),
        "ruleset_default": NO_RULES_LABEL,   # 已无下拉框；默认不加载规则
        "manual_range": list(manual), "auto_range_after": [round(v, 3) for v in auto_rng],
        "curves": [curves_before, curves_after],
        "header_left": left, "header_right": right,
        "header_left_with_rules": left2, "header_right_with_rules": right2,
        "per_axis": {
            "axes": [n for n, _ in axes], "curve_count": n_curves, "intervals": total,
            "region_items": per_axis_regions, "marker_items": per_axis_markers,
            "axis_y_ranges": {n: [round(float(v), 3) for v in pi.getViewBox().viewRange()[1]]
                              for n, pi in axes},
            "scaled_checked": [n for n, _ in scaled],
            "x_axis_on": [n for n, pi in axes if pi.getAxis("bottom").labelText],
            "y_axis_widths": ywidths or [],
            "y_axis_left_px": vbx or [],
            "cursor": {
                "t": round(t_cur, 6), "sample_index": sample,
                "local_time": w.local_time_text,
                "readout": w.cursor_label.text(),
                "line_values": {n: _fmt_value(arrs[n][sample]) for n in arrs},
            },
        },
        "bad": sorted(bad),
        "checks": checks, "passed": all(checks.values()),
    }
    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, "p3_verify_%s.json" % tag)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, sort_keys=True)
    w.close()
    return all(checks.values()), report, out




def run_cli_verify():
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", action="store_true", help="offscreen 程序化验收，不显示界面")
    ap.add_argument("--verify-add-signals", action="store_true",
                    help="验收任意文件打开与信号勾选增删（可用 --dat 换数据集，需先有同名 P2 清单）")
    ap.add_argument("--bad-add-signals", default="",
                    help="变异测试：nodesc（描述映射读不到）")
    ap.add_argument("--verify-dbc-reselect", action="store_true",
                    help="验收重选 DBC 集合后下拉框刷新、写回映射不含旧 DBC 名")
    ap.add_argument("--bad-dbc-reselect", default="",
                    help="变异测试：stale（重选不刷新下拉框）")
    ap.add_argument("--verify-mf4", action="store_true",
                    help="验收 MDF 4.x (.MF4) 适配：真实样本 scan/load + GUI 打开出曲线")
    ap.add_argument("--bad-mf4", default="",
                    help="变异测试：nover（MDF 版本读错）")
    ap.add_argument("--verify-poem", action="store_true",
                    help="验收顶栏诗词标语：来自诗词表、长度不超限、每次启动换一句")
    ap.add_argument("--bad-poem", default="",
                    help="变异测试：fixed（标语写死不轮换）")
    ap.add_argument("--verify-dbcdir", action="store_true",
                    help="验收 DBC 统一目录：配置在 DBC/、只存文件名、复制去重、读取拼路径")
    ap.add_argument("--bad-dbcdir", default="",
                    help="变异测试：noresolve（读取不拼 DBC 目录）")
    ap.add_argument("--verify-ui-native", action="store_true",
                    help="同 --verify-ui，但用系统真实字体渲染（窗口不上屏），"
                         "核对真机字体下标题不截断、列宽仍自适应")
    ap.add_argument("--verify-next-segment", action="store_true",
                    help="验收子规则右键「下一段」：逐段游标状态机")
    ap.add_argument("--bad-next-segment", default="",
                    help="变异测试：alwaysfirst / alwaysjump / nowrap / noreset（重进不重置游标）")
    ap.add_argument("--verify-transition", action="store_true",
                    help="验收跳变条件：单点判定 / 时长忽略 / 编辑器往返")
    ap.add_argument("--bad-transition", default="",
                    help="变异测试：noignore（跳变错误套用时长过滤）")
    ap.add_argument("--verify-window", action="store_true",
                    help="验收窗口条件：波动/变化次数编辑器往返与写盘")
    ap.add_argument("--bad-window", default="",
                    help="变异测试：nowin（窗口条件丢失）")
    ap.add_argument("--verify-workflow", action="store_true",
                    help="验收默认空画布 / 右键添加信号 / 多规则集合并评估")
    ap.add_argument("--bad-workflow", default="",
                    help="变异测试：nodefault / noadd / single / nonote / nomenuadd（逗号分隔）")
    ap.add_argument("--verify-ui", action="store_true",
                    help="验收默认不加载规则、Y 量程可调、表头自适应、每信号一个 Y 轴、"
                         "X 轴落底、Y 标尺左对齐、可拖动标尺读数（可用 --dat 换数据集）")
    ap.add_argument("--dat")
    ap.add_argument("--dbc", nargs="*", default=None,
                    help="BLF 解码用 DBC 集合（--verify-blf 必需）")
    ap.add_argument("--rules", default=_fixtures.FIXTURE_PATHS["gear"],
                    help="规则集路径（缺省用验收夹具 gear；BLF 验收请显式传 BLF 规则集）")
    ap.add_argument("--tag", default="case")
    ap.add_argument("--bad-render", action="store_true",
                    help="变异测试：故意少画一个区间带，验收应失败")
    ap.add_argument("--bad-axis", action="store_true",
                    help="变异测试：分轴模式少建一个子图，验收应失败")
    ap.add_argument("--bad-y-share", action="store_true",
                    help="变异测试：所有分轴都套全局包络（共用一把尺），验收应失败")
    ap.add_argument("--bad-menu", action="store_true",
                    help="变异测试：右键菜单动作不接功能，验收应失败")
    ap.add_argument("--bad-ui", default="",
                    help="变异测试：逗号分隔，可选 xaxis（时间轴回上面）/ yalign（Y 不统宽）"
                         " / cursor（标尺各走各的）")
    ap.add_argument("--bad-nev", action="store_true",
                    help="变异测试：把未评估条目写成「0 命中」，not_evaluated_distinct 应变红")
    ap.add_argument("--verify-rule-edit", action="store_true",
                    help="验收界面里改规则并自动写回 JSON（载入/改值/增删/拒存/联动）")
    ap.add_argument("--bad-rule-edit", default="",
                    help="变异测试：mangle（改写复杂表达式）/ noapply（写完不重算），逗号分隔")
    ap.add_argument("--verify-export", action="store_true",
                    help="验收 GUI 导出可复现记录（与 CLI 同链一致、三态不折叠、守卫拒存）")
    ap.add_argument("--bad-export", default="",
                    help="变异测试：fold（未评估折叠成未命中）/ nowrite（只算不写盘），逗号分隔")
    ap.add_argument("--verify-new-ruleset", action="store_true",
                    help="验收新建空白规则集（真实信号骨架/不覆盖/限 rules//自动重算）")
    ap.add_argument("--bad-new-ruleset", default="",
                    help="变异测试：overwrite（覆盖已有文件）/ bump（新文件也自增版本），逗号分隔")
    ap.add_argument("--verify-enum", action="store_true",
                    help="验收枚举文字标签（只显示层，数值与快照不受影响）")
    ap.add_argument("--bad-enum", default="",
                    help="变异测试：nolabel（映射失效）/ wrongmap（4 错标成 N档），逗号分隔")
    ap.add_argument("--verify-rule-marks", action="store_true",
                    help="验收逐规则彩色标注与勾选显隐（色板独立/色差达标/不重建树）")
    ap.add_argument("--bad-rule-marks", default="",
                    help="变异测试：nocolor（全同色）/ curvepalette（退回曲线色板）/ notoggle（勾选不生效）")
    ap.add_argument("--verify-panels", action="store_true",
                    help="验收侧栏收起/展开与右栏多 Tab（信号值 Tab 行/单位/读数更新）")
    ap.add_argument("--bad-panels", default="",
                    help="变异测试：nocollapse（按钮不收起）/ novalues（信号值不刷新）"
                         " / noinset（收起不留内缩带）/ noselection（选择机制没装），逗号分隔")
    ap.add_argument("--verify-selops", action="store_true",
                    help="验收选中操作批：移除/solo/隐藏/复制/统计/属性/CSV/曲线样式/Y自适应/采样点")
    ap.add_argument("--bad-selops", default="",
                    help="变异测试：noremove/nosolo/nohide/nocopy/badstats/nostyle/nosamples/noexport")
    ap.add_argument("--verify-blf", action="store_true",
                    help="验收 BLF GUI 链路（需 --dat .blf 与 --dbc；先有同名 P2 清单）")
    ap.add_argument("--verify-startup", action="store_true",
                    help="验收启动 preflight 递归判空与自愈（临时目录，不碰真实 rules/）")
    ap.add_argument("--bad-startup", default="", help="变异测试：noseed（自愈不写文件）")
    ap.add_argument("--verify-blf-switch", action="store_true",
                    help="验收连续加载两个 BLF 后示波器数据切换")
    ap.add_argument("--bad-blf-switch", default="",
                    help="变异测试：nostrict（第二个 BLF 不重绘）")
    ap.add_argument("--verify-blf-incremental", action="store_true",
                    help="验收勾选增量解码（合成 BLF，秒级；不需真实文件）")
    ap.add_argument("--bad-blf-incremental", default="",
                    help="变异测试：noincr（强制全量重算）")
    ap.add_argument("--verify-cursor-init", action="store_true",
                    help="验收未拖标尺时它落在数据视野内（合成 epoch 数据，秒级）")
    ap.add_argument("--bad-cursor-init", default="",
                    help="变异测试：noinit（标尺钉在 0，复现 BLF 看不见标尺）")
    ap.add_argument("--verify-mouse", action="store_true",
                    help="验收鼠标语义：左键框选放大 / 中键左右拖 / 滚轮禁用")
    ap.add_argument("--verify-cursor-dclick", action="store_true",
                    help="验收左键双击定位 cursor1 / 右键双击定位 cursor2（含分轴）")
    ap.add_argument("--verify-cursor2", action="store_true",
                    help="验收双标尺与值列 C1/C2/Δ")
    ap.add_argument("--bad-cursor2", default="",
                    help="变异测试：noc2（右键不定位）/ baddhead（线头显示 Δ）")
    ap.add_argument("--bad-cursor-dclick", default="",
                    help="变异测试：nodouble（双击不定位）/ menuconflict（右键菜单抢双击）")
    ap.add_argument("--bad-mouse", default="",
                    help="变异测试：nowheel / nopan / norect（逗号分隔）")
    ap.add_argument("--verify-reltime", action="store_true",
                    help="验收 X 轴相对时间显示（合成 epoch 数据，不读真实 BLF）")
    ap.add_argument("--bad-reltime", default="",
                    help="变异测试：nooffset（不设零点）/ noscalefix（恢复 SI 前缀，复现 x1e+09）")
    ap.add_argument("--bad-blf-gui", default="",
                    help="变异测试：noscan（扫描不建树）/ noeval（加载后不评估）/ nocache（丢会话重扫）"
                         "/ nobounds（标尺重新参与量程）")
    ap.add_argument("--simulate-network", action="store_true",
                    help="变异测试：模拟一次连接尝试，no_network_call 应变红")
    args = ap.parse_args()

    bad_edit = {t.strip() for t in (args.bad_rule_edit or "").split(",") if t.strip()}
    unknown_edit = bad_edit - {"mangle", "noapply"}
    if unknown_edit:
        ap.error("--bad-rule-edit 只能取 mangle / noapply，收到：%s"
                 % ",".join(sorted(unknown_edit)))

    if args.verify_rule_edit or bad_edit:
        tag = "rule_edit" if not bad_edit else "rule_edit_bad_" + "_".join(sorted(bad_edit))
        ok, report, out = verify_rule_edit(tag, bad_edit)
        for k, v in report["checks"].items():
            print("  %-30s %s" % (k, "OK" if v else "FAIL"))
        print("RULE-EDIT VERIFY %s | 规则 %d 条基线 | 产出 %s"
              % ("PASS" if ok else "FAIL", report["rule_count"]["base"], out))
        if bad_edit:
            if ok:
                print("REVERT-TO-FAIL FAILED：规则编辑缺陷没抓到", file=sys.stderr)
                sys.exit(1)
            print("REVERT-TO-FAIL OK：规则编辑缺陷被断言抓到（%s）" % tag)
            return
        if not ok:
            sys.exit(1)
        return

    bad_st = {t.strip() for t in (args.bad_startup or "").split(",") if t.strip()}
    unknown_st = bad_st - {"noseed"}
    if unknown_st:
        ap.error("--bad-startup 只能取 noseed，收到：%s" % ",".join(sorted(unknown_st)))
    if args.verify_startup or bad_st:
        tag_st = "startup" if not bad_st else "startup_bad_" + "_".join(sorted(bad_st))
        ok, report, out = verify_startup(tag_st, bad_st)
        for k, v in report["checks"].items():
            print("  %-28s %s" % (k, "OK" if v else "FAIL"))
        print("STARTUP VERIFY %s | 产出 %s" % ("PASS" if ok else "FAIL", out))
        if bad_st:
            if ok:
                print("REVERT-TO-FAIL FAILED：自愈缺陷没抓到", file=sys.stderr)
                sys.exit(1)
            print("REVERT-TO-FAIL OK：自愈缺陷被断言抓到（%s）" % tag_st)
            return
        if not ok:
            sys.exit(1)
        return

    bad_sw = {t.strip() for t in (args.bad_blf_switch or "").split(",") if t.strip()}
    unknown_sw = bad_sw - {"nostrict"}
    if unknown_sw:
        ap.error("--bad-blf-switch 只能取 nostrict，收到：%s" % ",".join(sorted(unknown_sw)))
    if args.verify_blf_switch or bad_sw:
        tag = "blf_switch" if not bad_sw else "blf_switch_bad_" + "_".join(sorted(bad_sw))
        ok, report, out = verify_blf_switch(tag, bad_sw)
        for k, v in report["checks"].items():
            print("  %-28s %s" % (k, "OK" if v else "FAIL"))
        print("BLF-SWITCH VERIFY %s | 产出 %s" % ("PASS" if ok else "FAIL", out))
        if bad_sw:
            if ok:
                print("REVERT-TO-FAIL FAILED：BLF 切换缺陷没抓到", file=sys.stderr)
                sys.exit(1)
            print("REVERT-TO-FAIL OK：BLF 切换缺陷被断言抓到（%s）" % tag)
            return
        if not ok:
            sys.exit(1)
        return

    bad_incr = {t.strip() for t in (args.bad_blf_incremental or "").split(",") if t.strip()}
    unknown_incr = bad_incr - {"noincr", "nodesc"}
    if unknown_incr:
        ap.error("--bad-blf-incremental 只能取 noincr / nodesc，收到：%s" % ",".join(sorted(unknown_incr)))
    if args.verify_blf_incremental or bad_incr:
        tag = "blf_incremental" if not bad_incr else "blf_incremental_bad_" + "_".join(sorted(bad_incr))
        ok, report, out = verify_blf_incremental(tag, bad_incr)
        for k, v in report["checks"].items():
            print("  %-30s %s" % (k, "OK" if v else "FAIL"))
        print("BLF-INCREMENTAL VERIFY %s | 产出 %s" % ("PASS" if ok else "FAIL", out))
        if bad_incr:
            if ok:
                print("REVERT-TO-FAIL FAILED：增量缺陷没抓到", file=sys.stderr)
                sys.exit(1)
            print("REVERT-TO-FAIL OK：增量缺陷被断言抓到（%s）" % tag)
            return
        if not ok:
            sys.exit(1)
        return

    bad_ci = {t.strip() for t in (args.bad_cursor_init or "").split(",") if t.strip()}
    unknown_ci = bad_ci - {"noinit"}
    if unknown_ci:
        ap.error("--bad-cursor-init 只能取 noinit，收到：%s" % ",".join(sorted(unknown_ci)))
    if args.verify_cursor_init or bad_ci:
        tag = "cursor_init" if not bad_ci else "cursor_init_bad_" + "_".join(sorted(bad_ci))
        ok, report, out = verify_cursor_init(tag, bad_ci)
        for k, v in report["checks"].items():
            print("  %-30s %s" % (k, "OK" if v else "FAIL"))
        print("CURSOR-INIT VERIFY %s | 产出 %s" % ("PASS" if ok else "FAIL", out))
        if bad_ci:
            if ok:
                print("REVERT-TO-FAIL FAILED：标尺初始位置缺陷没抓到", file=sys.stderr)
                sys.exit(1)
            print("REVERT-TO-FAIL OK：标尺初始位置缺陷被断言抓到（%s）" % tag)
            return
        if not ok:
            sys.exit(1)
        return

    bad_c2 = {t.strip() for t in (args.bad_cursor2 or "").split(",") if t.strip()}
    unknown_c2 = bad_c2 - {"noc2", "baddhead", "badhover", "whitebg"}
    if unknown_c2:
        ap.error("--bad-cursor2 只能取 noc2 / baddhead / badhover / whitebg，收到：%s" % ",".join(sorted(unknown_c2)))
    if args.verify_cursor2 or bad_c2:
        tag = "cursor2" if not bad_c2 else "cursor2_bad_" + "_".join(sorted(bad_c2))
        ok, report, out = verify_cursor2(tag, bad_c2)
        for k, v in report["checks"].items():
            print("  %-30s %s" % (k, "OK" if v else "FAIL"))
        print("CURSOR2 VERIFY %s | 产出 %s" % ("PASS" if ok else "FAIL", out))
        if bad_c2:
            if ok:
                print("REVERT-TO-FAIL FAILED：cursor2 缺陷没抓到", file=sys.stderr)
                sys.exit(1)
            print("REVERT-TO-FAIL OK：cursor2 缺陷被断言抓到（%s）" % tag)
            return
        if not ok:
            sys.exit(1)
        return

    bad_cd = {t.strip() for t in (args.bad_cursor_dclick or "").split(",") if t.strip()}
    unknown_cd = bad_cd - {"nodouble", "menuconflict"}
    if unknown_cd:
        ap.error("--bad-cursor-dclick 只能取 nodouble / menuconflict，收到：%s"
                 % ",".join(sorted(unknown_cd)))
    if args.verify_cursor_dclick or bad_cd:
        tag = "cursor_dclick" if not bad_cd else "cursor_dclick_bad_" + "_".join(sorted(bad_cd))
        ok, report, out = verify_cursor_dclick(tag, bad_cd)
        for k, v in report["checks"].items():
            print("  %-30s %s" % (k, "OK" if v else "FAIL"))
        print("CURSOR-DCLICK VERIFY %s | 产出 %s" % ("PASS" if ok else "FAIL", out))
        if bad_cd:
            if ok:
                print("REVERT-TO-FAIL FAILED：双击定位缺陷没抓到", file=sys.stderr)
                sys.exit(1)
            print("REVERT-TO-FAIL OK：双击定位缺陷被断言抓到（%s）" % tag)
            return
        if not ok:
            sys.exit(1)
        return

    bad_mouse = {t.strip() for t in (args.bad_mouse or "").split(",") if t.strip()}
    unknown_mouse = bad_mouse - {"nowheel", "nopan", "norect"}
    if unknown_mouse:
        ap.error("--bad-mouse 只能取 nowheel / nopan / norect，收到：%s"
                 % ",".join(sorted(unknown_mouse)))
    if args.verify_mouse or bad_mouse:
        tag = "mouse" if not bad_mouse else "mouse_bad_" + "_".join(sorted(bad_mouse))
        ok, report, out = verify_mouse(tag, bad_mouse)
        for k, v in report["checks"].items():
            print("  %-30s %s" % (k, "OK" if v else "FAIL"))
        print("MOUSE VERIFY %s | 产出 %s" % ("PASS" if ok else "FAIL", out))
        if bad_mouse:
            if ok:
                print("REVERT-TO-FAIL FAILED：鼠标语义缺陷没抓到", file=sys.stderr)
                sys.exit(1)
            print("REVERT-TO-FAIL OK：鼠标语义缺陷被断言抓到（%s）" % tag)
            return
        if not ok:
            sys.exit(1)
        return

    bad_ns = {t.strip() for t in (args.bad_next_segment or "").split(",") if t.strip()}
    unknown_ns = bad_ns - {"alwaysfirst", "alwaysjump", "nowrap", "noreset"}
    if unknown_ns:
        ap.error("--bad-next-segment 只能取 alwaysfirst / alwaysjump / nowrap / noreset，收到：%s" % ",".join(sorted(unknown_ns)))
    if args.verify_next_segment or bad_ns:
        tag = "next_segment" if not bad_ns else "next_segment_bad_" + "_".join(sorted(bad_ns))
        ok, report, out = verify_next_segment(tag, bad_ns)
        for k, v in report["checks"].items():
            print("  %-30s %s" % (k, "OK" if v else "FAIL"))
        print("NEXT-SEGMENT VERIFY %s | 产出 %s" % ("PASS" if ok else "FAIL", out))
        if bad_ns:
            if ok:
                print("REVERT-TO-FAIL FAILED：下一段缺陷没抓到", file=sys.stderr)
                sys.exit(1)
            print("REVERT-TO-FAIL OK：下一段缺陷被断言抓到（%s）" % tag)
            return
        if not ok:
            sys.exit(1)
        return

    bad_tr = {t.strip() for t in (args.bad_transition or "").split(",") if t.strip()}
    unknown_tr = bad_tr - {"noignore", "nochange"}
    if unknown_tr:
        ap.error("--bad-transition 只能取 noignore / nochange，收到：%s" % ",".join(sorted(unknown_tr)))
    if args.verify_transition or bad_tr:
        tag = "transition" if not bad_tr else "transition_bad_" + "_".join(sorted(bad_tr))
        ok, report, out = verify_transition(tag, bad_tr)
        for k, v in report["checks"].items():
            print("  %-30s %s" % (k, "OK" if v else "FAIL"))
        print("TRANSITION VERIFY %s | 产出 %s" % ("PASS" if ok else "FAIL", out))
        if bad_tr:
            if ok:
                print("REVERT-TO-FAIL FAILED：跳变缺陷没抓到", file=sys.stderr)
                sys.exit(1)
            print("REVERT-TO-FAIL OK：跳变缺陷被断言抓到（%s）" % tag)
            return
        if not ok:
            sys.exit(1)
        return

    bad_win = {t.strip() for t in (args.bad_window or "").split(",") if t.strip()}
    unknown_win = bad_win - {"nowin"}
    if unknown_win:
        ap.error("--bad-window 只能取 nowin，收到：%s" % ",".join(sorted(unknown_win)))
    if args.verify_window or bad_win:
        tag = "window" if not bad_win else "window_bad_" + "_".join(sorted(bad_win))
        ok, report, out = verify_window(tag, bad_win)
        for k, v in report["checks"].items():
            print("  %-30s %s" % (k, "OK" if v else "FAIL"))
        print("WINDOW VERIFY %s | 产出 %s" % ("PASS" if ok else "FAIL", out))
        if bad_win:
            if ok:
                print("REVERT-TO-FAIL FAILED：窗口缺陷没抓到", file=sys.stderr)
                sys.exit(1)
            print("REVERT-TO-FAIL OK：窗口缺陷被断言抓到（%s）" % tag)
            return
        if not ok:
            sys.exit(1)
        return

    bad_wf = {t.strip() for t in (args.bad_workflow or "").split(",") if t.strip()}
    unknown_wf = bad_wf - {"nodefault", "noadd", "single", "nonote", "nomenuadd", "nomenuaxis"}
    if unknown_wf:
        ap.error("--bad-workflow 只能取 nodefault / noadd / single / nonote / nomenuadd / nomenuaxis，收到：%s"
                 % ",".join(sorted(unknown_wf)))
    if args.verify_workflow or bad_wf:
        tag = "workflow" if not bad_wf else "workflow_bad_" + "_".join(sorted(bad_wf))
        ok, report, out = verify_workflow(tag, bad_wf)
        for k, v in report["checks"].items():
            print("  %-30s %s" % (k, "OK" if v else "FAIL"))
        print("WORKFLOW VERIFY %s | 产出 %s" % ("PASS" if ok else "FAIL", out))
        if bad_wf:
            if ok:
                print("REVERT-TO-FAIL FAILED：工作流缺陷没抓到", file=sys.stderr)
                sys.exit(1)
            print("REVERT-TO-FAIL OK：工作流缺陷被断言抓到（%s）" % tag)
            return
        if not ok:
            sys.exit(1)
        return

    bad_rel = {t.strip() for t in (args.bad_reltime or "").split(",") if t.strip()}
    unknown_rel = bad_rel - {"nooffset", "noscalefix"}
    if unknown_rel:
        ap.error("--bad-reltime 只能取 nooffset / noscalefix，收到：%s" % ",".join(sorted(unknown_rel)))
    if args.verify_reltime or bad_rel:
        tag = "reltime" if not bad_rel else "reltime_bad_" + "_".join(sorted(bad_rel))
        ok, report, out = verify_reltime(tag, bad_rel)
        for k, v in report["checks"].items():
            print("  %-30s %s" % (k, "OK" if v else "FAIL"))
        print("RELTIME VERIFY %s | 产出 %s" % ("PASS" if ok else "FAIL", out))
        if bad_rel:
            if ok:
                print("REVERT-TO-FAIL FAILED：相对时间缺陷没抓到", file=sys.stderr)
                sys.exit(1)
            print("REVERT-TO-FAIL OK：相对时间缺陷被断言抓到（%s）" % tag)
            return
        if not ok:
            sys.exit(1)
        return

    bad_export = {t.strip() for t in (args.bad_export or "").split(",") if t.strip()}
    unknown_export = bad_export - {"fold", "nowrite"}
    if unknown_export:
        ap.error("--bad-export 只能取 fold / nowrite，收到：%s"
                 % ",".join(sorted(unknown_export)))
    if args.verify_export or bad_export:
        tag = "export" if not bad_export else "export_bad_" + "_".join(sorted(bad_export))
        ok, report, out = verify_export(tag, bad_export)
        for k, v in report["checks"].items():
            print("  %-26s %s" % (k, "OK" if v else "FAIL"))
        print("EXPORT VERIFY %s | 命中用例 %s | 未评估用例 %s | 产出 %s"
              % ("PASS" if ok else "FAIL", report["hit_summary"],
                 report["nev_summary"], out))
        if bad_export:
            if ok:
                print("REVERT-TO-FAIL FAILED：导出缺陷没抓到", file=sys.stderr)
                sys.exit(1)
            print("REVERT-TO-FAIL OK：导出缺陷被断言抓到（%s）" % tag)
            return
        if not ok:
            sys.exit(1)
        return

    bad_new = {t.strip() for t in (args.bad_new_ruleset or "").split(",") if t.strip()}
    unknown_new = bad_new - {"overwrite", "bump"}
    if unknown_new:
        ap.error("--bad-new-ruleset 只能取 overwrite / bump，收到：%s"
                 % ",".join(sorted(unknown_new)))
    if args.verify_new_ruleset or bad_new:
        tag = "new_ruleset" if not bad_new else "new_ruleset_bad_" + "_".join(sorted(bad_new))
        ok, report, out = verify_new_ruleset(tag, bad_new)
        for k, v in report["checks"].items():
            print("  %-26s %s" % (k, "OK" if v else "FAIL"))
        print("NEW-RULESET VERIFY %s | 骨架信号 %s | 产出 %s"
              % ("PASS" if ok else "FAIL", report["skeleton_signal"], out))
        if bad_new:
            if ok:
                print("REVERT-TO-FAIL FAILED：新建规则集缺陷没抓到", file=sys.stderr)
                sys.exit(1)
            print("REVERT-TO-FAIL OK：新建规则集缺陷被断言抓到（%s）" % tag)
            return
        if not ok:
            sys.exit(1)
        return

    bad_enum = {t.strip() for t in (args.bad_enum or "").split(",") if t.strip()}
    unknown_enum = bad_enum - {"nolabel", "wrongmap"}
    if unknown_enum:
        ap.error("--bad-enum 只能取 nolabel / wrongmap，收到：%s"
                 % ",".join(sorted(unknown_enum)))
    if args.verify_enum or bad_enum:
        tag = "enum" if not bad_enum else "enum_bad_" + "_".join(sorted(bad_enum))
        ok, report, out = verify_enum(tag, bad_enum)
        for k, v in report["checks"].items():
            print("  %-26s %s" % (k, "OK" if v else "FAIL"))
        print("ENUM VERIFY %s | 映射 %s | 产出 %s"
              % ("PASS" if ok else "FAIL", report["mapping"], out))
        if bad_enum:
            if ok:
                print("REVERT-TO-FAIL FAILED：枚举标签缺陷没抓到", file=sys.stderr)
                sys.exit(1)
            print("REVERT-TO-FAIL OK：枚举标签缺陷被断言抓到（%s）" % tag)
            return
        if not ok:
            sys.exit(1)
        return

    bad_marks = {t.strip() for t in (args.bad_rule_marks or "").split(",") if t.strip()}
    unknown_marks = bad_marks - {"nocolor", "curvepalette", "notoggle"}
    if unknown_marks:
        ap.error("--bad-rule-marks 只能取 nocolor / curvepalette / notoggle，收到：%s"
                 % ",".join(sorted(unknown_marks)))
    if args.verify_rule_marks or bad_marks:
        tag = "rule_marks" if not bad_marks else "rule_marks_bad_" + "_".join(sorted(bad_marks))
        ok, report, out = verify_rule_marks(tag, bad_marks)
        for k, v in report["checks"].items():
            print("  %-28s %s" % (k, "OK" if v else "FAIL"))
        print("RULE-MARKS VERIFY %s | 规则色 %s | 产出 %s"
              % ("PASS" if ok else "FAIL", report["rule_colors"], out))
        if bad_marks:
            if ok:
                print("REVERT-TO-FAIL FAILED：规则标注缺陷没抓到", file=sys.stderr)
                sys.exit(1)
            print("REVERT-TO-FAIL OK：规则标注缺陷被断言抓到（%s）" % tag)
            return
        if not ok:
            sys.exit(1)
        return

    bad_panels = {t.strip() for t in (args.bad_panels or "").split(",") if t.strip()}
    unknown_panels = bad_panels - {"nocollapse", "novalues", "noinset", "noselection"}
    if unknown_panels:
        ap.error("--bad-panels 只能取 nocollapse / novalues / noinset / noselection，收到：%s"
                 % ",".join(sorted(unknown_panels)))
    if args.verify_panels or bad_panels:
        tag = "panels" if not bad_panels else "panels_bad_" + "_".join(sorted(bad_panels))
        ok, report, out = verify_panels(tag, bad_panels)
        for k, v in report["checks"].items():
            print("  %-28s %s" % (k, "OK" if v else "FAIL"))
        print("PANELS VERIFY %s | 产出 %s" % ("PASS" if ok else "FAIL", out))
        if bad_panels:
            if ok:
                print("REVERT-TO-FAIL FAILED：侧栏/Tab 缺陷没抓到", file=sys.stderr)
                sys.exit(1)
            print("REVERT-TO-FAIL OK：侧栏/Tab 缺陷被断言抓到（%s）" % tag)
            return
        if not ok:
            sys.exit(1)
        return

    bad_selops = {t.strip() for t in (args.bad_selops or "").split(",") if t.strip()}
    known_selops = {"noremove", "nosolo", "nohide", "nocopy",
                    "badstats", "nostyle", "nosamples", "noexport"}
    unknown_selops = bad_selops - known_selops
    if unknown_selops:
        ap.error("--bad-selops 只能取 %s，收到：%s"
                 % ("/".join(sorted(known_selops)), ",".join(sorted(unknown_selops))))
    if args.verify_selops or bad_selops:
        tag = "selops" if not bad_selops else "selops_bad_" + "_".join(sorted(bad_selops))
        ok, report, out = verify_selops(tag, bad_selops)
        for k, v in report["checks"].items():
            print("  %-28s %s" % (k, "OK" if v else "FAIL"))
        print("SELOPS VERIFY %s | 产出 %s" % ("PASS" if ok else "FAIL", out))
        if bad_selops:
            if ok:
                print("REVERT-TO-FAIL FAILED：选中操作缺陷没抓到", file=sys.stderr)
                sys.exit(1)
            print("REVERT-TO-FAIL OK：选中操作缺陷被断言抓到（%s）" % tag)
            return
        if not ok:
            sys.exit(1)
        return

    bad_blf_gui = {t.strip() for t in (args.bad_blf_gui or "").split(",") if t.strip()}
    unknown_blf_gui = bad_blf_gui - {"noscan", "noeval", "nocache", "nobounds"}
    if unknown_blf_gui:
        ap.error("--bad-blf-gui 只能取 noscan / noeval / nocache / nobounds，收到：%s"
                 % ",".join(sorted(unknown_blf_gui)))
    if args.verify_blf or bad_blf_gui:
        if not args.dat or not args.dat.lower().endswith(".blf"):
            ap.error("--verify-blf 需要 --dat 指向 .blf")
        if not args.dbc:
            ap.error("--verify-blf 需要 --dbc（DBC 集合）")
        tag = args.tag if args.tag != "case" else "blf_" + os.path.splitext(
            os.path.basename(args.rules))[0]
        if bad_blf_gui:
            tag = "blf_bad_" + "_".join(sorted(bad_blf_gui))
        ok, report, out = verify_blf(args.dat, args.dbc, args.rules, tag, bad_blf_gui)
        for k, v in report["checks"].items():
            print("  %-26s %s" % (k, "OK" if v else "FAIL"))
        print("BLF VERIFY %s | P2区间=%d | 产出 %s"
              % ("PASS" if ok else "FAIL", report["p2_total_intervals"], out))
        if bad_blf_gui:
            if ok:
                print("REVERT-TO-FAIL FAILED：BLF GUI 缺陷没抓到", file=sys.stderr)
                sys.exit(1)
            print("REVERT-TO-FAIL OK：BLF GUI 缺陷被断言抓到（%s）" % tag)
            return
        if not ok:
            sys.exit(1)
        return

    bad_as = {t.strip() for t in (args.bad_add_signals or "").split(",") if t.strip()}
    unknown_as = bad_as - {"nodesc"}
    if unknown_as:
        ap.error("--bad-add-signals 只能取 nodesc，收到：%s" % ",".join(sorted(unknown_as)))
    if args.verify_add_signals or bad_as:
        ok, report, out = verify_add_signals(args.dat, bad_as)
        for k, v in report["checks"].items():
            print("  %-28s %s" % (k, "OK" if v else "FAIL"))
        tag_as = "add_signals" if not bad_as else "add_signals_bad_" + "_".join(sorted(bad_as))
        print("ADD-SIGNALS VERIFY %s | 信号 %d | 曲线 %d->%d->%d | 产出 %s"
              % ("PASS" if ok else "FAIL", report["signals_available"],
                 report["curves_initial"], report["curves_after_add"],
                 report["curves_after_remove"], out))
        if bad_as:
            if ok:
                print("REVERT-TO-FAIL FAILED：描述缺陷没抓到", file=sys.stderr)
                sys.exit(1)
            print("REVERT-TO-FAIL OK：描述缺陷被断言抓到（%s）" % tag_as)
            return
        if not ok:
            sys.exit(1)
        return

    bad_dr = {t.strip() for t in (args.bad_dbc_reselect or "").split(",") if t.strip()}
    unknown_dr = bad_dr - {"stale"}
    if unknown_dr:
        ap.error("--bad-dbc-reselect 只能取 stale，收到：%s" % ",".join(sorted(unknown_dr)))
    if args.verify_dbc_reselect or bad_dr:
        tag = "dbc_reselect" if not bad_dr else "dbc_reselect_bad_" + "_".join(sorted(bad_dr))
        ok, report, out = verify_dbc_reselect(tag, bad_dr)
        for k, v in report["checks"].items():
            print("  %-28s %s" % (k, "OK" if v else "FAIL"))
        print("DBC-RESELECT VERIFY %s | 旧 %s -> 新 %s | 产出 %s"
              % ("PASS" if ok else "FAIL", report["old_keys"], report["new_keys"], out))
        if bad_dr:
            if ok:
                print("REVERT-TO-FAIL FAILED：重选缺陷没被抓到", file=sys.stderr)
                sys.exit(1)
            print("REVERT-TO-FAIL OK：重选缺陷被断言抓到（%s）" % tag)
            return
        if not ok:
            sys.exit(1)
        return

    bad_mf4 = {t.strip() for t in (args.bad_mf4 or "").split(",") if t.strip()}
    unknown_mf4 = bad_mf4 - {"nover"}
    if unknown_mf4:
        ap.error("--bad-mf4 只能取 nover，收到：%s" % ",".join(sorted(unknown_mf4)))
    if args.verify_mf4 or bad_mf4:
        tag = "mf4" if not bad_mf4 else "mf4_bad_" + "_".join(sorted(bad_mf4))
        ok, report, out = verify_mf4(tag, bad_mf4)
        for k, v in report["checks"].items():
            print("  %-28s %s" % (k, "OK" if v else "FAIL"))
        print("MF4 VERIFY %s | 版本 %s | 信号 %d | 点 %d | 负dt %d | 规则命中 %d/%d | 产出 %s"
              % ("PASS" if ok else "FAIL", report["mdf_version"], report["signals"],
                 report["points"], report["negative_dt"], report["rules_hit"], 6, out))
        if bad_mf4:
            if ok:
                print("REVERT-TO-FAIL FAILED：MDF 版本缺陷没被抓到", file=sys.stderr)
                sys.exit(1)
            print("REVERT-TO-FAIL OK：MDF 版本缺陷被断言抓到（%s）" % tag)
            return
        if not ok:
            sys.exit(1)
        return

    bad_poem = {t.strip() for t in (args.bad_poem or "").split(",") if t.strip()}
    unknown_poem = bad_poem - {"fixed"}
    if unknown_poem:
        ap.error("--bad-poem 只能取 fixed，收到：%s" % ",".join(sorted(unknown_poem)))
    if args.verify_poem or bad_poem:
        tag = "poem" if not bad_poem else "poem_bad_" + "_".join(sorted(bad_poem))
        ok, report, out = verify_poem(tag, bad_poem)
        for k, v in report["checks"].items():
            print("  %-28s %s" % (k, "OK" if v else "FAIL"))
        print("POEM VERIFY %s | 标语 '%s' -> '%s' | 共 %d 句 | 产出 %s"
              % ("PASS" if ok else "FAIL", report["first"], report["second"],
                 report["poem_count"], out))
        if bad_poem:
            if ok:
                print("REVERT-TO-FAIL FAILED：标语轮换缺陷没被抓到", file=sys.stderr)
                sys.exit(1)
            print("REVERT-TO-FAIL OK：标语轮换缺陷被断言抓到（%s）" % tag)
            return
        if not ok:
            sys.exit(1)
        return

    bad_dbcdir = {t.strip() for t in (args.bad_dbcdir or "").split(",") if t.strip()}
    unknown_dbcdir = bad_dbcdir - {"noresolve"}
    if unknown_dbcdir:
        ap.error("--bad-dbcdir 只能取 noresolve，收到：%s" % ",".join(sorted(unknown_dbcdir)))
    if args.verify_dbcdir or bad_dbcdir:
        tag = "dbcdir" if not bad_dbcdir else "dbcdir_bad_" + "_".join(sorted(bad_dbcdir))
        ok, report, out = verify_dbcdir(tag, bad_dbcdir)
        for k, v in report["checks"].items():
            print("  %-28s %s" % (k, "OK" if v else "FAIL"))
        print("DBCDIR VERIFY %s | 配置 %s | DBC %d 个 | 产出 %s"
              % ("PASS" if ok else "FAIL", report["config"], report["dbc_count"], out))
        if bad_dbcdir:
            if ok:
                print("REVERT-TO-FAIL FAILED：DBC 路径解析缺陷没被抓到", file=sys.stderr)
                sys.exit(1)
            print("REVERT-TO-FAIL OK：DBC 路径解析缺陷被断言抓到（%s）" % tag)
            return
        if not ok:
            sys.exit(1)
        return

    bad_ui = {t.strip() for t in (args.bad_ui or "").split(",") if t.strip()}
    unknown = bad_ui - {"xaxis", "yalign", "cursor"}
    if unknown:
        ap.error("--bad-ui 只能取 xaxis / yalign / cursor，收到：%s" % ",".join(sorted(unknown)))

    if (args.verify_ui or args.bad_axis or args.bad_y_share or args.bad_menu
            or bad_ui or args.verify_ui_native):
        tag = "ui_native" if args.verify_ui_native else "ui"
        if args.bad_axis:
            tag = "ui_bad_axis"
        elif args.bad_y_share:
            tag = "ui_bad_y_share"
        elif args.bad_menu:
            tag = "ui_bad_menu"
        elif bad_ui:
            tag = "ui_bad_" + "_".join(sorted(bad_ui))
        if args.dat:   # 换数据集时 tag 带数据名，不覆盖默认用例的基线证据
            tag += "_" + os.path.splitext(os.path.basename(args.dat))[0]
        ok, report, out = verify_ui(tag, bad_axis=args.bad_axis,
                                    bad_y_share=args.bad_y_share,
                                    bad_menu=args.bad_menu, bad=bad_ui,
                                    native=args.verify_ui_native, dat=args.dat)
        for k, v in report["checks"].items():
            print("  %-32s %s" % (k, "OK" if v else "FAIL"))
        pa = report.get("per_axis", {})
        print("UI VERIFY %s | 分轴 %d 轴 / 区间 %d / 区间带 %d / 标记 %d | 产出 %s"
              % ("PASS" if ok else "FAIL", pa.get("curve_count", 0), pa.get("intervals", 0),
                 pa.get("region_items", 0), pa.get("marker_items", 0), out))
        if args.bad_axis or args.bad_y_share or args.bad_menu or bad_ui:
            if ok:
                print("REVERT-TO-FAIL FAILED：分轴缺陷没被抓到", file=sys.stderr)
                sys.exit(1)
            print("REVERT-TO-FAIL OK：分轴缺陷被断言抓到（%s）" % tag)
            return
        if not ok:
            sys.exit(1)
        return

    if args.verify:
        if not args.dat:
            ap.error("--verify 需要 --dat")
        ok, report, out = verify(args.dat, args.rules, args.tag,
                                 args.bad_render, args.simulate_network, args.bad_nev)
        for k, v in report["checks"].items():
            print("  %-26s %s" % (k, "OK" if v else "FAIL"))
        print("P3 VERIFY %s | P2区间=%d GUI区间=%d | 产出 %s"
              % ("PASS" if ok else "FAIL", report["p2_total_intervals"],
                 report["gui_region_count"], out))
        if args.bad_render:
            if ok:
                print("REVERT-TO-FAIL FAILED：少画一个标记没被抓到", file=sys.stderr)
                sys.exit(1)
            print("REVERT-TO-FAIL OK：少画一个标记被断言抓到")
            return
        if args.simulate_network:
            if ok:
                print("REVERT-TO-FAIL FAILED：模拟网络调用没被抓到", file=sys.stderr)
                sys.exit(1)
            print("REVERT-TO-FAIL OK：模拟网络调用被 no_network_call 抓到")
            return
        if args.bad_nev:
            if ok:
                print("REVERT-TO-FAIL FAILED：未评估写成 0 命中没被抓到", file=sys.stderr)
                sys.exit(1)
            print("REVERT-TO-FAIL OK：未评估写成 0 命中被断言抓到")
            return
        if not ok:
            sys.exit(1)
        return

    # 正常 GUI：由用户拖放或点击打开；打开即自动扫描并出曲线
    app = QtWidgets.QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())

