# -*- coding: utf-8 -*-
import sys
sys.stdout.reconfigure(encoding="utf-8")
import ir_fetch_proto as F
from urllib.parse import quote

cases = [
    ("interim report  (annual?=no)", "2025財務年度中期報告.pdf"),       # 中期報告 interim
    ("annual report   (年度報告)",    "2025年度報告.pdf"),                               # 年度報告
    ("annual (年報)",                 "2025年報.pdf"),                                            # 年報
    ("consolidated FS (綜合財務報表)", "2025綜合財務報表.pdf"),                   # 綜合財務報表
    ("annual report 2024",            "2024年度報告.pdf"),
    ("quarterly announce(季度公告)",  "2025季度業績公告.pdf"),                   # 季度...公告
    ("ESG report",                    "2025ESG可持續發展報告.pdf"),          # ESG 可持續
]
for label, fn in cases:
    url = "https://data.alibabagroup.com/ecms-files/x/" + quote(fn)
    print(f"{F.score_pdf(url, ''):+6.0f}   {label}")
