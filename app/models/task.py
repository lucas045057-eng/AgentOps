from pydantic import BaseModel


class TaskCreate(BaseModel):

    project_id: int

    name: str

    description: str