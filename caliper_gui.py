# -*- coding: utf-8 -*-
"""
GUI 与图上标注。

只读 EvaluationResult，不回写内核、不直接调 asammdf（读文件一律经 caliper_core）。
两栏：多曲线（含工况区间带与标记点）| 工况栏（三态分区 + 添加信号等）。
支持两类数据文件：.dat（MDF，经 asammdf）与 .blf（CAN/LIN，凭用户手选 DBC 解码）。

运行（正常 GUI，或双击 start.bat）:
  "python" caliper_gui.py
程序化验收（offscreen，绝不截屏/录屏/抓大屏；开关转发到 caliper_verify.py）:
  "python" caliper_gui.py --verify --dat <file> --tag <case>
  单项：--verify-add-signals / --verify-ui / --verify-ui-native / --verify-workflow
        --verify-rule-edit / --verify-export / --verify-new-ruleset / --verify-enum
        --verify-rule-marks / --verify-panels / --verify-selops / --verify-transition
        --verify-next-segment / --verify-reltime / --verify-cursor-init / --verify-cursor2
        --verify-cursor-dclick / --verify-mouse / --verify-blf-switch / --verify-blf-incremental
        --verify-startup
  对应 --bad-* 为变异测试（证明断言有区分力）；BLF 全量验收另见 --verify-blf（需 --dat/--dbc）。

默认不加载任何规则集：打开文件只出曲线、不做工况评估；在顶栏「选择规则集」里选了才评估。
曲线区右键菜单：`修改 Y 轴量程…` / `Y 轴量程自适应` / `每个信号一个 Y 轴（分轴显示）`。
分轴模式下每个信号一个子图、一个独立 Y 标尺（X 轴联动），每个子图右键同样有量程项；
X 轴刻度值与「时间 / s」只挂在最下方子图，各子图 Y 标尺统一宽度、左边界对齐。
红色竖线是标尺：可左右拖动，停在哪个时刻就读那个时刻各信号的值（值直接写在子图上，
同时汇总到图下那行）；点击右侧命中工况条目也会把标尺跳到该区间起点。

改规则不用人去编辑 JSON：顶栏「编辑规则」或右栏条目右键「编辑「X」…」打开规则编辑器，
条件按「类型 / 信号 / 判据」逐行填，支持定值 / 跳变 / 波动幅度 / 变化次数，行间任一或全部；
保存即写回 rules/ 下对应子目录的 json（自动备份 .bak、版本号 +1），写完界面立刻按新规则重算。

--verify 用 QT_QPA_PLATFORM=offscreen 启动，屏蔽 socket.connect 证明零网络调用，
并用程序化断言核对「图上标记条数 == 独立清单条数」，不依赖任何画面观察。
分轴模式的轴数、每轴独立量程、X 轴联动、区间带随轴倍增同样由程序化断言核对。
"""
import argparse
import csv
import datetime
import html
import json
import math
import os
import random
import re
import shutil
import socket
import sys

# 必须在 QApplication 构造前设置；offscreen 下界面根本不出现在屏幕，满足隐私要求。
if any(a in sys.argv for a in ("--verify", "--verify-add-signals", "--verify-ui",
                               "--bad-axis", "--bad-y-share", "--bad-menu", "--bad-ui",
                               "--verify-rule-edit", "--bad-rule-edit",
                               "--verify-export", "--bad-export",
                               "--verify-new-ruleset", "--bad-new-ruleset",
                               "--verify-enum", "--bad-enum",
                               "--verify-rule-marks", "--bad-rule-marks",
                               "--verify-panels", "--bad-panels",
                               "--verify-selops", "--bad-selops",
                               "--verify-blf", "--bad-blf-gui")):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets, QtTest

import caliper_core
import caliper_report
import caliper_rules
import verify_fixtures as _fixtures   # 验收专用规则集：内容内嵌，不依赖 rules/ 示例文件

ROOT = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
        else os.path.dirname(os.path.abspath(__file__)))  # 打包后 exe 所在目录即数据根目录
OUT_DIR = os.path.join(ROOT, "out")
RULES_DIR = os.path.join(ROOT, "rules")
DATA_DIR = os.path.join(ROOT, "realdata")  # 验收/探测用真实数据
USER_ROLE = QtCore.Qt.ItemDataRole.UserRole
RULE_NAME_ROLE = USER_ROLE + 4      # 清单条目 -> 规则名，右键「编辑这条规则」用
NO_RULES_LABEL = "不加载规则"
PER_AXIS_LABEL = "每个信号一个 Y 轴（分轴显示）"

# 顶栏标语：毛主席诗词名句，每次启动换一句（QSettings 记索引轮换，保证相邻两次不同）。
# 选句标准：脍炙人口 + 6~8 字短句——顶栏右侧按钮多，长句会被挤到显示不全。
POEMS = [
    "江山如此多娇",      # 沁园春·雪
    "风景这边独好",      # 清平乐·会昌
    "踏遍青山人未老",    # 清平乐·会昌
    "雄关漫道真如铁",    # 忆秦娥·娄山关
    "不到长城非好汉",    # 清平乐·六盘山
    "红军不怕远征难",    # 七律·长征
    "万类霜天竞自由",    # 沁园春·长沙
    "战地黄花分外香",    # 采桑子·重阳
    "天翻地覆慨而慷",    # 七律·人民解放军占领南京
    "数风流人物",        # 沁园春·雪
]


def pick_poem():
    """轮换取一句诗词：QSettings 记上次索引，下次 +1；不可用时退化到随机。"""
    try:
        s = QtCore.QSettings("Caliper", "Henry")
        last = int(s.value("poem_index", "-1"))
        idx = (last + 1) % len(POEMS)
        s.setValue("poem_index", idx)
        s.sync()
        return POEMS[idx]
    except Exception:
        return random.choice(POEMS)
SHARED_AXIS_LABEL = "所有信号共用一个 Y 轴（合并显示）"
Y_AXIS_MIN_WIDTH = 44          # Y 标尺最小宽度，保证短刻度也有标尺感

# ---- 视觉主题：浅色、低饱和、数据优先 ----
# 正常应用的浅色系：白底 + 浅灰面板 + 深色文字，
# 单色蓝强调 + 圆角卡片；曲线用白底上高辨识的深一档色板，网格压淡，数据是主体。
BG_APP = "#F3F4F6"             # 窗口底（浅灰）
BG_CANVAS = "#FFFFFF"          # 绘图区底（白）
PANEL = "#FFFFFF"              # 面板/卡片
PANEL_HI = "#F5F7FA"           # 输入框、悬浮态
PANEL_SUNK = "#FAFBFC"         # 内嵌区域
BORDER = "#D9DEE5"
BORDER_HI = "#B9C2CF"
TEXT = "#1F2733"
TEXT_DIM = "#5A6675"
TEXT_FAINT = "#8A94A3"
ACCENT = "#3E8BFF"             # 唯一强调色：选中、焦点、主按钮
ACCENT_HI = "#2E77E6"
HIT_COLOR = "#0E9F6E"          # 命中
MISS_COLOR = "#8A94A3"         # 未命中
NEUTRAL_COLOR = "#C77E14"      # 未评估
GRID_ALPHA = 0.12              # 网格压淡，只留一点点参照
CURVE_COLORS = ["#2F6FED", "#0E9F6E", "#C77400", "#0E9AA7",
                "#7C5CD6", "#D6409F", "#E5484D"]
# 规则标注专用色板：与曲线色板完全独立（深一档、低饱和），防止区间带/标记与曲线撞色。
# 选色经过色差断言：每支对全部曲线色 ≥150、两两 ≥100（加权 RGB 距离）。
RULE_MARK_COLORS = ["#4E6E23", "#7A2340", "#24556B", "#55337F", "#6E6259"]
RULE_CURVE_MIN_DIST = 150      # 规则色 vs 任一曲线色的最小色差
RULE_PAIR_MIN_DIST = 100       # 规则色之间的最小色差
CURSOR_COLOR = "#E5484D"
CURSOR_HOVER = "#C93A3F"
CURSOR2_COLOR = "#B8860B"       # 深金黄：白底上清晰，且明显区别于红线
CURSOR2_HOVER = "#8A6508"       # 比常态 #B8860B 更深：悬停=加重，与 cursor1 方向一致


def load_signal_desc():
    """读 变量描述.json（由 xlsx_to_desc.py 生成）；缺失/损坏就降级为空，不影响启动。"""
    try:
        with open(os.path.join(ROOT, "description", "变量描述.json"), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


SIGNAL_DESC = load_signal_desc()   # 模块级一次加载；改了 Excel 重跑脚本后重启界面生效


def _color_dist(a, b):
    """加权 RGB 色差（绿通道权重最高，近似人眼敏感度）。只用于配色校验，不参与数据。"""
    ca, cb = QtGui.QColor(a), QtGui.QColor(b)
    dr, dg, db = ca.red() - cb.red(), ca.green() - cb.green(), ca.blue() - cb.blue()
    return math.sqrt(2 * dr * dr + 4 * dg * dg + 3 * db * db)


def _alpha(color, a):
    """把 #RRGGBB 转成带 alpha 的 QColor（0-255）。"""
    c = QtGui.QColor(color)
    c.setAlpha(int(a))
    return c


def _ui_font(size_px, weight=QtGui.QFont.Weight.Normal):
    f = QtGui.QFont("Microsoft YaHei UI")
    f.setPixelSize(int(size_px))
    f.setWeight(weight)
    return f


TICK_FONT = _ui_font(11)        # 轴上刻度字
LABEL_FONT = _ui_font(11, QtGui.QFont.Weight.DemiBold)


def _qss():
    """全局样式表。只改外观，不动任何控件的行为与对象名。"""
    return """
    QWidget { color: %(TEXT)s; font-family: "Microsoft YaHei UI", "Segoe UI";
              font-size: 12px; }
    QMainWindow, QWidget#App { background: %(BG_APP)s; }
    QToolTip { background: %(PANEL_HI)s; color: %(TEXT)s; border: 1px solid %(BORDER_HI)s;
               padding: 5px 8px; border-radius: 6px; }

    QFrame#TopBar { background: %(PANEL)s; border: none;
                    border-bottom: 1px solid %(BORDER)s; }
    QLabel#Brand { font-size: 15px; font-weight: 600; color: %(TEXT)s; }
    QLabel#BrandSub { color: %(TEXT_FAINT)s; font-size: 11px; }
    QLabel#PlotHead { color: %(TEXT_DIM)s; font-size: 12px; }
    QLabel#PanelTitle { color: %(TEXT_FAINT)s; font-size: 11px; font-weight: 600; }
    QLabel#Empty { color: %(TEXT_FAINT)s; font-size: 12px; }

    QFrame#Panel { background: %(PANEL)s; border: 1px solid %(BORDER)s;
                   border-radius: 10px; }
    QFrame#PlotPanel { background: %(BG_CANVAS)s; border: 1px solid %(BORDER)s;
                       border-radius: 10px; }
    QFrame#PlotHead { background: transparent; border: none;
                      border-bottom: 1px solid %(BORDER)s; }
    QFrame#RuleHead { background: transparent; border: none; }

    QPushButton { background: %(PANEL_HI)s; border: 1px solid %(BORDER)s;
                  border-radius: 8px; padding: 7px 14px; color: %(TEXT)s; }
    QPushButton:hover { background: #E8EDF4; border-color: %(BORDER_HI)s; }
    QPushButton:pressed { background: %(PANEL_SUNK)s; }
    QPushButton:disabled { color: %(TEXT_FAINT)s; border-color: %(BORDER)s; }
    QPushButton[kind="primary"] { background: %(ACCENT)s; border: 1px solid %(ACCENT)s;
                                  color: #FFFFFF; font-weight: 600; }
    QPushButton[kind="primary"]:hover { background: %(ACCENT_HI)s; border-color: %(ACCENT_HI)s; }
    QPushButton[kind="ghost"] { background: transparent; }
    QPushButton#PanelHandle { background: %(BORDER)s; border: 1px solid %(BORDER_HI)s;
                              border-radius: 9px; padding: 0; color: %(TEXT_DIM)s;
                              font-size: 9px; }
    QPushButton#PanelHandle:hover { background: %(BORDER_HI)s; color: %(TEXT)s; }

    QComboBox, QLineEdit, QDoubleSpinBox { background: %(PANEL_HI)s;
        border: 1px solid %(BORDER)s; border-radius: 8px; padding: 6px 10px;
        color: %(TEXT)s; selection-background-color: %(ACCENT)s;
        selection-color: #FFFFFF; }
    QComboBox:hover, QLineEdit:hover, QDoubleSpinBox:hover { border-color: %(BORDER_HI)s; }
    QComboBox:focus, QLineEdit:focus, QDoubleSpinBox:focus { border-color: %(ACCENT)s; }
    QComboBox::drop-down { border: none; width: 20px; }
    QComboBox QAbstractItemView { background: %(PANEL_HI)s; border: 1px solid %(BORDER_HI)s;
        border-radius: 8px; padding: 4px; outline: none;
        selection-background-color: #DCE7F8; selection-color: %(TEXT)s; }

    QTreeWidget { background: transparent; border: none; outline: none;
                  show-decoration-selected: 1; }
    QTreeWidget::item { padding: 4px 2px; border: none; border-radius: 6px; }
    QTreeWidget::item:hover { background: #EEF1F5; }
    QTreeWidget::item:selected { background: #DCE7F8; color: %(TEXT)s; }
    QTreeWidget::indicator { width: 14px; height: 14px; border-radius: 4px;
        border: 1px solid %(BORDER_HI)s; background: %(PANEL_SUNK)s; }
    QTreeWidget::indicator:hover { border-color: %(ACCENT)s; }
    QTreeWidget::indicator:checked { background: %(ACCENT)s; border-color: %(ACCENT)s; }
    QHeaderView { background: transparent; }
    QHeaderView::section { background: transparent; color: %(TEXT_FAINT)s;
        border: none; border-bottom: 1px solid %(BORDER)s; padding: 6px 2px; }

    QScrollBar:vertical { background: transparent; width: 10px; margin: 2px 2px 2px 0; }
    QScrollBar::handle:vertical { background: #C4CCD8; border-radius: 4px; min-height: 28px; }
    QScrollBar::handle:vertical:hover { background: #AAB4C2; }
    QScrollBar:horizontal { background: transparent; height: 10px; margin: 0 2px 2px 2px; }
    QScrollBar::handle:horizontal { background: #C4CCD8; border-radius: 4px; min-width: 28px; }
    QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
    QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }

    QSplitter::handle { background: transparent; }
    QSplitter::handle:horizontal { width: 12px; }
    QTabWidget::pane { border: 1px solid %(BORDER)s; border-radius: 8px;
                       background: %(PANEL)s; top: -1px; }
    QTabBar::tab { background: transparent; color: %(TEXT_DIM)s; padding: 6px 14px;
                   border: 1px solid transparent; border-top-left-radius: 8px;
                   border-top-right-radius: 8px; }
    QTabBar::tab:selected { background: %(PANEL)s; color: %(TEXT)s;
                            border-color: %(BORDER)s; border-bottom-color: %(PANEL)s; }
    QTabBar::tab:hover { color: %(TEXT)s; }
    QDialog { background: %(PANEL)s; }
    QMessageBox { background: %(PANEL)s; }

    QTableWidget { background: %(PANEL_SUNK)s; border: 1px solid %(BORDER)s;
                   border-radius: 10px; gridline-color: transparent; outline: none; }
    QTableWidget::item { padding: 4px 6px; border: none; }
    QTableWidget::item:hover { background: #EEF1F5; }
    QTableWidget::item:selected { background: #DCE7F8; color: %(TEXT)s; }
    QTableWidget QLineEdit, QTableWidget QComboBox { border-radius: 6px; padding: 3px 6px; }
    QLabel#DialogHint { color: %(TEXT_FAINT)s; font-size: 11px; }
    QLabel#Ok { color: %(HIT_COLOR)s; font-size: 11px; }
    QLabel#Bad { color: #D93A3F; font-size: 11px; }
    QLabel#Warn { color: %(NEUTRAL_COLOR)s; font-size: 11px; }
    QPlainTextEdit { background: %(PANEL_SUNK)s; border: 1px solid %(BORDER)s;
                     border-radius: 8px; color: %(TEXT)s; padding: 6px;
                     selection-background-color: %(ACCENT)s; selection-color: #FFFFFF; }
    QCheckBox { color: %(TEXT_DIM)s; }
    QCheckBox::indicator { width: 14px; height: 14px; border-radius: 4px;
        border: 1px solid %(BORDER_HI)s; background: %(PANEL_SUNK)s; }
    QCheckBox::indicator:checked { background: %(ACCENT)s; border-color: %(ACCENT)s; }
    """ % globals()


_THEMED = False


def apply_theme(app):
    """给 QApplication 上浅色主题：Fusion 风格 + 浅色调色板 + 全局 QSS。

    只做一次；重复调用是幂等的，verify 里直接 new MainWindow 也能拿到同样的外观。
    """
    global _THEMED
    if app is None or _THEMED:
        return
    app.setStyle("Fusion")
    pal = QtGui.QPalette()
    pal.setColor(QtGui.QPalette.ColorRole.Window, QtGui.QColor(BG_APP))
    pal.setColor(QtGui.QPalette.ColorRole.WindowText, QtGui.QColor(TEXT))
    pal.setColor(QtGui.QPalette.ColorRole.Base, QtGui.QColor(PANEL_SUNK))
    pal.setColor(QtGui.QPalette.ColorRole.AlternateBase, QtGui.QColor(PANEL))
    pal.setColor(QtGui.QPalette.ColorRole.Text, QtGui.QColor(TEXT))
    pal.setColor(QtGui.QPalette.ColorRole.Button, QtGui.QColor(PANEL_HI))
    pal.setColor(QtGui.QPalette.ColorRole.ButtonText, QtGui.QColor(TEXT))
    pal.setColor(QtGui.QPalette.ColorRole.Highlight, QtGui.QColor(ACCENT))
    pal.setColor(QtGui.QPalette.ColorRole.HighlightedText, QtGui.QColor("#FFFFFF"))
    pal.setColor(QtGui.QPalette.ColorRole.ToolTipBase, QtGui.QColor(PANEL_HI))
    pal.setColor(QtGui.QPalette.ColorRole.ToolTipText, QtGui.QColor(TEXT))
    pal.setColor(QtGui.QPalette.ColorRole.PlaceholderText, QtGui.QColor(TEXT_FAINT))
    app.setPalette(pal)
    app.setStyleSheet(_qss())
    app.setFont(_ui_font(12))
    pg.setConfigOptions(antialias=True, background=BG_CANVAS, foreground=TEXT_DIM)
    _THEMED = True


class RelativeTimeAxis(pg.AxisItem):
    """X 轴刻度显示「相对首帧的秒数」，不改 Measurement.axis 里的真实时基。

    BLF 的轴是 epoch 秒（约 1.8e9），直接交给 pyqtgraph 会显示成 1.8e9；
    这里只在画刻度文字时减掉首帧时刻。数据、规则判定、报告证据都不受影响。
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.origin = 0.0
        # 关键：pyqtgraph 按 ViewBox 的 epoch 范围（1.8e9）自动算 SI 前缀 -> label 出现
        # "(x1e+09)"、tickStrings 的 scale 变 1e9，减完 origin 的相对秒又被乘回去。
        # 关掉自动前缀后 scale 恒 1，label 只剩「时间 / s」，刻度由 tickStrings 给相对秒。
        self.enableAutoSIPrefix(False)

    def set_origin(self, t0):
        self.origin = float(t0 or 0.0)

    def tickStrings(self, values, scale, spacing):
        # scale 已恒为 1；仍按父类 spacing 规则决定小数位，只把数值平移到相对坐标
        return super().tickStrings([float(v) - self.origin for v in values],
                                   scale, spacing)


class MdaViewBox(pg.ViewBox):
    """示波器鼠标语义（对标 MDA）：左键框选放大、中键左右拖动、右键缩放、滚轮禁用、左键双击定位标尺。

    pyqtgraph 的 RectMode 把左键和中键都用于画框，PanMode 反过来；两种内置模式都
    不满足需求，所以重写事件分流，只保留一份实现，共用轴与每个分轴子图都注入它。
    """

    sigDoubleClick = QtCore.Signal(float)        # 左键双击处的数据 X -> cursor1
    sigRightDoubleClick = QtCore.Signal(float)   # 右键双击处的数据 X -> cursor2

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setMouseMode(pg.ViewBox.RectMode)   # 左键画框放大
        self._menu_timer = QtCore.QTimer(self)   # 右键菜单延迟：给双击留出判定窗口
        self._menu_timer.setSingleShot(True)
        # 必须 > 系统双击间隔（本机 500ms），否则间隔 220~500ms 的双击会被菜单抢走。
        # 跟随系统值再加余量；单击时菜单晚弹出这么久，感知不明显。
        interval = QtWidgets.QApplication.instance().doubleClickInterval() + 80
        self._menu_timer.setInterval(interval)
        self._pending_menu_ev = None
        self._menu_timer.timeout.connect(self._fire_pending_menu)

    def _fire_pending_menu(self):
        """右键单击（没等到双击）才弹菜单。"""
        ev = self._pending_menu_ev
        self._pending_menu_ev = None
        if ev is not None and self.menuEnabled():
            super().mouseClickEvent(ev)

    def mouseClickEvent(self, ev):
        if ev.button() == QtCore.Qt.MouseButton.RightButton and self.menuEnabled():
            # 延迟弹菜单；双击到达时 mouseDoubleClickEvent 会取消定时器
            self._pending_menu_ev = ev
            self._menu_timer.start()
            ev.accept()
            return
        super().mouseClickEvent(ev)

    def mouseDoubleClickEvent(self, ev):
        """左键双击定位 cursor1、右键双击定位 cursor2；双击不拖动，不会触发框选。"""
        if ev.button() in (QtCore.Qt.MouseButton.LeftButton,
                           QtCore.Qt.MouseButton.RightButton):
            if ev.button() == QtCore.Qt.MouseButton.RightButton:
                self._menu_timer.stop()          # 双击成立：取消待弹的右键菜单
                self._pending_menu_ev = None
            pos = self.childGroup.mapFromParent(ev.pos())   # ViewBox 局部坐标 -> 数据坐标
            (self.sigDoubleClick if ev.button() == QtCore.Qt.MouseButton.LeftButton
             else self.sigRightDoubleClick).emit(float(pos.x()))
            ev.accept()
        else:
            ev.ignore()      # 中键双击不定位；父类无固定期限行为，直接忽略即可

    def wheelEvent(self, ev, axis=None):
        """滚轮不做缩放；忽略事件，交给上层如有需要。"""
        ev.ignore()

    def mouseDragEvent(self, ev, axis=None):
        if ev.button() == QtCore.Qt.MouseButton.MiddleButton:
            # 中键平移：临时切 PanMode 且只放开 X，拖动完立刻还原，Y 量程绝不动
            old_mode = self.state["mouseMode"]
            old_enabled = tuple(self.state["mouseEnabled"])
            self.setMouseMode(pg.ViewBox.PanMode)
            self.setMouseEnabled(x=True, y=False)
            try:
                super().mouseDragEvent(ev, axis=axis)
            finally:
                self.setMouseEnabled(x=old_enabled[0], y=old_enabled[1])
                self.setMouseMode(old_mode)
            return
        super().mouseDragEvent(ev, axis=axis)   # 左键=框选、右键=缩放，走 pyqtgraph 原生


def _dot_icon(color, size=8):
    """状态圆点图标：比文字「命中/未命中」更快被眼睛抓到。"""
    pm = QtGui.QPixmap(size * 2, size * 2)
    pm.fill(QtCore.Qt.GlobalColor.transparent)
    p = QtGui.QPainter(pm)
    try:
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        p.setPen(QtCore.Qt.PenStyle.NoPen)
        p.setBrush(QtGui.QColor(color))
        p.drawEllipse(1, 1, size * 2 - 2, size * 2 - 2)
    finally:
        p.end()
    return QtGui.QIcon(pm)


def _mode_name(mode):
    """Qt 枚举转可序列化名字，避免 json 里出现 <ResizeMode.Stretch: 1>。"""
    return getattr(mode, "name", str(mode).split(".")[-1])


def _fmt_value(v):
    """标尺读数：4 位有效数字，整数不带小数点。只用于显示，不参与计算。"""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(v):
        return "—"
    return "%.4g" % v


# 枚举文字标签：只显示层增强，引擎与规则仍用数值。
# 只收有权威依据的映射；没依据的信号不编标签。
ENUM_LABELS = {
    "GrMod_stDrvAct": {1.0: "P档", 2.0: "R档", 3.0: "N档", 4.0: "D档"},
}


def _enum_suffix(name, v):
    """读数值对应的枚举标签后缀，如 " (D档)"；无映射 / 非有限值返回空串。"""
    try:
        lab = ENUM_LABELS.get(name, {}).get(float(v))
    except (TypeError, ValueError):
        return ""
    return " (%s)" % lab if lab else ""


def _near(vals, want, tol=0.01):
    return (len(vals) == 2 and abs(float(vals[0]) - want[0]) <= tol
            and abs(float(vals[1]) - want[1]) <= tol)


def _header_snapshot(tree):
    """表头现状：列宽模式、实际列宽、标题像素宽、视口宽。"""
    h = tree.header()
    fm = h.fontMetrics()
    return {
        "mode0": _mode_name(h.sectionResizeMode(0)),
        "mode1": _mode_name(h.sectionResizeMode(1)),
        "w0": h.sectionSize(0),
        "w1": h.sectionSize(1),
        "title0_px": fm.horizontalAdvance(tree.headerItem().text(0)),
        "viewport": tree.viewport().width(),
    }


def _center_on_screen(dlg):
    """弹窗统一居中于屏幕。"""
    scr = dlg.screen() or QtWidgets.QApplication.primaryScreen()
    g = scr.availableGeometry()
    dlg.move(g.center() - dlg.rect().center())


class StatsDialog(QtWidgets.QDialog):
    """选中信号的统计信息（全时程，numpy 独立重算）。只读表格。"""

    def __init__(self, rows, parent=None):
        super().__init__(parent)
        self.setWindowTitle("统计信息 · 全时程")
        self.resize(680, 320)
        v = QtWidgets.QVBoxLayout(self)
        t = QtWidgets.QTableWidget(len(rows), 7)
        t.setHorizontalHeaderLabels(["信号", "min", "max", "mean", "std", "NaN 数", "样本数"])
        t.verticalHeader().setVisible(False)
        t.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        for i, r in enumerate(rows):
            vals = [r["name"]]
            vals += [_fmt_value(r[k]) if r[k] is not None else "—"
                     for k in ("min", "max", "mean", "std")]
            vals += [str(r["nan"]), str(r["count"])]
            for c, s in enumerate(vals):
                t.setItem(i, c, QtWidgets.QTableWidgetItem(s))
        t.horizontalHeader().setSectionResizeMode(
            0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        v.addWidget(t)
        box = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Close)
        box.rejected.connect(self.reject)
        box.accepted.connect(self.accept)
        v.addWidget(box)


class PropsDialog(QtWidgets.QDialog):
    """选中信号的属性：单位 / 组 / 索引 / 样本数 / 栅格 / NaN（来自扫描与 quality）。"""

    def __init__(self, rows, parent=None):
        super().__init__(parent)
        self.setWindowTitle("信号属性")
        self.resize(680, 320)
        v = QtWidgets.QVBoxLayout(self)
        t = QtWidgets.QTableWidget(len(rows), 7)
        t.setHorizontalHeaderLabels(["信号", "单位", "组", "索引", "样本数", "栅格中位 ms", "NaN 数"])
        t.verticalHeader().setVisible(False)
        t.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        for i, r in enumerate(rows):
            vals = [r["name"], r["unit"] or "—",
                    "—" if r["group"] is None else str(r["group"]),
                    "—" if r["index"] is None else str(r["index"]),
                    str(r["samples"]),
                    "—" if r["raster_ms"] is None else str(r["raster_ms"]),
                    str(r["nan"])]
            for c, s in enumerate(vals):
                t.setItem(i, c, QtWidgets.QTableWidgetItem(s))
        t.horizontalHeader().setSectionResizeMode(
            0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        v.addWidget(t)
        box = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Close)
        box.rejected.connect(self.reject)
        box.accepted.connect(self.accept)
        v.addWidget(box)


class ExportRangeDialog(QtWidgets.QDialog):
    """CSV 导出范围：全时程 / 当前 X 视野。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("导出选中信号")
        v = QtWidgets.QVBoxLayout(self)
        self.rb_full = QtWidgets.QRadioButton("全时程")
        self.rb_full.setChecked(True)
        self.rb_view = QtWidgets.QRadioButton("当前 X 视野（缩放后的时间段）")
        v.addWidget(self.rb_full)
        v.addWidget(self.rb_view)
        box = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        v.addWidget(box)


class AddSignalsDialog(QtWidgets.QDialog):
    """从尚未勾选的可用信号里多选加入示波器：两列（信号名/描述）+ 搜索 + 全选/清空。"""

    def __init__(self, available, parent=None, desc=None):
        super().__init__(parent)
        self.desc = desc or {}       # 当前文件的描述映射：.dat=JSON / .blf=DBC comment
        self.setWindowTitle("添加信号到示波器")
        self.resize(560, 520)
        v = QtWidgets.QVBoxLayout(self)
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("搜索信号名或描述")
        self.search.setClearButtonEnabled(True)
        v.addWidget(self.search)
        # 两列：信号名 + 中文描述；信号名存 UserRole，选中返回用它，不受显示列影响
        self.list = QtWidgets.QTreeWidget()
        self.list.setHeaderLabels(["信号名", "描述"])
        self.list.setRootIsDecorated(False)
        self.list.setUniformRowHeights(True)
        self.list.header().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.list.header().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Stretch)
        for name in available:
            desc = self.desc.get(name, "")
            it = QtWidgets.QTreeWidgetItem([name, desc])
            it.setData(0, QtCore.Qt.ItemDataRole.UserRole, name)
            it.setToolTip(1, desc)          # 描述列被截断时悬停显示完整描述
            it.setToolTip(0, name)          # 信号名列同理（超长名）
            it.setFlags(it.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(0, QtCore.Qt.CheckState.Unchecked)
            self.list.addTopLevelItem(it)
        v.addWidget(self.list, 1)
        row = QtWidgets.QHBoxLayout()
        b_all = QtWidgets.QPushButton("全选（含筛选）")
        b_none = QtWidgets.QPushButton("清空")
        row.addWidget(b_all)
        row.addWidget(b_none)
        row.addStretch(1)
        v.addLayout(row)
        box = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        v.addWidget(box)
        self.search.textChanged.connect(self._filter)
        b_all.clicked.connect(self._select_all)
        b_none.clicked.connect(self._clear_all)

    def _filter(self, text):
        key = (text or "").lower()
        for i in range(self.list.topLevelItemCount()):
            it = self.list.topLevelItem(i)
            # 同时匹配信号名与描述：搜中文也能定位到信号
            it.setHidden(bool(key) and key not in (it.text(0) + " " + it.text(1)).lower())

    def _select_all(self):
        for i in range(self.list.topLevelItemCount()):
            it = self.list.topLevelItem(i)
            if not it.isHidden():
                it.setCheckState(0, QtCore.Qt.CheckState.Checked)

    def _clear_all(self):
        for i in range(self.list.topLevelItemCount()):
            self.list.topLevelItem(i).setCheckState(0, QtCore.Qt.CheckState.Unchecked)

    def selected_names(self):
        return [self.list.topLevelItem(i).data(0, QtCore.Qt.ItemDataRole.UserRole)
                for i in range(self.list.topLevelItemCount())
                if self.list.topLevelItem(i).checkState(0) == QtCore.Qt.CheckState.Checked]


class MultiRulesDialog(QtWidgets.QDialog):
    """多选规则集：勾选多个 ruleset 一起评估。"""

    def __init__(self, items, checked_paths, parent=None):
        """items: [(显示名, 路径)]；checked_paths: 当前已选路径集合。"""
        super().__init__(parent)
        self.setWindowTitle("选择规则集（可多选）")
        self.resize(720, 460)
        v = QtWidgets.QVBoxLayout(self)
        v.addWidget(QtWidgets.QLabel("勾选要一起评估的规则集，可多选；右侧显示所选规则集的备注："))
        self._doc_cache = {}        # 路径 -> 规则集 doc（note + 子规则），读一次缓存
        self.note = QtWidgets.QTextBrowser()
        self.note.setPlaceholderText("点选一个规则集，这里显示它的备注（note）。")
        self.note.setOpenExternalLinks(False)

        self.list = QtWidgets.QListWidget()
        for display, path in items:
            it = QtWidgets.QListWidgetItem(display)
            it.setData(QtCore.Qt.ItemDataRole.UserRole, path)
            it.setData(QtCore.Qt.ItemDataRole.UserRole + 1, display)
            it.setFlags(it.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(QtCore.Qt.CheckState.Checked
                             if path in checked_paths else QtCore.Qt.CheckState.Unchecked)
            self.list.addItem(it)
        # 左右分栏：清单 + 备注
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.list, 1)
        row.addWidget(self.note, 1)
        v.addLayout(row, 1)
        self.list.currentItemChanged.connect(self._show_note)
        self.list.itemChanged.connect(self._on_check_changed)   # 勾选时也刷新备注
        # 初始显示第一个已勾选项的备注
        first = next((self.list.item(i) for i in range(self.list.count())
                      if self.list.item(i).checkState() == QtCore.Qt.CheckState.Checked), None)
        if first is not None:
            self.list.setCurrentItem(first)

        box = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        v.addWidget(box)

    def _doc_of(self, path):
        if path not in self._doc_cache:
            try:
                self._doc_cache[path] = caliper_rules.load_ruleset_doc(path)
            except (OSError, ValueError):
                self._doc_cache[path] = {}
        return self._doc_cache[path]

    def _show_note(self, current, _previous=None):
        """右侧详情：规则集名 + 备注 + 子规则列表（名字 / 条件 / 时长参数）。"""
        if current is None:
            self.note.clear()
            return
        path = current.data(QtCore.Qt.ItemDataRole.UserRole)
        display = current.data(QtCore.Qt.ItemDataRole.UserRole + 1) or current.text()
        doc = self._doc_of(path)
        esc = html.escape
        text = esc((doc.get("note") or "").strip())
        parts = ["<b>%s</b>" % esc(display), "<br><br>",
                 text or "（该规则集没有备注）"]
        rules = doc.get("rules") or []
        parts.append("<br><br><b>子规则（%d 条）：</b>" % len(rules))
        if not rules:
            parts.append("<br>（该规则集没有子规则）")
        for i, r in enumerate(rules, 1):
            name = esc(str(r.get("name", "")))
            when = esc(str(r.get("when", "")))   # when 含 > < 等，必须转义
            dur = esc("%s/%s/%s ms" % (r.get("min_duration_ms", "—"),
                                       r.get("hysteresis_ms", "—"),
                                       r.get("merge_gap_ms", "—")))
            parts.append("<br>%d. <b>%s</b>" % (i, name))
            parts.append("<br>&nbsp;&nbsp;&nbsp;条件：%s" % when)
            parts.append("<br>&nbsp;&nbsp;&nbsp;时长/回差/合并：%s" % dur)
        self.note.setText("".join(parts))

    def _on_check_changed(self, item):
        # 勾选动作不改变当前项时，仍确保备注显示的是它
        if self.list.currentItem() is item:
            self._show_note(item)

    def selected_paths(self):
        return [self.list.item(i).data(QtCore.Qt.ItemDataRole.UserRole)
                for i in range(self.list.count())
                if self.list.item(i).checkState() == QtCore.Qt.CheckState.Checked]


class CurveStyleDialog(QtWidgets.QDialog):
    """修改曲线：连线类型（折线/阶梯/散点）、线宽、颜色；可恢复默认。"""

    KINDS = ["line", "step", "scatter"]

    def __init__(self, cur, parent=None):
        super().__init__(parent)
        self.setWindowTitle("修改曲线")
        self._reset = False
        self._color = cur.get("color")
        form = QtWidgets.QFormLayout(self)
        self.cmb_kind = QtWidgets.QComboBox()
        self.cmb_kind.addItems(["折线", "阶梯", "散点"])
        self.cmb_kind.setCurrentIndex(self.KINDS.index(cur.get("kind", "line")))
        form.addRow("连线类型", self.cmb_kind)
        self.spin_width = QtWidgets.QDoubleSpinBox()
        self.spin_width.setRange(0.5, 8.0)
        self.spin_width.setSingleStep(0.2)
        self.spin_width.setDecimals(1)
        self.spin_width.setValue(float(cur.get("width", 1.8)))
        form.addRow("线宽", self.spin_width)
        self.btn_color = QtWidgets.QPushButton(self._color or "默认（跟随色板）")
        self.btn_color.clicked.connect(self._pick_color)
        form.addRow("颜色", self.btn_color)
        bar = QtWidgets.QHBoxLayout()
        self.btn_reset = QtWidgets.QPushButton("恢复默认")
        self.btn_reset.clicked.connect(self._do_reset)
        bar.addWidget(self.btn_reset)
        bar.addStretch(1)
        form.addRow(bar)
        box = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        form.addRow(box)

    def _pick_color(self):
        cd = QtWidgets.QColorDialog(QtGui.QColor(self._color or "#2F6FED"), self)
        cd.setWindowTitle("选择曲线颜色")
        _center_on_screen(cd)
        if cd.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            c = cd.selectedColor()
            if c.isValid():
                self._color = c.name()
                self.btn_color.setText(self._color)

    def _do_reset(self):
        self._reset = True
        self.accept()

    def values(self):
        if self._reset:
            return None
        out = {"kind": self.KINDS[self.cmb_kind.currentIndex()],
               "width": self.spin_width.value()}
        if self._color:
            out["color"] = self._color
        return out


class BlfScanWorker(QtCore.QThread):
    """后台单遍扫描 BLF（顺带收集原始报文）+ 自适应归属，建常驻会话。

    471MB 级文件不冻界面；扫描完成后勾选信号只从会话提取，不再重扫文件。
    """
    progress = QtCore.Signal(int, int)
    finished_ok = QtCore.Signal(object)
    failed = QtCore.Signal(str)

    def __init__(self, path, dbc_paths, priority, parent=None):
        super().__init__(parent)
        self.path, self.dbc_paths, self.priority = path, dbc_paths, priority
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        try:
            def cb(n, total):
                if self._cancel:
                    raise RuntimeError("cancelled")
                self.progress.emit(n, total or 0)

            sess = caliper_core.BlfSession(self.path, dbc_paths=self.dbc_paths,
                                       channel_priority=self.priority, progress=cb)
            self.finished_ok.emit(sess.entry())
        except Exception as e:
            self.failed.emit(str(e))


class BlfLoadWorker(QtCore.QThread):
    """后台从会话缓存提取 wanted 报文 -> Measurement（零重扫）。"""
    progress = QtCore.Signal(int, int)
    finished_ok = QtCore.Signal(object)
    failed = QtCore.Signal(str)

    def __init__(self, session, wanted, parent=None):
        super().__init__(parent)
        self.session, self.wanted = session, wanted
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        try:
            if self._cancel:
                raise RuntimeError("cancelled")
            m = self.session.load(self.wanted)
            self.finished_ok.emit(m)
        except Exception as e:
            self.failed.emit(str(e))


class BlfFullLoadWorker(QtCore.QThread):
    """无会话时的兜底：完整单遍加载（扫描+收集+提取），语义与 load_blf 一致。"""
    progress = QtCore.Signal(int, int)
    finished_ok = QtCore.Signal(object)
    failed = QtCore.Signal(str)

    def __init__(self, path, wanted, dbc_paths, priority, parent=None):
        super().__init__(parent)
        self.path, self.wanted = path, wanted
        self.dbc_paths, self.priority = dbc_paths, priority
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        try:
            def cb(n, total):
                if self._cancel:
                    raise RuntimeError("cancelled")
                self.progress.emit(n, total or 0)

            m = caliper_core.load_blf(self.path, signals=self.wanted,
                                  dbc_paths=self.dbc_paths,
                                  channel_priority=self.priority, progress=cb)
            self.finished_ok.emit(m)
        except Exception as e:
            self.failed.emit(str(e))


class BlfMapDialog(QtWidgets.QDialog):
    """通道 -> 主 DBC 映射（自适应结果可人工覆盖）+ 重选 DBC 集合。"""

    def __init__(self, channels, priority, dbc_keys, parent=None):
        super().__init__(parent)
        self.setWindowTitle("BLF 通道映射")
        self.setMinimumWidth(560)
        v = QtWidgets.QVBoxLayout(self)
        hint = QtWidgets.QLabel("默认按报文自适应归属（唯一 ID 帧数主导）。"
                                "这里可人工指定每通道的主 DBC；共有 ID 按主 DBC 优先解析。")
        hint.setObjectName("DialogHint")
        hint.setWordWrap(True)
        v.addWidget(hint)
        self.combos = {}
        self.channels = list(channels)
        self.dbc_keys = list(dbc_keys)      # 当前 DBC 名集合（重选后会更新）
        self.priority = dict(priority or {})
        self.form = QtWidgets.QFormLayout()
        self._fill_combos()                 # 首次建下拉框；重选 DBC 后复用同一逻辑重填
        v.addLayout(self.form)
        self.btn_reselect = QtWidgets.QPushButton("重选 DBC 集合…")
        v.addWidget(self.btn_reselect)
        box = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        v.addWidget(box)
        self.new_dbc_paths = None
        self.btn_reselect.clicked.connect(self._reselect)

    def _fill_combos(self):
        """按当前 dbc_keys 建/重建每个通道的下拉框；旧 priority 值若不在新集合里落到第一项。"""
        for ch in self.channels:
            cmb = self.combos.get(ch)
            if cmb is None:                 # 首次：新建并加入布局
                cmb = QtWidgets.QComboBox()
                self.combos[ch] = cmb
                self.form.addRow("通道 %s 主 DBC" % ch, cmb)
            cmb.blockSignals(True)
            cmb.clear()
            cmb.addItems(self.dbc_keys)
            cur = (self.priority.get(str(ch)) or self.priority.get(ch) or [None])[0]
            if cur in self.dbc_keys:
                cmb.setCurrentText(cur)
            cmb.blockSignals(False)

    def _reselect(self):
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self, "选择 DBC（可多选）", "", "DBC (*.dbc)")
        if not paths:
            return
        self.new_dbc_paths = paths
        # 关键：下拉框选项必须跟着新 DBC 集合刷新，否则确定后写回的还是旧 DBC 名
        self.dbc_keys = [os.path.splitext(os.path.basename(p))[0] for p in paths]
        self._fill_combos()

    def priority_override(self):
        # 只写回当前（可能已重选）集合里存在的名字，杜绝旧 DBC 名残留
        return {str(ch): [cmb.currentText()] for ch, cmb in self.combos.items()
                if cmb.currentText() in self.dbc_keys}


class YRangeDialog(QtWidgets.QDialog):
    """手动设定 Y 轴上下限。只为「看得清」，不参与任何数据计算。"""

    def __init__(self, parent=None, lo=-1.0, hi=1.0):
        super().__init__(parent)
        self.setWindowTitle("修改 Y 轴量程")
        form = QtWidgets.QFormLayout(self)
        self.spin_lo = QtWidgets.QDoubleSpinBox()
        self.spin_hi = QtWidgets.QDoubleSpinBox()
        for spin in (self.spin_lo, self.spin_hi):
            spin.setDecimals(4)
            spin.setRange(-1e9, 1e9)
            spin.setSingleStep(1.0)
        self.spin_lo.setValue(float(lo))
        self.spin_hi.setValue(float(hi))
        form.addRow("下限", self.spin_lo)
        form.addRow("上限", self.spin_hi)
        box = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        form.addRow(box)

    def values(self):
        return self.spin_lo.value(), self.spin_hi.value()


class SignalPicker(QtWidgets.QWidget):
    """信号选择框：点文本区即弹两列（信号名/描述）搜索面板，也可直接输入信号名。

    用 QWidget 复合而非自定义 QComboBox 下拉——全局样式把 combo 的 drop-down 压成
    20px 又不画图标，箭头根本看不见。实测点文本区弹面板就够用，不再放多余的下拉按钮。
    跳变条件与普通条件用同一个类，样式同步。
    """

    editTextChanged = QtCore.Signal(str)   # 兼容原 QComboBox 接口（条件行连它刷预览）

    def __init__(self, signals=None, desc=None, parent=None):
        super().__init__(parent)
        self.desc = dict(desc or {})
        self._popup = None
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self._edit = QtWidgets.QLineEdit()
        self._edit.setPlaceholderText("点击选择或输入信号名")
        self._edit.setToolTip("点击搜索信号（信号名或中文描述），也可直接输入")
        self._edit.textChanged.connect(self.editTextChanged.emit)
        self._edit.installEventFilter(self)
        lay.addWidget(self._edit, 1)
        self.setSignals(signals or [], self.desc)

    # ---- 兼容 QComboBox 的接口，调用方/验收无需改 ----
    def lineEdit(self):
        return self._edit

    def currentText(self):
        return self._edit.text()

    def setEditText(self, text):
        self._edit.setText(str(text))

    def findText(self, text):
        return next((i for i, (n, _d) in enumerate(self._items) if n == text), -1)

    def setCurrentIndex(self, idx):
        if 0 <= idx < len(self._items):
            self.setEditText(self._items[idx][0])

    def eventFilter(self, obj, ev):
        if (obj is self._edit
                and ev.type() == QtCore.QEvent.Type.MouseButtonPress
                and self._items):
            self.showPopup()
            return True   # 拦下点击，让面板搜索框抢到焦点
        return super().eventFilter(obj, ev)

    def setSignals(self, signals, desc=None):
        if desc is not None:
            self.desc = dict(desc)
        self._items = [(n, self.desc.get(n, "")) for n in signals]

    def showPopup(self):
        """弹搜索面板：顶部搜索框 + 两列信号表。"""
        if self._popup is None:
            self._popup = _SignalPickerPopup(self)
        self._popup.refresh(self._items, self.currentText())
        self._popup.show()
        self._popup.raise_()
        self._popup.activateWindow()
        if self._popup.search is not None:
            self._popup.search.setFocus()

    def hidePopup(self):
        if self._popup is not None and self._popup.isVisible():
            self._popup.hide()


class _SignalPickerPopup(QtWidgets.QFrame):
    """SignalPicker 的下拉面板：搜索框 + 两列信号表（无边框弹出窗）。"""

    def __init__(self, picker):
        super().__init__(picker, QtCore.Qt.WindowType.Popup | QtCore.Qt.WindowType.FramelessWindowHint)
        self.picker = picker
        self.setObjectName("SignalPickerPopup")
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(4)
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("搜索信号名或描述")
        self.search.setClearButtonEnabled(True)
        lay.addWidget(self.search)
        self.table = QtWidgets.QTreeWidget()
        self.table.setHeaderLabels(["信号名", "描述"])
        self.table.setRootIsDecorated(False)
        self.table.setUniformRowHeights(True)
        hdr = self.table.header()
        hdr.setFixedHeight(28)             # 表头给足高度，文字不被上下裁切
        hdr.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 230)  # 信号名列够宽，长信号名不被描述挤掉
        lay.addWidget(self.table, 1)
        self.search.textChanged.connect(self._filter)
        self.search.returnPressed.connect(self._on_enter)
        self.table.itemClicked.connect(self._pick)

    def _on_enter(self):
        """回车：优先选中当前高亮/第一个可见项；若输入的是精确信号名，用它（可手输不存在的名）。"""
        key = self.search.text().strip()
        for i in range(self.table.topLevelItemCount()):
            it = self.table.topLevelItem(i)
            if it.text(0).lower() == key.lower():       # 精确信号名：直接选中
                self._pick(it, 0)
                return
        for i in range(self.table.topLevelItemCount()):
            it = self.table.topLevelItem(i)
            if not it.isHidden():                        # 中文描述搜索：选第一个命中项
                self._pick(it, 0)
                return
        if key:                                          # 搜不到：当作手输信号名回显
            self.picker.setEditText(key)
            self.picker.editTextChanged.emit(self.picker.currentText())
            self.hide()
            self.picker.hidePopup()

    def refresh(self, items, current):
        self.search.clear()
        self.table.clear()
        for name, desc in items:
            it = QtWidgets.QTreeWidgetItem([name, desc])
            it.setData(0, USER_ROLE, name)
            it.setToolTip(1, desc)          # 描述列被列宽截断时，悬停显示完整描述
            it.setToolTip(0, name)          # 信号名同理（可能有超长名）
            self.table.addTopLevelItem(it)
        # 定位当前值，方便直接看到/改选
        for i in range(self.table.topLevelItemCount()):
            if self.table.topLevelItem(i).text(0) == current:
                self.table.setCurrentItem(self.table.topLevelItem(i))
                break
        self.resize(max(self.picker.width(), 480), 360)   # 够宽，避免信号名/描述两列互相挤掉文字
        # 贴到 picker 下方，屏幕边缘自动翻转
        pos = self.picker.mapToGlobal(QtCore.QPoint(0, self.picker.height()))
        screen = self.screen() or QtWidgets.QApplication.primaryScreen()
        area = screen.availableGeometry()
        if pos.x() + self.width() > area.right():
            pos.setX(area.right() - self.width())
        if pos.y() + self.height() > area.bottom():
            pos.setY(self.picker.mapToGlobal(QtCore.QPoint(0, 0)).y() - self.height())
        self.move(pos)

    def _filter(self, text):
        key = (text or "").lower()
        for i in range(self.table.topLevelItemCount()):
            it = self.table.topLevelItem(i)
            it.setHidden(bool(key) and key not in (it.text(0) + " " + it.text(1)).lower())

    def _pick(self, item, _col):
        self.picker.setEditText(item.data(0, USER_ROLE))   # 回显信号名，不带描述
        self.picker.editTextChanged.emit(self.picker.currentText())
        self.hide()
        self.picker.hidePopup()


class ConditionDialog(QtWidgets.QDialog):
    """一条规则的条件编辑：统一表格，每行选类型（定值/跳变/波动/变化次数）。

    行间用「任一(OR)/全部(AND)」连接。四种条件共用信号选择器（SignalPicker），
    判据区随类型切换。无高级表达式模式——复杂逻辑由多条件行 + 任一/全部表达。
    保留 when()/transition()/window_condition() 兼容接口供既有调用方读取。
    """

    TYPE_LABELS = ("定值", "跳变", "波动幅度", "变化次数")
    TYPE_KEYS = ("value", "change", "swing", "changes")

    def __init__(self, when="", rule_name="", signals=None, parent=None, transition=None,
                 desc=None, window=None, conditions=None, join=None):
        super().__init__(parent)
        self.setWindowTitle("编辑条件 · %s" % (rule_name or "新规则"))
        self.resize(900, 520)
        self.signals = list(signals or [])
        self.desc = dict(desc or {})
        self._updating = False

        # 初始条件：优先新格式 conditions；否则从旧字段转换
        if isinstance(conditions, list) and conditions:
            self.rows = [dict(c) for c in conditions]
            self.join_value_str = "any" if str(join).lower() == "any" else "all"
        else:
            conds, j = caliper_rules.normalize_conditions({
                "when": when, "transition": transition, "window": window})
            self.rows = [dict(c) for c in conds] or [
                {"type": "value", "signal": (self.signals[0] if self.signals else ""),
                 "op": "==", "threshold": 0}]
            self.join_value_str = j
        self._restyle_targets = []

        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(16, 14, 16, 14)
        v.setSpacing(10)
        hint = QtWidgets.QLabel("每行一个条件，选类型后填信号和判据；"
                                "行与行之间按「任一」或「全部」组合。")
        hint.setObjectName("DialogHint")
        hint.setWordWrap(True)
        v.addWidget(hint)

        # 顶部：连接符 + 消息
        top = QtWidgets.QHBoxLayout()
        top.addWidget(QtWidgets.QLabel("以下条件"))
        self.cmb_join = QtWidgets.QComboBox()
        self.cmb_join.addItems(["任一（满足其一）", "全部（都满足）"])
        self.cmb_join.setCurrentIndex(0 if self.join_value_str == "any" else 1)
        self.cmb_join.currentIndexChanged.connect(self._on_join_changed)
        top.addWidget(self.cmb_join)
        top.addWidget(QtWidgets.QLabel("满足"))
        top.addStretch(1)
        self.msg = QtWidgets.QLabel("")
        self.msg.setObjectName("Ok")
        top.addWidget(self.msg)
        v.addLayout(top)

        # 统一条件表格
        self.table = QtWidgets.QTableWidget()
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels(["条件类型", "信号", "判据"])
        # 复合控件塞进 cellWidget 后 ResizeToContents 算不准（中文标签/多输入框被截断），
        # 显式给类型和判据列固定宽度，信号列吃掉剩余空间。
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Fixed)
        hdr.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(0, 112)     # "变化次数" + 下拉箭头
        self.table.setColumnWidth(2, 470)     # 变化次数行：三组等宽字段 + 等距间隔
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        v.addWidget(self.table, 1)

        bar = QtWidgets.QHBoxLayout()
        btn_add = QtWidgets.QPushButton("+ 条件")
        btn_add.clicked.connect(self.add_row)
        bar.addWidget(btn_add)
        btn_del = QtWidgets.QPushButton("删除选中")
        btn_del.clicked.connect(self.del_selected)
        bar.addWidget(btn_del)
        bar.addStretch(1)
        v.addLayout(bar)

        btns = QtWidgets.QHBoxLayout()
        btns.addStretch(1)
        self.btn_cancel = QtWidgets.QPushButton("取消")
        self.btn_cancel.clicked.connect(self.reject)
        btns.addWidget(self.btn_cancel)
        self.btn_ok = QtWidgets.QPushButton("确定")
        self.btn_ok.setProperty("kind", "primary")
        self.btn_ok.clicked.connect(self.accept)
        btns.addWidget(self.btn_ok)
        v.addLayout(btns)

        for _ in self.rows:
            self._append_row()
        self._validate()

    # ---- 表格行构建 ----
    def _append_row(self):
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setRowHeight(row, 40)
        self._updating = True        # 建控件时的 setCurrentIndex 不能触发重置 rows
        # 类型下拉
        cmb = QtWidgets.QComboBox()
        cmb.addItems(self.TYPE_LABELS)
        cmb.setFixedWidth(104)           # 跟类型列宽配套，保证四个中文字显示全
        idx = self.TYPE_KEYS.index(self.rows[row].get("type", "value")) \
            if self.rows[row].get("type", "value") in self.TYPE_KEYS else 0
        cmb.setCurrentIndex(idx)
        cmb.currentIndexChanged.connect(lambda vv, r=row: self._on_type_changed(r, vv))
        self.table.setCellWidget(row, 0, cmb)
        # 信号选择器
        pick = SignalPicker(self.signals, self.desc)
        sig = self.rows[row].get("signal", "")
        if sig:
            pick.setEditText(str(sig))
        pick.editTextChanged.connect(lambda _t, r=row: self._on_field_changed(r))
        self.table.setCellWidget(row, 1, pick)
        # 判据区（占位，下面重建）
        self._build_criteria(row)
        self._updating = False       # 恢复，后续用户填值要能触发同步

    @staticmethod
    def _field(label_text, widget, width=84):
        """一个「标签+控件」字段：标签等宽、控件等宽，字段内间距固定。"""
        box = QtWidgets.QWidget()
        fl = QtWidgets.QHBoxLayout(box)
        fl.setContentsMargins(0, 0, 0, 0)
        fl.setSpacing(5)
        lab = QtWidgets.QLabel(label_text)
        lab.setFixedWidth(50)
        fl.addWidget(lab)
        widget.setFixedWidth(width)
        fl.addWidget(widget)
        return box

    def _build_criteria(self, row):
        """按当前类型重建第 row 行的判据控件，并从 self.rows 回填原值。

        所有字段走 _field（标签等宽、控件等宽），字段间等距、末尾 stretch，
        保证四种类型的判据左对齐、间隔一致，不长长短短。
        """
        host = QtWidgets.QWidget()
        lay = QtWidgets.QHBoxLayout(host)
        lay.setContentsMargins(6, 0, 6, 0)
        lay.setSpacing(14)
        c = self.rows[row]
        t = c.get("type", "value")

        def edit(text, placeholder="", width=84):
            e = QtWidgets.QLineEdit()
            e.setText(str(text) if text is not None else "")
            if placeholder:
                e.setPlaceholderText(placeholder)
            e.textChanged.connect(lambda _t, r=row: self._on_field_changed(r))
            return e

        if t == "value" and c.get("expr"):
            # 旧规则里的复杂表达式：无高级编辑界面，但必须原样保留（只读展示）
            ro = QtWidgets.QLineEdit(str(c["expr"]))
            ro.setReadOnly(True)
            ro.setToolTip("复杂表达式，保留原样；用表格条件表达不了")
            lay.addWidget(ro)
        elif t == "value":
            op = QtWidgets.QComboBox()
            op.addItems(list(caliper_rules.CMP_OPS))
            op.setCurrentIndex(max(0, list(caliper_rules.CMP_OPS).index(c.get("op", "=="))))
            op.currentIndexChanged.connect(lambda _v, r=row: self._on_field_changed(r))
            lay.addWidget(self._field("比较", op))
            lay.addWidget(self._field("阈值", edit(c.get("threshold", 0))))
        elif t == "change":
            lay.addWidget(self._field("从", edit(c.get("from", ""))))
            lay.addWidget(self._field("变为", edit(c.get("to", ""))))
        else:  # swing / changes
            lay.addWidget(self._field("窗口ms", edit(c.get("window_ms", ""))))
            lay.addWidget(self._field("阈值", edit(c.get("threshold", ""))))
            if t == "changes":
                lay.addWidget(self._field("最小变化", edit(c.get("min_step", ""),
                                                        "可空")))
        lay.addStretch(1)
        self.table.setCellWidget(row, 2, host)

    # ---- 交互 ----
    def _on_type_changed(self, row, idx):
        if self._updating:
            return
        t = self.TYPE_KEYS[idx]
        self.rows[row] = {"type": t, "signal": self._signal_at(row)}
        self._build_criteria(row)
        self._validate()

    def _signal_at(self, row):
        w = self.table.cellWidget(row, 1)
        return w.currentText().strip() if w is not None else ""

    def _on_field_changed(self, row):
        if self._updating:
            return
        self._sync_row(row)
        self._validate()

    def _sync_row(self, row):
        """把第 row 行控件的值写回 self.rows[row]。"""
        if row >= len(self.rows):
            return
        c = self.rows[row]
        c["signal"] = self._signal_at(row)
        host = self.table.cellWidget(row, 2)
        if host is None:
            return
        edits = host.findChildren(QtWidgets.QLineEdit)
        combos = host.findChildren(QtWidgets.QComboBox)
        if c.get("type") == "value" and c.get("expr"):
            return                       # 复杂表达式原样保留，不被控件值覆盖
        if c.get("type") == "value":
            if combos:
                c["op"] = combos[0].currentText()
            if edits:
                c["threshold"] = edits[0].text()
        elif c.get("type") == "change":
            if len(edits) >= 2:
                c["from"], c["to"] = edits[0].text(), edits[1].text()
        else:
            if edits:
                c["window_ms"] = edits[0].text()
            if len(edits) >= 2:
                c["threshold"] = edits[1].text()
            if c.get("type") == "changes" and len(edits) >= 3:
                c["min_step"] = edits[2].text() or None

    def _on_join_changed(self, _idx):
        self.join_value_str = "any" if self.cmb_join.currentIndex() == 0 else "all"
        self._validate()

    def add_row(self):
        self._sync_all()
        self.rows.append({"type": "value",
                          "signal": (self.signals[0] if self.signals else ""),
                          "op": "==", "threshold": 0})
        self._append_row()
        self._validate()

    def del_selected(self):
        rows = sorted({idx.row() for idx in self.table.selectedIndexes()}, reverse=True)
        if not rows:
            return
        self._sync_all()
        for r in rows:
            self.table.removeRow(r)
            if r < len(self.rows):
                self.rows.pop(r)
        self._validate()

    def _sync_all(self):
        for r in range(self.table.rowCount()):
            self._sync_row(r)

    def _conditions_out(self):
        """导出规范化的 conditions（数值字段转 float，非法留 None 让校验报错）。"""
        self._sync_all()
        out = []
        for c in self.rows:
            cc = {"type": c.get("type", "value")}
            if cc["type"] == "value" and c.get("expr"):
                cc["expr"] = str(c["expr"])      # 复杂表达式原样保留
            elif cc["type"] == "value":
                cc["signal"] = str(c.get("signal") or "").strip()
                cc["op"] = c.get("op", "==")
                try:
                    cc["threshold"] = float(c.get("threshold"))
                except (TypeError, ValueError):
                    cc["threshold"] = None
            elif cc["type"] == "change":
                cc["signal"] = str(c.get("signal") or "").strip()
                for k in ("from", "to"):
                    try:
                        cc[k] = float(c.get(k))
                    except (TypeError, ValueError):
                        cc[k] = None
            else:
                cc["signal"] = str(c.get("signal") or "").strip()
                for k in ("window_ms", "threshold"):
                    try:
                        cc[k] = float(c.get(k))
                    except (TypeError, ValueError):
                        cc[k] = None
                if cc["type"] == "changes" and c.get("min_step") not in (None, ""):
                    try:
                        cc["min_step"] = float(c["min_step"])
                    except (TypeError, ValueError):
                        cc["min_step"] = -1
            out.append(cc)
        return out

    # ---- 兼容接口：供 RuleSetDialog 现有调用读取 ----
    def conditions_data(self):
        return self._conditions_out()

    def join_value(self):
        return self.join_value_str

    def when(self):
        """从条件行拼出 when 表达式（仅 value 类；复杂场景由 conditions 承载）。"""
        simple, parts = [], []
        for c in self._conditions_out():
            if c["type"] != "value":
                continue
            if c.get("expr"):
                parts.append(c["expr"])
            elif c.get("signal") and c.get("threshold") is not None:
                simple.append((c["signal"], c["op"], c["threshold"]))
        joiner = "&" if self.join_value_str == "all" else "|"
        text = caliper_rules.conds_to_when(simple, joiner)
        all_parts = ([text] if text else []) + parts
        return ((" %s " % ("&" if joiner == "&" else "|")).join(
            "(%s)" % p if " " in p and not p.startswith("(") else p for p in all_parts))

    def transition(self):
        for c in self._conditions_out():
            if c["type"] == "change":
                return {"signal": c.get("signal"), "from": c.get("from"), "to": c.get("to")}
        return None

    def window_condition(self):
        for c in self._conditions_out():
            if c["type"] in ("swing", "changes"):
                w = {"signal": c.get("signal"), "kind": c["type"],
                     "window_ms": c.get("window_ms"), "threshold": c.get("threshold")}
                if c.get("min_step") is not None:
                    w["min_step"] = c["min_step"]
                return w
        return None

    def _validate(self, *args):
        errs = []
        caliper_rules.validate_conditions(self._conditions_out(), self.join_value_str,
                                      "条件", errs)
        missing = [c.get("signal") for c in self._conditions_out()
                   if c.get("signal") and self.signals and c["signal"] not in self.signals]
        if errs:
            self.msg.setObjectName("Bad")
            self.msg.setText(errs[0])
            self.btn_ok.setEnabled(False)
        elif missing:
            self.msg.setObjectName("Warn")
            self.msg.setText("本文件没有 %s——这条规则会显示「未评估」" % "、".join(set(missing)))
            self.btn_ok.setEnabled(True)
        else:
            self.msg.setObjectName("Ok")
            self.msg.setText("✓ 条件可用")
            self.btn_ok.setEnabled(True)
        self._restyle(self.msg)

    def _restyle(self, widget):
        try:
            style = {
                "Ok": "color: #1D9E75;",
                "Warn": "color: #BA7517;",
                "Bad": "color: #A32D2D;",
            }.get(widget.objectName(), "")
            widget.setStyleSheet(style)
        except Exception:
            pass

    def accept(self):
        self._sync_all()
        errs = []
        caliper_rules.validate_conditions(self._conditions_out(), self.join_value_str,
                                      "条件", errs)
        if errs:
            self._validate()
            return
        super().accept()


class RuleSetDialog(QtWidgets.QDialog):
    """规则集编辑窗口：表格改规则，改完自动写回 JSON（自动备份，版本号 +1）。"""

    COLS = ("规则名", "条件", "最短持续 ms", "滞回 ms", "合并间隔 ms", "标记")
    COL_NAME, COL_WHEN, COL_MIN, COL_HYST, COL_GAP, COL_MARK = range(6)

    def __init__(self, path, signals=None, parent=None, focus=None, desc=None):
        super().__init__(parent)
        self.path = path
        self.signals = list(signals or [])
        self.desc = dict(desc or {})   # 信号名 -> 描述，传给 ConditionDialog 的 SignalPicker
        self.doc = caliper_rules.load_ruleset_doc(path)
        if not isinstance(self.doc.get("rules"), list):
            # rules/ 下除了规则集还有「待补清单」之类文件，误编辑一次就会毁掉它的内容
            raise ValueError("「%s」里没有 rules 列表，不是规则集，不能在这里改"
                             % os.path.basename(path))
        self.saved_version = int(self.doc.get("version", 1))
        self.setWindowTitle("编辑规则 · %s" % os.path.basename(path))
        self.resize(1020, 640)
        self._build()
        self._load_table()
        if focus:
            self.select_rule(focus)

    def _build(self):
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(16, 14, 16, 14)
        v.setSpacing(10)

        head = QtWidgets.QHBoxLayout()
        head.setSpacing(10)
        head.addWidget(QtWidgets.QLabel("规则集名称"))
        self.ed_name = QtWidgets.QLineEdit(str(self.doc.get("name") or ""))
        head.addWidget(self.ed_name, 1)
        self.lb_ver = QtWidgets.QLabel("版本 %d" % self.saved_version)
        self.lb_ver.setObjectName("DialogHint")
        head.addWidget(self.lb_ver)
        v.addLayout(head)

        note = QtWidgets.QLabel("备注")
        note.setObjectName("DialogHint")
        v.addWidget(note)
        self.ed_note = QtWidgets.QLineEdit(str(self.doc.get("note") or ""))
        v.addWidget(self.ed_note)

        hint = QtWidgets.QLabel(
            "改完点「保存并应用」直接写回 JSON——不用手改文件。旧版自动备份为同名 .bak，"
            "版本号自动 +1，界面立刻按新规则重算。双击「条件」列编辑条件。")
        hint.setObjectName("DialogHint")
        hint.setWordWrap(True)
        v.addWidget(hint)

        self.table = QtWidgets.QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(list(self.COLS))
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(
            QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(
            QtWidgets.QAbstractItemView.EditTrigger.DoubleClicked
            | QtWidgets.QAbstractItemView.EditTrigger.SelectedClicked
            | QtWidgets.QAbstractItemView.EditTrigger.EditKeyPressed)
        self.table.cellDoubleClicked.connect(self._on_double_click)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(self.COL_NAME, QtWidgets.QHeaderView.ResizeMode.Fixed)
        hh.setSectionResizeMode(self.COL_WHEN, QtWidgets.QHeaderView.ResizeMode.Stretch)
        for c in (self.COL_MIN, self.COL_HYST, self.COL_GAP):
            hh.setSectionResizeMode(c, QtWidgets.QHeaderView.ResizeMode.Fixed)
            self.table.setColumnWidth(c, 104)
        hh.setSectionResizeMode(self.COL_MARK, QtWidgets.QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(self.COL_NAME, 170)
        self.table.setColumnWidth(self.COL_MARK, 88)
        v.addWidget(self.table, 1)

        bar = QtWidgets.QHBoxLayout()
        bar.setSpacing(8)
        self.btn_add = QtWidgets.QPushButton("新增规则")
        self.btn_add.clicked.connect(self.add_rule)
        self.btn_cond = QtWidgets.QPushButton("编辑条件…")
        self.btn_cond.clicked.connect(self.edit_condition)
        self.btn_del = QtWidgets.QPushButton("删除")
        self.btn_del.clicked.connect(self.remove_selected)
        self.btn_up = QtWidgets.QPushButton("上移")
        self.btn_up.clicked.connect(lambda: self.move_row(-1))
        self.btn_down = QtWidgets.QPushButton("下移")
        self.btn_down.clicked.connect(lambda: self.move_row(1))
        for b in (self.btn_add, self.btn_cond, self.btn_del, self.btn_up, self.btn_down):
            b.setProperty("kind", "ghost")
            bar.addWidget(b)
        bar.addStretch(1)
        self.btn_revert = QtWidgets.QPushButton("放弃修改")
        self.btn_revert.clicked.connect(self.revert)
        bar.addWidget(self.btn_revert)
        v.addLayout(bar)

        self.msg = QtWidgets.QLabel("")
        self.msg.setWordWrap(True)
        v.addWidget(self.msg)

        btns = QtWidgets.QHBoxLayout()
        btns.addStretch(1)
        self.btn_cancel = QtWidgets.QPushButton("关闭")
        self.btn_cancel.clicked.connect(self.reject)
        self.btn_save = QtWidgets.QPushButton("保存并应用")
        self.btn_save.setProperty("kind", "primary")
        self.btn_save.clicked.connect(self.accept)
        btns.addWidget(self.btn_cancel)
        btns.addWidget(self.btn_save)
        v.addLayout(btns)
        self._say("", "")

    # ---- 数据 <-> 表格 ----
    def _load_table(self):
        self.table.setRowCount(0)
        for r in self.doc.get("rules", []):
            self._append_row(r)

    def _append_row(self, rule):
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setRowHeight(row, 32)
        name = str(rule.get("name") or "")
        self.table.setItem(row, self.COL_NAME, QtWidgets.QTableWidgetItem(name))
        when = str(rule.get("when") or "")
        trans = rule.get("transition")
        win = rule.get("window")
        conds = rule.get("conditions")
        if conds:
            join = str(rule.get("join", "all"))
            summary = self._conditions_summary(conds, join)
            head = summary
            display = summary
        else:
            summary = self._when_summary(when, trans, win)
            head = (("跳变 %s: %s→%s" % (trans["signal"], trans["from"], trans["to"]))
                    if trans else "")
            if win:
                head += (("；" if head else "")
                         + "窗口 %s %s %sms > %s" % (win.get("kind"), win.get("signal"),
                                                    win.get("window_ms"), win.get("threshold")))
            display = summary
        item = QtWidgets.QTableWidgetItem(display)
        item.setToolTip(head + ("；" if head and when else "") + when)
        item.setFlags(item.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)  # 条件只能走对话框
        item.setData(USER_ROLE, when)
        item.setData(USER_ROLE + 1, dict(trans) if trans else None)
        item.setData(USER_ROLE + 3, dict(win) if win else None)
        if conds:
            item.setData(USER_ROLE + 4, list(conds))
            item.setData(USER_ROLE + 5, str(rule.get("join", "all")))
        self.table.setItem(row, self.COL_WHEN, item)
        no_duration = bool(conds) and not any(c.get("type") == "value" for c in conds)
        for col, key in ((self.COL_MIN, "min_duration_ms"),
                         (self.COL_HYST, "hysteresis_ms"),
                         (self.COL_GAP, "merge_gap_ms")):
            val = rule.get(key, 0)
            if trans is not None or no_duration:
                cell = QtWidgets.QTableWidgetItem("—")   # 无条件整形，时长参数无意义
                cell.setData(USER_ROLE + 2, val)         # 原值随行保存，引擎会忽略它
                cell.setFlags(cell.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
                cell.setToolTip("只有定值条件才用时长整形，此参数不生效")
            else:
                cell = QtWidgets.QTableWidgetItem(caliper_rules.fmt_num(val)
                                                  if isinstance(val, (int, float)) else str(val))
            cell.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight
                                  | QtCore.Qt.AlignmentFlag.AlignVCenter)
            self.table.setItem(row, col, cell)
        mark = rule.get("mark") if isinstance(rule.get("mark"), dict) else {}
        mcell = QtWidgets.QTableWidgetItem(str(mark.get("label") or ""))
        mcell.setData(USER_ROLE, dict(mark))     # 颜色等原字段随单元格一起搬家
        self.table.setItem(row, self.COL_MARK, mcell)
        return row

    @staticmethod
    def _when_summary(when, trans=None, win=None):
        """条件列只显示人能扫一眼的摘要；拆不出来的标「高级」，原文在 tooltip 里。"""
        if trans is not None:
            head = "跳变 %s: %s→%s" % (trans["signal"], trans["from"], trans["to"])
        elif win is not None:
            kind = "波动" if win.get("kind") == "swing" else "变化"
            head = "窗口 %s %s/%sms > %s" % (kind, win.get("signal"),
                                            win.get("window_ms"), win.get("threshold"))
        else:
            head = ""
        if not str(when or "").strip():
            return head or "（未设置，双击填写）"
        conds, join = caliper_rules.when_to_conds(when)
        text = ("高级：" + when) if conds is None else caliper_rules.conds_to_when(conds, join or "&")
        return (head + " 且 " + text) if head else text

    def when_at(self, row):
        item = self.table.item(row, self.COL_WHEN)
        return (item.data(USER_ROLE) if item else "") or ""

    def transition_at(self, row):
        item = self.table.item(row, self.COL_WHEN)
        return (item.data(USER_ROLE + 1) if item else None)

    def window_at(self, row):
        item = self.table.item(row, self.COL_WHEN)
        return (item.data(USER_ROLE + 3) if item else None)

    def conditions_at(self, row):
        item = self.table.item(row, self.COL_WHEN)
        return (item.data(USER_ROLE + 4) if item else None)

    def join_at(self, row):
        item = self.table.item(row, self.COL_WHEN)
        return (item.data(USER_ROLE + 5) if item else None) or "all"

    def set_conditions(self, row, conds, join, when_summary, trans, win):
        """保存统一条件表格的结果到该行（新格式主存，旧字段同步兼容视图）。"""
        item = self.table.item(row, self.COL_WHEN)
        item.setData(USER_ROLE + 4, list(conds))
        item.setData(USER_ROLE + 5, join)
        item.setData(USER_ROLE, when_summary)
        item.setData(USER_ROLE + 1, dict(trans) if trans else None)
        item.setData(USER_ROLE + 3, dict(win) if win else None)
        item.setText(self._conditions_summary(conds, join))
        # 时长列：只有定值条件参与整形才可用；否则置灰显示 —（保留原值）
        has_value = any(c.get("type") == "value" for c in conds)
        for col in (self.COL_MIN, self.COL_HYST, self.COL_GAP):
            cell = self.table.item(row, col)
            if not has_value:
                if cell.data(USER_ROLE + 2) is None and cell.text() != "—":
                    cell.setData(USER_ROLE + 2, cell.text())
                cell.setText("—")
                cell.setFlags(cell.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
                cell.setToolTip("只有定值条件才用时长整形")
            else:
                orig = cell.data(USER_ROLE + 2)
                if orig is not None:
                    cell.setText(str(orig))
                    cell.setData(USER_ROLE + 2, None)
                cell.setFlags(cell.flags() | QtCore.Qt.ItemFlag.ItemIsEditable)
                cell.setToolTip("")

    @staticmethod
    def _conditions_summary(conds, join):
        parts = []
        for c in conds:
            t = c.get("type")
            if t == "value" and c.get("expr"):
                parts.append("高级：" + str(c["expr"]))
            elif t == "value" and c.get("signal"):
                parts.append("%s %s %s" % (c["signal"], c.get("op", "=="),
                                           caliper_rules.fmt_num(c.get("threshold", 0) or 0)))
            elif t == "change":
                parts.append("跳变 %s: %s→%s" % (c.get("signal"), c.get("from"), c.get("to")))
            elif t == "swing":
                parts.append("波动 %s %sms>%s" % (c.get("signal"), c.get("window_ms"), c.get("threshold")))
            elif t == "changes":
                parts.append("变化 %s %sms>%s" % (c.get("signal"), c.get("window_ms"), c.get("threshold")))
        glue = " 任一 " if join == "any" else " 全部 "
        return glue.join(parts) if parts else "（未设置，双击填写）"

    def set_when(self, row, when, trans=None, win=None):
        if trans is None and win is None and not str(when or "").strip():
            raise ValueError("条件不能为空（跳变/窗口条件请在编辑对话框里启用）")
        if str(when or "").strip():
            err = caliper_rules.validate_when(when)
            if err:
                raise ValueError(err)
        item = self.table.item(row, self.COL_WHEN)
        item.setData(USER_ROLE, when)
        item.setData(USER_ROLE + 1, dict(trans) if trans else None)
        item.setData(USER_ROLE + 3, dict(win) if win else None)
        item.setText(self._when_summary(when, trans, win))
        tip = (("跳变 %s: %s→%s" % (trans["signal"], trans["from"], trans["to"]))
               if trans else "")
        if win:
            tip += (("；" if tip else "")
                    + "窗口 %s %s %sms > %s" % (win.get("kind"), win.get("signal"),
                                               win.get("window_ms"), win.get("threshold")))
        item.setToolTip(tip + ("；" if tip and when else "") + when)
        # 跳变是单点：三个时长列置灰显示 —（文件里的原值保留，引擎会忽略）
        for col in (self.COL_MIN, self.COL_HYST, self.COL_GAP):
            cell = self.table.item(row, col)
            if trans is not None:
                if cell.data(USER_ROLE + 2) is None and cell.text() != "—":
                    cell.setData(USER_ROLE + 2, cell.text())   # 保存切换前的原值
                cell.setText("—")
                cell.setFlags(cell.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
                cell.setToolTip("跳变条件命中单个采样点，此参数不生效")
            else:
                orig = cell.data(USER_ROLE + 2)
                if orig is not None:
                    cell.setText(str(orig))
                    cell.setData(USER_ROLE + 2, None)
                cell.setFlags(cell.flags() | QtCore.Qt.ItemFlag.ItemIsEditable)
                cell.setToolTip("")

    def set_cell(self, row, col, text):
        """验收与程序化填写用：等同于用户在单元格里键入。"""
        self.table.item(row, col).setText(str(text))

    # ---- 行操作 ----
    def add_rule(self, name=None, when=None):
        row = self.table.rowCount()
        rule = {"name": name or ("新规则 %d" % (row + 1)),
                "when": when or "",
                "min_duration_ms": 200, "hysteresis_ms": 50, "merge_gap_ms": 100,
                "mark": {"label": "新"}}
        return self._append_row(rule)

    def selected_row(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        return rows[0] if rows else (self.table.rowCount() - 1 if self.table.rowCount() else -1)

    def remove_selected(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        if not rows and self.table.rowCount():
            rows = [self.table.rowCount() - 1]
        for r in rows:
            self.table.removeRow(r)
        self._say("已删除 %d 条，点「保存并应用」才写盘" % len(rows), "Warn")

    def move_row(self, delta):
        row = self.selected_row()
        target = row + delta
        if row < 0 or not (0 <= target < self.table.rowCount()):
            return
        data = [self.table.item(row, c) for c in range(len(self.COLS))]
        swap = [self.table.item(target, c) for c in range(len(self.COLS))]
        for c in range(len(self.COLS)):
            self.table.setItem(row, c, swap[c])
            self.table.setItem(target, c, data[c])
        self.table.selectRow(target)

    def _on_double_click(self, row, col):
        if col == self.COL_WHEN:
            self.edit_condition(row)

    def edit_condition(self, row=None):
        row = self.selected_row() if row is None else row
        if row < 0:
            self._say("先选中一条规则", "Bad")
            return None
        dlg = ConditionDialog(self.when_at(row), self.name_at(row), self.signals, self,
                              transition=self.transition_at(row), desc=self.desc,
                              window=self.window_at(row),
                              conditions=self.conditions_at(row), join=self.join_at(row))
        if dlg.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            self.set_conditions(row, dlg.conditions_data(), dlg.join_value(),
                                dlg.when(), dlg.transition(), dlg.window_condition())
            self._say("条件已改，点「保存并应用」写盘", "Warn")
            return dlg
        return None

    def select_rule(self, name):
        """按规则名选中并滚动到可见；找不到就不动（重名已被校验挡住，按名字定位可靠）。"""
        for row in range(self.table.rowCount()):
            if self.name_at(row) == name:
                self.table.selectRow(row)
                self.table.scrollToItem(self.table.item(row, 0))
                return row
        return -1

    def name_at(self, row):
        item = self.table.item(row, self.COL_NAME)
        return item.text().strip() if item else ""

    # ---- 保存 ----
    def collect(self):
        """表格 -> 规则集文档；返回 (doc, 错误列表)。表格没改动也保证结构完整。"""
        rules, errs = [], []
        for row in range(self.table.rowCount()):
            name = self.name_at(row)
            when = self.when_at(row).strip()
            trans = self.transition_at(row)
            win = self.window_at(row)
            conds = self.conditions_at(row)
            rule = {"name": name}
            if conds:
                rule["conditions"] = list(conds)
                rule["join"] = self.join_at(row)
                rule["when"] = when          # 兼容视图
            else:
                rule["when"] = when
            if trans is not None:
                rule["transition"] = dict(trans)
            if win is not None:
                rule["window"] = dict(win)
            no_duration = bool(conds) and not any(c.get("type") == "value" for c in conds)
            for col, key in ((self.COL_MIN, "min_duration_ms"),
                             (self.COL_HYST, "hysteresis_ms"),
                             (self.COL_GAP, "merge_gap_ms")):
                cell = self.table.item(row, col)
                text = (cell.text() if cell else "").strip()
                if trans is not None or no_duration:
                    # 无条件整形的时长参数不生效；单元格显示 —，原值随行保存以便兼容
                    orig = cell.data(USER_ROLE + 2) if cell else None
                    rule[key] = float(orig) if orig is not None else 0.0
                    continue
                try:
                    rule[key] = float(text) if text else 0.0
                except ValueError:
                    errs.append("第 %d 行「%s」不是数字" % (row + 1, key))
            mcell = self.table.item(row, self.COL_MARK)
            label = (mcell.text() if mcell else "").strip()
            mark = dict(mcell.data(USER_ROLE) or {}) if mcell else {}
            if label:
                mark["label"] = label
            else:
                mark.pop("label", None)
            rule["mark"] = mark
            rules.append(rule)
        doc = {"name": self.ed_name.text().strip(),
               "version": int(self.doc.get("version", 1)),
               "note": self.ed_note.text().strip(),
               "rules": rules}
        if "enabled" in self.doc:
            doc["enabled"] = self.doc["enabled"]
        return doc, errs

    def save(self):
        """校验并写回 JSON；成功返回 True，并把 self.doc 更新为刚写盘的内容。"""
        doc, errs = self.collect()
        if errs:
            self._say("；".join(errs), "Bad")
            return False
        try:
            ver, bak, _digest = caliper_rules.save_ruleset(self.path, doc)
        except ValueError as e:
            self._say(str(e), "Bad")
            return False
        self.doc = caliper_rules.load_ruleset_doc(self.path)
        self.saved_version = ver
        self.lb_ver.setText("版本 %d" % ver)
        self._load_table()
        self._say("已写回 %s（版本 %d）%s"
                  % (os.path.basename(self.path), ver,
                     "，旧版备份 " + os.path.basename(bak) if bak else ""), "Ok")
        return True

    def accept(self):
        if self.save():
            super().accept()

    def is_dirty(self):
        """表格内容是否与文件里的一致（版本号由写盘自增，不参与比较）。

        比较前把两边的规则补齐成同一形状：缺 mark / 缺数值字段的规则，
        在表格里会被填成 {} 与 0，不归一化就会把「没改」误判成「改过」。
        """
        doc, errs = self.collect()
        if errs:
            return True

        def norm(d):
            out = {k: v for k, v in d.items() if k != "version"}
            rules = []
            for r in out.get("rules", []):
                r = dict(r)
                r.setdefault("mark", {})
                for k in caliper_rules.NUM_FIELDS:
                    r.setdefault(k, 0.0)
                rules.append(r)
            out["rules"] = rules
            out.setdefault("name", "")
            out.setdefault("note", "")
            return out

        return norm(doc) != norm(self.doc)

    def reject(self):
        if self.is_dirty():
            ans = QtWidgets.QMessageBox.question(
                self, "放弃修改？", "规则改了还没保存，关掉就白改了。仍要关闭吗？")
            if ans != QtWidgets.QMessageBox.StandardButton.Yes:
                return
        super().reject()

    def revert(self):
        self.doc = caliper_rules.load_ruleset_doc(self.path)
        self.ed_name.setText(str(self.doc.get("name") or ""))
        self.ed_note.setText(str(self.doc.get("note") or ""))
        self._load_table()
        self._say("已回到文件里的内容", "")

    def _say(self, text, kind):
        self.msg.setObjectName(kind or "DialogHint")
        self.msg.setText(text)
        self.msg.style().unpolish(self.msg)
        self.msg.style().polish(self.msg)


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        apply_theme(QtWidgets.QApplication.instance())
        self.setWindowTitle("Caliper · By Henry")
        self.resize(1200, 700)
        self.setAcceptDrops(True)
        self._x_origin = 0.0        # X 轴显示零点：BLF 取首帧 epoch 秒，MDF 保持 0
        self.dat_path = None
        self.rules_path = None      # 主规则集（下拉单选）：供编辑/导出命名
        self.rules_paths = []       # 实际参与评估的规则集列表（支持多规则）
        self.rule_paths = {}        # 显示名 -> 路径
        self.rule_signals = set()
        self.scan_info = None       # 运行时扫描结果，不依赖权威清单
        self.signal_map = {}        # name -> (group,index)，精确取数
        self.checked = set()        # 勾选的信号：既加载数据，也画曲线
        self.m = None
        self.result = None
        self.regions = []           # 工况区间带，数量必须等于命中区间数
        self.rule_regions = {}      # 规则名 -> [区间带]，逐规则着色与显隐
        self.rule_markers = {}      # 规则名 -> [标记点 ScatterPlotItem]
        self.rule_marks_visible = {}  # 规则名 -> 是否在图上标注（缺省 True）
        self.rule_marks = {}        # 规则名 -> mark（来自规则集 Rule，显示层用）
        self.hidden_signals = set()   # 隐藏选中：只藏曲线，勾选与值列保留
        self.curve_style = {}       # 信号名 -> {kind: line/step/scatter, width, color}
        self.sample_points_on = set()  # 显示采样点的信号
        self.sample_items = {}      # 信号名 -> [采样点 ScatterPlotItem]
        self.signal_desc = SIGNAL_DESC  # 当前文件的信号描述：.dat 用 JSON，.blf 用 DBC comment
        self.blf_dbc_paths = None   # BLF：用户手选的 DBC 集合
        self.blf_priority = {}      # BLF：通道主 DBC 人工覆盖
        self.blf_channels = []
        self.blf_session = None     # BLF：单遍扫描后的常驻会话（原始报文缓存，勾选零重扫）
        self._blf_sync = False      # 验收用：同步走扫描/加载链路
        self._blf_bad = set()       # 验收变异：nocache=每次加载丢会话
        self._blf_dbc_override = None
        self._blf_scan_token = 0
        self._blf_load_token = 0
        self._blf_worker = None
        self._blf_progress = None
        self.marker_count = 0
        self.curve_count = 0        # 当前曲线条数，供程序化验收
        self.curve_colors = {}
        self.last_jump = None
        self._seg_nav = {}          # 子规则「下一段」游标：{规则名: 当前段索引(-1=未开始)}
        self._clicked_rule = None   # 当前左键激活的子规则：已激活的那条再左键点无动作
        self.local_time_text = ""
        self.auto_y = True          # Y 轴是否自适应；手动改量程后置 False
        self.y_range = None         # 共用轴模式的手动量程 (lo, hi)，重绘后要贴回去
        self.per_axis = False       # False=共用一个 Y 轴；True=每个信号一个 Y 轴
        self.axis_plots = []        # 分轴模式：[(信号名, PlotItem)]，顺序即上下顺序
        self.axis_ranges = {}       # 分轴模式的手动量程：name -> (lo, hi)
        self.cursors = []           # 分轴子图标尺：[(信号名, line1, line2)]，红线/黄线各一条
        self.cursor_x = None        # cursor1（红）所在时刻；None 表示还没定位
        self.cursor2_x = None       # cursor2（深黄）所在时刻；两条独立运动
        self.region_intervals = 0   # 已画工况区间个数（与轴数无关）
        self.units = {}             # 信号名 -> 单位，用于分轴 Y 轴标题
        self._loading_tree = False  # 建树时屏蔽 itemChanged，避免递归
        self._loading_rule_tree = False  # 填清单时屏蔽勾选回调，避免回调里重建树
        self._batch = False         # 批量勾选时只重绘一次
        self._syncing_cursor = False  # 多轴标尺联动时屏蔽回环
        self._build_ui()

    def _build_ui(self):
        root = QtWidgets.QWidget()
        root.setObjectName("App")
        outer = QtWidgets.QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # 顶栏：品牌 + 文件 + 规则集 + 重跑。原来挤在左栏顶部的按钮挪到这里，读图区更干净。
        top = QtWidgets.QFrame()
        top.setObjectName("TopBar")
        tv = QtWidgets.QHBoxLayout(top)
        tv.setContentsMargins(18, 11, 16, 11)
        tv.setSpacing(12)
        brand = QtWidgets.QVBoxLayout()
        brand.setSpacing(1)
        title = QtWidgets.QLabel(pick_poem())
        title.setObjectName("Brand")
        self.brand_title = title   # 供验收读取当前诗句
        self.brand_sub = QtWidgets.QLabel("拖入 .dat 文件，或点右侧「打开文件」")
        self.brand_sub.setObjectName("BrandSub")
        brand.addWidget(title)
        brand.addWidget(self.brand_sub)
        tv.addLayout(brand)
        tv.addStretch(1)

        self.btn_open = QtWidgets.QPushButton("打开文件")
        self.btn_open.setProperty("kind", "primary")
        self.btn_open.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.btn_open.clicked.connect(self.pick_file)
        self.refresh_rules()   # 扫描可用规则集，供「选择规则集」对话框用
        self.btn_multi_rules = QtWidgets.QPushButton("选择规则集")
        self.btn_multi_rules.setProperty("kind", "primary")
        self.btn_multi_rules.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.btn_multi_rules.setToolTip("勾选规则集一起评估（可多选，不选则不评估）")
        self.btn_multi_rules.clicked.connect(self.choose_multi_rules)
        self.btn_run = QtWidgets.QPushButton("重新分析")
        self.btn_run.setProperty("kind", "ghost")
        self.btn_run.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.btn_run.clicked.connect(self.run_analysis)
        self.btn_edit_rules = QtWidgets.QPushButton("编辑规则")
        self.btn_edit_rules.setProperty("kind", "ghost")
        self.btn_edit_rules.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.btn_edit_rules.setToolTip("在界面里改工况规则，改完自动写回 JSON")
        self.btn_edit_rules.clicked.connect(self.edit_rules)
        self.btn_new_rules = QtWidgets.QPushButton("新建规则集")
        self.btn_new_rules.setProperty("kind", "ghost")
        self.btn_new_rules.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.btn_new_rules.setToolTip("用本文件的真实信号建一个最小规则集骨架，随后在编辑器里改条件")
        self.btn_new_rules.clicked.connect(self.new_ruleset)
        self.btn_export = QtWidgets.QPushButton("导出记录")
        self.btn_export.setProperty("kind", "ghost")
        self.btn_export.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.btn_export.setToolTip("把当前评估结果写成可复现记录 JSON（文件哈希/时点/原始值/规则版本/三态）")
        self.btn_export.clicked.connect(self.export_record_dialog)
        self.btn_blf_map = QtWidgets.QPushButton("BLF 映射")
        self.btn_blf_map.setProperty("kind", "ghost")
        self.btn_blf_map.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.btn_blf_map.setToolTip("通道→主 DBC 映射（自适应结果可人工覆盖）与 DBC 集合重选")
        self.btn_blf_map.setEnabled(False)
        self.btn_blf_map.clicked.connect(self.edit_blf_map)
        tv.addWidget(self.btn_multi_rules)
        tv.addWidget(self.btn_edit_rules)
        tv.addWidget(self.btn_new_rules)
        tv.addWidget(self.btn_export)
        tv.addWidget(self.btn_blf_map)
        tv.addWidget(self.btn_run)
        tv.addWidget(self.btn_open)
        outer.addWidget(top)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(12)

        # 勾选改由「选择规则集」/示波器右键「添加信号…」驱动。
        # 但 signal_tree / search / signal_header_label 仍是 checked 状态载体与取数链路
        # （set_checked / _checked_from_tree / BLF 增量都读写它们），保留为隐藏对象，
        # 不加入可见布局——只删 UI，不改勾选语义。
        left = QtWidgets.QFrame()
        left.setObjectName("Panel")
        left.setVisible(False)
        self.left_panel = left
        lv = QtWidgets.QVBoxLayout(left)
        lv.setContentsMargins(12, 11, 10, 10)
        lv.setSpacing(9)
        lv_head = QtWidgets.QHBoxLayout()
        lv_head.setSpacing(6)
        lv_title = QtWidgets.QLabel("可用信号")
        lv_title.setObjectName("PanelTitle")
        self.signal_header_label = QtWidgets.QLabel("—")
        self.signal_header_label.setObjectName("PanelTitle")
        self.signal_header_label.setAlignment(
            QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
        lv_head.addWidget(lv_title)
        lv_head.addStretch(1)
        lv_head.addWidget(self.signal_header_label)
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("搜索信号名")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.filter_signals)
        self.signal_tree = QtWidgets.QTreeWidget()
        self.signal_tree.setHeaderLabels(["信号", "单位"])
        self.signal_tree.itemChanged.connect(self.on_signal_toggled)
        self.signal_tree.setMinimumWidth(260)
        self.signal_tree.setRootIsDecorated(False)     # 平铺列表，不要树形缩进
        self.signal_tree.setUniformRowHeights(True)
        self.signal_tree.setIndentation(0)
        self.signal_tree.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.signal_tree.setVerticalScrollMode(
            QtWidgets.QAbstractItemView.ScrollMode.ScrollPerPixel)
        lv.addLayout(lv_head)
        lv.addWidget(self.search)
        lv.addWidget(self.signal_tree, 1)

        # 中栏：读数条 + 曲线区（共用轴 / 分轴）+ 图例
        self.plot = pg.PlotWidget(
            axisItems={"bottom": RelativeTimeAxis(orientation="bottom")},
            viewBox=MdaViewBox())
        self.plot.getPlotItem().getViewBox().sigDoubleClick.connect(self._set_cursor)
        self.plot.getPlotItem().getViewBox().sigRightDoubleClick.connect(self._set_cursor2)
        self.plot.setLabel("bottom", "时间 / s")
        self.plot.showGrid(x=True, y=True, alpha=GRID_ALPHA)
        self.cursor = self._make_cursor_line("{value:.3f} s")   # cursor1 红：竖直标尺，可拖动
        self.plot.addItem(self.cursor, ignoreBounds=True)   # 标尺 boundingRect 随视野变，参与量程会反馈放大
        self.cursor2 = self._make_cursor_line("", CURSOR2_COLOR, CURSOR2_HOVER)
        self.plot.addItem(self.cursor2, ignoreBounds=True)
        # 分轴视图：每个信号一个子图一个 Y 轴；与共用轴视图互斥，用 QStackedWidget 切换
        self.stack = pg.GraphicsLayoutWidget()
        self.plot_host = QtWidgets.QStackedWidget()
        self.plot_host.addWidget(self.plot)     # 0 = 共用轴
        self.plot_host.addWidget(self.stack)    # 1 = 分轴

        self.cursor_label = QtWidgets.QLabel("拖动红色标尺线，图例上读数")
        self.cursor_label.setObjectName("PlotHead")
        self.cursor_label.setWordWrap(False)
        self.local_label = QtWidgets.QLabel("本地时间：—")
        self.local_label.setObjectName("PlotHead")
        self.local_label.setAlignment(
            QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
        head = QtWidgets.QFrame()
        head.setObjectName("PlotHead")
        hv = QtWidgets.QHBoxLayout(head)
        hv.setContentsMargins(14, 8, 14, 8)
        hv.setSpacing(14)
        hv.addWidget(self.cursor_label, 1)
        hv.addWidget(self.local_label)

        panel = QtWidgets.QFrame()
        panel.setObjectName("PlotPanel")
        pv = QtWidgets.QVBoxLayout(panel)
        pv.setContentsMargins(1, 1, 1, 1)
        pv.setSpacing(0)
        pv.addWidget(head)
        pv.addWidget(self.plot_host, 1)

        mid = QtWidgets.QWidget()
        mv = QtWidgets.QVBoxLayout(mid)
        mv.setContentsMargins(0, 0, 0, 0)
        mv.setSpacing(0)
        mv.addWidget(panel, 1)
        self._mid_widget = mid
        self._mid_layout = mv
        self._plot_panel = panel

        # 右栏：多 Tab（工况评估 / 信号值），可整栏收起
        right = QtWidgets.QFrame()
        right.setObjectName("Panel")
        self.right_panel = right
        rv = QtWidgets.QVBoxLayout(right)
        rv.setContentsMargins(12, 11, 10, 10)
        rv.setSpacing(9)
        self.right_tabs = QtWidgets.QTabWidget()
        self.right_tabs.setObjectName("RightTabs")

        # Tab 1：三态工况清单（原右栏内容）
        tab_eval = QtWidgets.QWidget()
        ev = QtWidgets.QVBoxLayout(tab_eval)
        ev.setContentsMargins(0, 8, 0, 0)
        ev.setSpacing(6)
        ev_head = QtWidgets.QHBoxLayout()
        ev_head.setSpacing(6)
        self.rule_scope_label = QtWidgets.QLabel("未加载")
        self.rule_scope_label.setObjectName("PanelTitle")
        self.rule_scope_label.setAlignment(
            QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
        ev_head.addStretch(1)
        ev_head.addWidget(self.rule_scope_label)
        self.rule_tree = QtWidgets.QTreeWidget()
        self.rule_tree.setHeaderLabels(["规则 / 状态", "明细"])
        self.rule_tree.itemClicked.connect(self.on_item_clicked)
        self.rule_tree.itemChanged.connect(self.on_rule_mark_toggled)
        self.rule_tree.setContextMenuPolicy(
            QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.rule_tree.customContextMenuRequested.connect(self.on_rule_context_menu)
        self.rule_tree.setMinimumWidth(260)
        self.rule_tree.setIndentation(14)
        self.rule_tree.setUniformRowHeights(True)
        self.rule_tree.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.rule_tree.setVerticalScrollMode(
            QtWidgets.QAbstractItemView.ScrollMode.ScrollPerPixel)
        ev.addLayout(ev_head)
        ev.addWidget(self.rule_tree, 1)
        self.right_tabs.addTab(tab_eval, "工况评估")

        # Tab 2：示波器中各信号在标尺时刻的值（原图例读数挪到这里）
        tab_vals = QtWidgets.QWidget()
        vv = QtWidgets.QVBoxLayout(tab_vals)
        vv.setContentsMargins(0, 8, 0, 0)
        vv.setSpacing(6)
        val_hint = QtWidgets.QLabel("已加入示波器的信号在标尺时刻的值（最近邻实测值，不插值）。"
                                    "点击选中，Ctrl+点击多选，Ctrl+A 全选。")
        val_hint.setObjectName("DialogHint")
        val_hint.setWordWrap(True)
        self.value_tree = QtWidgets.QTreeWidget()
        self.value_tree.setHeaderLabels(["信号", "C1", "C2", "Δ", "单位"])
        self.value_tree.setRootIsDecorated(False)
        self.value_tree.setUniformRowHeights(True)
        self.value_tree.setIndentation(0)
        self.value_tree.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.value_tree.setVerticalScrollMode(
            QtWidgets.QAbstractItemView.ScrollMode.ScrollPerPixel)
        vh = self.value_tree.header()
        vh.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        for col in (1, 2, 3, 4):   # C1 / C2 / Δ / 单位
            vh.setSectionResizeMode(col, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        vh.setStretchLastSection(False)
        self._setup_value_selection()
        vv.addWidget(val_hint)
        vv.addWidget(self.value_tree, 1)
        self.right_tabs.addTab(tab_vals, "选中信号")

        rv.addWidget(self.right_tabs, 1)

        # 侧栏收起拉手：仅右栏。
        self.btn_right_panel = QtWidgets.QPushButton("▶", right)
        self.btn_right_panel.setObjectName("PanelHandle")
        self.btn_right_panel.setCheckable(True)
        self.btn_right_panel.setChecked(True)
        self.btn_right_panel.setFixedSize(18, 64)
        self.btn_right_panel.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.btn_right_panel.setToolTip("收起工况栏")
        self.btn_right_panel.toggled.connect(self.toggle_right_panel)
        for widget in (mid, right):
            widget.installEventFilter(self)
        self._update_mid_margins()
        self._place_panel_handles()

        splitter.addWidget(mid)     # 左栏已删：两栏布局（示波器 + 右栏）
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([900, 330])

        body = QtWidgets.QWidget()
        bv = QtWidgets.QHBoxLayout(body)
        bv.setContentsMargins(12, 12, 12, 12)
        bv.setSpacing(0)
        bv.addWidget(splitter)
        outer.addWidget(body, 1)
        self.setCentralWidget(root)
        self._render_empty_hint()

        self._setup_headers()
        self._setup_plot_menu()

    def toggle_right_panel(self, on):
        """右栏拉手：收起/展开右栏（Tab 与清单对象都保留，展开即恢复）。"""
        self.right_panel.setVisible(bool(on))
        self.btn_right_panel.setParent(self.right_panel if on else self._mid_widget)
        self.btn_right_panel.setVisible(True)
        self.btn_right_panel.setText("▶" if on else "◀")
        self.btn_right_panel.setToolTip("收起工况栏" if on else "展开工况栏")
        self._update_mid_margins()
        self._place_panel_handles()

    def _update_mid_margins(self):
        """右栏收起时，曲线区给右侧拉手留 24px 内缩带，示波器不贴边。"""
        r = 0 if self.btn_right_panel.isChecked() else 24
        self._mid_layout.setContentsMargins(0, 0, r, 0)

    def _place_panel_handles(self):
        """右栏拉手垂直居中贴边：展开贴边栏内侧，收起贴曲线区边缘。"""
        rh = self.btn_right_panel
        if rh.parent() is self.right_panel:
            pw, ph = self.right_panel.width(), self.right_panel.height()
            rh.move(2, max(0, (ph - 64) // 2))
        else:
            mw, mh = self._mid_widget.width(), self._mid_widget.height()
            rh.move(max(0, mw - 20), max(0, (mh - 64) // 2))
        rh.raise_()

    def eventFilter(self, obj, event):
        if obj in (getattr(self, "_mid_widget", None),
                   getattr(self, "right_panel", None)) \
                and event.type() == QtCore.QEvent.Type.Resize:
            self._place_panel_handles()
        return super().eventFilter(obj, event)

    def _render_empty_hint(self):
        """没有文件时的提示：不弹对话框，也不画空坐标系。"""
        self.plot.getPlotItem().setTitle(
            "<span style='color:%s;font-size:12px'>拖入 .dat 测量文件开始</span>" % TEXT_FAINT)

    def _apply_plot_style(self, plot_item):
        """统一起见：白底、淡网格、细轴线、朝内短线刻度。"""
        plot_item.showGrid(x=True, y=True, alpha=GRID_ALPHA)
        for side in ("left", "bottom"):
            ax = plot_item.getAxis(side)
            ax.setPen(pg.mkPen(BORDER, width=1))
            ax.setTextPen(pg.mkPen(TEXT_DIM))
            ax.setStyle(tickLength=-5, tickTextOffset=7, tickFont=TICK_FONT)
            ax.label.setFont(LABEL_FONT)
        plot_item.getViewBox().setContentsMargins(10, 6, 8, 6)

    # ---- 表头：标题宽度自适应，不截断标题 ----
    def _setup_headers(self):
        """左树：信号名列拉伸 + 单位列按内容；右树：状态列拉伸 + 明细列按内容（限宽）。

        两侧都关掉「末列拉伸」，否则第二列会被撑满、第一列标题被挤掉。
        """
        left = self.signal_tree.header()
        left.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        left.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        left.setStretchLastSection(False)
        left.setMinimumSectionSize(48)
        left.setDefaultAlignment(QtCore.Qt.AlignmentFlag.AlignLeft
                                 | QtCore.Qt.AlignmentFlag.AlignVCenter)
        self.signal_tree.headerItem().setTextAlignment(
            1, QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
        self.signal_tree.setTextElideMode(QtCore.Qt.TextElideMode.ElideRight)

        right = self.rule_tree.header()
        right.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        right.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Interactive)
        right.setStretchLastSection(False)
        right.setMinimumSectionSize(48)
        self.rule_tree.setTextElideMode(QtCore.Qt.TextElideMode.ElideRight)

    def _fit_rule_detail_column(self):
        """明细列按当前内容宽度自适应，上限 260 px，超出省略并用 tooltip 看全。"""
        h = self.rule_tree.header()
        fm = h.fontMetrics()
        widest = fm.horizontalAdvance(self.rule_tree.headerItem().text(1))
        for i in range(self.rule_tree.topLevelItemCount()):
            top = self.rule_tree.topLevelItem(i)
            widest = max(widest, fm.horizontalAdvance(top.text(1)))
            for j in range(top.childCount()):
                widest = max(widest, fm.horizontalAdvance(top.child(j).text(1)))
        h.resizeSection(1, max(90, min(widest + 16, 260)))

    # ---- 轴模式 + Y 轴量程：右键菜单 / 手动 / 自适应 ----
    def _setup_plot_menu(self):
        """共用轴视图的右键菜单。分轴的每个子图在 _render_per_axis 里各自挂一套。"""
        self._attach_menu(self.plot.getPlotItem().getViewBox(), None)

    def _attach_menu(self, vb, name=None):
        """给一个 ViewBox 挂菜单项。name=None 表示共用轴，否则是该子图对应的信号名。

        菜单项加在 pyqtgraph 自带项之后（它自带 View All / X axis / Y axis）。
        """
        if vb.menu is None:          # 菜单被禁用时先建出来
            vb.setMenuEnabled(True)
        menu = vb.menu
        menu.addSeparator()
        # 示波器右键「添加信号…」完全复用右栏「选中信号」的同一入口
        act_add = menu.addAction("添加信号…")
        act_add.triggered.connect(lambda *_: self.sel_add_signals())
        menu.addSeparator()
        act_manual = menu.addAction("修改 Y 轴量程…")
        act_manual.triggered.connect(lambda *_: self.edit_y_range(name))
        act_auto = menu.addAction("Y 轴量程自适应")
        act_auto.triggered.connect(lambda *_: self.enable_auto_y(name))
        if name is None:
            act = menu.addAction(PER_AXIS_LABEL)
            act.setCheckable(True)
            act.setChecked(bool(self.per_axis))
            act.toggled.connect(self.set_per_axis)   # 放在 setChecked 之后，避免构造期触发渲染
            self.act_per_axis = act
            self.act_y_manual, self.act_y_auto = act_manual, act_auto
        else:
            act = menu.addAction(SHARED_AXIS_LABEL)
            act.triggered.connect(lambda *_: self.set_per_axis(False))

    def set_per_axis(self, on):
        """切换轴模式。True=每个信号一个 Y 轴；False=所有信号共用一个 Y 轴。

        模式切换必须重画：分轴的子图数量随勾选信号数变化，共用轴视图也要重新灌数据。
        """
        on = bool(on)
        self.per_axis = on
        self.plot_host.setCurrentIndex(1 if on else 0)
        act = getattr(self, "act_per_axis", None)
        if act is not None and act.isChecked() != on:   # 与菜单勾选态同步，防递归
            old = act.blockSignals(True)
            try:
                act.setChecked(on)
            finally:
                act.blockSignals(old)
        self.render()

    def _axis_viewbox(self, name):
        """取分轴模式下某信号子图的 ViewBox；还没建出来返回 None。"""
        for n, pi in self.axis_plots:
            if n == name:
                return pi.getViewBox()
        return None

    def edit_y_range(self, name=None):
        """右键「修改 Y 轴量程…」。name=None 改共用轴，否则只改该信号自己的轴。"""
        vb = self._axis_viewbox(name) if name is not None else self.plot.getPlotItem().getViewBox()
        if vb is None:
            return
        lo, hi = vb.viewRange()[1]
        dlg = YRangeDialog(self, lo, hi)
        if dlg.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            new_lo, new_hi = dlg.values()
            if not self.set_y_range(new_lo, new_hi, name):
                QtWidgets.QMessageBox.warning(self, "量程无效", "下限必须小于上限。")

    def set_y_range(self, lo, hi, name=None):
        """设定固定 Y 量程；返回 False 表示参数无效（不改动当前视图）。

        name=None 落到共用轴，否则只落到该信号的分轴量程表里（其它轴不动）。
        """
        lo, hi = float(lo), float(hi)
        if not lo < hi:
            return False
        if name is None:
            self.auto_y = False
            self.y_range = (lo, hi)
            vb = self.plot.getPlotItem().getViewBox()
        else:
            self.axis_ranges[name] = (lo, hi)
            vb = self._axis_viewbox(name)
            if vb is None:      # 该信号的子图还没建：先记住，下次渲染自动贴上
                return True
        vb.enableAutoRange(axis=pg.ViewBox.YAxis, enable=False)
        vb.setYRange(lo, hi, padding=0)
        if name is not None:
            self._align_y_axes()    # 量程变了刻度文本也变，Y 标尺宽度要跟着重算
        return True

    def enable_auto_y(self, name=None):
        """右键「Y 轴量程自适应」。name=None 作用于共用轴，否则只作用于该信号的轴。"""
        if name is None:
            self.auto_y = True
            self.y_range = None
            vb = self.plot.getPlotItem().getViewBox()
        else:
            self.axis_ranges.pop(name, None)
            vb = self._axis_viewbox(name)
            if vb is None:
                return
        vb.enableAutoRange(axis=pg.ViewBox.YAxis, enable=True)
        if name is not None:
            self._align_y_axes()

    def _apply_y_view(self):
        """渲染会重建子图，用户设定的量程必须重新贴回，否则勾一次信号就被重置。"""
        if self.per_axis:
            for name, pi in self.axis_plots:
                vb = pi.getViewBox()
                rng = self.axis_ranges.get(name)
                if rng:
                    vb.enableAutoRange(axis=pg.ViewBox.YAxis, enable=False)
                    vb.setYRange(rng[0], rng[1], padding=0)
                else:
                    vb.enableAutoRange(axis=pg.ViewBox.YAxis, enable=True)
            self._align_y_axes()     # 量程变了刻度文本也变，统宽要跟着重算
            return
        vb = self.plot.getPlotItem().getViewBox()
        if self.auto_y:
            vb.enableAutoRange(axis=pg.ViewBox.YAxis, enable=True)
        elif self.y_range:
            vb.enableAutoRange(axis=pg.ViewBox.YAxis, enable=False)
            vb.setYRange(self.y_range[0], self.y_range[1], padding=0)

    # ---- Y 标尺：统一宽度左对齐 ----
    def _y_axis_width(self, name, axis):
        """估算该信号 Y 标尺需要多宽：最宽刻度文本 + 刻度偏移 + 刻线长 + 标题占位。

        取「数据跨度」与「当前视图量程」的并集来估，这样手动放大量程后文字变长也不会被裁。
        """
        vals = np.asarray(self.m.signals[name], dtype=float)
        lo, hi = float(np.nanmin(vals)), float(np.nanmax(vals))
        rng = self.axis_ranges.get(name)      # 手动量程比数据范围更宽时，刻度文本也会更长
        if rng:
            lo, hi = min(lo, rng[0]), max(hi, rng[1])
        try:
            vlo, vhi = axis.range
            lo, hi = min(lo, float(vlo)), max(hi, float(vhi))
        except (TypeError, ValueError):
            pass
        if not (math.isfinite(lo) and math.isfinite(hi)) or hi <= lo:
            hi = lo + 1.0
        st = axis.style
        fm = QtGui.QFontMetrics(st.get("tickFont") or axis.font())
        try:
            labels = axis.tickStrings([lo, (lo + hi) / 2.0, hi], 1.0, (hi - lo) / 3.0)
        except Exception:                     # 刻度文本格式化失败不该拖垮渲染
            labels = ["%g" % lo, "%g" % hi]
        text_w = max([fm.horizontalAdvance(s) for s in labels] + [30])
        extra = st["tickTextOffset"][0] + max(0, st["tickLength"])
        label_w = axis.label.boundingRect().height() * 0.8 if axis.label.isVisible() else 0
        return int(math.ceil(text_w + extra + label_w)) + 6

    def _align_y_axes(self):
        """所有子图的 Y 标尺统一宽度 -> 刻度数字右端对齐，绘图区左边界也随之对齐。"""
        if not self.axis_plots:
            return
        width = max(Y_AXIS_MIN_WIDTH,
                    max(self._y_axis_width(n, pi.getAxis("left"))
                        for n, pi in self.axis_plots))
        for _, pi in self.axis_plots:
            pi.getAxis("left").setWidth(width)

    # ---- 竖直标尺：可拖动，停在哪儿读哪儿的信号值 ----
    def _make_cursor_line(self, label_text, color=CURSOR_COLOR, hover=CURSOR_HOVER):
        """造一条可拖动的竖直标尺线，线头带读数标签（透明底、细字、不挡曲线）。"""
        # 线宽必须整数：Qt 把浮点宽四舍五入，1.6/2.4 都会变成 2，加粗根本不生效
        line = pg.InfiniteLine(
            pos=0, angle=90, movable=True,
            pen=pg.mkPen(color, width=2),
            hoverPen=pg.mkPen(hover, width=4),
            label=label_text,
            labelOpts={"position": 0.92, "color": color, "movable": False,
                       "fill": QtGui.QBrush(QtCore.Qt.BrushStyle.NoBrush)})
        line.sigPositionChanged.connect(self._on_cursor_moved)
        return line

    def _all_cursor_lines(self, which=1):
        """取同一类标尺的所有实例（共用轴 + 各分轴子图）；which=1 红 / 2 黄。"""
        if which == 2:
            return [self.cursor2] + [l2 for _, _l1, l2 in self.cursors]
        return [self.cursor] + [l1 for _, l1, _l2 in self.cursors]

    def _on_cursor_moved(self, line):
        """拖动任一条标尺 -> 同类标尺（含各分轴）跟到同一时刻，另一条不动。"""
        if self._syncing_cursor:
            return
        which = 2 if line is self.cursor2 or line in [l2 for _, _l1, l2 in self.cursors] else 1
        x = float(line.value())
        if which == 2:
            self.cursor2_x = x
        else:
            self.cursor_x = x
        self._syncing_cursor = True
        try:
            for other in self._all_cursor_lines(which):
                if other is not line:
                    other.setValue(x)
        finally:
            self._syncing_cursor = False
        self._refresh_cursor_readout()

    def _set_cursor(self, x):
        """左键双击 / 工况回跳：把 cursor1（红）移到 x，并刷新读数。"""
        self.cursor_x = float(x)
        self._syncing_cursor = True
        try:
            for line in self._all_cursor_lines(1):
                line.setValue(self.cursor_x)
        finally:
            self._syncing_cursor = False
        self._refresh_cursor_readout()

    def _set_cursor2(self, x):
        """右键双击：把 cursor2（深黄）移到 x，cursor1 与读数条不变。"""
        self.cursor2_x = float(x)
        self._syncing_cursor = True
        try:
            for line in self._all_cursor_lines(2):
                line.setValue(self.cursor2_x)
        finally:
            self._syncing_cursor = False
        self._refresh_cursor_readout()

    def _rel_time(self, x):
        """绝对秒 -> 相对首帧秒：标尺读数独占这个转换口，便于变异验证。"""
        ax = self.plot.getPlotItem().getAxis("bottom")
        origin = ax.origin if isinstance(ax, RelativeTimeAxis) else self._x_origin
        return float(x) - origin

    def _refresh_cursor_readout(self):
        """标尺读数：时刻 + 本地时间 + 右栏「信号值」Tab + 分轴子图上的直接标注。"""
        x = self.cursor_x
        if self.m is None or (x is None and self.cursor2_x is None):
            return
        # 标尺读数用相对秒（与横轴一致），BLF 不再甩出 1.8e9；本地时间仍由相对秒推算
        x2 = self.cursor2_x
        # 读数条给出 t1 / t2 / Δt；Δt = t2 - t1，两个绝对时刻相减已是相对量，
        # 不能再过 _rel_time（那会多减一次 origin，BLF 上凭空少 1.8e9 秒）。
        t1 = "%.3f s" % self._rel_time(x) if x is not None else "—"
        t2 = "%.3f s" % self._rel_time(x2) if x2 is not None else "—"
        dt = ("%+.3f s" % (x2 - x)) if (x is not None and x2 is not None) else "—"
        self.cursor_label.setText(
            '<span style="color:%s;font-weight:600">t1 = %s</span>'
            '<span style="color:%s">  t2 = %s</span>'
            '<span style="color:%s">  Δt = %s</span>'
            % (CURSOR_COLOR, t1, CURSOR2_COLOR, t2, TEXT, dt))
        self._refresh_value_tab()
        # 线头标签：cursor1 显示时刻，cursor2 显示相对 cursor1 的时间差（两条都定位时）
        if self.cursor.label is not None:
            self.cursor.label.setText(t1)
            self.cursor.label.updatePosition()
        if self.cursor2.label is not None:
            self.cursor2.label.setText(
                ("Δt %+.3f s" % (x2 - x)) if (x is not None and x2 is not None)
                else t2)
            self.cursor2.label.updatePosition()
        # 分轴：每支标尺在自己的子图上写该信号在该时刻的值
        for name, line1, line2 in self.cursors:
            unit = (self.units.get(name) or "").strip()
            v1 = self._value_at(name, x) if x is not None else None
            text1 = (_fmt_value(v1) + _enum_suffix(name, v1)
                     + ((" " + unit) if unit else "")) if v1 is not None else "—"
            if line1.label is not None:
                line1.label.setText(text1)
                line1.label.updatePosition()
            if line2.label is not None:
                # 线头标该交点处的当前值（Δ 只放右栏值列，不在这里重复）
                v2 = self._value_at(name, x2) if x2 is not None else None
                text2 = (_fmt_value(v2) + _enum_suffix(name, v2)
                         + ((" " + unit) if unit else "")) if v2 is not None else "—"
                line2.label.setText(text2)
                line2.label.updatePosition()
        if self.m.start_time_local and x is not None:   # 本地时间跟 cursor1；未定位则不显示
            base = datetime.datetime.fromisoformat(self.m.start_time_local)
            local = base + datetime.timedelta(seconds=x)
            self.local_time_text = "本地时间：" + local.strftime("%Y-%m-%d %H:%M:%S")
            self.local_label.setText(self.local_time_text)

    def refresh_rules(self):
        """递归扫 rules/ 下所有 json（含 DAT/ / BLF/ / templates/ 子目录，跳过 enabled=false）。

        显示名带相对路径前缀（如 DAT/Gear_Dat.json、templates/xxx.json），用户可分辨分类。
        _extra_rule_dir 供验收注入夹具目录（out/_verify_rules/）：验收不依赖 rules/ 示例文件，
        但要让夹具出现在「选择规则集」列表里，才能测多规则选择等 GUI 交互。
        """
        self.rule_paths = {}
        # 含 templates 一起扫；显示名就是相对路径（天然带子目录前缀）
        for rel in caliper_rules.list_rulesets(skip_dirs=()):
            path = os.path.join(RULES_DIR, rel.replace("/", os.sep))
            try:
                with open(path, encoding="utf-8") as f:
                    doc = json.load(f)
            except (OSError, ValueError):
                continue
            if doc.get("enabled") is False:
                continue
            self.rule_paths[rel] = path
        extra = getattr(self, "_extra_rule_dir", None)
        if extra and os.path.isdir(extra):
            for name in sorted(os.listdir(extra)):
                if name.endswith(".json"):
                    self.rule_paths["fixtures/" + name] = os.path.join(extra, name)

    def edit_rules(self, focus=None):
        """规则修改入口：界面里改，保存即写回 JSON，不用人去编辑文件。

        focus 给规则名时，编辑器打开后直接选中那一条（从清单右键进来时用）。
        """
        if not self.rules_path:
            QtWidgets.QMessageBox.information(
                self, "先选一个规则集",
                "当前没有加载规则集，没有可编辑的文件。\n"
                "请先点「选择规则集」勾选一个，再点「编辑规则」。")
            return None
        try:
            dlg = RuleSetDialog(self.rules_path, signals=sorted(self.signal_map),
                                parent=self, focus=focus, desc=self.signal_desc)
        except ValueError as e:
            QtWidgets.QMessageBox.warning(self, "打不开这个文件", str(e))
            return None
        code = dlg.exec()
        if code == QtWidgets.QDialog.DialogCode.Accepted:
            self.after_rules_saved(dlg.path)
        return dlg

    def on_rule_context_menu(self, pos):
        """清单上右键：改这一条 / 逐段浏览命中 / 重新分析。"""
        item = self.rule_tree.itemAt(pos)
        rule_name = item.data(0, RULE_NAME_ROLE) if item is not None else None
        menu = QtWidgets.QMenu(self)
        act_next = None
        if rule_name:
            rr = next((r for r in (self.result.results if self.result else [])
                       if r.rule_name == rule_name), None)
            has_segments = bool(rr and rr.intervals)
            act_next = menu.addAction("下一段")
            act_next.setEnabled(has_segments)
            act_next.setToolTip("逐段跳到这条规则的命中区间；到末段后再点循环回第一段"
                                if has_segments else "这条规则没有命中区间")
            menu.addSeparator()
        act = menu.addAction("编辑「%s」…" % rule_name if rule_name else "编辑规则集…")
        act.setEnabled(bool(self.rules_path))
        act_run = menu.addAction("重新分析")
        act_run.setEnabled(bool(self.dat_path))
        chosen = menu.exec(self.rule_tree.viewport().mapToGlobal(pos))
        if chosen is act_next:
            self.next_segment(rule_name)
        elif chosen is act:
            self.edit_rules(rule_name)
        elif chosen is act_run:
            self.run_analysis()

    def after_rules_saved(self, path):
        """规则文件写盘后：刷新规则清单、仍选中它、把新信号勾上、立刻按新规则重算。"""
        self.refresh_rules()
        self.rules_path = path
        self.rules_paths = [path] if path else []
        self.rule_scope_label.setText(os.path.basename(path))
        self.rule_signals = {s for r in caliper_rules.load_ruleset(path)
                             for s in r.required_signals}
        if self.dat_path:
            # 新规则用到的新信号要立刻有曲线，否则改完条件仍看不到它
            self.checked |= {n for n in self.rule_signals if n in self.signal_map}
            self._fill_signal_tree()
            self.run_analysis()

    # ---- 导出可复现记录 / 新建规则集 ----
    def export_record(self, path):
        """把当前评估结果写成可复现记录 JSON；前提不满足返回 None，不落盘。

        复用 caliper_report.build_record，与 CLI 导出走同一条链，两边产物字节一致。
        """
        if not self.dat_path or not self.rules_path or self.m is None or self.result is None:
            return None
        if len(self._rules_to_evaluate()) > 1:
            # 导出记录的 ruleset 字段只装单个文件；多规则下写主规则会误导，拒绝而非产出错记录
            QtWidgets.QMessageBox.information(
                self, "多规则集不支持导出记录",
                "当前评估了 %d 个规则集，可复现记录只支持单个规则集。\n"
                "请在「多规则集」里只保留一个后再导出。" % len(self._rules_to_evaluate()))
            return None
        record = caliper_report.build_record(self.result, self.m, self.dat_path, self.rules_path)
        caliper_report.write_json(record, path)
        return record

    def export_record_dialog(self):
        if not self.dat_path:
            QtWidgets.QMessageBox.information(
                self, "先打开测量文件", "还没有数据文件，没有可导出的评估结果。")
            return None
        if not self.rules_path:
            QtWidgets.QMessageBox.information(
                self, "先选一个规则集",
                "当前没有加载规则集，没有评估结果可导出。\n"
                "请先点「选择规则集」勾选一个，再点「导出记录」。")
            return None
        stem = os.path.splitext(os.path.basename(self.dat_path))[0]
        ruleset = os.path.splitext(os.path.basename(self.rules_path))[0]
        default = os.path.join(os.path.dirname(self.dat_path),
                               "record_%s_%s.json" % (stem, ruleset))
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "导出可复现记录", default, "JSON (*.json)")
        if not path:
            return None
        record = self.export_record(path)
        if record is None:
            QtWidgets.QMessageBox.warning(self, "导出失败", "评估结果不可用，请先点「重新分析」。")
            return None
        QtWidgets.QMessageBox.information(self, "已导出", "可复现记录已写入：\n%s" % path)
        return record

    def create_ruleset(self, path, bad=frozenset()):
        """新建最小规则集骨架并写盘。成功返回 (实际路径, "")，失败返回 (None, 原因)。

        骨架条件用本文件真实信号名（不许编信号），阈值 0 只是占位，随后进编辑器改。
        不覆盖已有文件；只允许落在 rules/ 里，否则界面下拉找不到它。
        bad 仅供变异测试：overwrite=跳过已存在检查，bump=新文件也自增版本号。
        """
        if not self.signal_map:
            return None, "先打开一个测量文件：新建规则集需要用真实信号名做骨架。"
        path = os.path.abspath(path)
        if not path.lower().endswith(".json"):
            path += ".json"
        rel = os.path.relpath(path, RULES_DIR)
        if os.path.isabs(rel) or rel == ".." or rel.startswith(".." + os.sep):
            return None, "规则集要保存在 rules/ 目录（或其 templates/ 子目录）里，否则界面下拉找不到它。"
        if os.path.exists(path) and "overwrite" not in bad:
            return None, "文件已存在，新建不覆盖已有规则集，请换个名字。"
        sig = sorted(self.signal_map)[0]
        doc = {"name": os.path.splitext(os.path.basename(path))[0], "version": 1,
               "note": "界面新建的骨架规则集：条件与阈值请在编辑器里改成实际工况。",
               "rules": [{"name": "新规则 1", "when": "%s == 0" % sig,
                          "min_duration_ms": 200, "hysteresis_ms": 50,
                          "merge_gap_ms": 100, "mark": {"label": "新"}}]}
        caliper_rules.save_ruleset(path, doc, bump_version="bump" in bad)
        return path, ""

    def new_ruleset(self):
        if not self.signal_map:
            QtWidgets.QMessageBox.information(
                self, "先打开测量文件",
                "新建规则集需要用本文件的真实信号名做骨架。\n请先拖入或打开一个 .dat 文件。")
            return None
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "新建规则集", os.path.join(RULES_DIR, "new_rules.json"), "规则集 (*.json)")
        if not path:
            return None
        created, msg = self.create_ruleset(path)
        if created is None:
            QtWidgets.QMessageBox.warning(self, "没法新建", msg)
            return None
        self.after_rules_saved(created)
        return self.edit_rules("新规则 1")

    # ---- BLF 接入：后台扫描/加载 + 进度条 + DBC 手选 + 映射覆盖 ----
    def _blf_config(self):
        if os.path.exists(caliper_core.BLF_CONFIG):
            with open(caliper_core.BLF_CONFIG, encoding="utf-8") as f:
                return json.load(f)
        return {}

    def _blf_save_config(self, cfg):
        os.makedirs(caliper_core.DBC_DIR, exist_ok=True)
        with open(caliper_core.BLF_CONFIG, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)

    @staticmethod
    def _stage_dbcs(paths):
        """把选中的 DBC 复制到 DBC/（按文件名去重），返回存配置用的文件名列表。"""
        os.makedirs(caliper_core.DBC_DIR, exist_ok=True)
        names = []
        for p in paths:
            name = os.path.basename(p)
            dest = os.path.join(caliper_core.DBC_DIR, name)
            if not os.path.exists(dest):        # 同名已存在，跳过复制
                shutil.copyfile(p, dest)
            names.append(name)
        return names

    @staticmethod
    def _blf_abs_paths(cfg_paths):
        """配置里的文件名 -> DBC/ 下的完整路径；兼容旧配置存的完整路径。"""
        return [caliper_core.resolve_dbc_path(p) for p in cfg_paths]

    def _blf_progress_dialog(self, label, total):
        dlg = QtWidgets.QProgressDialog(label, "取消", 0, max(1, total), self)
        dlg.setWindowTitle("BLF")
        dlg.setWindowModality(QtCore.Qt.WindowModality.WindowModal)
        dlg.setMinimumDuration(0)
        _center_on_screen(dlg)
        dlg.canceled.connect(self._blf_cancel)
        self._blf_progress = dlg
        return dlg

    def _blf_cancel(self):
        if self._blf_worker is not None:
            self._blf_worker.cancel()

    def _blf_on_progress(self, n, total):
        if self._blf_progress is not None:
            self._blf_progress.setMaximum(max(1, total))
            self._blf_progress.setValue(n)

    def _blf_close_progress(self):
        if self._blf_progress is not None:
            self._blf_progress.close()
            self._blf_progress = None

    def _blf_failed(self, msg):
        self._blf_close_progress()
        if msg != "cancelled":
            QtWidgets.QMessageBox.warning(self, "BLF 处理失败", msg)

    def open_blf(self, path, auto=True):
        cfg = self._blf_config()
        cfg_paths = cfg.get("dbc_paths") or []
        if self._blf_dbc_override:
            paths = list(self._blf_dbc_override)     # 验收注入的已是完整路径
        elif cfg_paths:
            paths = self._blf_abs_paths(cfg_paths)   # 配置存文件名 -> 完整路径
        else:
            picked, _ = QtWidgets.QFileDialog.getOpenFileNames(
                self, "选择 DBC（可多选，自适应映射的依据）", "", "DBC (*.dbc)")
            if not picked:
                return None
            paths = list(picked)
            cfg["dbc_paths"] = self._stage_dbcs(picked)   # 复制到 DBC/ + 只存文件名
            self._blf_save_config(cfg)
        self.blf_dbc_paths = paths
        self.blf_priority = cfg.get("channel_priority") or {}
        self.dat_path = path
        self.btn_blf_map.setEnabled(True)
        self._blf_scan_token += 1
        token = self._blf_scan_token
        sess = self.blf_session
        if sess is not None and sess.reusable(path, paths):
            # 同文件同 DBC（内容也没变）：只重算归属，不重扫 471MB
            entry = sess.remap(channel_priority=self.blf_priority)
            self._on_blf_scanned(entry, token, auto)
            return entry
        if self._blf_sync:
            try:
                entry = self._blf_scan_sync(path)
            except Exception as e:
                self._blf_failed(str(e))
                return None
            self._on_blf_scanned(entry, token, auto)
            return entry
        self._blf_progress_dialog("扫描 BLF 并缓存报文…", 100)
        w = BlfScanWorker(path, paths, self.blf_priority, parent=self)  # parent 保活：换文件时旧 worker 不被 GC 中途销毁
        w.progress.connect(self._blf_on_progress)
        w.finished_ok.connect(lambda e, tok=token: self._on_blf_scanned(e, tok, auto))
        w.failed.connect(self._blf_failed)
        self._blf_worker = w
        w.start()
        return None

    def _blf_scan_sync(self, path):
        sess = caliper_core.BlfSession(path, dbc_paths=self.blf_dbc_paths,
                                   channel_priority=self.blf_priority)
        return sess.entry()

    def _on_blf_scanned(self, entry, token, auto=True):
        if token != self._blf_scan_token:
            return
        self._blf_close_progress()
        self.blf_session = entry.get("session")
        # 重映射后会话对象没换但归属变了：旧 m 的轴/信号已失效，禁止对它走增量
        self.m = None
        self.blf_channels = entry["res"]["channels"]
        self.scan_info = {"signals": [
            {"name": s["name"], "unit": s["unit"], "group": s["ch"],
             "index": int(s["id"], 16)} for s in entry["signals"]],
            "config": "blf-%dch" % len(self.blf_channels)}
        self.signal_map = {s["name"]: (s["ch"], int(s["id"], 16))
                           for s in entry["signals"]}
        # BLF 描述来自各信号所属 DBC 的 comment；键用带 _chN 后缀的界面名
        self.signal_desc = {s["name"]: s.get("desc", "") for s in entry["signals"]}
        self.units = {s["name"]: s["unit"] for s in entry["signals"]}
        self.brand_sub.setText("%s · %d 个信号"
                               % (os.path.basename(self.dat_path), len(self.signal_map)))
        self.axis_ranges = {}
        self.cursor_x = None
        self.cursor2_x = None
        self.local_time_text = ""
        self.last_jump = None
        self._seg_nav = {}          # 换文件后段游标失效
        self._clicked_rule = None   # 换文件后激活态失效
        self.hidden_signals = set()
        self.curve_style = {}
        self.sample_points_on = set()
        self.cursor_label.setText("拖动红色标尺线，右栏「选中信号」读数")
        self.local_label.setText("本地时间：—")
        self._sync_rule_signals()
        # 默认只勾选规则集所需信号；没规则就空画布，由用户主动「添加信号」
        self.checked = {n for n in self.rule_signals if n in self.signal_map}
        self._fill_signal_tree()
        if auto:
            self.run_analysis()

    def _on_blf_loaded(self, m, token):
        if token != self._blf_load_token:
            return
        self._blf_close_progress()
        if "nocache" in self._blf_bad:
            self.blf_session = None     # 变异：丢掉会话，下次加载被迫重扫
        self.m = m
        rs, marks = self._load_all_rules()
        if rs:
            self.rule_marks = marks
            self.result = caliper_rules.evaluate(m, rs)
        else:
            self.rule_marks = {}
            self.result = None
        self.render()

    def edit_blf_map(self):
        if not self.dat_path or not self.dat_path.lower().endswith(".blf"):
            return None
        keys = [os.path.splitext(os.path.basename(p))[0] for p in self.blf_dbc_paths]
        dlg = BlfMapDialog(self.blf_channels, self.blf_priority, keys, self)
        _center_on_screen(dlg)
        if dlg.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return None
        cfg = self._blf_config()
        if dlg.new_dbc_paths:
            cfg["dbc_paths"] = self._stage_dbcs(dlg.new_dbc_paths)   # 复制到 DBC/ + 存文件名
        # 没重选时保留 cfg 里原有的文件名，不写回完整路径
        cfg["channel_priority"] = dlg.priority_override()
        self._blf_save_config(cfg)
        self.open_blf(self.dat_path)
        return dlg

    # ---- 拖放 ----
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if urls:
            path = urls[0].toLocalFile()
            if path.lower().endswith((".dat", ".mf4", ".blf")):
                self.open_file(path)
        event.acceptProposedAction()

    def pick_file(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "选择测量文件", "", "MDF/BLF (*.dat *.mf4 *.blf)")
        if path:
            self.open_file(path)

    # ---- 业务入口 ----
    def open_file(self, path, auto=True):
        """打开任意测量文件：运行时扫描全部信号，不要求在 P0 清单里登记。

        auto=True 时立即分析并出曲线；verify 用 auto=False 避免重复跑。
        """
        if path.lower().endswith(".blf"):
            return self.open_blf(path, auto=auto)
        self.dat_path = path
        self.blf_session = None     # 换到 MDF：释放 BLF 原始报文缓存
        self.signal_desc = SIGNAL_DESC  # 换回 .dat：描述改回 JSON（不再用上一个 BLF 的 DBC comment）
        self.scan_info = caliper_core.scan(path)
        self.signal_map = {s["name"]: (s["group"], s["index"])
                           for s in self.scan_info["signals"]}
        self.units = {s["name"]: s["unit"] for s in self.scan_info["signals"]}
        self.brand_sub.setText("%s · %d 个信号" % (os.path.basename(path), len(self.signal_map)))
        self.axis_ranges = {}       # 换文件后旧信号的分轴量程不再有意义
        self.rule_marks_visible = {}  # 换文件后旧的标注显隐不再有意义
        self.hidden_signals = set()
        self.curve_style = {}
        self.sample_points_on = set()
        self.cursor_x = None        # 换文件后旧标尺位置不再有意义
        self.cursor2_x = None
        self.local_time_text = ""
        self.last_jump = None
        self._seg_nav = {}          # 换文件后段游标失效
        self._clicked_rule = None   # 换文件后激活态失效
        self.cursor_label.setText("拖动红色标尺线，图例上读数")
        self.local_label.setText("本地时间：—")
        self._sync_rule_signals()
        # 默认只勾选规则集所需信号；没规则就空画布，由用户主动「添加信号」
        self.checked = {n for n in self.rule_signals if n in self.signal_map}
        self._fill_signal_tree()
        if auto:
            self.run_analysis()

    def _rules_to_evaluate(self):
        """当前参与评估的规则集路径列表：多选列表优先，否则主规则（兼容单选）。"""
        return list(self.rules_paths) if self.rules_paths else (
            [self.rules_path] if self.rules_path else [])

    def _load_all_rules(self):
        """加载全部选中规则集的 rules 并合并；返回 (合并 rules, rule_marks)。"""
        rs, marks = [], {}
        for p in self._rules_to_evaluate():
            part = caliper_rules.load_ruleset(p)
            rs.extend(part)
            marks.update({r.name: dict(r.mark or {}) for r in part})
        return rs, marks

    def _sync_rule_signals(self):
        """规则所需信号 = 全部选中规则集的并集。"""
        self.rule_signals = {s for p in self._rules_to_evaluate()
                             for r in caliper_rules.load_ruleset(p) for s in r.required_signals}

    def choose_multi_rules(self):
        """工具栏「多规则集」：勾选多个一起评估；主规则取第一个供编辑/导出命名。"""
        items = sorted(self.rule_paths.items(), key=lambda kv: kv[0])
        current = set(self._rules_to_evaluate())
        dlg = MultiRulesDialog(items, current, self)
        if dlg.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return None
        paths = dlg.selected_paths()
        self.rules_paths = paths
        self.rules_path = paths[0] if paths else None   # 主规则：编辑/导出用
        self._sync_rule_signals()
        self.rule_scope_label.setText(
            ("%d 个规则集" % len(paths)) if len(paths) > 1 else
            (os.path.basename(paths[0]) if paths else "未加载"))
        if self.rule_signals and self.signal_map:
            missing = {n for n in self.rule_signals if n in self.signal_map} - self.checked
            if missing:
                self.checked |= missing
                self.set_checked(missing, True)
                return paths
        if self.dat_path:
            self.run_analysis()
        return paths

    def load_rules(self, path):
        self.rules_path = path
        self.rules_paths = [path] if path else []      # 单选入口：评估列表同步为单条
        self._sync_rule_signals()
        if self.rule_signals and self.signal_map:
            missing = {n for n in self.rule_signals if n in self.signal_map} - self.checked
            if missing:
                self.checked |= missing
                self.set_checked(missing, True)
                return

    def _blf_incremental(self, sess, wanted):
        """勾选只增加信号时的快路径：只解码新增那几条，已有 ndarray 原地复用。

        返回 True 表示已就地更新 self.m 并完成评估渲染；返回 False 表示条件不满足，
        调用方必须退回全量 load。不新增信号、有信号被移除、引入更密时基、或变异
        noincr 时都走全量——保证判定与数值和全量路径逐值一致。
        """
        if "noincr" in self._blf_bad or self.m is None or self.m.mdf_version != "BLF":
            return False
        old = set(self.m.requested)
        added = set(wanted) - old
        if not added or old - set(wanted):        # 纯新增才算增量；有移除就全量重建
            return False
        axis = self.m.axis
        if not len(axis):
            return False
        # 新信号所属报文比当前时基更密时会改变 axis -> 所有信号都要重新对齐 -> 回退全量
        if any(sess.frame_count(n) > len(axis) for n in added):
            return False
        for name in sorted(added):
            got = sess.extract_signal(name, axis)
            if got is None:                       # 新信号没录到：记入未采集，不报错
                self.m.not_collected.append(name)
                self.m.readback_ok[name] = False
                continue
            values, unit, ok = got
            self.m.signals[name] = values         # 已有信号的 ndarray 一个都不碰
            self.m.units[name] = unit
            self.m.readback_ok[name] = ok
        self.m.requested = list(wanted)
        self._reevaluate_and_render()
        return True

    def _reevaluate_and_render(self):
        """增量更新 self.m 后重跑规则与渲染（规则信号没变，结果应与全量一致）。"""
        rs, marks = self._load_all_rules()
        if rs:
            self.rule_marks = marks
            self.result = caliper_rules.evaluate(self.m, rs)
        else:
            self.rule_marks = {}
            self.result = None
        self.render()

    def run_analysis(self):
        """加载 -> （有规则才）评估 -> 渲染。规则所需信号始终加载，勾选信号追加加载并画曲线。"""
        if not self.dat_path:
            raise RuntimeError("未指定数据文件")
        wanted = sorted(self.rule_signals | self.checked)
        if self.dat_path.lower().endswith(".blf"):
            self._blf_load_token += 1
            token = self._blf_load_token
            sess = self.blf_session
            if sess is not None and not sess.reusable(self.dat_path, self.blf_dbc_paths or []):
                sess = None
            # 纯勾选且会话可用：先试增量，只解码新增信号（勾一个加一个不卡）
            if (sess is not None and not self._blf_sync
                    and self._blf_incremental(sess, wanted)):
                return self.result
            if self._blf_sync:
                try:
                    if sess is not None:
                        m = sess.load(wanted)
                    else:
                        m = caliper_core.load_blf(self.dat_path, signals=wanted,
                                              dbc_paths=self.blf_dbc_paths,
                                              channel_priority=self.blf_priority)
                except Exception as e:
                    self._blf_failed(str(e))
                    return None
                self._on_blf_loaded(m, token)
                return self.result
            if sess is not None:
                self._blf_progress_dialog("解码选中信号…", 100)
                w = BlfLoadWorker(sess, wanted, parent=self)
            else:
                # 无会话（扫描被跳过/文件已变）：退回完整单遍加载
                self._blf_progress_dialog("扫描并解码选中信号…", 100)
                w = BlfFullLoadWorker(self.dat_path, wanted, self.blf_dbc_paths,
                                      self.blf_priority, parent=self)
            w.progress.connect(self._blf_on_progress)
            w.finished_ok.connect(lambda m, tok=token: self._on_blf_loaded(m, tok))
            w.failed.connect(self._blf_failed)
            self._blf_worker = w
            w.start()
            return None
        self.m = caliper_core.load(self.dat_path, signals=wanted, signal_map=self.signal_map)
        self._reevaluate_and_render()
        return self.result

    def render(self):
        """画所有勾选信号的曲线，叠加工况区间带与标记点，填充三态清单。

        两种轴模式：共用轴（单图多曲线，`self.plot`）与分轴（每信号一个子图一个 Y 轴，
        `self.stack`）。区间带与起点标记在两种模式下都画；`region_intervals` 记区间个数，
        与轴数无关，`len(self.regions)` / `marker_count` 记实际画出的图形个数（分轴会随轴倍增）。
        """
        # X 轴显示零点：BLF 轴是 epoch 秒（~1.8e9），用首帧做零点显示相对秒；
        # MDF 轴本来就接近 0，保持零点 0。只影响刻度文字，m.axis 数据一个字节不动。
        self._x_origin = (float(self.m.axis[0]) if (self.m is not None
                          and len(self.m.axis) and self.m.mdf_version == "BLF") else 0.0)
        self.plot.getPlotItem().getAxis("bottom").set_origin(self._x_origin)
        names = []
        listed = []
        if self.m is not None and len(self.m.axis):
            listed = sorted(n for n in self.checked if n in self.m.signals)
            names = [n for n in listed if n not in self.hidden_signals]
        # 色点/曲线颜色按「列出的信号」顺序分配：隐藏某条不会让其余信号变色
        for idx, n in enumerate(listed):
            st = self.curve_style.get(n) or {}
            c = st.get("color")
            self.curve_colors[n] = (c if isinstance(c, str)
                                    and re.fullmatch(r"#[0-9A-Fa-f]{6}", c)
                                    else CURVE_COLORS[idx % len(CURVE_COLORS)])

        # 先清空再重置容器：清空会销毁图形项，旧引用不可再用
        self.plot.clear()
        self.stack.clear()
        self.axis_plots = []
        self.cursors = []
        self.regions = []
        self.rule_regions = {}
        self.rule_markers = {}
        self.sample_items = {}
        self.region_intervals = 0
        self.marker_count = 0
        self.curve_count = 0

        hits = [r for r in (self.result.results if self.result else [])
                if r.status == "hit" and r.intervals]
        self.region_intervals = sum(len(r.intervals) for r in hits) if names else 0

        if self.per_axis and names:
            self._render_per_axis(names, hits)
        else:
            self._render_shared(names, hits)

        # 不再重建信号树：树只在 open_file 时建一次，渲染只更新曲线与清单
        self._fill_rule_tree()
        self._apply_y_view()   # 清空会重置视图，手动量程必须重贴
        self._apply_rule_mark_visibility()   # 勾选掉的规则重绘后仍保持隐藏
        self._refresh_value_tab()            # 信号值 Tab 随勾选与标尺同步
        if self.cursor_x is not None:      # 标尺位置也要跨重绘保留
            self._set_cursor(self.cursor_x)
        if self.cursor2_x is not None:
            self._set_cursor2(self.cursor2_x)
        if self.cursor_x is None or self.cursor2_x is None:
            self._place_cursor_at_data_mid()

    def _place_cursor_at_data_mid(self):
        """用户还没动过的标尺摆进数据视野：cursor1 在 1/3、cursor2 在 2/3，错开可见。

        BLF 时基是 1.8e9，钉在 0 会跑到视野外十亿秒。只挪线、不设位置状态、
        不刷读数，保持"未动标尺时值列显示 —"的语义。
        """
        if self.m is None or not len(self.m.axis):
            return
        n = len(self.m.axis)
        t1 = float(self.m.axis[n // 3])
        t2 = float(self.m.axis[2 * n // 3])
        self._syncing_cursor = True
        try:
            if self.cursor_x is None:
                for line in self._all_cursor_lines(1):
                    line.setValue(t1)     # 屏蔽 sigPositionChanged，不触发 _on_cursor_moved
            if self.cursor2_x is None:
                for line in self._all_cursor_lines(2):
                    line.setValue(t2)
        finally:
            self._syncing_cursor = False

    def _rule_colors(self, hits):
        """每条命中规则的标注色：规则色板与曲线色板完全独立，色差防冲突。

        mark.color 只有与全部曲线色「明显可区分」（色差 ≥150）、且与已选规则色
        不撞（≥100）时才被采用；否则从规则专用色板 RULE_MARK_COLORS 顺序分配
        （同样跳过与已选色撞色的候选）。规则文件里的颜色不保证可用，辨识度优先。
        """
        out, used = {}, []
        n = len(RULE_MARK_COLORS)
        for i, r in enumerate(hits):
            c = self.rule_marks.get(r.rule_name, {}).get("color")
            ok = (isinstance(c, str) and re.fullmatch(r"#[0-9A-Fa-f]{6}", c)
                  and all(_color_dist(c, cc) >= RULE_CURVE_MIN_DIST for cc in CURVE_COLORS)
                  and all(_color_dist(c, u) >= RULE_PAIR_MIN_DIST for u in used))
            if not ok:
                cands = RULE_MARK_COLORS[i % n:] + RULE_MARK_COLORS[:i % n]
                c = next((x for x in cands
                          if all(_color_dist(x, u) >= RULE_PAIR_MIN_DIST for u in used)),
                         cands[0])
            out[r.rule_name] = c
            used.append(c)
        return out

    def _apply_rule_mark_visibility(self):
        """按右栏勾选状态显隐各规则的区间带与标记点。只改可见性，不重绘、不重建树。"""
        for name, items in self.rule_regions.items():
            vis = self.rule_marks_visible.get(name, True)
            for it in items:
                it.setVisible(vis)
        for name, items in self.rule_markers.items():
            vis = self.rule_marks_visible.get(name, True)
            for it in items:
                it.setVisible(vis)

    def on_rule_mark_toggled(self, item, column):
        """命中条目复选框：勾选=在图上标注，取消=隐藏该规则的区间带与标记点。

        只切图形项可见性，绝不在此回调里重建清单树（itemChanged 里析构树项会段错）。
        """
        if self._loading_rule_tree or column != 0:
            return
        name = item.data(0, RULE_NAME_ROLE)
        if not name or not (item.flags() & QtCore.Qt.ItemFlag.ItemIsUserCheckable):
            return
        self.rule_marks_visible[name] = (
            item.checkState(0) == QtCore.Qt.CheckState.Checked)
        self._apply_rule_mark_visibility()

    def _add_region(self, plot_item, t0, t1, color):
        """在指定子图上画一条工况区间带（用该规则自己的颜色），登记到 self.regions。

        白底上 alpha 要压得很低：几段区间叠在子图上时，太重会盖掉曲线本身。
        """
        # 填充压到几乎只有一层薄雾，靠两条边界线把区间边界说清楚：
        # 区间带是提示，不是主体，不能盖过曲线。
        region = pg.LinearRegionItem(values=[t0, t1], movable=False,
                                     brush=pg.mkBrush(_alpha(color, 30)),
                                     pen=pg.mkPen(_alpha(color, 160), width=1),
                                     hoverBrush=pg.mkBrush(_alpha(color, 45)))
        region.setZValue(-20)        # 垫在曲线与网格之间，不挡住数据
        plot_item.addItem(region)
        self.regions.append(region)
        return region

    def _render_shared(self, names, hits):
        """共用轴：所有曲线画在一张图上，共用一个 Y 轴（不同量纲会互相压扁，这是已知取舍）。

        区间带与标记点逐规则着色：每条子规则用自己的 mark.color（缺省按色板），
        标记点按规则分组，供右栏复选框逐规则显隐。
        """
        self.plot.addItem(self.cursor, ignoreBounds=True)   # 标尺 boundingRect 随视野变，参与量程会反馈放大
        self.plot.addItem(self.cursor2, ignoreBounds=True)
        self.plot.getPlotItem().setTitle(None)      # 有数据了，去掉空状态提示
        self._apply_plot_style(self.plot.getPlotItem())
        for name in names:
            self._plot_series(self.plot.getPlotItem(), self.m.axis,
                              self.m.signals[name], self.curve_colors[name],
                              style=self.curve_style.get(name))
            self.curve_count += 1
            self._add_sample_points(self.plot.getPlotItem(), name)
        self.plot.setLabel("left", ("%d 条曲线叠加" % len(names)) if names else "—")
        colors = self._rule_colors(hits)
        for r in hits:
            color = colors[r.rule_name]
            marker_x, marker_y = [], []
            for iv in r.intervals:
                reg = self._add_region(self.plot, iv["t0"], iv["t1"], color)
                self.rule_regions.setdefault(r.rule_name, []).append(reg)
                marker_x.append(iv["t0"])
                # 标记点贴在第一条曲线自己的实测值上，不再借档位快照当 y（会落到图表底边外）
                marker_y.append(self._value_at(names[0], iv["t0"]) if names else 0.0)
            if marker_x:
                sc = pg.ScatterPlotItem(
                    x=marker_x, y=marker_y, symbol="o", size=9,
                    pen=pg.mkPen(BG_CANVAS, width=1.6), brush=pg.mkBrush(color))
                self.plot.addItem(sc)
                self.rule_markers.setdefault(r.rule_name, []).append(sc)
                self.marker_count += len(marker_x)

    def _plot_series(self, plot_item, x, y, color, fill=False, style=None):
        """画一条曲线：折线（默认）/ 阶梯 / 散点三种连线类型，线宽与颜色可按信号改。

        阶梯用 stepMode='left'，与内核 last-value-hold 语义一致（值保持到下一个采样点）。
        数据是 10 ms 栅格，几万到几十万点，clipToView + peak 降采样才能拖得动。
        常量信号（跨度 0）不填充：它填出来是一整块色块，只会盖住网格。
        """
        st = style or {}
        kind = st.get("kind", "line")
        width = float(st.get("width", 1.8))
        if kind == "scatter":
            item = plot_item.plot(x, y, pen=None, symbol="o", symbolSize=4,
                                  symbolBrush=pg.mkBrush(color), symbolPen=None)
        elif kind == "step":
            item = plot_item.plot(x, y, pen=pg.mkPen(color, width=width),
                                  stepMode="left")
        else:
            item = plot_item.plot(x, y, pen=pg.mkPen(color, width=width))
        arr = np.asarray(y, dtype=float)
        finite = arr[np.isfinite(arr)]
        span = float(finite.max() - finite.min()) if finite.size else 0.0
        if fill and kind != "scatter" and span > 0:
            item.setFillLevel(float(finite.min()))
            item.setBrush(pg.mkBrush(_alpha(color, 16)))
        item.setClipToView(True)
        item.setDownsampling(auto=True, method="peak")
        return item

    def _add_sample_points(self, plot_item, name):
        """显示采样点：把该信号每一个采样到值的点画出来（不降采样、不插值）。"""
        if name not in self.sample_points_on:
            return
        sc = pg.ScatterPlotItem(x=self.m.axis, y=self.m.signals[name],
                                symbol="o", size=3, pen=None,
                                brush=pg.mkBrush(self.curve_colors[name]))
        plot_item.addItem(sc)
        self.sample_items.setdefault(name, []).append(sc)

    def _render_per_axis(self, names, hits):
        """分轴：每个信号一个子图、一个独立 Y 标尺，X 轴联动且只在最下方标刻度。

        信号名与单位放子图标题（横排、一眼可读），Y 轴只留纯刻度数字：
        长信号名竖排在轴上会把轴撑得很宽，也会和刻度叠在一起。
        """
        last = len(names) - 1
        top = None
        colors = self._rule_colors(hits)
        self.stack.ci.layout.setVerticalSpacing(6)     # 子图之间留缝，刻度不互相贴脸
        for idx, name in enumerate(names):
            pi = self.stack.addPlot(row=idx, col=0, viewBox=MdaViewBox(),
                                    axisItems={"bottom": RelativeTimeAxis(orientation="bottom")})
            pi.getViewBox().sigDoubleClick.connect(self._set_cursor)        # 左键双击 -> cursor1
            pi.getViewBox().sigRightDoubleClick.connect(self._set_cursor2)  # 右键双击 -> cursor2
            pi.getAxis("bottom").set_origin(self._x_origin)   # 分轴横轴同样显示相对秒
            self._apply_plot_style(pi)
            pi.setLabel("left", "")                    # 刻度留着，名字交给标题
            pi.setTitle(self._axis_label(name), size="9pt", color=TEXT_DIM)
            if idx == 0:
                top = pi
            else:
                pi.setXLink(top)                     # 缩放/平移联动
            if idx == last:
                pi.setLabel("bottom", "时间 / s")     # 最下方子图承担时间轴
            else:
                pi.setLabel("bottom", "")
                pi.getAxis("bottom").setStyle(showValues=False)   # 不标刻度、不占高度
            color = self.curve_colors[name]
            self._plot_series(pi, self.m.axis, self.m.signals[name], color,
                              fill=True, style=self.curve_style.get(name))
            self.curve_count += 1
            self._add_sample_points(pi, name)

            # 0 基线：不参与自动量程，只做视觉参照
            zero = pg.InfiniteLine(pos=0, angle=0,
                                   pen=pg.mkPen(BORDER_HI, width=1,
                                                style=QtCore.Qt.PenStyle.DashLine))
            zero.setZValue(-15)
            pi.addItem(zero, ignoreBounds=True)

            line1 = self._make_cursor_line("")      # cursor1 红：读数由 _refresh_cursor_readout 填
            pi.addItem(line1, ignoreBounds=True)   # 标尺不参与量程（同共用轴）
            line2 = self._make_cursor_line("", CURSOR2_COLOR, CURSOR2_HOVER)
            pi.addItem(line2, ignoreBounds=True)
            self.cursors.append((name, line1, line2))

            for r in hits:
                color = colors[r.rule_name]
                marker_x, marker_y = [], []
                for iv in r.intervals:
                    reg = self._add_region(pi, iv["t0"], iv["t1"], color)
                    self.rule_regions.setdefault(r.rule_name, []).append(reg)
                    marker_x.append(iv["t0"])
                    marker_y.append(self._value_at(name, iv["t0"]))
                if marker_x:
                    sc = pg.ScatterPlotItem(
                        x=marker_x, y=marker_y, symbol="o", size=7,
                        pen=pg.mkPen(BG_CANVAS, width=1.4), brush=pg.mkBrush(color))
                    pi.addItem(sc)
                    self.rule_markers.setdefault(r.rule_name, []).append(sc)
                    self.marker_count += len(marker_x)

            self._attach_menu(pi.getViewBox(), name)   # 每个子图各有一套量程菜单
            self.axis_plots.append((name, pi))
        self._align_y_axes()        # Y 标尺统宽 -> 左对齐

    def _axis_label(self, name):
        unit = (self.units.get(name) or "").strip()
        return "%s [%s]" % (name, unit) if unit else name

    def _value_at(self, name, t):
        """取某信号在时刻 t 的最近邻实测值；非有限值按 0 处理，保证标记个数可预期。"""
        axis = self.m.axis
        vals = self.m.signals[name]
        i = int(np.searchsorted(axis, t))
        i = max(0, min(i, len(axis) - 1))
        if i > 0 and abs(float(axis[i - 1]) - t) <= abs(float(axis[i]) - t):
            i -= 1
        try:
            v = float(vals[i])
        except (TypeError, ValueError):
            return 0.0
        return v if math.isfinite(v) else 0.0

    def _setup_value_selection(self):
        """「选中信号」Tab 行选择：Ctrl+点击多选、Ctrl+A 全选；高亮沿用全局选中样式。"""
        self.value_tree.setSelectionMode(
            QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.value_tree.setSelectionBehavior(
            QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        sc = QtGui.QShortcut(QtGui.QKeySequence.StandardKey.SelectAll, self.value_tree)
        sc.setContext(QtCore.Qt.ShortcutContext.WidgetShortcut)
        sc.activated.connect(self.value_tree.selectAll)
        sc_del = QtGui.QShortcut(QtGui.QKeySequence.StandardKey.Delete, self.value_tree)
        sc_del.setContext(QtCore.Qt.ShortcutContext.WidgetShortcut)
        sc_del.activated.connect(self.sel_remove)
        sc_copy = QtGui.QShortcut(QtGui.QKeySequence.StandardKey.Copy, self.value_tree)
        sc_copy.setContext(QtCore.Qt.ShortcutContext.WidgetShortcut)
        sc_copy.activated.connect(self.sel_copy)
        self.value_tree.setContextMenuPolicy(
            QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.value_tree.customContextMenuRequested.connect(self.on_value_context_menu)

    def _refresh_value_tab(self):
        """右栏「选中信号」Tab：示波器中各信号在标尺时刻的值（最近邻实测值，不插值）。

        行结构随勾选变化重建（按名字保留选中态）；标尺移动只更新值列，不重建树。
        """
        names = sorted(n for n in self.checked
                       if self.m is not None and n in self.m.signals)
        if (self.value_tree.topLevelItemCount() != len(names)
                or any(self.value_tree.topLevelItem(i).text(0) != n
                       for i, n in enumerate(names))):
            kept = {it.text(0) for it in self.value_tree.selectedItems()}
            self.value_tree.clear()
            for n in names:
                item = QtWidgets.QTreeWidgetItem(
                    [n, "—", "—", "—", (self.units.get(n) or "").strip()])
                item.setIcon(0, _dot_icon(self.curve_colors.get(n, TEXT_DIM), 6))
                for col in (1, 2, 3):
                    item.setTextAlignment(
                        col, QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
                item.setToolTip(0, n)
                self.value_tree.addTopLevelItem(item)
                if n in kept:
                    item.setSelected(True)
        x, x2 = self.cursor_x, self.cursor2_x
        for i, n in enumerate(names):
            item = self.value_tree.topLevelItem(i)
            v1 = self._value_at(n, x) if x is not None else None
            v2 = self._value_at(n, x2) if x2 is not None else None
            item.setText(1, _fmt_value(v1) + _enum_suffix(n, v1) if v1 is not None else "—")
            item.setText(2, _fmt_value(v2) + _enum_suffix(n, v2) if v2 is not None else "—")
            # Δ = C2 - C1，纯数值；两条都定位才算，符号保留
            item.setText(3, "%+.4g" % (v2 - v1) if (v1 is not None and v2 is not None) else "—")

    # ---- 「选中信号」选中操作（对标 MDA；全部 GUI 外壳层，引擎零改动） ----
    def _selected_signals(self):
        return [it.text(0) for it in self.value_tree.selectedItems()]

    def _set_checked_set(self, target):
        """批量设勾选态并只重绘一次（移除 / 仅显示共用）。"""
        self._batch = True
        try:
            for i in range(self.signal_tree.topLevelItemCount()):
                it = self.signal_tree.topLevelItem(i)
                it.setCheckState(
                    0, QtCore.Qt.CheckState.Checked if it.text(0) in target
                    else QtCore.Qt.CheckState.Unchecked)
        finally:
            self._batch = False
        self.checked = self._checked_from_tree()
        if self.dat_path:
            self.run_analysis()

    def sel_add_signals(self):
        """右栏右键「添加信号…」：从尚未勾选的可用信号里多选加入，走 set_checked 只重绘一次。"""
        available = sorted(n for n in self.signal_map if n not in self.checked)
        if not available:
            QtWidgets.QMessageBox.information(
                self, "没有可添加的信号", "所有可用信号都已在示波器中。")
            return []
        dlg = AddSignalsDialog(available, self, desc=self.signal_desc)
        if dlg.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return []
        names = dlg.selected_names()
        if names:
            self.set_checked(names, True)
        return names

    def sel_remove(self):
        """从示波器移除：取消勾选（左栏同步），区间带不受影响。"""
        sel = self._selected_signals()
        if sel:
            self._set_checked_set(self.checked - set(sel))

    def sel_solo(self):
        """仅显示选中：隐藏其他信号（勾选与值列保留），与「移除」语义区分。

        恢复：Ctrl+A 全选后「显示选中（取消隐藏）」即回到全部显示。
        """
        sel = set(self._selected_signals())
        if sel:
            listed = {n for n in self.checked
                      if self.m is not None and n in self.m.signals}
            self.hidden_signals = listed - sel
            self.render()

    def sel_hide(self):
        """隐藏选中：只藏曲线，勾选与值列保留（与「移除」不同）。"""
        sel = self._selected_signals()
        if sel:
            self.hidden_signals |= set(sel)
            self.render()

    def sel_show(self):
        sel = self._selected_signals()
        if sel:
            self.hidden_signals -= set(sel)
            self.render()

    def sel_copy(self):
        """复制为表格：选中行的 信号/值/单位 TSV 进剪贴板。"""
        sel = self._selected_signals()
        if not sel:
            return
        lines = ["信号\tC1\tC2\tΔ\t单位"]
        for i in range(self.value_tree.topLevelItemCount()):
            it = self.value_tree.topLevelItem(i)
            if it.text(0) in sel:
                lines.append("\t".join(it.text(c) for c in range(5)))
        QtWidgets.QApplication.clipboard().setText("\n".join(lines))

    def stats_rows(self, names):
        """全时程统计：numpy 独立重算，对话框与验收共用同一来源。"""
        rows = []
        for n in names:
            a = np.asarray(self.m.signals[n], dtype=float)
            fin = a[np.isfinite(a)]
            rows.append({
                "name": n,
                "min": float(fin.min()) if fin.size else None,
                "max": float(fin.max()) if fin.size else None,
                "mean": float(fin.mean()) if fin.size else None,
                "std": float(fin.std()) if fin.size else None,
                "nan": int(np.count_nonzero(np.isnan(a))),
                "count": int(a.size),
            })
        return rows

    def sel_stats(self):
        sel = self._selected_signals()
        if not sel:
            return None
        dlg = StatsDialog(self.stats_rows(sel), self)
        _center_on_screen(dlg)
        dlg.exec()
        return dlg

    def props_rows(self, names):
        rows = []
        q = self.m.quality if self.m is not None else {}
        for n in names:
            g, i = self.signal_map.get(n, (None, None))
            rows.append({
                "name": n, "unit": self.units.get(n) or "",
                "group": g, "index": i,
                "samples": int(len(self.m.axis)) if self.m is not None else 0,
                "raster_ms": q.get("raster_median_ms"),
                "nan": (q.get("nan_counts") or {}).get(n, 0),
            })
        return rows

    def sel_props(self):
        sel = self._selected_signals()
        if not sel:
            return None
        dlg = PropsDialog(self.props_rows(sel), self)
        _center_on_screen(dlg)
        dlg.exec()
        return dlg

    def export_csv(self, names, path, view_range=None):
        """导出选中信号 CSV：时间列 + 每信号一列；view_range=(lo,hi) 按视野切片。

        值取内核对齐后的数组（hold 语义），NaN 写空串；utf-8-sig 便于 Excel 直开。
        """
        axis = np.asarray(self.m.axis, dtype=float)
        if view_range is not None:
            lo, hi = view_range
            idx = np.flatnonzero((axis >= lo) & (axis <= hi))
        else:
            idx = np.arange(len(axis))
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            wr = csv.writer(f)
            wr.writerow(["time_s"] + list(names))
            for i in idx:
                row = [repr(float(axis[i]))]
                for n in names:
                    v = float(self.m.signals[n][i])
                    row.append("" if math.isnan(v) else repr(v))
                wr.writerow(row)
        return int(len(idx))

    def sel_export(self):
        sel = self._selected_signals()
        if not sel or self.m is None:
            return None
        dlg = ExportRangeDialog(self)
        _center_on_screen(dlg)
        if dlg.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return None
        vr = None
        if dlg.rb_view.isChecked():
            vr = tuple(self._active_plot().getViewBox().viewRange()[0])
        stem = os.path.splitext(os.path.basename(self.dat_path))[0]
        default = os.path.join(os.path.dirname(self.dat_path), stem + "_selected.csv")
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "导出选中信号", default, "CSV (*.csv)")
        if not path:
            return None
        n = self.export_csv(sel, path, vr)
        QtWidgets.QMessageBox.information(
            self, "已导出", "已写 %d 行 × %d 个信号：\n%s" % (n, len(sel), path))
        return path

    def sel_auto_y(self):
        """Y 轴量程自适应：复用示波器既有 enable_auto_y（分轴逐轴 / 共用轴全局）。"""
        sel = self._selected_signals()
        if not sel:
            return
        if self.per_axis:
            for n in sel:
                self.enable_auto_y(n)
        else:
            self.enable_auto_y()

    def apply_curve_style(self, names, style):
        """style=None 恢复默认；否则按信号写入样式并重绘。"""
        for n in names:
            if style is None:
                self.curve_style.pop(n, None)
            else:
                self.curve_style[n] = dict(style)
        self.render()

    def sel_style(self):
        sel = self._selected_signals()
        if not sel:
            return None
        dlg = CurveStyleDialog(self.curve_style.get(sel[0]) or {}, self)
        _center_on_screen(dlg)
        if dlg.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return None
        self.apply_curve_style(sel, dlg.values())
        return dlg

    def sel_samples(self, on):
        """显示采样点：每一个采样到值的点都画出来（开关按选中信号）。"""
        sel = self._selected_signals()
        if not sel:
            return
        if on:
            self.sample_points_on |= set(sel)
        else:
            self.sample_points_on -= set(sel)
        self.render()

    def on_value_context_menu(self, pos):
        sel = self._selected_signals()
        menu = QtWidgets.QMenu(self)
        acts = []
        # 添加信号不依赖选中态：列出尚未勾选的可用信号
        a_add = menu.addAction("添加信号…")
        a_add.triggered.connect(self.sel_add_signals)
        menu.addSeparator()
        for text, slot in [("从示波器移除\tDel", self.sel_remove),
                           ("仅显示选中", self.sel_solo),
                           ("隐藏选中", self.sel_hide),
                           ("显示选中（取消隐藏）", self.sel_show)]:
            a = menu.addAction(text)
            a.triggered.connect(slot)
            acts.append(a)
        menu.addSeparator()
        for text, slot in [("复制为表格\tCtrl+C", self.sel_copy),
                           ("统计信息…", self.sel_stats),
                           ("信号属性…", self.sel_props),
                           ("导出 CSV…", self.sel_export)]:
            a = menu.addAction(text)
            a.triggered.connect(slot)
            acts.append(a)
        menu.addSeparator()
        for text, slot in [("Y 轴量程自适应", self.sel_auto_y),
                           ("修改曲线…", self.sel_style)]:
            a = menu.addAction(text)
            a.triggered.connect(slot)
            acts.append(a)
        # 分轴显示复用示波器右键那个 act_per_axis：同一 QAction 挂两个菜单，
        # 勾选态与 set_per_axis 连接天然同步，不写第二套。全局视图，不随选中态禁用。
        menu.addSeparator()
        menu.addAction(self.act_per_axis)
        a = menu.addAction("显示采样点")
        a.setCheckable(True)
        a.setChecked(bool(sel) and all(n in self.sample_points_on for n in sel))
        a.toggled.connect(self.sel_samples)
        acts.append(a)
        for a in acts:
            a.setEnabled(bool(sel))
        menu.exec(self.value_tree.viewport().mapToGlobal(pos))

    def _active_plot(self):
        """当前可见的绘图对象：分轴取第一个子图（X 联动，改它就等于改全部）。"""
        if self.per_axis and self.axis_plots:
            return self.axis_plots[0][1]
        return self.plot.getPlotItem()

    # ---- 信号树：全部可用信号 + 勾选 + 搜索 ----
    def _fill_signal_tree(self):
        self._loading_tree = True
        try:
            self.signal_tree.clear()
            if self.scan_info is None:
                return
            units = {s["name"]: s["unit"] for s in self.scan_info["signals"]}
            for name in sorted(self.signal_map):
                item = QtWidgets.QTreeWidgetItem([name, units.get(name, "")])
                item.setFlags(item.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(0, QtCore.Qt.CheckState.Checked if name in self.checked
                                   else QtCore.Qt.CheckState.Unchecked)
                item.setTextAlignment(
                    1, QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
                item.setToolTip(0, name)     # 列宽不够时靠 tooltip 看全名
                self.signal_tree.addTopLevelItem(item)
            self.signal_header_label.setText("%d 个" % len(self.signal_map))
            self.filter_signals(self.search.text())
        finally:
            self._loading_tree = False

    def on_signal_toggled(self, item, column):
        """勾选变化 -> 重新收集全树状态 -> 重新加载并渲染。

        注意：这里绝不能重建信号树本身，否则在 itemChanged 回调中析构树项会导致段错。
        """
        if self._loading_tree or self._batch or column != 0:
            return
        self.checked = self._checked_from_tree()
        if self.dat_path:
            self.run_analysis()

    def _checked_from_tree(self):
        out = set()
        for i in range(self.signal_tree.topLevelItemCount()):
            item = self.signal_tree.topLevelItem(i)
            if item.checkState(0) == QtCore.Qt.CheckState.Checked:
                out.add(item.text(0))
        return out

    def set_checked(self, names, checked):
        """verify 用：批量勾选，走真实树项状态，只触发一次重绘。"""
        target = set(names)
        state = QtCore.Qt.CheckState.Checked if checked else QtCore.Qt.CheckState.Unchecked
        self._batch = True
        try:
            for i in range(self.signal_tree.topLevelItemCount()):
                item = self.signal_tree.topLevelItem(i)
                if item.text(0) in target:
                    item.setCheckState(0, state)
        finally:
            self._batch = False
        self.checked = self._checked_from_tree()
        if self.dat_path:
            self.run_analysis()

    def filter_signals(self, text):
        key = (text or "").lower()
        for i in range(self.signal_tree.topLevelItemCount()):
            item = self.signal_tree.topLevelItem(i)
            item.setHidden(bool(key) and key not in item.text(0).lower())

    # ---- 三态清单与回跳 ----
    def _fill_rule_tree(self):
        self._loading_rule_tree = True
        try:
            self.rule_tree.clear()
            if self.result is None:
                # 默认不加载规则：明确告诉用户是「没评估」，不是「0 命中」
                self.rule_tree.addTopLevelItem(
                    QtWidgets.QTreeWidgetItem(["未加载规则集（在顶栏选择规则集）", ""]))
                self._fit_rule_detail_column()
                return
            hits = [r for r in self.result.results if r.status == "hit" and r.intervals]
            colors = self._rule_colors(hits)
            groups = {}
            for key, label, dot in [("hit", "命中", HIT_COLOR),
                                    ("no_hit", "未命中", MISS_COLOR),
                                    ("not_evaluated", "未评估", NEUTRAL_COLOR)]:
                top = QtWidgets.QTreeWidgetItem([label, ""])
                top.setIcon(0, _dot_icon(dot, 7))
                top.setToolTip(0, "已评估，满足规则条件" if key == "hit"
                               else ("已评估，本文件无满足条件的时段" if key == "no_hit"
                                     else "本文件缺这条规则需要的信号，未做评估"))
                f = top.font(0)
                f.setWeight(QtGui.QFont.Weight.DemiBold)
                f.setPixelSize(11)
                top.setFont(0, f)
                top.setForeground(0, QtGui.QBrush(QtGui.QColor(TEXT_FAINT)))
                top.setBackground(0, QtGui.QBrush(QtGui.QColor("#EEF1F5")))
                self.rule_tree.addTopLevelItem(top)
                top.setFirstColumnSpanned(True)      # 分组头铺满整行，和条目分层
                groups[key] = top
            for r in self.result.results:
                if r.status == "not_evaluated":
                    detail = "缺 %d 个信号" % len(r.missing)
                    text = "未评估 · %s" % r.rule_name      # 状态前置：窄栏省略时也不会被裁掉
                    tip = "未评估：本文件没有 " + ", ".join(r.missing)
                else:
                    detail = ("%d 段" % len(r.intervals)) if r.status == "hit" else "—"
                    text = r.rule_name
                    tip = "%s：%d 个满足条件的时段" % (r.rule_name, len(r.intervals))
                child = QtWidgets.QTreeWidgetItem([text, detail])
                child.setToolTip(0, tip)
                child.setToolTip(1, tip)
                if r.status == "hit":
                    # 色点用该规则自己的标注色，与图上区间带/标记一一对应
                    dot = colors.get(r.rule_name, HIT_COLOR)
                    child.setIcon(0, _dot_icon(dot, 6))
                    child.setForeground(1, QtGui.QBrush(QtGui.QColor(HIT_COLOR)))
                    # 复选框：勾选=在图上标注，取消=隐藏（只切可见性，不重建树）
                    child.setFlags(child.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
                    child.setCheckState(
                        0, QtCore.Qt.CheckState.Checked
                        if self.rule_marks_visible.get(r.rule_name, True)
                        else QtCore.Qt.CheckState.Unchecked)
                    child.setToolTip(0, tip + "（取消勾选可隐藏图中标注）")
                else:
                    dot = MISS_COLOR if r.status == "no_hit" else NEUTRAL_COLOR
                    child.setIcon(0, _dot_icon(dot, 6))
                    # 默认 flags 含可勾选位：没区间的规则不给复选框，显式去掉
                    child.setFlags(child.flags() & ~QtCore.Qt.ItemFlag.ItemIsUserCheckable)
                    if r.status == "no_hit":
                        child.setForeground(0, QtGui.QBrush(QtGui.QColor(TEXT_FAINT)))
                        child.setForeground(1, QtGui.QBrush(QtGui.QColor(TEXT_FAINT)))
                child.setData(0, RULE_NAME_ROLE, r.rule_name)
                if r.intervals:
                    child.setData(0, USER_ROLE, [r.intervals[0]["t0"], r.intervals[0]["t1"]])
                groups[r.status].addChild(child)
            for top in groups.values():
                self.rule_tree.expandItem(top)
            self._fit_rule_detail_column()
        finally:
            self._loading_rule_tree = False

    def _jump_to_interval(self, t0, t1):
        """光标与视图跳到一段区间；左键单击与右键「下一段」共用这条链。"""
        self._set_cursor(t0)         # 标尺跳到该工况起点，读数（含本地时间）由它一并刷新
        span = max(t1 - t0, 1.0)
        self._active_plot().setXRange(t0 - span * 0.1, t1 + span * 0.1, padding=0)
        self.last_jump = (t0, t1)

    def on_item_clicked(self, item, column):
        """左键点击子规则 -> 跳该规则第一段。已选中高亮的那条再点无动作。

        不用 item.isSelected()：itemClicked 触发时 Qt 早已把它设为选中，永远为真。
        改用自己的 _clicked_rule 记录「当前激活的那条」——点新条目才跳转并更新，
        重复点同一条（视觉上正是选中高亮态）直接返回。
        """
        rule_name = item.data(0, RULE_NAME_ROLE)
        if rule_name and self._clicked_rule == rule_name:
            return
        self._clicked_rule = rule_name
        data = item.data(0, USER_ROLE)
        if not data:
            return
        # 重新进入一条规则：游标归到第一段。否则之前用「下一段」推进到的段号会残留，
        # 切走再切回虽然跳回第一段，下次「下一段」却从旧段号继续（跳到第四段而非第二段）。
        self._seg_nav[rule_name] = 0
        self._jump_to_interval(float(data[0]), float(data[1]))

    def next_segment(self, rule_name):
        """子规则右键「下一段」：逐段浏览该规则的命中区间。

        每条规则独立游标：首次跳第一段，再点跳下一段，末段再点循环回第一段。
        返回是否发生了跳转，供验收断言。
        """
        if not rule_name or self.result is None:
            return False
        rr = next((r for r in self.result.results if r.rule_name == rule_name), None)
        if rr is None or not rr.intervals:
            return False
        ivs = rr.intervals
        idx = self._seg_nav.get(rule_name, -1)
        nxt = 0 if idx < 0 else (idx + 1) % len(ivs)   # 末段后回到第一段，循环浏览
        iv = ivs[nxt]
        self._seg_nav[rule_name] = nxt
        self._jump_to_interval(float(iv["t0"]), float(iv["t1"]))
        return True

    def click_first_hit(self):
        """verify 用：模拟点击第一个命中条目。"""
        top = self.rule_tree.topLevelItem(0)
        for i in range(top.childCount()):
            child = top.child(i)
            if child.data(0, USER_ROLE):
                self.on_item_clicked(child, 0)
                return True
        return False


def main():
    """GUI / 验收统一入口：验收参数全部转发到 caliper_verify，否则正常起 GUI。"""
    # 不维护白名单：新增验收开关时不会漏（曾漏 --verify-cursor2 导致它误入 GUI 分支）。
    # 所有验收参数都以 --verify 或 --bad- 开头，未知项交给 caliper_verify 的 argparse 报错。
    if any(a.startswith("--verify") or a.startswith("--bad-") for a in sys.argv[1:]):
        from caliper_verify import run_cli_verify
        return run_cli_verify()

    app = QtWidgets.QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
