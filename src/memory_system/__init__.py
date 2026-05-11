"""Memory System — conversational long-term memory for AI agents, powered by mem0."""

__version__ = "0.2.0"

from memory_system.config import Settings
from memory_system.main import create_app
from memory_system.api.models import (
    MemoryRequest,
    MemoryStoreResponse,
    MemoryRecallResponse,
    RetrievedMemory,
)
from memory_system.core.memory_service import MemoryService

__all__ = [
    "__version__",
    "Settings",
    "create_app",
    "MemoryRequest",
    "MemoryStoreResponse",
    "MemoryRecallResponse",
    "RetrievedMemory",
    "MemoryService",
]
