#!/usr/bin/env python3
# REAL payment → private CHANNEL (1 bot = 1 produk)
import asyncio, json, os, random, string, time, html
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ChatJoinRequestHandler, ContextTypes

CONFIG_PATH = "config.json"
UI_PATH     = "ui.json"
STATE_PATH  = "orders_state.json"
LOG_DIR     = "logs"
PAYLOG_PATH = os.path.join(LOG_DIR, "payments.jsonl")

def load_json(path: str) -> Dict[str, Any]:
    if not os.path.exists(path): return {}
    with open(path, "r", encoding="utf-8") as f: return json.load(f)

def save_json(path: str, obj: Dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, default=str)

def append_jsonl(path: str, row: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

def rupiah(n: int) -> str: return f"Rp{int(n):,}".replace(",", ".")
def now_utc() -> datetime: return datetime.now(timezone.utc)
def ts() -> str: return now_utc().strftime("%Y-%m-%d %H:%M:%S%z")
def gen_order_id(prefix: str="ORD"):
    suf = ''.join(random.choices(string.ascii_uppercase + string.digits, k=4))
    return f"{prefix}-{int(time.time())}-{suf}"

async def edit_or_reply(q, text, kb=None):
    try:
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb, disable_web_page_preview=True)
    except Exception:
        await q.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb, disable_web_page_preview=True)

def admin_log(app: Application, cfg: Dict[str, Any], text: str):
    for chat_id in cfg.get("admin_chat_ids", []):
        try: asyncio.create_task(app.bot.send_message(chat_id, text))
        except Exception: pass
    append_jsonl(PAYLOG_PATH, {"t": ts(), "msg": text})

MIN_M, MAX_M = 1, 12
def get_cart(state, user_id):
    carts = state.setdefault("carts", {})
    cart  = carts.setdefault(str(user_id), {"months": 1})
    cart["months"] = max(MIN_M, min(MAX_M, int(cart.get("months", 1))))
    return cart

def cart_text(ui, product_name, price_pm, months):
    est_exp = (datetime.now() + timedelta(days=30 * months)).strftime("%a, %d %b %Y")
    total = price_pm * months
    return ui["cart_template"].format(plan=product_name, months=months, expire_date=est_exp, price=rupiah(total))

def cart_kb(ui):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(ui["menu_titles"]["duration"], callback_data="noop")],
        [InlineKeyboardButton("−1", callback_data="month:-1"),
         InlineKeyboardButton("+1", callback_data="month:+1")],
        [InlineKeyboardButton(ui["buttons"]["cancel"],   callback_data="cancel"),
         InlineKeyboardButton(ui["buttons"]["continue"], callback_data="continue")]
    ])

def sub_key(chat_id: int, user_id: int) -> str: return f"{chat_id}|{user_id}"

async def schedule_revoke(app: Application, chat_id: int, user_id: int, expires_at: datetime):
    delay = max(0, (expires_at - now_utc()).total_seconds())
    app.job_queue.run_once(job_revoke, delay, data={"chat_id": chat_id, "user_id": user_id})

async def job_revoke(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    chat_id, user_id = data["chat_id"], data["user_id"]
    app  = context.application
    state = app.bot_data["state"]; cfg = app.bot_data["cfg"]
    subs = state.get("subs", {})
    key  = sub_key(chat_id, user_id)
    info = subs.get(key)
    if not info: return
    try:
        exp = datetime.fromisoformat(info["expires_at"])
    except Exception:
        exp = now_utc()
    if exp > now_utc():
        await schedule_revoke(app, chat_id, user_id, exp); return
    try:
        await app.bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
        await app.bot.unban_chat_member(chat_id=chat_id, user_id=user_id)
    except Exception as e:
        admin_log(app, cfg, f"[REVOKE_FAILED] user={user_id} chat={chat_id} err={e}")
    subs.pop(key, None); save_json(STATE_PATH, state)
    admin_log(app, cfg, f"[REVOKED] user={user_id} chat={chat_id} at={ts()}")
    try: await app.bot.send_message(user_id, "⛔️ Masa aktif channel berakhir. Perpanjang untuk akses lagi.")
    except Exception: pass

async def job_sweeper(context: ContextTypes.DEFAULT_TYPE):
    app = context.application
    state, cfg = app.bot_data["state"], app.bot_data["cfg"]
    now = now_utc()
    for k, v in list(state.get("subs", {}).items()):
        try: exp = datetime.fromisoformat(v["expires_at"])
        except Exception: exp = now
        if exp <= now:
            chat_id, user_id = map(int, k.split("|", 1))
            context.application.job_queue.run_once(job_revoke, 0, data={"chat_id": chat_id, "user_id": user_id})

async def fetch_transactions(cfg: Dict[str, Any]):
    url  = cfg["payments"].get("mutasi_url")
    token= cfg["payments"].get("auth_token")
    if not url: return []
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.get(url, headers={"Authorization": f"Bearer {token}"})
            r.raise_for_status()
            data = r.json()
            return data if isinstance(data, list) else data.get("transactions", [])
    except Exception as e:
        append_jsonl(PAYLOG_PATH, {"t": ts(), "msg": f"[HEALTH] fetch_transactions error {e}"})
        return []

def match_order(state, pending_orders: Dict[str, Any], tx: Dict[str, Any], window_hours=24):
    try:
        amount_paid = int(tx.get("amount", 0)); ttx = datetime.fromisoformat(tx.get("ts_iso"))
    except Exception:
        return None
    for oid, o in pending_orders.items():
        if o.get("status") != "PENDING": continue
        if int(o["amount_expected"]) != amount_paid: continue
        created = datetime.fromisoformat(o.get("created_at")) if o.get("created_at") else now_utc()
        if abs((ttx - created).total_seconds()) <= window_hours * 3600: return oid
    return None

async def job_poll_payments(context: ContextTypes.DEFAULT_TYPE):
    app = context.application
    cfg, state = app.bot_data["cfg"], app.bot_data["state"]
    txs = await fetch_transactions(cfg)
    if not txs: return
    pending = state.get("orders", {})
    processed = state.setdefault("processed_tx_ids", [])
    changed = False
    for tx in txs:
        tx_id = str(tx.get("tx_id") or tx.get("id") or "")
        if not tx_id or tx_id in processed: continue
        oid = match_order(state, pending, tx, window_hours=int(cfg["payments"].get("match_window_hours", 24)))
        if not oid:
            admin_log(app, cfg, f"[PAYMENT_UNMATCHED] amount={tx.get('amount')} time={tx.get('ts_iso')} note={tx.get('note')}")
            processed.append(tx_id); changed = True; continue
        o = pending[oid]
        o["status"] = "PAID_WAITING_JOIN"; o["paid_at"] = now_utc().isoformat(); o["tx_id"] = tx_id
        changed = True; processed.append(tx_id)
        try:
            invite = await app.bot.create_chat_invite_link(
                chat_id=o["chat_id"], expire_date=int(time.time()) + 300, creates_join_request=True
            )
        except Exception as e:
            admin_log(app, cfg, f"[ACCESS_LINK_FAILED] order={oid} user={o['user_id']} err={e}")
            continue
        guard = state.setdefault("guard", {})
        guard[invite.invite_link] = {"user_id": o["user_id"], "chat_id": o["chat_id"], "months": o["months"], "order_id": oid}
        save_json(STATE_PATH, state)
        msg = (f"✅ Pembayaran terverifikasi.\nDurasi: {o['months']} bulan\n\n"
               f"Ajukan join (5 menit):\n{invite.invite_link}\n\n"
               f"Masa aktif dihitung saat pengajuan Anda disetujui.")
        try: await app.bot.send_message(o["user_id"], msg, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        except Exception: pass
        admin_log(app, cfg, f"[PAID_MATCHED] order={oid} user={o['user_id']} amount={o['amount_expected']} tx={tx_id}")
    if changed: save_json(STATE_PATH, state)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg, ui, state = context.bot_data["cfg"], context.bot_data["ui"], context.bot_data["state"]
    prod = cfg["product"]
    await update.message.reply_text(ui.get("welcome", "Selamat datang."))
    cart = get_cart(state, update.effective_user.id); save_json(STATE_PATH, state)
    text = cart_text(ui, prod["name"], int(prod["price_per_month"]), cart["months"])
    await update.message.reply_text(text, reply_markup=cart_kb(ui))

async def cmd_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(context.bot_data["ui"].get("info", "-"))

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.bot_data["state"]; user_id = update.effective_user.id
    lines = []
    for k, v in state.get("subs", {}).items():
        chat_id_str, uid_str = k.split("|", 1)
        if uid_str != str(user_id): continue
        lines.append(f"• Chat {chat_id_str}: join {v.get('join_at')}, habis {v.get('expires_at')}")
    await update.message.reply_text("Tidak ada langganan aktif." if not lines else "Langganan aktif:\n" + "\n".join(lines))

async def cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg, ui, state = context.bot_data["cfg"], context.bot_data["ui"], context.bot_data["state"]
    q = update.callback_query; await q.answer()
    action, arg = q.data.split(":", 1) if ":" in q.data else (q.data, "")
    prod = cfg["product"]; price_pm = int(prod["price_per_month"]); channel_id = int(prod["chat_id"])
    user_id = update.effective_user.id
    if action in ("noop",):
        cart = get_cart(state, user_id); save_json(STATE_PATH, state)
        return await edit_or_reply(q, cart_text(ui, prod["name"], price_pm, cart["months"]), cart_kb(ui))
    if action == "month" and arg in ("+1", "-1"):
        cart = get_cart(state, user_id)
        cart["months"] = max(MIN_M, min(MAX_M, cart["months"] + (1 if arg == "+1" else -1)))
        save_json(STATE_PATH, state)
        return await edit_or_reply(q, cart_text(ui, prod["name"], price_pm, cart["months"]), cart_kb(ui))
    if action == "cancel":
        state.setdefault("carts", {}).pop(str(user_id), None); save_json(STATE_PATH, state)
        return await edit_or_reply(q, "❌ Keranjang dibersihkan.")
    if action == "continue":
        cart = get_cart(state, user_id)
        months = cart["months"]; total  = months * price_pm
        order_id = gen_order_id(cfg["payments"].get("order_prefix", "ORD"))
        amount  = total + random.randint(1, 999)
        o = {"order_id": order_id, "user_id": user_id, "months": months,
             "amount_expected": amount, "status": "PENDING", "chat_id": channel_id, "created_at": now_utc().isoformat()}
        state.setdefault("orders", {})[order_id] = o; save_json(STATE_PATH, state)
        instr = (f"✅ Order dibuat\n\nProduk: {html.escape(prod['name'])}\nDurasi: {months} bulan\n"
                 f"Total: {rupiah(total)}\nNominal unik transfer: <b>{rupiah(amount)}</b>\n\n"
                 f"Silakan bayar sesuai nominal unik. Setelah terdeteksi, bot akan kirim link pengajuan join.")
        pay = cfg.get("payments", {})
        try:
            if pay.get("qris_file_id"):
                await q.message.reply_photo(photo=pay["qris_file_id"], caption=instr, parse_mode=ParseMode.HTML)
            elif pay.get("qris_image_path") and os.path.exists(pay["qris_image_path"]):
                with open(pay["qris_image_path"], "rb") as f:
                    await q.message.reply_photo(photo=f, caption=instr, parse_mode=ParseMode.HTML)
            elif pay.get("qris_image_url"):
                await q.message.reply_photo(photo=pay["qris_image_url"], caption=instr, parse_mode=ParseMode.HTML)
            else:
                await edit_or_reply(q, instr)
        except Exception as e:
            await edit_or_reply(q, f"{instr}\n\n⚠️ QRIS gagal ditampilkan: {e}")
        return

async def on_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    req = update.chat_join_request
    link = req.invite_link.invite_link if req.invite_link else None
    app = context.application
    state, cfg = app.bot_data["state"], app.bot_data["cfg"]
    guard = state.get("guard", {}); g = guard.get(link)
    if not g or g["user_id"] != req.from_user.id or g["chat_id"] != req.chat.id:
        try: await context.bot.decline_chat_join_request(chat_id=req.chat.id, user_id=req.from_user.id)
        except Exception: pass
        return
    await context.bot.approve_chat_join_request(chat_id=req.chat.id, user_id=req.from_user.id)
    try: await context.bot.revoke_chat_invite_link(chat_id=req.chat.id, invite_link=link)
    except Exception: pass
    guard.pop(link, None)
    subs = state.setdefault("subs", {})
    key  = sub_key(req.chat.id, req.from_user.id)
    months = int(g.get("months", 1)); now = now_utc(); base = now
    if key in subs:
        try:
            cur_exp = datetime.fromisoformat(subs[key]["expires_at"])
            if cur_exp > now: base = cur_exp
        except Exception: pass
    expires_at = base + timedelta(days=30 * months)
    subs[key] = {"join_at": now.isoformat(), "expires_at": expires_at.isoformat(), "last_order_id": g.get("order_id")}
    save_json(STATE_PATH, state)
    await schedule_revoke(app, req.chat.id, req.from_user.id, expires_at)
    try:
        await context.bot.send_message(req.from_user.id,
            f"🎉 Akses channel diaktifkan.\nJoin: {now.strftime('%a, %d %b %Y %H:%M UTC')}\n"
            f"Habis: {expires_at.strftime('%a, %d %b %Y %H:%M UTC')}\nDurasi dibeli: {months} bulan")
    except Exception: pass
    admin_log(app, cfg, f"[GRANTED] user={req.from_user.id} chat={req.chat.id} months={months} until={expires_at.isoformat()}")

async def main():
    cfg, ui = load_json(CONFIG_PATH), load_json(UI_PATH)
    state = load_json(STATE_PATH) or {"orders": {}, "carts": {}, "subs": {}, "guard": {}}
    save_json(STATE_PATH, state)
    app = Application.builder().token(cfg["telegram_bot_token"]).build()
    app.bot_data.update({"cfg": cfg, "ui": ui, "state": state})
    for k, v in state.get("subs", {}).items():
        try:
            chat_id, user_id = map(int, k.split("|", 1))
            exp = datetime.fromisoformat(v["expires_at"])
            await schedule_revoke(app, chat_id, user_id, exp)
        except Exception: pass
    poll_sec = int(cfg["payments"].get("poll_interval_sec", 30))
    app.job_queue.run_repeating(job_poll_payments, interval=poll_sec, first=5)
    app.job_queue.run_repeating(job_sweeper, interval=600, first=60)
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("info",   cmd_info))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CallbackQueryHandler(cb, pattern=r"^(noop|month|cancel|continue)(:.*)?$"))
    app.add_handler(ChatJoinRequestHandler(on_join_request))
    print("REAL Channel Bot started."); await app.run_polling()

if __name__ == "__main__":
    try: asyncio.run(main())
    except RuntimeError:
        import nest_asyncio; nest_asyncio.apply()
        asyncio.get_event_loop().run_until_complete(main())
