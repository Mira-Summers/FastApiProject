from pydantic import BaseModel
from typing import Optional,List

class PodInfo(BaseModel):
    name: str
    namespace: str
    status: str
    node: Optional[str] = None
    restarts: int = 0
    age: Optional[str] = None
class RollbackRequest(BaseModel):
    release_name: str
    namespace: str = "default"
    revision: Optional[int] = None
class RollbackResponse(BaseModel):
    success: bool
    message: str
    output: Optional[str] = None
class LogResponse(BaseModel):
    pod_name: str
    namespace: str
    logs: List[str]