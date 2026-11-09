"""Immutable, content addressed blobs. Staging stays on the destination filesystem."""
import hashlib
import os
from pathlib import Path
import tempfile

from . import protocol as p


class BlobStore:
    def __init__(self, data_dir, max_bytes=p.DEFAULTS['max_blob_bytes']):
        self.root = Path(data_dir) / 'blobs'
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes

    def path(self, digest):
        p.check_sha256(digest)
        return self.root / digest[:2] / digest[2:4] / digest

    def exists(self, digest, size=None):
        path = self.path(digest)
        return path.is_file() and (size is None or path.stat().st_size == size)

    def read(self, digest):
        return self.path(digest).read_bytes()

    def put(self, digest, data):
        target = self.path(digest)
        if len(data) > self.max_bytes:
            raise OverflowError('blob exceeds max_blob_bytes')
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix='.upload-', dir=str(target.parent))
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            with open(temporary, 'rb') as stream:
                actual = hashlib.sha256(stream.read()).hexdigest()
            if actual != digest:
                raise ValueError('blob sha256 mismatch')
            existed = target.exists()
            os.replace(temporary, str(target))
            if os.name == 'posix':
                directory = os.open(str(target.parent), os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            return not existed
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def manifest(self, value):
        raw = p.canonical_json(p.validate_manifest(value))
        digest = p.sha256_bytes(raw)
        self.put(digest, raw)
        return digest

    def missing(self, manifest):
        return sorted({entry['sha256'] for entry in manifest['files']
                       if not self.exists(entry['sha256'], entry['size'])})
