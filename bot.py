async def generate_and_send_upi_payment(user_id: int, user_name: str, amount: float, context: ContextTypes.DEFAULT_TYPE, previous_message=None, prod_id: int = None, qty: int = None):
    wait_msg = None
    if previous_message:
        try:
            wait_msg = await previous_message.edit_text("⏳ <i>Generating Direct UPI Scanner...</i>", parse_mode="HTML")
        except Exception:
            pass

    ok, order_id, res_data = await create_smb_order(user_id, user_name, amount, currency="INR", prod_id=prod_id, qty=qty)
    if not ok:
        err_text = f"⚠️ <b>Gateway Error:</b> {res_data}\nPlease try again later or contact support."
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Back to Wallet", callback_data="cancel_payment_flow")]])
        if wait_msg:
            await wait_msg.edit_text(err_text, reply_markup=markup, parse_mode="HTML")
        else:
            await context.bot.send_message(chat_id=user_id, text=err_text, reply_markup=markup, parse_mode="HTML")
        return

    # Direct UPI Intent jar moddhe amount ebong order_id thakbe
    gateway_intent = res_data.get("intent_url") or res_data.get("upi_intent")
    if gateway_intent:
        upi_string = gateway_intent
    else:
        upi_string = f"upi://pay?pa={MERCHANT_VPA}&pn={urllib.parse.quote(BOT_NAME)}&am={amount:.2f}&cu=INR&tr={order_id}&tn={order_id}"

    qr_img = res_data.get("qr_code") or res_data.get("qr_image") or f"https://api.qrserver.com/v1/create-qr-code/?size=350x350&data={urllib.parse.quote(upi_string)}"

    cap = (
        f"⚡ <b>SCAN & PAY VIA ANY UPI APP</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💵 <b>Amount:</b> <code>{CURRENCY_SYMBOL}{amount:.2f}</code>\n"
        f"🆔 <b>Order ID:</b> <code>{order_id}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "📌 <b>Direct App Scan:</b>\n"
        "1. Scan directly using <b>Paytm, PhonePe, GPay, or BHIM</b>.\n"
        f"2. Pay exact amount: <b>{CURRENCY_SYMBOL}{amount:.2f}</b>.\n"
        "3. <b>Done!</b> Payment detects automatically in 3-5 seconds.\n\n"
        "🔄 <i>Auto-detecting payment status in real-time...</i>"
    )

    # Shudhu Cancel button thakbe, kono external payment page link thakbe na
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel / Back", callback_data=f"cancel_order_{order_id}")],
    ])

    if wait_msg:
        try:
            await wait_msg.delete()
        except Exception:
            pass

    sent_msg = await context.bot.send_photo(chat_id=user_id, photo=qr_img, caption=cap, reply_markup=markup, parse_mode="HTML")

    ACTIVE_PAYMENT_POLLS[order_id] = True
    asyncio.create_task(auto_poll_payment_status(order_id, user_id, amount, sent_msg.message_id, context, prod_id, qty))


async def generate_and_send_usdt_payment(user_id: int, user_name: str, usdt_amount: float, context: ContextTypes.DEFAULT_TYPE, previous_message=None, prod_id: int = None, qty: int = None):
    wait_msg = None
    if previous_message:
        try:
            wait_msg = await previous_message.edit_text("⏳ <i>Generating USDT Crypto Scanner...</i>", parse_mode="HTML")
        except Exception:
            pass

    ok, order_id, res_data = await create_smb_order(user_id, user_name, usdt_amount, currency="USDT", prod_id=prod_id, qty=qty)
    inr_equiv = usdt_amount * USDT_RATE_INR

    crypto_qr = res_data.get("qr_code") or res_data.get("qr_image") or f"https://api.qrserver.com/v1/create-qr-code/?size=350x350&data={USDT_WALLET_ADDRESS}"

    cap = (
        f"💎 <b>USDT (TRC-20) / BINANCE PAY QR</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💎 <b>Pay Exact:</b> <code>{usdt_amount:.2f} USDT</code>\n"
        f"💵 <b>Wallet Credit:</b> <code>{CURRENCY_SYMBOL}{inr_equiv:.2f}</code>\n"
        f"📍 <b>Deposit Address:</b>\n<code>{USDT_WALLET_ADDRESS}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "📌 <b>Direct Crypto Scan:</b>\n"
        "1. Scan with <b>Binance App, TrustWallet, or TronLink</b>.\n"
        "2. Send exact USDT on <b>TRC-20</b> network.\n"
        "3. <b>Done!</b> Auto-detects on chain instantly.\n\n"
        "🔄 <i>Auto-detecting payment status in real-time...</i>"
    )

    # Shudhu Cancel button thakbe
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel / Back", callback_data=f"cancel_order_{order_id}")],
    ])

    if wait_msg:
        try:
            await wait_msg.delete()
        except Exception:
            pass

    sent_msg = await context.bot.send_photo(chat_id=user_id, photo=crypto_qr, caption=cap, reply_markup=markup, parse_mode="HTML")

    ACTIVE_PAYMENT_POLLS[order_id] = True
    asyncio.create_task(auto_poll_payment_status(order_id, user_id, inr_equiv, sent_msg.message_id, context, prod_id, qty))
w
