# -*- coding: utf-8 -*-
"""生成界面预览图：合成数据 + 窗口不上屏，不读任何实测文件、不抓屏、不录屏。

用法：
    python preview_ui.py            # 默认用系统真实字体，输出到 out/preview_*.png
    python preview_ui.py --offscreen  # 无字体环境（只用于对比布局，中文会变方块）

为什么不用 QT_QPA_PLATFORM=offscreen 看效果：该平台在 Windows 上字体族数量为 0
（QFontDatabase.families() == 0），所有字符按同一方块宽度排版，看不出真实观感。
这里改用 windows 平台 + WA_DontShowOnScreen：窗口只在内存里布局绘制，绝不投到屏幕。
"""
import argparse
import os
import sys
from types import SimpleNamespace

import numpy as np

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(ROOT, "out")


def _prepare_platform(offscreen):
    if offscreen:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    else:
        os.environ.setdefault("QT_QPA_PLATFORM", "windows")


def synth():
    """造一组形状像真实工况的信号（阶跃档位、正弦踏板、转速、扭矩、制动）。"""
    n = 3000
    t = np.linspace(0.0, 30.0, n)
    drive = np.where(t < 8, 4, np.where(t < 14, 3, 4)).astype(float)
    pedal = np.clip(38 + 26 * np.sin(t / 3.1) + 6 * np.sin(t * 2.3), 0, 100)
    spd = np.clip(1200 + 480 * np.sin(t / 2.4 - 0.6) + 40 * np.sin(t * 7.0), 300, 2200)
    tq = np.clip(80 * np.sin(t / 2.0) + 12 * np.sin(t * 5.1), -60, 120)
    brk = np.clip(np.where((t > 11) & (t < 13.5), 22, 0) + 2 * np.sin(t), 0, 30)
    sig = {
        "GrMod_stDrvAct": drive,
        "AccP_percAccrPedl": pedal,
        "VehMot_spdAct": spd,
        "VehMot_tqAct": tq,
        "Brake_percBrkPedl": brk,
        "CstMod_stCoastRgnMod": np.full(n, 2.0),
        "ESC_stEPB": np.where(t > 25, 1.0, 0.0),
    }
    units = {"GrMod_stDrvAct": "-", "AccP_percAccrPedl": "%", "VehMot_spdAct": "rpm",
             "VehMot_tqAct": "Nm", "Brake_percBrkPedl": "%",
             "CstMod_stCoastRgnMod": "lvl", "ESC_stEPB": "-"}
    return t, sig, sorted(sig), units


def render_dialogs(names):
    """规则编辑器与条件编辑器的预览图。只读临时副本，绝不写真实规则文件。"""
    import json
    import shutil

    import caliper_gui
    from PySide6 import QtCore, QtWidgets

    os.makedirs(OUT_DIR, exist_ok=True)
    tmp = os.path.join(OUT_DIR, "_preview_rules.json")
    shutil.copyfile(caliper_gui._fixtures.FIXTURE_PATHS["gear"], tmp)
    doc = json.load(open(tmp, encoding="utf-8"))
    doc["name"] = "档位与蠕行工况"
    doc["note"] = "示例：演示界面改规则，保存即写回 JSON"
    doc["rules"].append({
        "name": "蠕行滑行（多条件）",
        "when": "(GrMod_stDrvAct == 3) & (AccP_percAccrPedl < 5)",
        "min_duration_ms": 500, "hysteresis_ms": 50, "merge_gap_ms": 120,
        "mark": {"color": "#3E8BFF", "label": "Co"}})
    doc["rules"].append({
        "name": "复杂表达式（高级）",
        "when": "abs(VehMot_tqAct) < 5",
        "min_duration_ms": 300, "hysteresis_ms": 0, "merge_gap_ms": 0,
        "mark": {"color": "#B08CFF", "label": "Tq"}})
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)

    outs = []
    dlg = caliper_gui.RuleSetDialog(tmp, signals=names)
    dlg.resize(1020, 620)
    dlg.setAttribute(QtCore.Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    dlg.show()
    dlg.table.selectRow(4)
    QtWidgets.QApplication.processEvents()
    p1 = os.path.join(OUT_DIR, "preview_rule_editor.png")
    dlg.grab().save(p1)
    outs.append(p1)

    cd = caliper_gui.ConditionDialog("(GrMod_stDrvAct == 3) & (AccP_percAccrPedl < 5)",
                                 "蠕行滑行", names)
    cd.resize(720, 460)
    cd.setAttribute(QtCore.Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    cd.show()
    QtWidgets.QApplication.processEvents()
    p2 = os.path.join(OUT_DIR, "preview_condition.png")
    cd.grab().save(p2)
    outs.append(p2)

    os.remove(tmp)
    for p in outs:
        print(p)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offscreen", action="store_true",
                    help="用无字体平台渲染（中文会显示成方块，仅用于看布局）")
    args = ap.parse_args()
    _prepare_platform(args.offscreen)
    sys.path.insert(0, ROOT)

    import caliper_gui
    import caliper_rules
    from PySide6 import QtCore, QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    axis, sig, names, units = synth()
    filler = ["Sig_%03d" % i for i in range(32)]

    w = caliper_gui.MainWindow()
    w.resize(1440, 860)
    # 窗口不上屏：只在内存里布局与绘制，屏幕上不会出现任何东西
    w.setAttribute(QtCore.Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    w.show()

    w.dat_path = "T1V_demo.dat"
    w.signal_map = {n: (0, i) for i, n in enumerate(names + filler)}
    w.scan_info = {"signals": [
        {"name": n, "unit": units.get(n, ""), "group": 0, "index": i}
        for i, n in enumerate(names + filler)]}
    w.units = {n: units.get(n, "") for n in names + filler}
    w.brand_sub.setText("T1V_demo.dat · %d 个信号" % len(w.signal_map))
    w.checked = set(names[:5])
    w._fill_signal_tree()
    w.m = SimpleNamespace(axis=axis, signals=sig,
                          start_time_local="2026-11-16T10:32:05")

    def interval(t0, t1):
        k = int(np.searchsorted(axis, t0))
        return {"t0": t0, "t1": t1, "duration_ms": int((t1 - t0) * 1000),
                "snapshot": {"GrMod_stDrvAct": float(sig["GrMod_stDrvAct"][k])}}

    results = [
        caliper_rules.RuleResult("D 档蠕行", 1, "hit", [], [interval(2.4, 5.1), interval(16.2, 18.9)]),
        caliper_rules.RuleResult("P 档踩油门", 1, "hit", [], [interval(8.6, 10.2)]),
        caliper_rules.RuleResult("制动能量回收", 1, "hit", [], [interval(11.2, 13.4)]),
        caliper_rules.RuleResult("坡道起步", 1, "no_hit", [], []),
        caliper_rules.RuleResult("怠速启停", 1, "no_hit", [], []),
        caliper_rules.RuleResult("蠕行扭矩超限", 1, "not_evaluated",
                             ["DrvReq_bCrpIPartRst_MP", "VehMotRA_bCrpTqInh"], []),
    ]
    w.result = caliper_rules.EvaluationResult("T1V_demo.dat", "0" * 32, 1, results,
                                          {"hit": 3, "no_hit": 2, "not_evaluated": 1})
    w.render()
    w._set_cursor(13.0)
    w.plot.getPlotItem().setXRange(0, 30, padding=0)
    app.processEvents()

    os.makedirs(OUT_DIR, exist_ok=True)
    shots = []
    for name, per_axis, size in [("preview_shared", False, (1440, 860)),
                                 ("preview_per_axis", True, (1440, 860)),
                                 ("preview_compact", True, (1180, 720))]:
        w.set_per_axis(per_axis)
        w.resize(*size)
        app.processEvents()
        if per_axis:
            for _, pi in w.axis_plots:
                pi.getViewBox().setXRange(0, 30, padding=0)
        else:
            w.plot.getPlotItem().setXRange(0, 30, padding=0)
        app.processEvents()
        path = os.path.join(OUT_DIR, name + ".png")
        w.grab().save(path)
        shots.append(path)
    for p in shots:
        print(p)
    render_dialogs(names)


if __name__ == "__main__":
    main()
