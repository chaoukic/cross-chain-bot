"""Builds (and with --send, sends) the 4 example alerts from the real CATE position. PAPER TRADING ONLY.
Every message is headed '🧪 EXAMPLE ONLY, no trade happened'. The token is never printed."""
import sys, time, json
from common import db, load_config
import notify, trader

cfg = load_config()
con = db()
p = dict(con.execute("SELECT * FROM positions WHERE symbol='CATE' ORDER BY id LIMIT 1").fetchone())
B = cfg["buckets"][p["bucket"]]
e, size, q = p["entry_price"], p["cost_usd"], p["qty"]
tp1, tp2, sl = e * (1 + B["take_profit_pct"] / 100), e * (1 + B["take_profit2_pct"] / 100), e * (1 - B["stop_loss_pct"] / 100)
half = q * B["take_profit_sell_fraction"]
cash0 = 1000 - size
held_part, held_full, held_sl = 3 * 3600 + 12 * 60, 26 * 3600 + 40 * 60, 5 * 3600 + 3 * 60
timing = {"pullback_pct": 7.6, "rise_before_pct": 42.8, "bounce_pct": 8.1, "vol_recover_ratio": 0.82}

m_buy = notify.fmt_buy(p["chain"], p["bucket"], p["symbol"], e, size, B, 999.41, url=p["url"], timing=timing, example=True)

pr1 = trader.sell_value(cfg, p, half, tp1)                     # first target: half sold
left = q - half
part_pnl = pr1 - size * half / q
bal1 = cash0 + pr1 + trader.sell_value(cfg, p, left, tp1)
m_part = notify.fmt_sell(p["chain"], p["symbol"], e, tp1, f"take profit 1 (+{B['take_profit_pct']:.1f}% >= +{B['take_profit_pct']}%)",
                         part_pnl, part_pnl / (size * half / q) * 100, held_part, bal1, bal1 - 1000, partial=True, sold_frac=half / q,
                         left_value=trader.sell_value(cfg, p, left, tp1), B=B, opened_at=p["opened_at"], url=p["url"], example=True)

pr2 = trader.sell_value(cfg, p, left, tp2)                     # final target: rest sold
pnl_full = pr1 + pr2 - size
bal2 = cash0 + pr1 + pr2
m_full = notify.fmt_sell(p["chain"], p["symbol"], e, tp2, f"take profit 2 (+{B['take_profit2_pct']:.1f}%)", pnl_full, pnl_full / size * 100,
                         held_full, bal2, bal2 - 1000, url=p["url"], after_partial=True, example=True)

pr3 = trader.sell_value(cfg, p, q, sl)                         # stop loss: everything sold
pnl_sl = pr3 - size
bal3 = cash0 + pr3
m_sl = notify.fmt_sell(p["chain"], p["symbol"], e, sl, f"stop loss (-{B['stop_loss_pct']:.1f}% <= -{B['stop_loss_pct']}%)", pnl_sl, pnl_sl / size * 100,
                       held_sl, bal3, bal3 - 1000, url=p["url"], example=True)

msgs = [("buy", m_buy), ("full sell (profit)", m_full), ("partial sell", m_part), ("stop-loss sell", m_sl)]
if "--send" not in sys.argv:
    for name, m in msgs:
        print(f"===== {name} (plain-text rendering) =====\n{notify.to_plain(m)}\n")
    sys.exit()
import telegram_bot
chat = telegram_bot.load_chat()
tg = telegram_bot.TG()
for i, (name, m) in enumerate(msgs):
    j = telegram_bot.send(tg, chat, m)
    print(name, "-> ok:", j.get("ok"), "| message_id:", (j.get("result") or {}).get("message_id"),
          "| parse_mode:", "HTML" if j.get("ok") and (j.get("result") or {}).get("entities") is not None else "?",
          "" if j.get("ok") else "| error: " + notify.redact(j.get("description")))
    if i < len(msgs) - 1:
        time.sleep(1.5)
