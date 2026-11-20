# -*- coding: utf-8 -*-
"""变量描述.xlsx -> 变量描述.json：{信号名: 中文描述}。

Excel 与生成的 JSON 都在 description/ 目录。改了 Excel 后自己跑一次：
  python xlsx_to_desc.py
界面运行时只读生成的 JSON，不依赖 Excel / openpyxl。
"""
import json
import os

import openpyxl

ROOT = os.path.dirname(os.path.abspath(__file__))
DESC_DIR = os.path.join(ROOT, "description")   # 变量描述 Excel / JSON 统一放这里
XLSX = os.path.join(DESC_DIR, "变量描述.xlsx")
OUT = os.path.join(DESC_DIR, "变量描述.json")
SHEET = "变量定义"
HEADER = ("变量名称", "变量描述")     # 第 1 行 A/B：脚本据此认表头， Excel 列序变了会明确报错


def main():
    wb = openpyxl.load_workbook(XLSX, read_only=True, data_only=True)
    try:
        ws = wb[SHEET]
        rows = ws.iter_rows(min_row=1, max_col=2, values_only=True)
        first = next(rows)                          # 先取一行再索引，别在推导式里反复 next
        header = (first[0], first[1])
        if header != HEADER:
            raise ValueError("表头不符：期望 %s，实际 %s" % (HEADER, header))
        desc, dup = {}, []
        for name, text in rows:                     # 第 2 行起：A=信号名 B=描述
            if name is None or not str(name).strip():
                continue
            name = str(name).strip()
            if name in desc:                        # 重复键会静默覆盖，必须挡住
                dup.append(name)
            desc[name] = "" if text is None else str(text).strip()
    finally:
        wb.close()

    if not desc:
        raise ValueError("Excel 里没读到任何变量，拒绝写空 JSON")
    if dup:
        raise ValueError("信号名重复 %d 个（前 5：%s），拒绝写出：%s"
                         % (len(dup), dup[:5], XLSX))

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(desc, f, ensure_ascii=False, indent=2, sort_keys=True)

    # 自检：读回确认可解析、条数一致，避免写了个坏文件自己不知道
    with open(OUT, encoding="utf-8") as f:
        back = json.load(f)
    assert len(back) == len(desc), "读回条数不符"
    print("转换完成：%s -> %s，共 %d 条" % (os.path.basename(XLSX),
                                         os.path.basename(OUT), len(desc)))


if __name__ == "__main__":
    main()
