import logging
import asyncio
from .storage import Storage
from .provider import ProviderClient

logger = logging.getLogger(__name__)


class BackgroundProcessor:
    def __init__(self, storage: Storage, provider: ProviderClient):
        self.storage = storage
        self.provider = provider
        self.running = False
        self.task = None

    async def start(self):
        self.running = True
        self.task = asyncio.create_task(self._process_loop())
        logger.info("Background processor started")

    async def stop(self):
        self.running = False
        if self.task:
            await self.task
        logger.info("Background processor stopped")

    async def _process_loop(self):
        while self.running:
            try:
                queue = self.storage.get_queue()

                if queue:
                    logger.info(f"Processing queue: {len(queue)} operations")

                    for operation_id in queue:
                        if not self.running:
                            break

                        op = self.storage.get_operation(operation_id)
                        if op is None:
                            self.storage.remove_from_queue(operation_id)
                            continue

                        if op["status"] in ["COMPLETED", "REJECTED"]:
                            self.storage.remove_from_queue(operation_id)
                            continue

                        await self._process_operation(operation_id, op)
                        self.storage.increment_attempts(operation_id)

                await asyncio.sleep(5)

            except Exception as e:
                logger.error(f"Error in processing loop: {e}")
                await asyncio.sleep(10)

    async def _process_operation(self, operation_id: str, operation: dict):
        try:
            # НЕ отправляем повторно, если уже есть providerPaymentId
            if operation.get("providerPaymentId"):
                logger.info(f"Operation {operation_id} already has providerPaymentId, waiting for callback")
                return

            logger.info(f"Processing operation: {operation_id}")

            provider_payment_id = await self.provider.create_payment(
                operation_id,
                operation["amount"],
                operation["currency"]
            )

            if provider_payment_id:
                self.storage.update_provider_payment_id(operation_id, provider_payment_id)
                logger.info(f"Provider payment ID saved: {provider_payment_id}")
            else:
                logger.warning(f"Failed to get providerPaymentId for {operation_id}")

        except Exception as e:
            logger.error(f"Error processing {operation_id}: {e}")