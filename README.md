# TuyaHome

**Personal project.** Serverless monitoring of a Tuya Smart Life home through Ukrainian power outages, with a Telegram bot for the family.

- Detects power and internet outages from device presence within 2–3 minutes; compares them with the DTEK outage schedule
- Air-raid alerts per user region, weather forecast vs. outdoor sensor, voltage/temperature/humidity dashboard
- Stays inside the 26,000-call monthly Tuya API quota with a per-device polling planner
- Python, AWS Lambda, DynamoDB, EventBridge, Terraform; runs at about $0/month on the AWS free tier

Full functional spec (Ukrainian): [docs/FUNCTIONAL_SPEC.md](docs/FUNCTIONAL_SPEC.md). Secrets and personal data are not part of this repository.

---


Свій дашборд і сповіщення для приладів Tuya Smart Life: напруга, температура, тривоги, **відключення світла**.

```
EventBridge (раз на snapshot_every_min) → Lambda poller → Tuya Cloud API
                                              ↓
                                         DynamoDB ← Lambda web (Function URL, https) ← браузер/телефон
                                              ↓
                                         Telegram (сповіщення)
```

## Файли

| Шлях | Що це |
|---|---|
| `app/config.json` | **усі налаштування**: інтервали опитування по приладах і параметрах, ліміт квоти, пороги напруги, коди тривог |
| `app/poller.py` | опитувач: знімок усіх приладів, детальні журнали, запобіжник квоти |
| `app/budget.py` | планувальник квоти; `python app/budget.py` — кошторис запитів на місяць |
| `app/rules.py` | правила подій: світло, тривоги, напруга, батареї, офлайн |
| `app/web.py`, `app/dashboard.html` | дашборд і JSON API (лише з ключем `?k=`) |
| `app/store.py` | DynamoDB у хмарі / JSON-файл локально |
| `infra/` | Terraform: усі ресурси `tuyahome-*`, регіон eu-north-1 |
| `run_local.py` | локальний запуск без AWS |
| `dump_devices.py` | повне вивантаження всіх приладів і DP у `out/` |

## Квота Tuya

Ліміт — на запити за місяць (`polling.monthly_api_quota`). Знімок стану всіх приладів — **один** запит
незалежно від кількості приладів. Детальна історія параметра (`source: log`) — запит на прилад за кожне
спрацювання. Для кожного приладу × параметра — свій `every_min`. Якщо сума більша за ліміт × `quota_safety_pct`,
детальні інтервали розтягуються автоматично; знімок (від нього залежить «немає світла») — в останню чергу.
Під час роботи лічильник реальних запитів не дає піти швидше за план.

## Команди

```bash
python app/budget.py                 # кошторис запитів
python run_local.py poll             # один цикл локально
python run_local.py serve            # дашборд на http://localhost:8765/?k=local
cd infra && terraform plan           # що зміниться в AWS
cd infra && terraform apply          # розгорнути
cd infra && terraform output -raw dashboard_url
cd infra && terraform destroy        # прибрати все tuyahome-*
```

`infra/terraform.tfvars` генерується з `.env` (секрети; у git не йде).
Пробна підписка IoT Core у Tuya періодично закінчується — тоді опитувач шле «Помилка Tuya API»; продовжити в консолі Tuya.
