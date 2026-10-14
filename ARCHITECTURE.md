# Фоновая обработка и закупочные заказы

## Компоненты

- `main_ai.py` — постоянно работающий веб-интерфейс.
- Neon/PostgreSQL — заявки, результаты поиска, очередь и черновики заказов.
- `worker.py` — отдельный Render Background Worker.
- `job_queue.py` — долговечная очередь без дополнительного Redis.
- `procurement.py` — группировка включённых строк по поставщикам и ручное подтверждение.

## Безопасный запуск

1. Создать Background Worker из `render.yaml` и скопировать в него те же секреты базы и поставщиков.
2. Убедиться, что worker запущен и пишет `worker started`.
3. Только после этого добавить веб-сервису `BACKGROUND_QUEUE_ENABLED=true`.
4. Проверить одну тестовую заявку: статус должен пройти `queued` → `processing` → `ready_for_review`.

Если worker ещё не создан, переменная должна оставаться `false`: веб-приложение продолжит старую синхронную обработку.

## Заказы поставщикам

После проверки подбора кнопка «Сформировать черновики заказов поставщикам» создаёт отдельный черновик для каждого выбранного поставщика. Кнопка подтверждения переводит черновик в `approved_for_submission`, но пока не отправляет его наружу. Живую отправку можно включать только после реализации и проверки методов создания, статуса и отмены заказа конкретного поставщика.

## Прайсы и собственный каталог

`/dashboard/prices` принимает XLSX, XLS и CSV. Оператор выбирает лист, строку заголовков, соответствие столбцов, единицу цены и режим НДС. Оригинал файла, метаданные, версии и позиции сохраняются в общей базе. Новый полный прайс атомарно заменяет активную версию только того же поставщика и названия прайса. Некорректный импорт не отключает прежние цены; точные повторы не создают активных дубликатов. Один импорт — один выбранный лист, цены в RUB, до 12 МБ и 100 000 строк.

`/dashboard/catalog` хранит товары отдельно от наблюдений цен и фактов продаж: поставщик, закупочная и продажная цены, дата, число предложений по позиции (включая наше), способ поиска, URL и исходная заявка. Ручное добавление и новые цены сохраняют историю. Подтверждённая оператором продажа из `/dashboard/automation/jobs/{job_id}/catalog-sale` сохраняется один раз по заявке и позиции. Отправка предложения не является подтверждением продажи. Число предложений без цены/места среди конкурентов не доказывает ценовое преимущество.

`PriceListAdapter` и `OwnCatalogAdapter` участвуют в основном подборе и повторном поиске позиции. SQL-индексы по артикулам и словам отбирают ограниченный набор кандидатов без повторного чтения Excel или выгрузки всех прайсов. Затем применяются общие проверки бренда, размеров, типа товара и единицы цены. Цены сравниваются за единицу заявки; наличие из исторического источника не считается текущим подтверждением. Сохранённые счета/результаты не изменяются при обновлении каталога.

## Возможность поиска цен через ИИ

Текущий `ai_panel._call_openai` анализирует переданные данные без веб-поиска. Для отдельного поиска по сайтам подходит Responses API с инструментом `web_search`, ограничением `allowed_domains` на выбранных продавцов и сохранением ссылок-источников. Документация: https://developers.openai.com/api/docs/guides/tools-web-search . Этот внешний поиск пока не подключён.

Предлагаемый процесс: ручная кнопка по конкретной позиции → точный товар/характеристики и требуемое количество → поиск на выбранных сайтах → список предложений со ссылками → проверка совпадения, единицы/упаковки, НДС, количества, даты, доставки → сохранение проверенного предложения в каталог. Формулировка результата — «минимальная подтверждённая цена среди проверенных предложений», без гарантии минимальной цены во всём интернете. Закрытые B2B-цены и объёмные скидки запрашиваются через API/поставщика. Результат без проверяемой страницы и цены не попадает в счёт. Вводятся лимит стоимости/числа запросов, кэш и ручной запуск; ИИ используется для пробелов и неоднозначных позиций, а не вместо индексированного каталога.
# Sniper Search (2026-10-14)

Private panel: `/dashboard/sniper`; linked from the saved requests list and
request review. The user enters a Sniper PAT or logs in to issue one using
`POST /user/login` then `POST /api/v0/token/issue`. An existing PAT is never
automatically revoked. The password is not stored; the PAT is Fernet encrypted
in PostgreSQL using a key derived from the stable application signing secret
(fallback: Zakupay API key). Changing that secret requires reconnecting Sniper.

Connection verification reads `/api/v0/requests/languages`, without search or
mail. New local drafts contain up to 20 goods and an optional saved request ID.
Only a second confirmed POST submits either `/api/v0/requests/scan` (contacts)
or `/api/v0/requests/wizard` (search plus mail). The wizard always searches for
its own recipients; it cannot mail a supplied exact recipient list. No pricing
adapter is added because contacts are not product prices or stock evidence.
Email/site exclusions are snapshotted into new drafts. Fractional quantities
are not rounded to fit Sniper's integer quantity field; include them in the
reviewed email body. Defaults are worldwide geography (ID 1), Russian (ID 1).

Durable compare-and-set submission state blocks double sends and all implicit
retries. Transport errors, invalid success responses, and upstream 5xx leave
an uncertain state requiring a check in Sniper's account. A crash after sending
also leaves the request blocked. State/results and the first 100 mailbox
replies refresh only on user action, with no automatic database/API polling.
Opening any Sniper page never starts a supplier search or sends mail. All
routes require the private panel session; POST actions also verify session-bound
CSRF tokens. API responses and credentials are never returned in error text.
