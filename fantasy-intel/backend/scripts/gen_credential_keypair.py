"""Generate a credential wrapping keypair.

    uv run python scripts/gen_credential_keypair.py --private-key-file credentials_v1.private.pem

Writes the private key to that file (mode 0600; never printed) and prints the public PEM, which goes into the API's
CREDENTIALS_PUBKEY_V<n> env var. The private key goes only into the GitHub Actions secret CREDENTIALS_PRIVKEY_V<n>;
delete the local file once the secret is stored."""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.control_plane.credential_crypto import generate_keypair  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--private-key-file", required=True, type=Path)
    args = ap.parse_args()
    if args.private_key_file.exists():
        print(f"refusing to overwrite {args.private_key_file}", file=sys.stderr)
        return 2
    private, public = generate_keypair()
    fd = os.open(args.private_key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(private)
    print(public, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
