import httpx
import logging
import asyncio
from typing import Optional, Dict, Any
import os

logger = logging.getLogger(__name__)


class ProviderClient:
    def __init__(self):
        # Берем URL провайдера из переменной окружения
        self.base_url = os.getenv("PROVIDER_URL", "http://localhost:8081")
        self.client = None
        self.max_retries = 3

    async def __aenter__(self):
        self.client = httpx.AsyncClient(timeout=30.0)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.client:
            await self.client.aclose()

    async def _ensure_client(self):
        if self.client is None:
            self.client = httpx.AsyncClient(timeout=30.0)

    async def create_payment(self, operation_id: str, amount: str, currency: str):
        """Отправляет запрос провайдеру с повторами при ошибках"""
        await self._ensure_client()

        url = f"{self.base_url}/payments"

        payload = {
            "operationId": operation_id,
            "amount": amount,
            "currency": currency
        }

        headers = {
            "Content-Type": "application/json",
            "Idempotency-Key": operation_id,
            "X-Correlation-ID": operation_id
        }

        # Начинаем цикл попыток
        for attempt in range(self.max_retries):
            try:
                logger.info(f"Payment attempt {attempt + 1}/{self.max_retries} for {operation_id}")

                response = await self.client.post(url, json=payload, headers=headers)

                if response.status_code == 202:
                    # УСПЕХ!
                    result = response.json()
                    provider_payment_id = result.get("providerPaymentId")
                    logger.info(f"Payment accepted for {operation_id}: {provider_payment_id}")
                    return provider_payment_id

                elif response.status_code in [503, 504, 500]:
                    # ОШИБКА СЕРВЕРА - можно повторить
                    wait_time = self._calculate_backoff(attempt)
                    logger.warning(f"Server error {response.status_code} for {operation_id}, retry in {wait_time:.2f}s")
                    await asyncio.sleep(wait_time)
                    continue

                else:
                    # ДРУГАЯ ОШИБКА - нет смысла повторять
                    logger.error(f"Unexpected status {response.status_code} for {operation_id}: {response.text}")
                    return None

            except httpx.TimeoutException:
                # ТАЙМАУТ - можно повторить
                wait_time = self._calculate_backoff(attempt)
                logger.warning(f"Timeout for {operation_id}, retry in {wait_time:.2f}s")
                await asyncio.sleep(wait_time)
                continue

            except Exception as e:
                # ДРУГИЕ ОШИБКИ - можно повторить
                wait_time = self._calculate_backoff(attempt)
                logger.warning(f"Error for {operation_id}: {e}, retry in {wait_time:.2f}s")
                await asyncio.sleep(wait_time)
                continue

        # ВСЕ ПОПЫТКИ ЗАКОНЧИЛИСЬ
        logger.error(f"Failed to create payment for {operation_id} after {self.max_retries} attempts")
        return None

    def _calculate_backoff(self, attempt: int) -> float:
        """Экспоненциальный backoff с jitter"""
        # base * 2^attempt (1, 2, 4, 8, 16 секунд)
        base = 1.0 * (2 ** attempt)
        # Добавляем случайность (jitter) чтобы не все сервисы стучались одновременно
        import random
        jitter = random.uniform(0, 0.5 * base)
        # Максимум 60 секунд
        return min(base + jitter, 60)

