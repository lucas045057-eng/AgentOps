from pydantic import BaseModel


class ProjectCreate(BaseModel):
    name: str
    url: str


class ProjectUpdate(BaseModel):
    name: str
    url: str