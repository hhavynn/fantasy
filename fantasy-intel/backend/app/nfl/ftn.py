"""FTN charting constants. Dependency-free so the API can use them without the sync stack."""

# FTN `read_thrown`: the nflreadr dictionary documents "0" = primary read, but observed
# 2023-2026 data conflicts (code "1" covers ~50-60% of targets; "0" mostly tags non-pass
# plays). Upstream clarification pending: https://github.com/nflverse/nflreadr/issues/325
# We store raw per-code counts and do NOT treat any numeric code as the primary read.
FTN_READ_SEMANTICS_STATUS = "UNVERIFIED"
FTN_ASSUMED_PRIMARY_READ_CODE = "1"   # display-only assumption; never used in scoring
FTN_SEMANTICS_REFERENCE = "https://github.com/nflverse/nflreadr/issues/325"
FTN_NUMERIC_CODES = ("0", "1", "2")
