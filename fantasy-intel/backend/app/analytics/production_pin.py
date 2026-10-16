"""The approved production artifact pin: a leaf module (standard library only, imports no other analytics module).

`APPROVED_ARTIFACT_SHA256` is the sha256 of the artifact FILE that made the production model's approved runs
(the run table's `artifact_id`). None means not pinned: any run of the production model version
counts, exactly as before. The hosted app never reads the approval registry, so this is its registry-free copy
of the production entry's `artifact.sha256`; a test fails if the two disagree. Pinning is one reviewed change
to BOTH (see the registry doc, "Pinning the production artifact").

The weekly projection view and the evidence readers (the forecast store and the promotion tracker) read the pin
from here at call time, so the evidence readers never need the consumer switch module.
"""

APPROVED_ARTIFACT_SHA256: str | None = None
# The model version the pin belongs to: a numpy-free copy of projection_source.MODEL_VERSION (tested equal).
# Reads of any other model version (the ifp-2 shadow, say) are never restricted by the pin.
PINNED_MODEL_VERSION = "ifp-1.0.0"
