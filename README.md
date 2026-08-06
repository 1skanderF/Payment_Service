# Payment Service — тестовое задание

Сервис проводит платёжную операцию через внешнего провайдера (provider-simulator) и
гарантирует корректное состояние операции при повторах, конкурентных запросах,
потерянных HTTP-ответах и перезапусках сервиса.

Главный инвариант: **на одну операцию — не более одного платежа провайдера, финальный
статус (`COMPLETED`/`REJECTED`) определяется только callback-квитанцией.**

## Стек

- Python 3.12, FastAPI, SQLite (файловое хранилище в volume), httpx
- Причина Python 3.12, а не 3.14: на момент разработки `pydantic-core` не собирался
  без ошибок под `cp314` в базовом образе `python:3.14` (нет системного C-компилятора
  для сборки Rust-расширения). Контракт сервиса и логика не зависят от версии
  интерпретатора, поэтому выбран стабильный 3.12.

## Архитектура

- `main.py` — HTTP-эндпоинты (FastAPI), только приём запросов и работа с `Storage`.
- `storage.py` — вся работа с SQLite: операции, события, очередь на обработку.
- `provider.py` — HTTP-клиент к provider-simulator, с ретраями и backoff+jitter при
  503/504/500 и сетевых ошибках.
- `background.py` — фоновый воркер, единственное место, откуда идёт вызов провайдера.
  Обходит очередь операций в статусе `PROCESSING` без сохранённого `providerPaymentId`
  и пытается создать платёж.

### Как обеспечена атомарность и защита от дублей

- `POST /operations` — вставка в SQLite ловит `IntegrityError` на повторном
  `operationId` → 409, без гонок check-then-act.
- `POST /operations/{id}/submit` — атомарный переход `CREATED → PROCESSING`
  выполняется без `await` между чтением и записью статуса (sqlite3 синхронный,
  внутри event loop это гарантирует отсутствие переключения на другую корутину
  между проверкой и обновлением). Операция сразу кладётся в очередь и подтверждается
  клиенту, не дожидаясь ответа провайдера — вызов провайдера идёт только в фоне.
- Вызов провайдера происходит **только из background-воркера** и только пока у
  операции ещё нет `providerPaymentId` — это исключает повторные платежи при
  повторных `submit` и при работе воркера параллельно с обработкой запроса.
- `POST /receipts` обрабатывает все случаи одной операцией над БД:
  - `providerPaymentId` ещё не установлен → устанавливается из первой валидной квитанции;
  - повтор той же квитанции → 204, новый переход не создаётся;
  - поздняя квитанция с противоположным результатом → 204, фиксируется как
    `RECEIPT_IGNORED`, финальный статус не меняется;
  - несовпадающий `providerPaymentId` после установления связи → 409.
- **Recovery при старте**: на `startup` сервис ищет все операции со статусом
  `PROCESSING` напрямую по таблице `operations` (не только по вспомогательной
  таблице очереди) и гарантирует их присутствие в очереди на обработку — повторный
  вызов провайдера безопасен благодаря одному и тому же `Idempotency-Key`.

## Запуск

```bash
docker compose up --build
```

Сервис слушает `http://localhost:8080`, provider-simulator — `http://localhost:8081`.

Проверка готовности:

```bash
curl http://localhost:8080/health
```

Остановка (данные сохраняются в volume `candidate-data`):

```bash
docker compose down
```

Полная очистка вместе с данными:

```bash
docker compose down -v
```

## Полный сквозной сценарий

### 1. Создать операцию

```bash
curl -X POST http://localhost:8080/operations \
  -H "Content-Type: application/json" \
  -d '{
    "operationId": "operation-123",
    "amount": "1000.00",
    "currency": "RUB",
    "description": "Оплата заказа"
  }'
```

Ожидаемый ответ (201):

```json
{
  "operationId": "operation-123",
  "amount": "1000.00",
  "currency": "RUB",
  "description": "Оплата заказа",
  "status": "CREATED",
  "providerPaymentId": null
}
```

Повторное создание того же `operationId` → 409.

### 2. Отправить операцию на обработку

```bash
curl -i -X POST http://localhost:8080/operations/operation-123/submit
```

Первый вызов → 202/200, статус `PROCESSING`. Повторный вызов не создаёт новое
намерение и просто возвращает текущее состояние операции.

### 3. Проверить текущее состояние

```bash
curl http://localhost:8080/operations/operation-123
```

Провайдер асинхронно обрабатывает платёж и присылает callback на `/receipts`.
Через несколько секунд статус должен смениться на `COMPLETED` или `REJECTED`
(в зависимости от того, что вернёт provider-simulator).

### 4. Посмотреть историю переходов

```bash
curl http://localhost:8080/operations/operation-123/events
```

Пример ответа:

```json
[
  {
    "eventId": 1,
    "type": "CREATED",
    "fromStatus": null,
    "toStatus": "CREATED",
    "message": "Operation created",
    "occurredAt": "2026-08-06T12:00:00Z"
  },
  {
    "eventId": 2,
    "type": "PROCESSING",
    "fromStatus": "CREATED",
    "toStatus": "PROCESSING",
    "message": "Status changed to PROCESSING",
    "occurredAt": "2026-08-06T12:00:01Z"
  },
  {
    "eventId": 3,
    "type": "COMPLETED",
    "fromStatus": "PROCESSING",
    "toStatus": "COMPLETED",
    "message": "Payment COMPLETED: aa5b7856-e9f2-4fd5-955b-38b1f28d9c57",
    "occurredAt": "2026-08-06T12:00:03Z"
  }
]
```

### 5. Проверка конкурентных submit (не создаёт второй платёж)

```bash
for i in 1 2 3 4 5; do
  curl -s -X POST http://localhost:8080/operations/operation-123/submit &
done
wait
```

Все ответы должны отражать один и тот же финальный/текущий статус операции,
провайдер получает ровно один запрос на создание платежа с данным `operationId`.

### 6. Проверка персистентности и восстановления после рестарта

```bash
docker compose stop candidate-service
docker compose start candidate-service
curl http://localhost:8080/operations/operation-123
```

Данные и история сохраняются (volume не удалялся), незавершённые операции в
статусе `PROCESSING` продолжают обрабатываться после старта.

## Переменные окружения

| Переменная    | Назначение                          | Значение по умолчанию (в compose)      |
|---------------|--------------------------------------|-----------------------------------------|
| `PROVIDER_URL`| Базовый URL provider-simulator       | `http://provider-simulator:8081`        |

