"""Перший вхід пилососа Dreame: пароль → токен у AWS SSM (SecureString). Запускається вручну на ПК.

Пароль вводиться приховано і нікуди не пишеться. У SSM іде лише токен входу (його Lambda пилососа далі
оновлює сама) і технічні дані пристрою (did, mac, адреса сервера, модель). Нічого секретного не друкує.

  <venv>\\Scripts\\python -I tools\\dreame_login.py <шлях до custom_components\\dreame_vacuum>          — увійти
  <venv>\\Scripts\\python -I tools\\dreame_login.py <шлях до custom_components\\dreame_vacuum> delete   — стерти токен
Потрібен AWS CLI з доступом до акаунта (той самий, що для terraform).
"""
import getpass
import importlib.util
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(HERE, "..", "app", "config.json"), encoding="utf-8")).get("vacuum", {})
PARAM = CFG.get("ssm_param", "/tuyahome/dreame")
REGION = "eu-north-1"


def aws(*args):
    return subprocess.run(["aws", "--region", REGION, *args], capture_output=True, text=True, encoding="utf-8")


def ssm_put(doc):
    fd, path = tempfile.mkstemp(suffix=".json")  # значення — через файл, щоб не світилось у списку процесів
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f)
        r = aws("ssm", "put-parameter", "--name", PARAM, "--type", "SecureString", "--overwrite",
                "--value", f"file://{path}")
        if r.returncode:
            sys.exit(f"Не вдалося записати в SSM: {r.stderr.strip()}")
    finally:
        os.remove(path)


def load_lib(path):
    # пакет dreame/ вантажимо напряму: папку інтеграції в sys.path не додаємо (там файли Home Assistant)
    pkg = os.path.join(path, "dreame")
    spec = importlib.util.spec_from_file_location("dreame", os.path.join(pkg, "__init__.py"),
                                                  submodule_search_locations=[pkg])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["dreame"] = mod
    spec.loader.exec_module(mod)


def main():
    if len(sys.argv) < 2:
        sys.exit("Вкажіть шлях до custom_components\\dreame_vacuum (з D:\\repos\\TuyaHome\\.dreame\\lib)")
    if len(sys.argv) > 2 and sys.argv[2] == "delete":
        r = aws("ssm", "delete-parameter", "--name", PARAM)
        print("Токен стерто." if not r.returncode else f"Не вдалося: {r.stderr.strip()}")
        return
    load_lib(sys.argv[1])
    from dreame.protocol import DreameVacuumDreameHomeCloudProtocol

    username = input("Логін Dreamehome (email або телефон): ").strip()
    password = getpass.getpass("Пароль (не відображається): ")
    bad = []
    if any("а" <= c.lower() <= "я" or c.lower() in "іїєґё" for c in password):
        bad.append("є КИРИЛИЦЯ (розкладка?)")
    if password != password.strip():
        bad.append("пробіл на початку/в кінці")
    print(f"   Логін: {username} · довжина пароля: {len(password)}"
          + (f" · ⚠️ {', '.join(bad)}" if bad else " · латиниця, без пробілів"))
    if input("   Відправити? Спроб входу 5, далі акаунт блокується (y/N): ").strip().lower() != "y":
        sys.exit("Скасовано, спробу не витрачено.")
    country = (input("Регіон хмари [eu]: ").strip() or "eu").lower()

    cloud = DreameVacuumDreameHomeCloudProtocol(username, password, "dreame", country)
    ok = cloud.login()
    password = None
    if not ok:
        sys.exit("✗ Вхід не вдався (див. повідомлення сервера вище).")
    resp = cloud.get_devices()
    records = (resp or {}).get("page", {}).get("records", []) if isinstance(resp, dict) else []
    vacuums = [d for d in records if ".vacuum." in str(d.get("model", ""))]
    if not vacuums:
        sys.exit("✗ Пилососів в акаунті не знайдено.")
    for i, d in enumerate(vacuums):
        print(f"   [{i}] {d.get('customName') or (d.get('deviceInfo') or {}).get('displayName')} · {d.get('model')}")
    dev = vacuums[0] if len(vacuums) == 1 else vacuums[int(input("Номер пилососа: ") or 0)]
    doc = {"token": cloud.auth_key, "country": country, "did": dev.get("did"), "mac": dev.get("mac"),
           "host": dev.get("bindDomain"), "model": dev.get("model"),
           "name": dev.get("customName") or (dev.get("deviceInfo") or {}).get("displayName")}
    cloud.disconnect()
    if not doc["token"]:
        sys.exit("✗ Сервер не видав токен.")
    ssm_put(doc)
    print(f"✓ Токен збережено в SSM {PARAM} (SecureString). Пароль ніде не збережено.\n"
          "  Далі: у боті натисніть «🤖 Пилосос» — Lambda сама оновлюватиме токен.")


if __name__ == "__main__":
    main()
