"""Read immutable pre-fix source files from Git objects."""
import hashlib
import re
import subprocess
from pathlib import Path
from .io import repo_path


class Snapshot:
    def __init__(self, directory, revision, repo=None):
        self.directory = Path(directory).resolve()
        self.repo = repo
        if not re.fullmatch(r"[0-9a-fA-F]{7,40}", revision or ""):
            raise ValueError("Use a real pre-fix commit SHA, not a branch or accepted patch ID")
        self.revision = revision
        self._cache = {}
        self._git("cat-file", "-e", revision + "^{commit}")

    def _git(self, *args):
        p = subprocess.run(["git", "-C", str(self.directory), *args], capture_output=True, timeout=120)
        if p.returncode:
            raise ValueError("Required pre-fix Git object is unavailable")
        return p.stdout

    def files(self):
        out = self._git("ls-tree", "-r", "-z", self.revision)
        paths = []
        for item in out.split(b"\0"):
            if not item:
                continue
            meta, name = item.split(b"\t", 1)
            # Index regular Git blobs from the selected revision.
            if meta.split()[0] in (b"100644", b"100755"):
                paths.append(name.decode("utf-8"))
        return sorted(paths)

    def text(self, filename):
        filename = repo_path(filename)
        if filename not in self._cache:
            raw = self._git("show", self.revision + ":" + filename)
            if b"\0" in raw or len(raw) > 4_000_000:
                raise ValueError("Binary or oversized file is ineligible for text access")
            self._cache[filename] = raw.decode("utf-8", errors="replace")
        return self._cache[filename]

    def span(self, filename, start, end):
        if isinstance(start, bool) or isinstance(end, bool) or not isinstance(start, int) or not isinstance(end, int):
            raise ValueError("Span lines must be integers")
        lines = self.text(filename).splitlines()
        if start < 1 or end < start or end > len(lines):
            raise ValueError("Invalid pre-fix source span")
        return "\n".join(lines[start - 1:end])

    def anchor(self, filename, start, end):
        text = self.span(filename, start, end)
        return {"kind": "source", "file": repo_path(filename), "start_line": start, "end_line": end,
                "base_commit": self.revision, "sha256": hashlib.sha256(text.encode()).hexdigest()}

    def blob_sha(self,filename):
        return self._git('rev-parse',self.revision+':'+repo_path(filename)).decode().strip()


def get_snapshot(instance, repo_map):
    if instance["repo"] not in repo_map:
        raise ValueError("Repository missing from repo map")
    return Snapshot(repo_map[instance["repo"]], instance["base_commit"], repo=instance["repo"])
