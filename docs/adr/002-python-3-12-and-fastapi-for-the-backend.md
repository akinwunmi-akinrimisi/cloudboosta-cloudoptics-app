# ADR-002: Python 3.12+ and FastAPI for the backend

**Status:** Proposed

## Context

Most of the work is billing-data processing (Parquet, DuckDB, statistics) and AWS API calls. The hiring pool in Lagos, Nairobi and Cape Town knows Python well.

## Decision

Python 3.12+ (3.13 in the repo), FastAPI, Pydantic v2, SQLAlchemy 2.x with psycopg 3, Alembic. Tooling: uv workspace, ruff, mypy strict, pytest with Hypothesis.

## Consequences

Strong data and AWS libraries (boto3 covers Cost Explorer, Cost Optimization Hub, Compute Optimizer, BCM Data Exports). OpenAPI 3.1 is generated, so the web client is typed from it. Lower raw CPU speed than Go or the JVM is acceptable because heavy maths runs in DuckDB and Arrow.

## Alternatives considered

Node.js + NestJS (weaker data ecosystem); Go (slower to build analytics; possible for a v2 collector); Kotlin/Spring (heavy for a small team).

## References

docs/11a §2.1
