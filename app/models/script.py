from pydantic import BaseModel


class ScriptCreate(BaseModel):

    task_id: int

    name: str

    path: str