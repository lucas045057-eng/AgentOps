from fastapi import FastAPI

app = FastAPI(
    title="AgentOps",
    version="0.1.0"
)

@app.get("/")
def root():
    return {
        "project": "AgentOps",
        "status": "running"
    }