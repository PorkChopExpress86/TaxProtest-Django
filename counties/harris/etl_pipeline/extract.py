"""
ETL Pipeline Extract Manager

Provides safe and efficient archive extraction with validation,
streaming support, and automatic cleanup.
"""

import json
import os
import shutil
import tarfile
import zipfile
from collections.abc import Callable, Generator
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

from counties.common.source_digests import PROVENANCE_FILENAME, file_digest
from counties.harris.source_catalog import DEFAULT_HCAD_SOURCE_CATALOG

from .config import DataSource, ETLConfig
from .logging import ETLLogger


@dataclass
class ExtractResult:
    """Result of an extraction operation."""

    source: DataSource
    success: bool
    extract_dir: Path | None = None
    files_extracted: list[str] | None = None
    error: str | None = None
    bytes_extracted: int = 0

    def __post_init__(self):
        if self.files_extracted is None:
            self.files_extracted = []

    def __str__(self) -> str:
        status = "SUCCESS" if self.success else "FAILED"
        count = len(self.files_extracted or [])
        return f"ExtractResult({self.source.name}: {status}, {count} files)"


class ExtractionError(Exception):
    """Exception raised for extraction failures."""

    pass


class ArchiveValidationError(ExtractionError):
    """Exception raised when archive validation fails."""

    pass


class ExtractManager:
    """Manages archive extraction with validation and streaming support.

    Features:
    - Support for ZIP, TAR, TAR.GZ formats
    - Streaming extraction for large files
    - Archive integrity validation
    - Memory-efficient processing
    - Automatic cleanup on errors
    - Pattern-based file filtering
    """

    def __init__(
        self,
        config: ETLConfig,
        logger: ETLLogger | None = None,
    ):
        self.config = config
        self.extract_config = config.extract
        self.extract_dir = config.extract_dir
        self.download_dir = config.download_dir
        self.logger = logger or ETLLogger(name="extract_manager")

    def _get_extract_path(self, source: DataSource) -> Path:
        """Get the extraction directory for a source: the configured root plus the catalog name."""
        return self.extract_dir / DEFAULT_HCAD_SOURCE_CATALOG.extract_dirname(source.source_id)

    def _validate_zip(self, archive_path: Path) -> bool:
        """Validate a ZIP archive."""
        try:
            with zipfile.ZipFile(archive_path, "r") as zf:
                # Check for bad files
                bad_file = zf.testzip()
                if bad_file:
                    self.logger.error(f"Corrupt file in archive: {bad_file}")
                    return False
                return True
        except zipfile.BadZipFile as e:
            self.logger.error(f"Invalid ZIP file: {e}")
            return False
        except OSError as e:
            self.logger.error(f"Error validating ZIP: {e}")
            return False

    def _validate_tar(self, archive_path: Path) -> bool:
        """Validate a TAR archive."""
        try:
            with tarfile.open(archive_path, "r:*") as tf:
                # Just try to list members
                tf.getmembers()
                return True
        except tarfile.TarError as e:
            self.logger.error(f"Invalid TAR file: {e}")
            return False
        except OSError as e:
            self.logger.error(f"Error validating TAR: {e}")
            return False

    def verify_archive(self, archive_path: Path) -> bool:
        """Verify archive integrity.

        Args:
            archive_path: Path to the archive file

        Returns:
            True if archive is valid
        """
        if not archive_path.exists():
            self.logger.error(f"Archive not found: {archive_path}")
            return False

        suffix = archive_path.suffix.lower()
        name_lower = archive_path.name.lower()

        if suffix == ".zip":
            return self._validate_zip(archive_path)
        elif suffix in [".tar", ".gz"] or name_lower.endswith(".tar.gz"):
            return self._validate_tar(archive_path)
        else:
            self.logger.warning(f"Unknown archive format: {suffix}")
            return True  # Assume valid if we can't check

    def _should_extract_file(
        self,
        filename: str,
        patterns: list[str] | None = None,
    ) -> bool:
        """Check if a file should be extracted based on patterns."""
        normalized = filename.replace("\\", "/")
        if ".gdb/" in normalized:
            return True

        # Get file extension
        ext = Path(filename).suffix.lower()

        # Check allowed extensions
        if self.extract_config.allowed_extensions:
            if ext not in self.extract_config.allowed_extensions:
                return False

        # Check extraction patterns
        if patterns:
            base_name = Path(filename).name
            return any(fnmatch(base_name, p) for p in patterns)

        return True

    def _extract_zip(
        self,
        archive_path: Path,
        dest_dir: Path,
        patterns: list[str] | None = None,
        progress_callback: Callable[[str, int, int], None] | None = None,
    ) -> tuple[list[str], int]:
        """Extract a ZIP archive."""
        extracted_files: list[str] = []
        total_bytes = 0

        with zipfile.ZipFile(archive_path, "r") as zf:
            members = zf.infolist()
            total = len(members)

            for idx, member in enumerate(members, 1):
                if member.is_dir():
                    continue

                if not self._should_extract_file(member.filename, patterns):
                    continue

                # Check file size limit
                if self.extract_config.max_file_size:
                    if member.file_size > self.extract_config.max_file_size:
                        self.logger.warning(
                            f"Skipping large file: {member.filename} "
                            f"({member.file_size:,} bytes)"
                        )
                        continue

                # Extract file
                zf.extract(member, dest_dir)
                extracted_files.append(member.filename)
                total_bytes += member.file_size

                if progress_callback:
                    progress_callback(member.filename, idx, total)

        return extracted_files, total_bytes

    def _extract_tar(
        self,
        archive_path: Path,
        dest_dir: Path,
        patterns: list[str] | None = None,
        progress_callback: Callable[[str, int, int], None] | None = None,
    ) -> tuple[list[str], int]:
        """Extract a TAR archive."""
        extracted_files: list[str] = []
        total_bytes = 0

        with tarfile.open(archive_path, "r:*") as tf:
            members = tf.getmembers()
            total = len(members)

            for idx, member in enumerate(members, 1):
                if member.isdir():
                    continue

                if not self._should_extract_file(member.name, patterns):
                    continue

                # Check file size limit
                if self.extract_config.max_file_size:
                    if member.size > self.extract_config.max_file_size:
                        self.logger.warning(
                            f"Skipping large file: {member.name} " f"({member.size:,} bytes)"
                        )
                        continue

                # Extract file
                tf.extract(member, dest_dir)
                extracted_files.append(member.name)
                total_bytes += member.size

                if progress_callback:
                    progress_callback(member.name, idx, total)

        return extracted_files, total_bytes

    def extract_archive(
        self,
        source: DataSource,
        archive_path: Path | None = None,
        dest_dir: Path | None = None,
        validate: bool | None = None,
        progress_callback: Callable[[str, int, int], None] | None = None,
    ) -> ExtractResult:
        """Extract an archive file.

        Args:
            source: Data source configuration
            archive_path: Path to archive (default: download_dir/filename)
            dest_dir: Destination directory (default: extract_dir/source_name)
            validate: Whether to validate before extraction (default from config)
            progress_callback: Optional callback(filename, current, total)

        Returns:
            ExtractResult with extraction status and metadata
        """
        archive_path = archive_path or self.get_archive_path(source)
        dest_dir = dest_dir or self._get_extract_path(source)
        validate = validate if validate is not None else self.extract_config.validate_archive

        self.logger.info(f"Extracting {source.name} from {archive_path}")

        try:
            # A missing archive is reported by validation, never by removing its extract.
            provenance = self._provenance(source, archive_path) if archive_path.is_file() else None
            unchanged = (
                None
                if provenance is None
                else self._unchanged_extract(source, dest_dir, provenance)
            )
            if unchanged is not None:
                self.logger.info(f"{source.name} is already extracted from this archive")
                return ExtractResult(
                    source=source,
                    success=True,
                    extract_dir=dest_dir,
                    files_extracted=unchanged,
                    bytes_extracted=0,
                )
            # An extraction stopped part way must never pass for the earlier one.
            (dest_dir / PROVENANCE_FILENAME).unlink(missing_ok=True)

            # Validate if requested
            if validate:
                if not self.verify_archive(archive_path):
                    return ExtractResult(
                        source=source,
                        success=False,
                        error="Archive validation failed",
                    )

            # Prepare destination directory
            if self.extract_config.overwrite_existing and dest_dir.exists():
                shutil.rmtree(dest_dir)
            dest_dir.mkdir(parents=True, exist_ok=True)

            # Determine extraction patterns
            patterns = source.extract_patterns or None

            # Extract based on format
            suffix = archive_path.suffix.lower()
            name_lower = archive_path.name.lower()

            if suffix == ".zip":
                files, bytes_extracted = self._extract_zip(
                    archive_path, dest_dir, patterns, progress_callback
                )
            elif suffix in [".tar", ".gz"] or name_lower.endswith(".tar.gz"):
                files, bytes_extracted = self._extract_tar(
                    archive_path, dest_dir, patterns, progress_callback
                )
            else:
                return ExtractResult(
                    source=source,
                    success=False,
                    error=f"Unsupported archive format: {suffix}",
                )

            self.logger.info(
                f"Extracted {len(files)} files ({bytes_extracted:,} bytes) " f"from {source.name}"
            )
            if provenance is not None:
                self._record_provenance(dest_dir, provenance, files)

            return ExtractResult(
                source=source,
                success=True,
                extract_dir=dest_dir,
                files_extracted=files,
                bytes_extracted=bytes_extracted,
            )

        except (OSError, ValueError, zipfile.BadZipFile, tarfile.TarError) as e:
            self.logger.exception(f"Error extracting {source.name}")
            # Cleanup on error
            if dest_dir.exists():
                shutil.rmtree(dest_dir, ignore_errors=True)
            return ExtractResult(
                source=source,
                success=False,
                error=str(e),
            )

    def _provenance(self, source: DataSource, archive_path: Path) -> dict:
        """Name the archive bytes and member selection an extract is made from."""
        return {
            "archive_sha256": file_digest(archive_path),
            "patterns": source.extract_patterns,
            "allowed_extensions": self.extract_config.allowed_extensions,
            "max_file_size": self.extract_config.max_file_size,
        }

    def _unchanged_extract(
        self, source: DataSource, dest_dir: Path, provenance: dict
    ) -> list[str] | None:
        """Return the members of a complete extract made from the same archive, if any."""
        try:
            recorded = json.loads((dest_dir / PROVENANCE_FILENAME).read_text())
            files = recorded.pop("files")
        except (OSError, ValueError, KeyError, AttributeError):
            return None
        if recorded != provenance:
            return None
        for name, digest in files.items():
            path = dest_dir / name
            if not path.is_file() or file_digest(path) != digest:
                return None
        if DEFAULT_HCAD_SOURCE_CATALOG.missing_required_members(
            source.source_id, [dest_dir / name for name in files]
        ):
            return None
        return list(files)

    def _record_provenance(self, dest_dir: Path, provenance: dict, files: list[str]) -> None:
        paths = {name: dest_dir / name for name in files}
        if not all(path.is_file() for path in paths.values()):
            return  # a member name the archive library rewrote; extract it again next time
        recorded = {
            **provenance,
            "files": {name: file_digest(path) for name, path in paths.items()},
        }
        partial = dest_dir / f"{PROVENANCE_FILENAME}.partial"
        partial.write_text(json.dumps(recorded, sort_keys=True))
        os.replace(partial, dest_dir / PROVENANCE_FILENAME)

    def extract_batch(
        self,
        sources: list[DataSource],
        progress_callback: Callable[[str, int, int], None] | None = None,
    ) -> list[ExtractResult]:
        """Extract multiple archives.

        Args:
            sources: List of data sources to extract
            progress_callback: Optional callback(source_name, current, total)

        Returns:
            List of ExtractResult for each source
        """
        results: list[ExtractResult] = []

        self.logger.info(f"Starting batch extraction of {len(sources)} archives")

        for idx, source in enumerate(sources, 1):
            if progress_callback:
                progress_callback(source.name, idx, len(sources))

            archive_path = self.get_archive_path(source)
            if not archive_path.exists():
                self.logger.warning(f"Archive not found: {archive_path}")
                results.append(
                    ExtractResult(
                        source=source,
                        success=False,
                        error="Archive not found",
                    )
                )
                continue

            result = self.extract_archive(source)
            results.append(result)

        # Log summary
        success_count = sum(1 for r in results if r.success)
        total_bytes = sum(r.bytes_extracted for r in results)
        total_files = sum(len(r.files_extracted or []) for r in results)

        self.logger.info(
            f"Batch extraction complete: {success_count}/{len(sources)} succeeded, "
            f"{total_files} files, {total_bytes:,} bytes"
        )

        return results

    def stream_extract(
        self,
        archive_path: Path,
        file_pattern: str | None = None,
    ) -> Generator[tuple[str, bytes], None, None]:
        """Stream files from an archive without full extraction.

        Yields:
            Tuple of (filename, file_contents)
        """
        suffix = archive_path.suffix.lower()

        if suffix == ".zip":
            with zipfile.ZipFile(archive_path, "r") as zf:
                for member in zf.infolist():
                    if member.is_dir():
                        continue
                    if file_pattern and not fnmatch(member.filename, file_pattern):
                        continue
                    with zf.open(member) as f:
                        yield member.filename, f.read()

        elif suffix in [".tar", ".gz"]:
            with tarfile.open(archive_path, "r:*") as tf:
                for member in tf.getmembers():
                    if member.isdir():
                        continue
                    if file_pattern and not fnmatch(member.name, file_pattern):
                        continue
                    f = tf.extractfile(member)
                    if f:
                        yield member.name, f.read()

    def get_extract_path(self, source: DataSource) -> Path:
        """Get the extraction directory for a source."""
        return self._get_extract_path(source)

    def get_archive_path(self, source: DataSource) -> Path:
        """Get the downloaded archive a source extracts from."""
        return self.download_dir / source.filename

    def is_extracted(self, source: DataSource) -> bool:
        """Check if a source has been extracted."""
        extract_path = self._get_extract_path(source)
        return extract_path.exists() and any(extract_path.iterdir())

    def list_extracted_files(self, source: DataSource) -> list[Path]:
        """List all files in the extraction directory."""
        extract_path = self._get_extract_path(source)
        if not extract_path.exists():
            return []
        return list(extract_path.rglob("*"))

    def cleanup(
        self,
        sources: list[DataSource] | None = None,
    ) -> None:
        """Clean up extracted files.

        Args:
            sources: Specific sources to clean (default: all)
        """
        if sources:
            for source in sources:
                extract_path = self._get_extract_path(source)
                if extract_path.exists():
                    shutil.rmtree(extract_path)
                    self.logger.debug(f"Removed {extract_path}")
        else:
            # Clean entire extract directory
            if self.extract_dir.exists():
                for item in self.extract_dir.iterdir():
                    if item.is_dir():
                        shutil.rmtree(item)
                    else:
                        item.unlink()
                self.logger.debug(f"Cleaned extract directory: {self.extract_dir}")
