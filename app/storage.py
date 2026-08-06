import sqlite3
import logging
from datetime import datetime
import os

logger = logging.getLogger(__name__)

class Storage:
    def __init__(self):
        data_dir = "data"
        if not os.path.exists(data_dir):
            os.makedirs(data_dir)

        db_path = os.path.join(data_dir, "operations.db")
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()

    def _init_db(self):

        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")

        # Таблица операций
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS operations (
                operation_id TEXT PRIMARY KEY,
                amount TEXT,
                currency TEXT,
                description TEXT,
                status TEXT,
                created_at TEXT,
                updated_at TEXT,
                provider_payment_id TEXT
            )
        """)

        # Таблица событий
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                operation_id TEXT,
                event_type TEXT,
                from_status TEXT,
                to_status TEXT,
                message TEXT,
                occurred_at TEXT,
                FOREIGN KEY (operation_id) REFERENCES operations(operation_id)
            )
        """)

        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS processing_queue (
                operation_id TEXT PRIMARY KEY,
                created_at TEXT,
                attempts INTEGER DEFAULT 0,
                last_attempt TEXT,
                FOREIGN KEY (operation_id) REFERENCES operations(operation_id)
            )
        """)

        self.conn.commit()

    def create_operation(self, op_id, amount, currency, description):
        now = datetime.utcnow().isoformat()

        try:
            self.conn.execute("""
                INSERT INTO operations 
                (operation_id, amount, currency, description, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (op_id, amount, currency, description, "CREATED", now, now))

            self.conn.execute("""
                INSERT INTO events 
                (operation_id, event_type, from_status, to_status, message, occurred_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (op_id, "CREATED", None, "CREATED", "Operation created", now))

            self.conn.commit()

        except sqlite3.IntegrityError:
            # Дубликат - возвращаем None
            return None

        cursor = self.conn.execute(
            "SELECT * FROM operations WHERE operation_id = ?",
            (op_id,)
        )
        row = cursor.fetchone()

        return {
            "operationId": row[0],
            "amount": row[1],
            "currency": row[2],
            "description": row[3],
            "status": row[4],
            "providerPaymentId": None
        }

    def get_operation(self, op_id):
        cursor = self.conn.execute(
            "SELECT * FROM operations WHERE operation_id = ?",
            (op_id,)
        )
        row = cursor.fetchone()

        if row is None:
            return None

        # Безопасно получаем provider_payment_id
        provider_payment_id = row[7] if len(row) > 7 else None

        return {
            "operationId": row[0],
            "amount": row[1],
            "currency": row[2],
            "description": row[3],
            "status": row[4],
            "providerPaymentId": provider_payment_id
        }

    def update_status(self, op_id, new_status):
        now = datetime.utcnow().isoformat()

        cursor = self.conn.execute(
            "SELECT status FROM operations WHERE operation_id = ?",
            (op_id,)
        )
        row = cursor.fetchone()

        if row is None:
            return False

        old_status = row[0]

        self.conn.execute("""
            UPDATE operations 
            SET status = ?, updated_at = ?
            WHERE operation_id = ?
        """, (new_status, now, op_id))

        self.conn.execute("""
            INSERT INTO events 
            (operation_id, event_type, from_status, to_status, message, occurred_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (op_id, new_status, old_status, new_status, f"Status changed to {new_status}", now))

        self.conn.commit()
        return True

    def update_status_with_payment_id(self, op_id, new_status, provider_payment_id):
        now = datetime.utcnow().isoformat()

        cursor = self.conn.execute(
            "SELECT status FROM operations WHERE operation_id = ?",
            (op_id,)
        )
        row = cursor.fetchone()

        if row is None:
            return False

        old_status = row[0]

        if old_status in ["COMPLETED", "REJECTED"]:
            self.conn.execute("""
                INSERT INTO events 
                (operation_id, event_type, from_status, to_status, message, occurred_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (op_id, "IGNORED", old_status, old_status, f"Ignored {new_status}, already {old_status}", now))
            self.conn.commit()
            return True

        self.conn.execute("""
            UPDATE operations 
            SET status = ?, provider_payment_id = ?, updated_at = ?
            WHERE operation_id = ?
        """, (new_status, provider_payment_id, now, op_id))

        self.conn.execute("""
            INSERT INTO events 
            (operation_id, event_type, from_status, to_status, message, occurred_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (op_id, new_status, old_status, new_status, f"Payment {new_status}: {provider_payment_id}", now))

        self.conn.commit()
        return True

    def get_events(self, op_id):
        cursor = self.conn.execute(
            "SELECT * FROM events WHERE operation_id = ? ORDER BY id ASC",
            (op_id,)
        )
        rows = cursor.fetchall()

        if not rows:
            return []

        events = []
        for row in rows:
            events.append({
                "eventId": row[0],
                "type": row[2],
                "fromStatus": row[3],
                "toStatus": row[4],
                "message": row[5],
                "occurredAt": row[6]
            })

        return events

    def add_to_queue(self, operation_id: str):
        """Добавить операцию в очередь обработки"""
        now = datetime.utcnow().isoformat()

        self.conn.execute("""
            INSERT OR REPLACE INTO processing_queue 
            (operation_id, created_at, attempts)
            VALUES (?, ?, 0)
        """, (operation_id, now))

        self.conn.commit()

    def remove_from_queue(self, operation_id: str):
        """Удалить операцию из очереди"""
        self.conn.execute(
            "DELETE FROM processing_queue WHERE operation_id = ?",
            (operation_id,)
        )
        self.conn.commit()

    def get_queue(self) -> list:
        """Получить все операции из очереди"""
        cursor = self.conn.execute(
            "SELECT operation_id FROM processing_queue ORDER BY created_at ASC"
        )
        rows = cursor.fetchall()
        return [row[0] for row in rows]

    def increment_attempts(self, operation_id: str):
        """Увеличить счетчик попыток"""
        now = datetime.utcnow().isoformat()
        self.conn.execute("""
            UPDATE processing_queue 
            SET attempts = attempts + 1, last_attempt = ?
            WHERE operation_id = ?
        """, (now, operation_id))
        self.conn.commit()

    def update_provider_payment_id(self, op_id, provider_payment_id):
        """Сохранить providerPaymentId от провайдера"""
        now = datetime.utcnow().isoformat()

        self.conn.execute("""
            UPDATE operations 
            SET provider_payment_id = ?, updated_at = ?
            WHERE operation_id = ?
        """, (provider_payment_id, now, op_id))

        self.conn.commit()

    def process_receipt(self, op_id, result, provider_payment_id, message):
        """Обрабатывает квитанцию, различая дубликат и конфликт"""
        now = datetime.utcnow().isoformat()

        cursor = self.conn.execute(
            "SELECT status, provider_payment_id FROM operations WHERE operation_id = ?",
            (op_id,)
        )
        row = cursor.fetchone()

        if row is None:
            return False

        old_status = row[0]
        current_ppid = row[1]

        # Если уже финальный статус
        if old_status in ["COMPLETED", "REJECTED"]:
            # Точный дубликат - ничего не делаем
            if old_status == result:
                logger.info(f"Duplicate receipt for {op_id}, ignoring")
                return True

            # Противоречащая квитанция - логируем как IGNORED
            self.conn.execute("""
                INSERT INTO events 
                (operation_id, event_type, from_status, to_status, message, occurred_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (op_id, "RECEIPT_IGNORED", old_status, old_status,
                  f"Conflicting receipt: {result}, already {old_status}", now))
            self.conn.commit()
            logger.warning(f"Conflicting receipt for {op_id}: {result} vs {old_status}")
            return True

        # Первая финальная квитанция
        ppid = current_ppid or provider_payment_id

        self.conn.execute("""
            UPDATE operations 
            SET status = ?, provider_payment_id = ?, updated_at = ?
            WHERE operation_id = ?
        """, (result, ppid, now, op_id))

        self.conn.execute("""
            INSERT INTO events 
            (operation_id, event_type, from_status, to_status, message, occurred_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (op_id, result, old_status, result, message, now))

        self.conn.commit()
        logger.info(f"Receipt processed: {op_id} -> {result}")
        return True

    def get_pending_operations(self) -> list:
        """Получить все операции со статусом PROCESSING"""
        cursor = self.conn.execute(
            "SELECT operation_id FROM operations WHERE status = 'PROCESSING'"
        )
        rows = cursor.fetchall()
        return [row[0] for row in rows]
