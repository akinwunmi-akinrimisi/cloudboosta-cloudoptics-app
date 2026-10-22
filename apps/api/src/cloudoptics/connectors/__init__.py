"""Cloud connections, credential brokering, permission checks and sync runs.

AWS now; Azure/GCP later behind the same interface.

Owns its own tables and exposes an internal interface; no cross-module table access
(docs/09 section 1.1).
"""
