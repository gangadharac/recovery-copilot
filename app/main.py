from fastapi import FastAPI
from app.webhooks.razorpay_webhook import router as razorpay_webhook_router
from app.db.database import init_db

# Initialize database schema on startup
init_db()

app = FastAPI(
    title="Revenue Recovery Agent",
    description="Event-Driven Razorpay Test Mode Webhook Ingestion & Autonomous Revenue Recovery Agent",
    version="1.0.0"
)

# Include Webhook Router
app.include_router(razorpay_webhook_router)

@app.on_event("startup")
def startup_rehydrate_schedules():
    from app.pipeline.scheduler import recovery_scheduler
    recovery_scheduler.rehydrate_and_run()

@app.get("/health", tags=["System"])
def root_health():
    return {
        "status": "ok",
        "service": "Revenue Recovery Agent",
        "version": "1.0.0"
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
