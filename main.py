from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import asyncio
from routers.autopilot import run_autopilot_loop
from database import init_db
from routers import auth, entities, projects, tasks, stages, websockets, ui, autopilot

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for startup and shutdown events"""
    # Startup
    await init_db()
    # Start autopilot background task
    asyncio.create_task(run_autopilot_loop())
    yield
    # Shutdown (nothing to do)

app = FastAPI(
    title="Agent Kanban Project Management API",
    description="A platform-agnostic project management system for humans and AI agents",
    version="1.0.0",
    lifespan=lifespan
)

# Mount static files
app.mount("/static", StaticFiles(directory="static"), name="static")

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(ui.router)
app.include_router(auth.router)
app.include_router(entities.router)
app.include_router(projects.router)
app.include_router(tasks.router)
app.include_router(stages.router)
app.include_router(websockets.router)
app.include_router(autopilot.router)


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "healthy"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
