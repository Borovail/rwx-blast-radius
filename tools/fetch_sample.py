"""Restore the organizer's public sample fixtures for local evaluation."""

import hashlib
import io
from pathlib import Path
import tarfile
from urllib.request import urlopen


KIT_URL = "https://www.joinbuilders.org/buildathon/tracks/blast-radius/files/blast-radius-sample.tar.gz"
SHA256 = "81efabbc69c3c4367d928f2dba0f33ce9dccccbd080b54721b2636e5d8afab9d"
DEST = Path(__file__).resolve().parents[1] / "sample"


def main():
    with urlopen(KIT_URL, timeout=60) as response:
        data = response.read()
    if hashlib.sha256(data).hexdigest() != SHA256:
        raise SystemExit("Starter kit checksum changed. Verify the organizer's release.")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for member in archive:
            if not member.isfile() or not member.name.startswith("sample/"):
                continue
            target = (DEST.parent / member.name).resolve()
            target.relative_to(DEST.resolve())
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(member) as source:
                target.write_bytes(source.read())


if __name__ == "__main__":
    main()
