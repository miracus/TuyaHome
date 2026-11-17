"""Доступ TuyaHome до чату з ботом ДТЕК від ВАШОГО Telegram-акаунта (для заявок «немає світла»).

  python tools/dtek_tg.py login      вхід у Telegram (номер, код, пароль 2FA вводите ви) → ключ у AWS SSM (SecureString)
  python tools/dtek_tg.py dump [N]   останні N повідомлень ЛИШЕ чату з ботом ДТЕК (текст + кнопки) — щоб налаштувати сценарій
  python tools/dtek_tg.py logout     завершити цей вхід у Telegram і стерти ключ з SSM

api_id / api_hash — створіть самі на https://my.telegram.org → API development tools (будь-яка назва застосунку).
Ключ входу ніде не друкується і на диск не пишеться: лише в SSM (/tuyahome/tg_user, ключ шифрування aws/ssm).
Відкликати можна й з телефона: Telegram → Налаштування → Пристрої → «TuyaHome»."""
import asyncio
import getpass
import json
import os
import subprocess
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8")
HERE = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(HERE, "..", "app", "config.json"), encoding="utf-8")).get("dtek_report", {})
PARAM = CFG.get("ssm_param", "/tuyahome/tg_user")
BOT = CFG.get("bot", "DTEKKyivRegionElektromerezhiBot")
REGION = "eu-north-1"


def aws(*args):
    return subprocess.run(["aws", "--region", REGION, *args], capture_output=True, text=True, encoding="utf-8")


def ssm_get():
    r = aws("ssm", "get-parameter", "--name", PARAM, "--with-decryption", "--query", "Parameter.Value", "--output", "text")
    if r.returncode:
        sys.exit("Ключа в SSM немає — спершу: python tools/dtek_tg.py login")
    return json.loads(r.stdout.strip())


def ssm_put(doc):
    # значення — через тимчасовий файл, щоб не світилось у списку процесів
    fd, path = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f)
        r = aws("ssm", "put-parameter", "--name", PARAM, "--type", "SecureString", "--overwrite",
                "--value", f"file://{path}")
        if r.returncode:
            sys.exit(f"Не вдалося записати в SSM: {r.stderr.strip()}")
    finally:
        os.remove(path)


def client(sec):
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    return TelegramClient(StringSession(sec.get("session")), int(sec["api_id"]), sec["api_hash"],
                          device_model="TuyaHome", app_version="1.0")


async def login():
    api_id = input("api_id (з my.telegram.org): ").strip()
    api_hash = getpass.getpass("api_hash (не відображається): ").strip()
    sec = {"api_id": api_id, "api_hash": api_hash, "session": None}
    cl = client(sec)
    await cl.start(phone=lambda: input("Номер телефону (+380…): ").strip(),
                   code_callback=lambda: input("Код з Telegram: ").strip(),
                   password=lambda: getpass.getpass("Пароль 2FA (якщо є, не відображається): "))
    me = await cl.get_me()
    sec["session"] = cl.session.save()
    await cl.disconnect()
    ssm_put(sec)
    print(f"✅ Увійшли як {me.first_name}. Ключ збережено в SSM {PARAM}.")


async def dump(n):
    async with client(ssm_get()) as cl:
        peer = await cl.get_entity(BOT)
        for m in reversed(await cl.get_messages(peer, limit=n)):
            who = "Я " if m.out else "ДТЕК"
            print(f"--- #{m.id} {m.date:%d.%m %H:%M} {who}")
            if m.message:
                print(m.message)
            if m.media and not m.message:
                print("[вкладення]")
            rm = m.reply_markup
            for row in getattr(rm, "rows", None) or []:
                kind = "inline" if type(rm).__name__ == "ReplyInlineMarkup" else "клавіатура"
                print(f"   [{kind}] " + " | ".join(getattr(b, "text", "") for b in row.buttons))


async def logout():
    async with client(ssm_get()) as cl:
        await cl.log_out()
    aws("ssm", "delete-parameter", "--name", PARAM)
    print("Вхід завершено, ключ стерто.")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "login":
        asyncio.run(login())
    elif cmd == "dump":
        asyncio.run(dump(int(sys.argv[2]) if len(sys.argv) > 2 else 40))
    elif cmd == "logout":
        asyncio.run(logout())
    else:
        print(__doc__)
