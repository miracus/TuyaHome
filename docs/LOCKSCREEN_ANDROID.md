# Стан дому на екрані блокування (Android, Automate)

Тихе сповіщення лише на екрані блокування: натиснули бокову кнопку — бачите стан дому, розблокували — зникло. Дані — з адреси `/api/lock` дашборда.

```
💡 Світло є (до 20:00)        ← або «🔌 Немає (23:00)» — коли мають дати за графіком
🕊 Тривоги немає              ← або «🔴 Тривога з 20:09»
⛅ 3° надворі · 🏠 19° вдома   ← значок неба з прогнозу (дощ/сніг/ніч — свої)
```

## Адреса

Найпростіше — надішліть боту команду **/lockscreen**: він відповість вашою особистою адресою. Або вручну: посилання на дашборд, де `/?k=` замінено на `/api/lock?k=`:

```
https://<адреса дашборда>/api/lock?k=<ваш ключ>
```

Відповідь — JSON: `title` (світло й інтернет), `text` (решта рядків), `updated` (час останнього опитування дому), `stale` (`true`, якщо дані старші за 15 хв — наприклад, опитувач стоїть).
Ключ — той самий, що в посиланні на дашборд: нікому не пересилайте.

## Сценарій (flow) в Automate — перевірена схема (22.11)

Сповіщення лише на екрані блокування: увімкнули екран заблокованим — свіжі дані (~0,8–1 с, це мережа телефона); розблокували — зникло. Поки екран світить, сценарій раз на секунду перевіряє «розблокували? згас?», тож ніколи не застрягає.

| Блок | Налаштування | Виходи |
|---|---|---|
| Flow beginning | — | → Device interactive |
| **Device interactive?** | Proceed **When changed** | YES і NO → Device unlocked (1) |
| Device unlocked? (1) | Proceed **Immediately** | YES → Cancel (B) · NO → HTTP request |
| HTTP request | URL з `/lockscreen`, GET, **Save to variable as text**, Response content `resp` | → Variable set |
| Variable set | `d` = (fx) `jsonDecode(resp)` | → Cancel (A) |
| Notification cancel (A) | ID (fx) `outputNotif` — прибрати попереднє | → Show notification |
| Show notification | Title (fx) `d["title"]`, Message (fx) `d["text"]`, Channel «Дім» (Medium, без звуку), Ongoing, Proceed Immediately, Output Notification ID `outputNotif` | YES і NO → Delay |
| Delay | 1 с | → Device unlocked (2) |
| Device unlocked? (2) | **Immediately** | YES → Cancel (B) · NO → Device interactive (2) |
| Device interactive? (2) | **Immediately** | YES → Delay · NO → Device interactive (перший) |
| Notification cancel (B) | ID (fx) `outputNotif` | → Device interactive (перший) |

Чому так: Samsung у момент увімкнення екрана часто спершу повідомляє «згас» (NO), тому обидва виходи першого блоку ведуть на перевірку — інакше «через раз». Без **Save to variable as text** змінна `resp` порожня, і замість даних на картці — назва сценарію.

Канали сповіщень Automate (Settings у самому Automate): **Running → Off** (прибирає «Running 1 fibers»), свій канал **«Дім» — Medium** (у Samsung для нього вимкнути звук і «Згорнути сповіщення»), інакше картка згортається в один рядок.

Запуск: **Start**; після перезавантаження: Automate → Settings → **Run on system startup** → цей flow.

## Повний чек-лист налаштувань (Samsung One UI + Automate, перевірено 22.11)

### Automate — сам застосунок
- Встановити з Google Play (LlamaLab Automate).
- Сценарій → **Privileges**: відмічені «показувати сповіщення», «отримувати повний доступ до мережі», «переглядати мережеві з'єднання», «ignore battery optimizations» (решта — як запропонує Automate).
- Попередження «Pause app activity if unused» → **FORGET** (на Samsung цей перемикач сірий, недоступний).
- Automate → **Settings → Run on system startup** → цей flow (запуск після перезавантаження).
- Automate → **Settings** → канали сповіщень:
  - **Running** → **Off** (прибирає картку «Running 1 fibers»);
  - **+** → новий канал **«Дім»**, важливість **Medium** (не Low — інакше Samsung згортає картку в один рядок).
- У блоці **Show notification** → Channel **«Дім»**.

### Блоки сценарію (усі поля, які змінювались)
| Блок | Поля |
|---|---|
| When device interactive (перший) | Proceed: **When changed** |
| Is device unlocked (обидва) | Proceed: **Immediately** |
| Is device interactive (у петлі) | Proceed: **Immediately** |
| HTTP request | URL — з команди бота **/lockscreen**; Method **GET**; Save response: **Save to variable as text**; Response content or filename: `resp` |
| Set variable | Variable `d`; Value (fx) `jsonDecode(resp)` |
| Show notification | Title (fx) `d["title"]`; Message (fx) `d["text"]`; Channel «Дім»; **Ongoing** ✓; Proceed **Immediately**; Output variables → Notification ID: `outputNotif` |
| Notification cancel (обидва) | Notification ID (fx) `outputNotif` |
| Delay | 1 с (менше секунди вікно вибору часу не дає) |

### Android / Samsung
- **Налаштування → Сповіщення → Сповіщення на екрані блокування**: увімкнено; стиль **«Деталі»**; **показувати беззвучні сповіщення** — увімкнено; вміст — показувати; Automate — у списку дозволених.
- **Налаштування → Програми → Automate → Сповіщення**: «Дозвіл на сповіщення» — увімкнено; «Звук і вібрація» — можна лишити вимкненим; **Екран блокування** ✓; «Відображення або приховування під час блокування» — Відображати завжди.
- Там же → **«Налаштуйте в програмі «Automate»»** → категорія **Running**: дозвіл на сповіщення — **вимкнено**; категорія **«Дім»**: «Згорнути сповіщення» — **вимкнено**, «Екран блокування» — **Показувати вміст**, звук — без звуку.
- **Налаштування → Програми → Automate → Акумулятор → «Без обмежень»** (інакше Samsung присипляє сценарій).
- Розблокування обличчям не заважає, але на перевіреному телефоні воно вимкнене.

## Батарея, трафік і швидкість

Запит — лише коли вмикаєте екран заблокованим (зазвичай 50–150 разів на добу), ~1 КБ кожен: практично 0% заряду. Самі дані на сервері оновлюються кожні 2 хв.

Від натискання до свіжих даних — зазвичай менше секунди; якщо дашборд давно ніхто не відкривав, перший запит може йти 1–2 с (холодний старт Lambda). Тому крок 4 спершу показує останні відомі дані з позначкою «⏳ оновлюю…».
