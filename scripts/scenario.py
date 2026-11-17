"""生成确定性的虚构对账场景（无真实转账，全部为虚构主体与金额）。

Rows are returned as a dict with ``bank`` / ``ledger`` (initial load) and
``late`` (loaded later through the UI to demonstrate late arrival).

Scenario coverage
-----------------
  BK-2026-0001 / GL-2026-0001  一对一，金额完全一致，同一合同号
  BK-2026-0002 / GL-2026-0002  一对一，摘要关键词一致，借记
  BK-2026-0003 / GL-0003+0004  拆单：银行 4,800 = 账务 3,000 + 1,800
  BK-0004+0005+0006 / GL-0005  合单：银行 2,000+1,500+500 = 账务 4,000
  BK-2026-0007 / GL-2026-0006  抹零 0.50，落在策略容差内
  BK-2026-0009                 仅银行存在（未达账）
  GL-2026-0008                 仅账务存在（未达账）
  BK-2026-0010 / GL-2026-0009  相差 0.20 元 > 策略容差，人工可确认（会要求确认警告）
  BK-2026-0011 / GL-2026-0010  金额一致但属于干扰项，正常自动匹配
  BK-2026-0008 / GL-2026-0007  迟到流水，初始不加载；补录后自动匹配
  GL-2026-0011                 干扰项，仅账务，未被自动占用
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def row(source, ref, account, value_date, amount_yuan, direction, counterparty, memo, posted_at=None):
    cents = round(abs(float(amount_yuan)) * 100)
    return {
        "external_ref": ref,
        "account": account,
        "value_date": value_date,
        "posted_at": posted_at or f"{value_date}T18:00:00Z",
        "amount_cents": cents,
        "direction": direction,
        "counterparty": counterparty,
        "memo": memo,
        "currency": "CNY",
    }


def build() -> dict:
    bank = [
        row("bank", "BK-2026-0001", "6222-8801", "2026-09-01", 12500.00, "credit", "华辰贸易", "货款 合同 HT-2026-07 尾款"),
        row("bank", "BK-2026-0002", "6222-8801", "2026-09-03", 860.00, "debit", "云脉云服务", "云脉云服务 9月订阅"),
        row("bank", "BK-2026-0003", "6222-8801", "2026-09-05", 4800.00, "credit", "启明供应链", "货款 批次 QM-7781"),
        row("bank", "BK-2026-0004", "6222-8801", "2026-09-08", 2000.00, "credit", "恒远物流", "运费 结算单 HY-09-A"),
        row("bank", "BK-2026-0005", "6222-8801", "2026-09-08", 1500.00, "credit", "恒远物流", "运费 结算单 HY-09-A"),
        row("bank", "BK-2026-0006", "6222-8801", "2026-09-09", 500.00, "credit", "恒远物流", "运费 结算单 HY-09-A"),
        row("bank", "BK-2026-0007", "6222-8801", "2026-09-11", 3120.50, "debit", "锐思办公", "办公用品 PO-8842"),
        row("bank", "BK-2026-0009", "6222-8801", "2026-09-12", 9999.00, "credit", "未名咨询", "预收 未开票"),
        row("bank", "BK-2026-0010", "6222-8801", "2026-09-14", 5000.00, "credit", "澜山设计", "设计费 一期 合同 DS-31"),
        row("bank", "BK-2026-0011", "6222-8801", "2026-09-18", 2400.00, "debit", "澜山设计", "设计费 二期 合同 DS-31"),
    ]
    ledger = [
        row("ledger", "GL-2026-0001", "6222-8801", "2026-09-01", 12500.00, "credit", "华辰贸易", "应收账款 核销 HT-2026-07"),
        row("ledger", "GL-2026-0002", "6222-8801", "2026-09-03", 860.00, "debit", "云脉云服务", "管理费用 云脉云服务 9月"),
        row("ledger", "GL-2026-0003", "6222-8801", "2026-09-05", 3000.00, "credit", "启明供应链", "应收账款 启明 首笔 QM-7781"),
        row("ledger", "GL-2026-0004", "6222-8801", "2026-09-06", 1800.00, "credit", "启明供应链", "应收账款 启明 次笔 QM-7781"),
        row("ledger", "GL-2026-0005", "6222-8801", "2026-09-09", 4000.00, "credit", "恒远物流", "应付运费 汇总 HY-09-A"),
        row("ledger", "GL-2026-0006", "6222-8801", "2026-09-11", 3120.00, "debit", "锐思办公", "办公用品 PO-8842（抹零）"),
        row("ledger", "GL-2026-0008", "6222-8801", "2026-09-13", 1500.00, "credit", "应收暂估", "暂估收入 待核销"),
        row("ledger", "GL-2026-0009", "6222-8801", "2026-09-15", 4998.00, "credit", "澜山设计", "设计费 一期 折让后"),
        row("ledger", "GL-2026-0010", "6222-8801", "2026-09-18", 2400.00, "debit", "澜山设计", "设计费 二期"),
        row("ledger", "GL-2026-0011", "6222-8801", "2026-09-02", 5000.00, "credit", "星河传媒", "广告费 预付"),
    ]
    late = {
        "bank": [
            row("bank", "BK-2026-0008", "6222-8801", "2026-08-20", 760.00, "debit", "速通快递",
                "快递费 8月补录", posted_at="2026-09-15T09:00:00Z"),
        ],
        "ledger": [
            row("ledger", "GL-2026-0007", "6222-8801", "2026-08-20", 760.00, "debit", "速通快递", "快递费 8月"),
        ],
    }
    return {"bank": bank, "ledger": ledger, "late": late}


def main(argv: list[str]) -> int:
    out = Path(argv[1]) if len(argv) > 1 else Path("data/scenario.json")
    payload = build()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    late_out = Path(__file__).resolve().parent.parent / "web" / "static" / "late.json"
    late_out.parent.mkdir(parents=True, exist_ok=True)
    late_out.write_text(json.dumps(payload["late"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out}: bank={len(payload['bank'])} ledger={len(payload['ledger'])} "
          f"late={len(payload['late']['bank'])}+{len(payload['late']['ledger'])}")
    print(f"wrote {late_out} (网页“补入迟到流水”数据源)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
