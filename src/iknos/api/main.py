from fastapi import FastAPI

app = FastAPI(title="Iknos API", version="0.1.0")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/version")
async def version() -> dict[str, str]:
    return {"name": "iknos", "version": "0.1.0"}
