# user_profile

A single cross-team **user profile** plus a central **artifact-association
registry**. Single-tenant today: one profile with `user_id = "default"` and no
authentication. The `user_id` is threaded through every store function as a
parameter defaulting to `DEFAULT_USER_ID`, so real auth can supply a real id
later without a data-model change.

## What it does

- Stores the editable profile (`display_name`, `email`, `bio`, free-form
  `preferences`) in Postgres.
- Records links between the profile and artifacts produced by other teams —
  brands, blog posts, projects, agentic teams, and integration configs — without
  copying any data. A link is just `(user_id, artifact_type, team, artifact_id)`.

## Layout

| File | Purpose |
|---|---|
| `postgres/__init__.py` | `SCHEMA: TeamSchema` — `user_profiles` + `user_profile_associations` (pure data; registered from the unified_api lifespan). |
| `models.py` | Pydantic `UserProfile`, `UserProfileUpdate`, `Association`, `AssociationList`. |
| `store.py` | CRUD via `shared_postgres.get_conn()`; `DEFAULT_USER_ID`; `record_association_safe` (best-effort). |
| `__init__.py` | Public surface + `ArtifactType` constants. |

HTTP routes live in `backend/unified_api/routes/user_profile.py` (mounted at
`/api/user-profile`), matching how Integrations and Product Delivery are served
in-process by the unified API.

## How teams link an artifact

Call the best-effort helper from the artifact's create path. It never raises —
a profile-link failure must not break artifact creation:

```python
from user_profile import ArtifactType, record_association_safe

record_association_safe(ArtifactType.BRAND, "branding", brand.id, label=brand.name)
```

Existing call sites: branding `create_brand`, blogging `create_blog_job`,
planning-v3 / coding-team `create_job`, agentic `create_team`, and the
integration setters in `unified_api/integrations_store.py`.

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/user-profile` | Current (default) profile; created on first read. |
| PUT | `/api/user-profile` | Partial update of profile fields. |
| GET | `/api/user-profile/associations?artifact_type=` | Linked artifacts, newest first. |
| GET | `/api/user-profile/integrations` | Integration status (pass-through to the integrations store). |

## Tests

`agents/user_profile/tests/` — a dict-backed fake for `get_conn`
(`_fake_postgres.py`), store unit tests, and FastAPI `TestClient` route tests
(100% line coverage on `store.py`, `models.py`, and the route module). Runs in
the default (non-integration) suite without a real Postgres.
