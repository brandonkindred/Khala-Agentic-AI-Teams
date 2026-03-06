from __future__ import annotations

from dataclasses import dataclass


@dataclass
class S3ObjectRef:
    uri: str


class S3Store:
    """Simple object store adapter."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put_bytes(self, key: str, payload: bytes) -> S3ObjectRef:
        uri = f"s3://studiogrid/{key}"
        self.objects[uri] = payload
        return S3ObjectRef(uri=uri)
