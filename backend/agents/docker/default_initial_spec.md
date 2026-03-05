# Task Manager API

## Overview

Build a simple REST API for a task management application. Users can create, read, update, and delete tasks.

## Requirements

- RESTful API with CRUD operations for tasks
- Each task has: title, description, status (todo/in_progress/done), due_date
- Authentication via JWT
- Persist data in PostgreSQL

## Acceptance Criteria

- POST /tasks creates a new task
- GET /tasks returns all tasks (filterable by status)
- GET /tasks/:id returns a single task
- PUT /tasks/:id updates a task
- DELETE /tasks/:id removes a task
- All endpoints require valid JWT except health check

## Constraints

- Use Python FastAPI or Java Spring Boot
- Use Docker for local development
- Include integration tests
