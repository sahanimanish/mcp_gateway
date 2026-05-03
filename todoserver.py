from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional, List

app = FastAPI(
    title="JSONPlaceholder Todos API",
    version="1.0.0",
    description="A free fake REST API for testing and prototyping."
)

# Database (in-memory for this example)
todos_db = [
    {"id": 1, "title": "delectus aut autem", "completed": False, "userId": 1},
    {"id": 2, "title": "quis ut et aut", "completed": False, "userId": 1},
    {"id": 3, "title": "fugiat veniam minus", "completed": False, "userId": 1},
    {"id": 4, "title": "et porro tempora", "completed": True, "userId": 1},
    {"id": 5, "title": "laboris sunt enim", "completed": False, "userId": 2},
]

# Counter for auto-incrementing IDs
next_id = 6


# Pydantic models
class TodoCreate(BaseModel):
    title: str
    completed: bool
    userId: int


class Todo(BaseModel):
    id: int
    title: str
    completed: bool
    userId: int

    class Config:
        from_attributes = True


# Routes
@app.get("/todos", response_model=List[Todo], summary="Get a list of todos")
def get_todos(userId: Optional[int] = None):
    """
    Get a list of todos.
    
    Query Parameters:
    - userId (optional): Filter by user ID
    """
    if userId is not None:
        return [todo for todo in todos_db if todo["userId"] == userId]
    return todos_db


@app.post("/todos", response_model=Todo, status_code=201, summary="Create a new todo")
def create_todo(todo: TodoCreate):
    """
    Create a new todo.
    
    Request body:
    - title (required): Title of the task
    - completed (required): Boolean indicating if the task is completed
    - userId (required): User ID associated with the todo
    """
    global next_id
    new_todo = {
        "id": next_id,
        "title": todo.title,
        "completed": todo.completed,
        "userId": todo.userId
    }
    todos_db.append(new_todo)
    next_id += 1
    return new_todo


@app.get("/todos/{id}", response_model=Todo, summary="Get a specific todo by its ID")
def get_todo_by_id(id: int):
    """
    Get a specific todo by its ID.
    
    Path Parameters:
    - id (required): The ID of the todo to fetch
    """
    for todo in todos_db:
        if todo["id"] == id:
            return todo
    raise HTTPException(status_code=404, detail="Todo not found")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8005)