"""Сторожовий пес: сигнали CloudWatch (опитувач не запускається, помилки, ліміт одночасних Lambda, повільний
дашборд) і бюджету AWS приходять через SNS → ця Lambda → Telegram власнику.

Окрема функція, бо коли «лежить» опитувач чи бот, повідомити про це вони самі не можуть (19.11 бот мовчав,
і про збій дізналися випадково). Текст сигналу — з AlarmDescription у Terraform; тут лише оформлення."""
import json
import os
import urllib.request


def owner_chats():
    """Особисті чати власників бота з DynamoDB; недоступно — запасний TELEGRAM_CHAT_ID."""
    try:
        from store import DynamoStore
        users = DynamoStore(os.environ["TABLE_NAME"]).get_meta("tg_users") or {}
        ids = [uid for uid, u in users.items() if u.get("role") == "owner" and u.get("status") == "approved"]
        if ids:
            return ids
    except Exception as e:
        print(f"[watchdog] tg_users: {type(e).__name__}: {e}")
    return [c for c in [os.environ.get("TELEGRAM_CHAT_ID")] if c]


def format_message(msg, subject=""):
    """SNS-повідомлення → текст. Сигнал CloudWatch — JSON з AlarmName/NewStateValue; бюджет — звичайний текст."""
    try:
        a = json.loads(msg)
    except (TypeError, ValueError):
        a = None
    if isinstance(a, dict) and "NewStateValue" in a:
        what = a.get("AlarmDescription") or a.get("AlarmName", "сигнал")
        if a["NewStateValue"] == "ALARM":
            return f"🚨 TuyaHome: {what}\n\n{a.get('NewStateReason', '')}".strip()
        if a["NewStateValue"] == "OK" and a.get("OldStateValue") == "ALARM":
            return f"✅ TuyaHome: відновлено — {what}"
        return None  # INSUFFICIENT_DATA і перший перехід в OK — не шлемо
    text = (subject + "\n\n" if subject else "") + str(msg or "")
    return f"💸 TuyaHome · бюджет AWS\n\n{text.strip()[:1500]}"


def send(token, chat_id, text):
    data = json.dumps({"chat_id": chat_id, "text": text, "disable_web_page_preview": True}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode()).get("ok")


def lambda_handler(event, context):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    sent = 0
    for rec in (event or {}).get("Records", []):
        sns = rec.get("Sns") or {}
        text = format_message(sns.get("Message"), sns.get("Subject") or "")
        if not text:
            continue
        for cid in owner_chats():
            try:
                sent += bool(send(token, cid, text))
            except Exception as e:
                print(f"[watchdog] send: {type(e).__name__}: {e}")
        print(f"[watchdog] {text[:200]}")
    return {"sent": sent}
