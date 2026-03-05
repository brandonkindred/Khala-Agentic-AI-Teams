# StudioGrid Team

Strands scaffold for a design-system multi-agent workflow.

## Quick start

```bash
cd studiogrid
cp .env.example .env
docker compose up --build
```

Run CLI:

```bash
PYTHONPATH=src python -m studiogrid.main run start --project-name Demo --intake examples/intake.json
```

Agent registry helpers:

```bash
PYTHONPATH=src python -m studiogrid.main registry list
PYTHONPATH=src python -m studiogrid.main registry find --problem "Need accessibility review" --skills accessibility_review
```
