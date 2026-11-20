# -*- coding: utf-8 -*-
"""
VCU 离线测量数据分析工具 —— 统一启动入口。

由 start.bat 调用，也可直接命令行使用：
  python start.py --check    只做环境自检，不启动 GUI（排查用）
  python start.py            自检通过后启动 GUI

关键约束：
- 运行时固定 D:\\Tools\\Python\\python.exe（3.9.6，实测已装齐依赖），
  不能靠 PATH —— PATH 里是 WorkBuddy 内置 3.13，未装依赖。
- pythonw.exe 无控制台，异常必须写 out/startup_error.log，禁止静默失败。
- 自检是「实际 import + 实际读文件」，不是只判断文件存在。
- rules/ 递归判空（规则集在 DAT/ / BLF/ / templates/ 子目录，不只看顶层）；
  真空时自愈：自动建 templates/ 并写一条最小测试规则，绝不因空目录挡住启动。
"""
import datetime
import importlib
import json
import os
import sys
import traceback

ROOT = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
        else os.path.dirname(os.path.abspath(__file__)))  # 打包后 exe 所在目录即数据根目录
OUT = os.path.join(ROOT, "out")
LOG = os.path.join(OUT, "startup_error.log")
CHANNELS = os.path.join(OUT, "channels.json")
RULES = os.path.join(ROOT, "rules")
DEPS = ["numpy", "asammdf", "PySide6", "pyqtgraph"]
MODULES = ["caliper_core", "caliper_rules", "caliper_gui"]


def find_rules(base, skip_dirs=("templates",)):
    """递归找 rules/ 下所有规则集 json，返回相对路径（排序稳定）。

    用户把规则集分到 DAT/ / BLF/ 子目录；只扫顶层会误判「目录为空」。
    默认跳过 templates/（那是模板不是可加载规则集），preflight 自愈会显式带上。
    """
    found = []
    for root, dirs, files in os.walk(base):
        dirs[:] = sorted(d for d in dirs if d not in skip_dirs)
        for name in files:
            if name.endswith(".json"):
                found.append(os.path.relpath(os.path.join(root, name), base).replace(os.sep, "/"))
    return sorted(found)


SEED_RULE = {
    "name": "示例规则集",
    "version": 1,
    "note": "启动时因规则集目录为空自动生成的最小样例，可自由编辑或删除。",
    "rules": [{
        "name": "示例条件", "when": "1 == 1",
        "min_duration_ms": 200, "hysteresis_ms": 50, "merge_gap_ms": 100,
        "mark": {"label": "示例"},
    }],
}


def seed_rules_if_empty(base=None):
    """rules/ 递归下一个 json 都没有时，建 templates/ 并写一条测试规则，返回是否生成。"""
    base = base or RULES
    if find_rules(base, skip_dirs=()):   # 含 templates 一起算，真空才自愈
        return False
    tpl = os.path.join(base, "templates")
    os.makedirs(tpl, exist_ok=True)
    seed = os.path.join(tpl, "seed_rule.json")
    if not os.path.exists(seed):
        with open(seed, "w", encoding="utf-8") as fh:
            json.dump(SEED_RULE, fh, ensure_ascii=False, indent=2)
    return True


def preflight():
    """返回错误列表；空列表表示可以启动 GUI。"""
    errors = []
    # 实际 import 才算通过，不用 find_spec —— 能抓到装了但导入失败的情况
    for mod in DEPS + MODULES:
        try:
            importlib.import_module(mod)
        except Exception as exc:
            errors.append("模块不可用 %s: %s" % (mod, exc))

    if not os.path.isfile(CHANNELS):
        errors.append("缺少 P0 通道清单: %s" % CHANNELS)
    else:
        try:
            with open(CHANNELS, encoding="utf-8") as fh:
                doc = json.load(fh)
            if not doc.get("files"):
                errors.append("P0 通道清单为空或格式错误: %s" % CHANNELS)
        except Exception as exc:
            errors.append("P0 通道清单无法解析: %s" % exc)

    # 递归判空（规则集可能在 DAT/ / BLF/ 子目录）；真空则自愈生成一条测试规则再验
    if not os.path.isdir(RULES):
        errors.append("规则集目录不存在: %s" % RULES)
    else:
        if seed_rules_if_empty():
            print("[preflight] 规则集目录为空，已自动生成 templates/seed_rule.json")
        if not find_rules(RULES, skip_dirs=()):
            errors.append("规则集目录为空: %s" % RULES)
    return errors


def write_log(text):
    """pythonw 无控制台，启动失败必须落盘，避免静默失败。"""
    try:
        os.makedirs(OUT, exist_ok=True)
        with open(LOG, "w", encoding="utf-8") as fh:
            fh.write(datetime.datetime.now().isoformat(timespec="seconds") + "\n")
            fh.write(text + "\n")
            fh.write("工作目录: %s\n" % ROOT)
            fh.write("Python: %s\n" % sys.executable)
            fh.write("请把 out/startup_error.log 发回开发者。\n")
    except Exception:
        pass


def launch_gui():
    errors = preflight()
    if errors:
        write_log("PREFLIGHT FAILED\n" + "\n".join(errors))
        return 1
    # 清掉旧失败日志：该文件存在即代表最近一次启动失败
    if os.path.exists(LOG):
        try:
            os.remove(LOG)
        except OSError:
            pass
    try:
        # 清掉参数，避免 caliper_gui 的 argparse 收到 bat 传进来的开关
        sys.argv = [os.path.join(ROOT, "caliper_gui.py")]
        import caliper_gui
        caliper_gui.main()   # 阻塞到窗口关闭；SystemExit 正常向上传播
        return 0
    except Exception:
        write_log(traceback.format_exc())
        return 1


def main():
    if "--check" in sys.argv:
        errors = preflight()
        if errors:
            for item in errors:
                print("PREFLIGHT ERROR:", item)
            print("请把窗口输出发回开发者。工作目录:", ROOT)
            return 1
        print("PREFLIGHT OK | Python", sys.version.split()[0], "|", sys.executable)
        print("依赖:", ", ".join(DEPS), "| 模块:", ", ".join(MODULES))
        print("通道清单:", CHANNELS, "| 规则集:", RULES)
        return 0
    return launch_gui()


if __name__ == "__main__":
    sys.exit(main())
