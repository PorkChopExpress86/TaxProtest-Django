"""
ETL Pipeline Download Manager

Provides robust file downloading with retry logic, checksum validation,
progress tracking, and parallel download support.
"""

import hashlib
import os
import random
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import DataSource, DownloadConfig, ETLConfig
from .logging import ETLLogger, ETLMetrics


@dataclass
class DownloadResult:
    """Result of a download operation."""
    source: DataSource
    success: bool
    local_path: Optional[Path] = None
    error: Optional[str] = None
    bytes_downloaded: int = 0
    duration: float = 0.0
    checksum_verified: bool = False
    attempts: int = 1
    
    def __str__(self) -> str:
        status = "SUCCESS" if self.success else "FAILED"
        return f"DownloadResult({self.source.name}: {status})"


class DownloadError(Exception):
    """Exception raised for download failures."""
    pass


class ChecksumError(DownloadError):
    """Exception raised for checksum verification failures."""
    pass


class DownloadManager:
    """Manages file downloads with retry logic and validation.
    
    Features:
    - Configurable retry with exponential backoff
    - SHA256 checksum validation
    - Parallel downloads for multiple files
    - Progress tracking and ETA calculation
    - Bandwidth throttling support
    - Resume partial downloads (when supported)
    """
    
    def __init__(
        self,
        config: ETLConfig,
        logger: Optional[ETLLogger] = None,
    ):
        self.config = config
        self.download_config = config.download
        self.download_dir = config.download_dir
        self.logger = logger or ETLLogger(name='download_manager')
        
        # Create session with retry logic
        self.session = self._create_session()
    
    def _create_session(self) -> requests.Session:
        """Create a requests session with retry configuration."""
        session = requests.Session()
        
        retry_strategy = Retry(
            total=self.download_config.retry.max_retries,
            backoff_factor=self.download_config.retry.initial_delay,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["HEAD", "GET"],
        )
        
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        
        return session
    
    def _calculate_delay(self, attempt: int) -> float:
        """Calculate delay for retry with exponential backoff and jitter."""
        delay = self.download_config.retry.initial_delay * (
            self.download_config.retry.exponential_base ** attempt
        )
        delay = min(delay, self.download_config.retry.max_delay)
        
        if self.download_config.retry.jitter:
            delay = delay * (0.5 + random.random())
        
        return delay
    
    def _get_file_hash(self, filepath: Path) -> str:
        """Calculate SHA256 hash of a file."""
        sha256_hash = hashlib.sha256()
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                sha256_hash.update(chunk)
        return sha256_hash.hexdigest()
    
    def verify_checksum(
        self,
        filepath: Path,
        expected_hash: str,
    ) -> bool:
        """Verify file checksum matches expected hash."""
        actual_hash = self._get_file_hash(filepath)
        return actual_hash.lower() == expected_hash.lower()
    
    def download_file(
        self,
        source: DataSource,
        dest_path: Optional[Path] = None,
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> DownloadResult:
        """Download a single file with retry logic.
        
        Args:
            source: Data source configuration
            dest_path: Destination path (default: download_dir/filename)
            progress_callback: Optional callback(bytes_downloaded, total_bytes)
        
        Returns:
            DownloadResult with download status and metadata
        """
        url = source.get_url(self.config.data_year)
        
        # Check for 404 and fallback if needed
        try:
             # Only check if it looks like a year-based URL (contains year digits)
             if str(self.config.data_year) in url:
                 head_resp = self.session.head(url, timeout=10)
                 if head_resp.status_code == 404:
                     fallback_year = self.config.data_year - 1
                     fallback_url = source.get_url(fallback_year)
                     self.logger.warning(f"URL {url} returned 404. Falling back to previous year: {fallback_url}")
                     url = fallback_url
        except Exception as e:
            self.logger.debug(f"Pre-download check failed for {url}: {e}")

        dest_path = dest_path or (self.download_dir / source.filename)
        
        self.logger.info(f"Downloading {source.name} from {url}")
        
        start_time = time.time()
        last_error: Optional[str] = None
        attempts = 0
        
        for attempt in range(self.download_config.retry.max_retries + 1):
            attempts += 1
            try:
                return self._download_with_progress(
                    url=url,
                    dest_path=dest_path,
                    source=source,
                    progress_callback=progress_callback,
                    start_time=start_time,
                    attempts=attempts,
                )
            except requests.exceptions.RequestException as e:
                last_error = str(e)
                self.logger.warning(
                    f"Download attempt {attempt + 1} failed for {source.name}: {e}"
                )
                
                if attempt < self.download_config.retry.max_retries:
                    delay = self._calculate_delay(attempt)
                    self.logger.info(f"Retrying in {delay:.1f} seconds...")
                    time.sleep(delay)
            except ChecksumError as e:
                last_error = str(e)
                self.logger.error(f"Checksum verification failed for {source.name}")
                break  # Don't retry checksum failures
            except Exception as e:
                last_error = str(e)
                self.logger.exception(f"Unexpected error downloading {source.name}")
                break
        
        # All retries exhausted
        return DownloadResult(
            source=source,
            success=False,
            error=last_error,
            duration=time.time() - start_time,
            attempts=attempts,
        )
    
    def _download_with_progress(
        self,
        url: str,
        dest_path: Path,
        source: DataSource,
        progress_callback: Optional[Callable[[int, int], None]],
        start_time: float,
        attempts: int,
    ) -> DownloadResult:
        """Download file with progress tracking."""
        # Ensure parent directory exists
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Stream download
        with self.session.get(
            url,
            stream=True,
            timeout=self.download_config.timeout,
            verify=self.download_config.verify_ssl,
        ) as response:
            response.raise_for_status()
            
            total_size = int(response.headers.get('content-length', 0))
            bytes_downloaded = 0
            
            with open(dest_path, 'wb') as f:
                for chunk in response.iter_content(
                    chunk_size=self.download_config.chunk_size
                ):
                    if chunk:
                        f.write(chunk)
                        bytes_downloaded += len(chunk)
                        
                        if progress_callback:
                            progress_callback(bytes_downloaded, total_size)
                        
                        # Bandwidth throttling
                        if self.download_config.bandwidth_limit:
                            elapsed = time.time() - start_time
                            expected_time = bytes_downloaded / self.download_config.bandwidth_limit
                            if expected_time > elapsed:
                                time.sleep(expected_time - elapsed)
        
        # Verify checksum if provided
        checksum_verified = False
        if source.checksum:
            if not self.verify_checksum(dest_path, source.checksum):
                dest_path.unlink(missing_ok=True)
                raise ChecksumError(
                    f"Checksum mismatch for {source.name}. "
                    f"Expected: {source.checksum}"
                )
            checksum_verified = True
        
        duration = time.time() - start_time
        self.logger.info(
            f"Downloaded {source.name}: {bytes_downloaded:,} bytes in {duration:.1f}s"
        )
        
        return DownloadResult(
            source=source,
            success=True,
            local_path=dest_path,
            bytes_downloaded=bytes_downloaded,
            duration=duration,
            checksum_verified=checksum_verified,
            attempts=attempts,
        )
    
    def download_batch(
        self,
        sources: List[DataSource],
        max_parallel: Optional[int] = None,
        progress_callback: Optional[Callable[[str, int, int], None]] = None,
    ) -> List[DownloadResult]:
        """Download multiple files, optionally in parallel.
        
        Args:
            sources: List of data sources to download
            max_parallel: Maximum concurrent downloads (default from config)
            progress_callback: Optional callback(source_name, current, total)
        
        Returns:
            List of DownloadResult for each source
        """
        max_parallel = max_parallel or self.download_config.max_parallel
        results: List[DownloadResult] = []
        
        self.logger.info(f"Starting batch download of {len(sources)} files")
        
        if max_parallel == 1:
            # Sequential download
            for idx, source in enumerate(sources, 1):
                if progress_callback:
                    progress_callback(source.name, idx, len(sources))
                result = self.download_file(source)
                results.append(result)
        else:
            # Parallel download
            with ThreadPoolExecutor(max_workers=max_parallel) as executor:
                future_to_source = {
                    executor.submit(self.download_file, source): source
                    for source in sources
                }
                
                for idx, future in enumerate(as_completed(future_to_source), 1):
                    source = future_to_source[future]
                    if progress_callback:
                        progress_callback(source.name, idx, len(sources))
                    
                    try:
                        result = future.result()
                        results.append(result)
                    except Exception as e:
                        results.append(DownloadResult(
                            source=source,
                            success=False,
                            error=str(e),
                        ))
        
        # Log summary
        success_count = sum(1 for r in results if r.success)
        total_bytes = sum(r.bytes_downloaded for r in results)
        self.logger.info(
            f"Batch download complete: {success_count}/{len(sources)} succeeded, "
            f"{total_bytes:,} bytes total"
        )
        
        return results
    
    def download_all(
        self,
        include_optional: bool = False,
    ) -> List[DownloadResult]:
        """Download all configured data sources.
        
        Args:
            include_optional: Include non-required sources
        
        Returns:
            List of DownloadResult for each source
        """
        if include_optional:
            sources = self.config.get_all_sources()
        else:
            sources = self.config.get_required_sources()
        
        return self.download_batch(sources)
    
    def get_local_path(self, source: DataSource) -> Path:
        """Get the local path for a data source."""
        return self.download_dir / source.filename
    
    def is_downloaded(self, source: DataSource) -> bool:
        """Check if a data source has been downloaded."""
        local_path = self.get_local_path(source)
        return local_path.exists() and local_path.stat().st_size > 0
    
    def cleanup(
        self,
        sources: Optional[List[DataSource]] = None,
        keep_archives: bool = False,
    ) -> None:
        """Clean up downloaded files.
        
        Args:
            sources: Specific sources to clean (default: all)
            keep_archives: Whether to keep ZIP/archive files
        """
        sources = sources or self.config.get_all_sources()
        
        for source in sources:
            local_path = self.get_local_path(source)
            if local_path.exists():
                if keep_archives and source.file_format.value in ['zip', 'tar', 'tar.gz']:
                    continue
                local_path.unlink()
                self.logger.debug(f"Removed {local_path}")
