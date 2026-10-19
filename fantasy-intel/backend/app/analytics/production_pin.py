"""Which artifacts the consumer view and the evidence readers accept: a leaf module (standard library only,
imports no other analytics module).

Two rules, because the production-line models are built differently:

* `PINNED_MODEL_VERSION` (ifp-1.0.0, retired: production before ifp-2.0.0) has ONE immutable artifact file.
  `APPROVED_ARTIFACT_SHA256` is the sha256 of that file (a run's `artifact_id`); None means not pinned, and
  every run of the version counts, exactly as before. The hosted app never reads the approval registry, so this
  is its registry-free copy of that entry's `artifact.sha256`; a test fails if the two disagree. Pinning is one
  reviewed change to BOTH (see the registry doc, "Pinning the production artifact").
* `REGISTERED_MODEL_VERSIONS` (ifp-2.2.0, the production model, ifp-2.1.0, its fallback, and the retired ifp-2.0.0)
  are retrained every week, so none has a single sha. Each artifact is approved by a row in the write-once `approved_artifacts`
  table of the database itself, for its own model version,
  written when the artifact is saved through the weekly-learning store and used by `project`. A run counts
  only if its `artifact_id` is registered for the model (and the target week, when recorded). A database
  without that table (an old local database or hosted copy) is read as before and marked unverified.

Other model versions are never restricted. The weekly projection view and the evidence readers (the forecast
store and the promotion tracker) read this at call time, so the evidence readers never need the consumer
switch module.
"""

APPROVED_ARTIFACT_SHA256: str | None = "a1fe1bc3f3c54143789bb4562695162df90535a1ff85f927841ef562ab699881"
# The versions the two rules belong to: copies of projection_source.RETIRED_VERSION and (MODEL_VERSION,
# FALLBACK_VERSION, RETIRED_WEEKLY_VERSION) (tested).
PINNED_MODEL_VERSION = "ifp-1.0.0"
REGISTERED_MODEL_VERSIONS = ("ifp-2.2.0", "ifp-2.1.0", "ifp-2.0.0")
REGISTERED_MODEL_VERSION = REGISTERED_MODEL_VERSIONS[0]     # the production one
