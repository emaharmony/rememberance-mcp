"""Security tests for backup archive extraction."""

from __future__ import annotations

import io
import tarfile

import pytest

from recall_mcp.admin import _extract_backup_archive


def _archive_with_member(name: str, content: bytes) -> io.BytesIO:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        member = tarfile.TarInfo(name)
        member.size = len(content)
        archive.addfile(member, io.BytesIO(content))
    buffer.seek(0)
    return buffer


def test_backup_archive_extracts_regular_files(tmp_path):
    buffer = _archive_with_member("recall/manifest.json", b"{}")
    with tarfile.open(fileobj=buffer, mode="r:gz") as archive:
        _extract_backup_archive(archive, tmp_path)

    assert (tmp_path / "recall" / "manifest.json").read_bytes() == b"{}"


def test_backup_archive_rejects_path_traversal(tmp_path):
    buffer = _archive_with_member("../outside.txt", b"unsafe")
    with tarfile.open(fileobj=buffer, mode="r:gz") as archive:
        with pytest.raises(SystemExit, match="unsafe path"):
            _extract_backup_archive(archive, tmp_path)

    assert not (tmp_path.parent / "outside.txt").exists()


def test_backup_archive_rejects_links(tmp_path):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        member = tarfile.TarInfo("link")
        member.type = tarfile.SYMTYPE
        member.linkname = "target"
        archive.addfile(member)
    buffer.seek(0)

    with tarfile.open(fileobj=buffer, mode="r:gz") as archive:
        with pytest.raises(SystemExit, match="unsafe entry"):
            _extract_backup_archive(archive, tmp_path)
