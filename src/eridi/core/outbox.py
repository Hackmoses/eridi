"""The outbox: files saved through Eridi wait here until their storage has them.

Saving never goes straight to storage. The bytes are first written to
`<app-data>/outbox/<job>.bin` with a small job file next to them (`<job>.json`), so a file
is safe on this computer even if the network, the storage or Eridi itself fails. Then the
upload is tried, and retried with growing waits until it works:

    saved (on this computer) → queued (waiting to retry) → uploading → uploaded

Other end states: `needs_confirmation` (a file with that name already exists and the user
must choose to replace it or save under a new name) and `blocked` (saving there isn't
allowed: Save is off for the storage, or the project's data label isn't approved there).

A job left in `uploading` by a crash is simply tried again. Uploads are idempotent: if the
storage already holds exactly these bytes, the job counts as uploaded.
"""

import contextlib
import hashlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import Any, Literal

from eridi.core.fsutil import atomic_write_bytes
from eridi.core.ids import is_ulid, new_ulid
from eridi.core.models import utc_now
from eridi.core.paths import app_home

log = logging.getLogger(__name__)

JobState = Literal["saved", "queued", "uploading", "uploaded", "needs_confirmation", "blocked"]
OnExists = Literal["ask", "replace", "rename"]
ON_EXISTS: tuple[str, ...] = ("ask", "replace", "rename")
"""What to do when the name is taken: ask the user, save as a new version of that file
(the storage keeps the old one where it keeps history), or save under a free name."""

FIRST_RETRY_S = 5.0
MAX_RETRY_S = 3600.0
KEEP_UPLOADED_S = 30 * 24 * 3600.0
"""Finished jobs (without their bytes) are listed for 30 days, then forgotten."""


class SaveRefusedError(Exception):
    """Saving there isn't allowed. The message says why and how to change it."""


class NameTakenError(Exception):
    """The storage already has a different file at this path."""


@dataclass(frozen=True)
class OutboxJob:
    id: str
    root_id: str
    path: str
    """Where the file goes, POSIX-style, relative to the storage."""
    size: int
    sha256: str
    created_at: str
    state: str = "saved"
    on_exists: str = "ask"
    project_id: str | None = None
    attempts: int = 0
    next_attempt: float = 0.0
    """Seconds since the epoch; the job isn't tried before then."""
    last_error: str | None = None
    uploaded_path: str | None = None
    """The path it was finally saved at (differs from `path` after a rename)."""
    uploaded_at: str | None = None
    finished: float | None = None

    @property
    def done(self) -> bool:
        return self.state == "uploaded"

    @property
    def waiting(self) -> bool:
        """Still to be uploaded without the user doing anything."""
        return self.state in ("saved", "queued", "uploading")


Uploader = Callable[[OutboxJob, bytes], str]
"""Puts the bytes in storage; returns the path used. Raises `NameTakenError`,
`SaveRefusedError`, or anything else for a failure worth retrying."""


def retry_delay(attempts: int) -> float:
    """5 s, 10 s, 20 s, … up to an hour."""
    return float(min(FIRST_RETRY_S * 2 ** max(attempts - 1, 0), MAX_RETRY_S))


def free_name(path: str, taken: Callable[[str], bool]) -> str:
    """`Folder/name (2).ext`, `(3)`, … : the first that `taken` says is free."""
    folder, _, name = path.rpartition("/")
    stem, dot, ext = name.rpartition(".")
    if not stem:  # no extension, or a dot-file
        stem, dot, ext = name, "", ""
    for n in range(2, 1000):
        candidate = f"{stem} ({n}){dot}{ext}"
        full = f"{folder}/{candidate}" if folder else candidate
        if not taken(full):
            return full
    raise SaveRefusedError(f"Could not find a free name for {path}. Choose another name.")


class Outbox:
    def __init__(self, folder: Path | None = None, *, clock: Callable[[], float] = time.time):
        self.folder = folder or app_home() / "outbox"
        self.clock = clock

    def _job_file(self, job_id: str) -> Path:
        return self.folder / f"{job_id}.json"

    def _data_file(self, job_id: str) -> Path:
        return self.folder / f"{job_id}.bin"

    def _write(self, job: OutboxJob) -> OutboxJob:
        body = json.dumps(asdict(job), indent=2, sort_keys=True) + "\n"
        atomic_write_bytes(self._job_file(job.id), body.encode("utf-8"))
        return job

    def add(
        self,
        root_id: str,
        path: str,
        data: bytes,
        *,
        on_exists: str = "ask",
        project_id: str | None = None,
    ) -> OutboxJob:
        """Keep the bytes on this computer and queue the upload."""
        if on_exists not in ON_EXISTS:
            raise ValueError(f"on_exists must be one of {', '.join(ON_EXISTS)}")
        job_id = new_ulid()
        atomic_write_bytes(self._data_file(job_id), data)  # bytes first, then the job
        job = OutboxJob(
            id=job_id, root_id=root_id, path=path, size=len(data),
            sha256=hashlib.sha256(data).hexdigest(), created_at=utc_now(),
            on_exists=on_exists, project_id=project_id,
        )  # fmt: skip
        return self._write(job)

    def get(self, job_id: str) -> OutboxJob | None:
        if not is_ulid(job_id):
            return None
        try:
            data = json.loads(self._job_file(job_id).read_text("utf-8"))
        except (OSError, ValueError):
            return None
        return _job(data)

    def jobs(self) -> list[OutboxJob]:
        """Every job, oldest first."""
        out: list[OutboxJob] = []
        if not self.folder.is_dir():
            return out
        for path in sorted(self.folder.glob("*.json")):
            job = self.get(path.stem)
            if job is not None:
                out.append(job)
        return out

    def data(self, job: OutboxJob) -> bytes:
        return self._data_file(job.id).read_bytes()

    def update(self, job: OutboxJob, **changes: Any) -> OutboxJob:
        return self._write(replace(job, **changes))

    def due(self) -> list[OutboxJob]:
        now = self.clock()
        return [j for j in self.jobs() if j.waiting and j.next_attempt <= now]

    def retry(self, job_id: str, on_exists: str | None = None) -> OutboxJob | None:
        """Try a job again at the next chance (after the user fixed or confirmed something)."""
        job = self.get(job_id)
        if job is None or job.done:
            return job
        if on_exists is not None and on_exists not in ON_EXISTS:
            raise ValueError(f"on_exists must be one of {', '.join(ON_EXISTS)}")
        return self.update(
            job, state="queued", next_attempt=0.0, last_error=None,
            on_exists=on_exists or job.on_exists,
        )  # fmt: skip

    def cancel(self, job_id: str) -> bool:
        """Forget a job that isn't uploaded, and its local copy."""
        job = self.get(job_id)
        if job is None:
            return False
        self._forget(job.id)
        return True

    def _forget(self, job_id: str) -> None:
        for path in (self._data_file(job_id), self._job_file(job_id)):
            with contextlib.suppress(FileNotFoundError):
                path.unlink()

    def run(self, job: OutboxJob, upload: Uploader) -> OutboxJob:
        """Try one upload now and record what happened."""
        try:
            data = self.data(job)
        except OSError:
            return self.update(
                job, state="blocked",
                last_error="The copy saved on this computer is gone. Save the file again.",
            )  # fmt: skip
        job = self.update(job, state="uploading", attempts=job.attempts + 1)
        try:
            saved_at = upload(job, data)
        except NameTakenError:
            return self.update(
                job, state="needs_confirmation",
                last_error=f"{job.path} already exists. Replace it, or save under a new name.",
            )  # fmt: skip
        except SaveRefusedError as exc:
            return self.update(job, state="blocked", last_error=str(exc))
        except Exception as exc:  # offline, throttled, storage busy: try again later
            log.info("Upload of outbox job %s failed; will retry: %s", job.id, exc)
            wait = retry_delay(job.attempts)
            return self.update(
                job, state="queued", next_attempt=self.clock() + wait, last_error=str(exc)
            )
        with contextlib.suppress(FileNotFoundError):
            self._data_file(job.id).unlink()
        return self.update(
            job, state="uploaded", uploaded_path=saved_at, uploaded_at=utc_now(),
            last_error=None, finished=self.clock(),
        )  # fmt: skip

    def process(self, upload: Uploader) -> list[OutboxJob]:
        """Upload every job that is due. Returns the jobs as they ended up."""
        results = [self.run(job, upload) for job in self.due()]
        self.prune()
        return results

    def prune(self) -> None:
        cutoff = self.clock() - KEEP_UPLOADED_S
        for job in self.jobs():
            if job.done and (job.finished or 0) < cutoff:
                self._forget(job.id)


def _job(data: Any) -> OutboxJob | None:
    if not isinstance(data, dict):
        return None
    known = {f.name for f in fields(OutboxJob)}
    try:
        return OutboxJob(**{k: v for k, v in data.items() if k in known})
    except TypeError:
        return None
