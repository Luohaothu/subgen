from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from . import platform_io
from .config import PipelineConfig, normalize_relative_path
from .domain import VideoRecord, stable_video_id
from .media import MediaProbeError, parse_probe, probe_media, stash_target_path


class SourceChangedError(MediaProbeError):
    pass


class InventoryValidationError(ValueError):
    pass


def _canonical_digest(value: object) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise InventoryValidationError(
            f"inventory evidence is not canonical JSON: {exc}"
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


def _source_projection(
    records: tuple[VideoRecord, ...], schema_version: str
) -> dict[str, object]:
    projected: list[dict[str, Any]] = []
    for record in records:
        row = record.to_dict()
        del row["target_exists"]
        projected.append(row)
    return {"schema_version": schema_version, "records": projected}


def _snapshot_projection(
    records: tuple[VideoRecord, ...], schema_version: str
) -> dict[str, object]:
    return {
        "schema_version": schema_version,
        "records": [record.to_dict() for record in records],
    }


def _validate_inventory_records(
    records: tuple[VideoRecord, ...], schema_version: str
) -> None:
    if type(records) is not tuple:
        raise InventoryValidationError("inventory records must be an immutable tuple")
    if type(schema_version) is not str or not schema_version.strip():
        raise InventoryValidationError("inventory schema version must be nonblank")
    if any(type(record) is not VideoRecord for record in records):
        raise InventoryValidationError("inventory records must be VideoRecord values")

    paths = tuple(record.relative_path for record in records)
    if paths != tuple(sorted(paths)):
        raise InventoryValidationError(
            "inventory records must be sorted by relative path"
        )
    identifiers = tuple(record.video_id for record in records)
    targets = tuple(record.target_relative_path for record in records)
    for values, label in (
        (paths, "source path"),
        (identifiers, "video ID"),
        (targets, "target path"),
    ):
        if len(values) != len(set(values)):
            raise InventoryValidationError(f"duplicate inventory {label}")

    for record in records:
        try:
            normalized = normalize_relative_path(record.relative_path)
            normalized_target = normalize_relative_path(record.target_relative_path)
        except ValueError as exc:
            raise InventoryValidationError(str(exc)) from exc
        if record.video_id != stable_video_id(normalized):
            raise InventoryValidationError(
                f"video ID is not bound to source path: {normalized}"
            )
        expected_target = stash_target_path(Path(normalized)).as_posix()
        if normalized_target != expected_target:
            raise InventoryValidationError(
                f"target path is not derived from source path: {normalized}"
            )
        if record.source_bytes <= 0:
            raise InventoryValidationError(
                f"source byte count must be positive: {normalized}"
            )
        if record.duration_ms <= 0:
            raise InventoryValidationError(
                f"source duration must be positive: {normalized}"
            )
        if any(
            value is None
            for value in (
                record.source_mtime_ns,
                record.source_device,
                record.source_inode,
                record.probe_sha256,
                record.presentation_origin_ms,
            )
        ):
            raise InventoryValidationError(
                f"source observation evidence is incomplete: {normalized}"
            )
        if record.inventory_schema_version != schema_version:
            raise InventoryValidationError(
                f"record schema does not match snapshot: {normalized}"
            )
        if record.audio_stream_index is None:
            if record.audio_offset_ms is not None:
                raise InventoryValidationError(
                    f"no-audio record contains an audio offset: {normalized}"
                )
        elif any(
            value is None
            for value in (
                record.audio_offset_ms,
                record.audio_codec,
                record.audio_channels,
                record.audio_sample_rate_hz,
            )
        ):
            raise InventoryValidationError(
                f"selected audio evidence is incomplete: {normalized}"
            )


@dataclass(frozen=True)
class InventorySnapshot:
    records: tuple[VideoRecord, ...]
    schema_version: str
    source_digest: str
    snapshot_digest: str

    def __post_init__(self) -> None:
        _validate_inventory_records(self.records, self.schema_version)
        expected_source = _canonical_digest(
            _source_projection(self.records, self.schema_version)
        )
        if self.source_digest != expected_source:
            raise InventoryValidationError("inventory source digest mismatch")
        expected_snapshot = _canonical_digest(
            _snapshot_projection(self.records, self.schema_version)
        )
        if self.snapshot_digest != expected_snapshot:
            raise InventoryValidationError("inventory snapshot digest mismatch")

    @classmethod
    def build(
        cls, records: tuple[VideoRecord, ...], *, schema_version: str
    ) -> InventorySnapshot:
        _validate_inventory_records(records, schema_version)
        source_digest = _canonical_digest(_source_projection(records, schema_version))
        snapshot_digest = _canonical_digest(
            _snapshot_projection(records, schema_version)
        )
        return cls(
            records=records,
            schema_version=schema_version,
            source_digest=source_digest,
            snapshot_digest=snapshot_digest,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "records": [record.to_dict() for record in self.records],
            "source_digest": self.source_digest,
            "snapshot_digest": self.snapshot_digest,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> InventorySnapshot:
        if not isinstance(value, Mapping) or set(value) != {
            "schema_version",
            "records",
            "source_digest",
            "snapshot_digest",
        }:
            raise InventoryValidationError("invalid inventory snapshot fields")
        raw_records = value["records"]
        if type(raw_records) is not list:
            raise InventoryValidationError("inventory records must decode as a list")
        try:
            records = tuple(VideoRecord.from_dict(row) for row in raw_records)
        except (TypeError, ValueError) as exc:
            raise InventoryValidationError(f"invalid inventory record: {exc}") from exc
        return cls(
            records=records,
            schema_version=value["schema_version"],
            source_digest=value["source_digest"],
            snapshot_digest=value["snapshot_digest"],
        )


def _require_config(config: PipelineConfig) -> None:
    if type(config) is not PipelineConfig:
        raise TypeError("inventory operation requires a frozen PipelineConfig")


def _close_scan_directory(fd: int, label: str, primary: BaseException | None) -> None:
    try:
        os.close(fd)
    except OSError as exc:
        if primary is not None:
            primary.add_note(f"{label} close failure: {exc!r}")
            return
        raise InventoryValidationError(f"{label} close failure: {exc}") from exc


def _close_media_descriptors(
    fds: tuple[int, ...], label: str, primary: BaseException | None
) -> None:
    failures: list[BaseException] = []
    for fd in reversed(fds):
        try:
            os.close(fd)
        except BaseException as exc:
            failures.append(exc)
    if not failures:
        return
    if primary is not None:
        for failure in failures:
            primary.add_note(f"{label} close failure: {failure!r}")
        return
    first = failures[0]
    if isinstance(first, Exception):
        raise MediaProbeError(f"{label} close failure: {first}") from first
    raise first


def _open_source_descriptor(
    config: PipelineConfig, relative_path: str
) -> tuple[int, tuple[int, int, int, int]]:
    parts = PurePosixPath(relative_path).parts
    directory_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    directory_flags |= getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0)
    source_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    source_flags |= getattr(os, "O_NOFOLLOW", 0)
    source_flags |= getattr(os, "O_NONBLOCK", 0)
    directory_fds: list[int] = []
    source_fd: int | None = None
    try:
        try:
            directory_fds.append(os.open(config.source_root, directory_flags))
            for component in parts[:-1]:
                directory_fds.append(
                    os.open(
                        component,
                        directory_flags,
                        dir_fd=directory_fds[-1],
                    )
                )
            source_fd = os.open(
                parts[-1],
                source_flags,
                dir_fd=directory_fds[-1],
            )
            generation = _stat_generation(os.fstat(source_fd), relative_path)
        except OSError as exc:
            raise MediaProbeError(
                f"source cannot be opened safely: {relative_path}: {exc}"
            ) from exc
    except BaseException as exc:
        cleanup = tuple([*directory_fds, *([] if source_fd is None else [source_fd])])
        _close_media_descriptors(cleanup, "source open", exc)
        raise
    try:
        _close_media_descriptors(
            tuple(directory_fds), "source parent descriptors", None
        )
    except BaseException as exc:
        _close_media_descriptors((source_fd,), "source descriptor", exc)
        raise
    return source_fd, generation


def discover_video_paths(config: PipelineConfig) -> tuple[str, ...]:
    _require_config(config)
    discovered: list[str] = []
    directory_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    directory_flags |= getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0)

    def visit(directory_fd: int, relative_parent: PurePosixPath | None) -> None:
        try:
            with os.scandir(directory_fd) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
        except OSError as exc:
            raise InventoryValidationError(
                f"cannot scan source directory {relative_parent or '.'}: {exc}"
            ) from exc
        for entry in entries:
            relative = (
                PurePosixPath(entry.name)
                if relative_parent is None
                else relative_parent / entry.name
            )
            try:
                if entry.is_dir(follow_symlinks=False):
                    try:
                        child_fd = os.open(
                            entry.name,
                            directory_flags,
                            dir_fd=directory_fd,
                        )
                    except OSError as exc:
                        raise InventoryValidationError(
                            f"source directory changed during scan: {relative}: {exc}"
                        ) from exc
                    try:
                        opened = os.fstat(child_fd)
                        if not stat.S_ISDIR(opened.st_mode):
                            raise InventoryValidationError(
                                f"source entry is not a directory: {relative}"
                            )
                        visit(child_fd, relative)
                    finally:
                        _close_scan_directory(
                            child_fd,
                            f"source directory {relative}",
                            sys.exc_info()[1],
                        )
                elif entry.is_file(follow_symlinks=False):
                    suffix = Path(entry.name).suffix.casefold()
                    if suffix in config.video_extensions:
                        discovered.append(normalize_relative_path(relative))
            except OSError as exc:
                raise InventoryValidationError(
                    f"cannot inspect source entry {relative}: {exc}"
                ) from exc

    try:
        root_fd = os.open(config.source_root, directory_flags)
    except OSError as exc:
        raise InventoryValidationError(f"source root is unsafe: {exc}") from exc
    try:
        opened = os.fstat(root_fd)
        if not stat.S_ISDIR(opened.st_mode):
            raise InventoryValidationError("source root is not a directory")
        visit(root_fd, None)
    finally:
        _close_scan_directory(root_fd, "source root", sys.exc_info()[1])
    ordered = tuple(sorted(discovered))
    if len(ordered) != len(set(ordered)):
        raise InventoryValidationError("source scan produced duplicate paths")
    return ordered


def _source_path(
    config: PipelineConfig, relative_path: str
) -> tuple[Path, tuple[tuple[int, int], ...]]:
    normalized = normalize_relative_path(relative_path)
    parts = PurePosixPath(normalized).parts
    parents = [config.source_root]
    current = config.source_root
    for component in parts[:-1]:
        current = current / component
        parents.append(current)
    generations: list[tuple[int, int]] = []
    for current in parents:
        try:
            info = os.stat(current, follow_symlinks=False)
        except OSError as exc:
            raise MediaProbeError(
                f"source parent cannot be observed: {normalized}: {exc}"
            ) from exc
        if not stat.S_ISDIR(info.st_mode):
            raise MediaProbeError(
                f"source parent is not a real directory: {normalized}"
            )
        if any(
            type(value) is not int or value < 0 for value in (info.st_dev, info.st_ino)
        ):
            raise MediaProbeError(f"source parent stat result is invalid: {normalized}")
        generations.append((info.st_dev, info.st_ino))
    return config.source_root.joinpath(*parts), tuple(generations)


def _stat_generation(info: object, relative_path: str) -> tuple[int, int, int, int]:
    try:
        mode = info.st_mode
        size = info.st_size
        mtime_ns = info.st_mtime_ns
        device = info.st_dev
        inode = info.st_ino
    except AttributeError as exc:
        raise MediaProbeError(
            f"source stat result is incomplete: {relative_path}"
        ) from exc
    if not stat.S_ISREG(mode):
        raise MediaProbeError(f"source is not a regular file: {relative_path}")
    if any(
        type(value) is not int or value < 0 for value in (size, mtime_ns, device, inode)
    ):
        raise MediaProbeError(f"source stat result is invalid: {relative_path}")
    return size, mtime_ns, device, inode


def observe_video(
    config: PipelineConfig,
    relative_path: str,
    *,
    probe: Callable[..., dict] = probe_media,
    stat_fn: Callable[..., object] = os.stat,
    lexists_fn: Callable[[os.PathLike[str] | str], bool] = os.path.lexists,
) -> VideoRecord:
    _require_config(config)
    normalized = normalize_relative_path(relative_path)
    source, before_parents = _source_path(config, normalized)
    source_fd: int | None = None
    try:
        source_fd, descriptor_generation = _open_source_descriptor(config, normalized)
        try:
            before = stat_fn(source, follow_symlinks=False)
        except OSError as exc:
            raise MediaProbeError(
                f"source cannot be observed: {normalized}: {exc}"
            ) from exc
        before_generation = _stat_generation(before, normalized)
        if before_generation != descriptor_generation:
            raise SourceChangedError(
                f"source path differs from opened descriptor: {normalized}"
            )
        probe = probe(
            Path(platform_io.fd_path(source_fd)),
            timeout_seconds=config.stage_settings.extraction.probe_timeout_seconds,
        )
        try:
            after_descriptor = os.fstat(source_fd)
            after = stat_fn(source, follow_symlinks=False)
        except OSError as exc:
            raise SourceChangedError(
                f"source disappeared during probe: {normalized}: {exc}"
            ) from exc
        try:
            after_descriptor_generation = _stat_generation(after_descriptor, normalized)
            after_generation = _stat_generation(after, normalized)
            rebound_source, after_parents = _source_path(config, normalized)
        except MediaProbeError as exc:
            raise SourceChangedError(
                f"source changed during probe: {normalized}: {exc}"
            ) from exc
        if rebound_source != source or before_parents != after_parents:
            raise SourceChangedError(
                f"source parent changed during probe: {normalized}"
            )
        if (
            before_generation != after_generation
            or descriptor_generation != after_descriptor_generation
            or after_generation != after_descriptor_generation
        ):
            raise SourceChangedError(f"source changed during probe: {normalized}")

        source_bytes, source_mtime_ns, source_device, source_inode = before_generation
        target_relative = stash_target_path(Path(normalized)).as_posix()
        target = config.source_root.joinpath(*PurePosixPath(target_relative).parts)
        try:
            target_exists = lexists_fn(target)
        except OSError as exc:
            raise MediaProbeError(
                f"target collision cannot be observed: {target_relative}: {exc}"
            ) from exc
        if type(target_exists) is not bool:
            raise MediaProbeError("target collision check must return boolean")
        try:
            _, target_parents = _source_path(config, normalized)
        except MediaProbeError as exc:
            raise SourceChangedError(
                f"source parent changed during target observation: {normalized}: {exc}"
            ) from exc
        if target_parents != before_parents:
            raise SourceChangedError(
                f"source parent changed during target observation: {normalized}"
            )
        return parse_probe(
            normalized,
            source_bytes,
            probe,
            target_exists,
            source_mtime_ns=source_mtime_ns,
            source_device=source_device,
            source_inode=source_inode,
            inventory_schema_version=config.schemas.inventory,
            audio_selection_policy=config.stage_settings.extraction.audio_selection,
        )
    finally:
        if source_fd is not None:
            _close_media_descriptors(
                (source_fd,), "source descriptor", sys.exc_info()[1]
            )


def validate_initial_inventory(
    snapshot: InventorySnapshot, config: PipelineConfig
) -> None:
    """Validate the one-time, pre-publication corpus observations."""

    _require_config(config)
    if type(snapshot) is not InventorySnapshot:
        raise TypeError("initial inventory gate requires an InventorySnapshot")
    failures: list[str] = []
    if snapshot.schema_version != config.schemas.inventory:
        failures.append(
            f"schema {snapshot.schema_version!r} != {config.schemas.inventory!r}"
        )
    if len(snapshot.records) != config.expected_video_count:
        failures.append(
            f"video count {len(snapshot.records)} != {config.expected_video_count}"
        )
    observed_no_audio = {
        record.relative_path
        for record in snapshot.records
        if record.audio_stream_index is None
    }
    expected_no_audio = set(config.known_no_audio_paths)
    if observed_no_audio != expected_no_audio:
        failures.append(
            "no-audio paths differ: "
            f"observed={sorted(observed_no_audio)!r}, "
            f"expected={sorted(expected_no_audio)!r}"
        )
    observed_collisions = {
        record.target_relative_path
        for record in snapshot.records
        if record.target_exists
    }
    expected_collisions = set(config.known_collision_paths)
    if observed_collisions != expected_collisions:
        failures.append(
            "target collisions differ: "
            f"observed={sorted(observed_collisions)!r}, "
            f"expected={sorted(expected_collisions)!r}"
        )
    if failures:
        raise InventoryValidationError(
            "initial pre-publication inventory gate failed: " + "; ".join(failures)
        )
