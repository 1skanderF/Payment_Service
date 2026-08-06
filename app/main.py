import logging
from fastapi import FastAPI, HTTPException
from contextlib import asynccontextmanager
from .models import OperationCreate, Receipt
from .storage import Storage
from .provider import ProviderClient
from .background import BackgroundProcessor

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

logger = logging.getLogger(__name__)

storage = Storage()
provider = ProviderClient()
background = BackgroundProcessor(storage, provider)


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("=== LIFESPAN STARTED ===")  # ВРЕМЕННО ДЛЯ ПРОВЕРКИ
    logger.info("Starting application...")

    await provider.__aenter__()
    await background.start()

    logger.info("Checking for unfinished operations...")

    pending = storage.get_pending_operations()
    for op_id in pending:
        logger.info(f"Found pending operation: {op_id}")
        storage.add_to_queue(op_id)

    queue = storage.get_queue()
    for op_id in queue:
        op = storage.get_operation(op_id)
        if op and op["status"] == "CREATED":
            logger.info(f"Recovered CREATED from queue: {op_id}")
            storage.update_status(op_id, "PROCESSING")

    logger.info(f"Recovery complete. {len(pending)} pending, {len(queue)} in queue")

    yield

    logger.info("Shutting down...")
    await background.stop()
    await provider.__aexit__(None, None, None)
    logger.info("Goodbye!")


# ВАЖНО: app создается ПОСЛЕ lifespan
app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/operations", status_code=201)
async def create_operation(data: OperationCreate):
    result = storage.create_operation(
        data.operationId,
        data.amount,
        data.currency,
        data.description
    )

    if result is None:
        raise HTTPException(status_code=409, detail="Operation already exists")

    return result


@app.get("/operations/{id}")
async def get_operation(id: str):
    result = storage.get_operation(id)
    if result is None:
        raise HTTPException(status_code=404, detail="Operation not found")
    return result


@app.get("/operations/{id}/events")
async def get_events(id: str):
    operation = storage.get_operation(id)
    if operation is None:
        raise HTTPException(status_code=404, detail="Operation not found")
    return storage.get_events(id)


@app.post("/operations/{id}/submit")
async def submit_operation(id: str):
    operation = storage.get_operation(id)
    if operation is None:
        raise HTTPException(status_code=404, detail="Operation not found")

    if operation["status"] != "CREATED":
        return operation

    success = storage.update_status(id, "PROCESSING")
    if not success:
        raise HTTPException(status_code=500, detail="Failed to update status")

    storage.add_to_queue(id)
    logger.info(f"Operation {id} added to processing queue")

    updated_operation = storage.get_operation(id)
    return updated_operation


@app.post("/receipts", status_code=204)
async def process_receipt(receipt: Receipt):
    operation = storage.get_operation(receipt.operationId)
    if operation is None:
        raise HTTPException(status_code=404, detail="Operation not found")

    if receipt.result not in ["COMPLETED", "REJECTED"]:
        raise HTTPException(status_code=400, detail="Invalid result")

    existing_ppid = operation.get("providerPaymentId")
    if existing_ppid is not None and existing_ppid != receipt.providerPaymentId:
        raise HTTPException(
            status_code=409,
            detail=f"ProviderPaymentId mismatch"
        )

    success = storage.process_receipt(
        receipt.operationId,
        receipt.result,
        receipt.providerPaymentId,
        receipt.message
    )

    if receipt.result in ["COMPLETED", "REJECTED"]:
        storage.remove_from_queue(receipt.operationId)
        logger.info(f"Operation {receipt.operationId} completed, removed from queue")

    return None