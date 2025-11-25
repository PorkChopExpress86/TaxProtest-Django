"""
ETL Pipeline Logging Module

Provides structured logging infrastructure for ETL operations with support
for file rotation, metrics collection, and context tracking.
"""

import logging
import logging.handlers
import json
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Generator
from dataclasses import dataclass, field


@dataclass
class ETLMetrics:
    """Collects metrics for ETL operations."""
    start_time: float = field(default_factory=time.time)
    end_time: Optional[float] = None
    records_processed: int = 0
    records_success: int = 0
    records_failed: int = 0
    records_skipped: int = 0
    bytes_downloaded: int = 0
    bytes_extracted: int = 0
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    
    @property
    def duration(self) -> float:
        """Get duration in seconds."""
        end = self.end_time or time.time()
        return end - self.start_time
    
    @property
    def records_per_second(self) -> float:
        """Get processing rate."""
        if self.duration > 0:
            return self.records_processed / self.duration
        return 0.0
    
    @property
    def success_rate(self) -> float:
        """Get success rate as percentage."""
        if self.records_processed > 0:
            return (self.records_success / self.records_processed) * 100
        return 0.0
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for logging/serialization."""
        return {
            'start_time': datetime.fromtimestamp(self.start_time).isoformat(),
            'end_time': datetime.fromtimestamp(self.end_time).isoformat() if self.end_time else None,
            'duration_seconds': round(self.duration, 2),
            'records_processed': self.records_processed,
            'records_success': self.records_success,
            'records_failed': self.records_failed,
            'records_skipped': self.records_skipped,
            'records_per_second': round(self.records_per_second, 2),
            'success_rate': round(self.success_rate, 2),
            'bytes_downloaded': self.bytes_downloaded,
            'bytes_extracted': self.bytes_extracted,
            'error_count': len(self.errors),
            'warning_count': len(self.warnings),
        }
    
    def add_error(self, error: str, context: Optional[Dict] = None) -> None:
        """Add an error to the metrics."""
        self.errors.append({
            'message': error,
            'timestamp': datetime.now().isoformat(),
            'context': context or {},
        })
    
    def add_warning(self, warning: str, context: Optional[Dict] = None) -> None:
        """Add a warning to the metrics."""
        self.warnings.append({
            'message': warning,
            'timestamp': datetime.now().isoformat(),
            'context': context or {},
        })
    
    def finish(self) -> None:
        """Mark the operation as complete."""
        self.end_time = time.time()


class StructuredLogFormatter(logging.Formatter):
    """JSON-formatted log output for structured logging."""
    
    def format(self, record: logging.LogRecord) -> str:
        log_data = {
            'timestamp': datetime.fromtimestamp(record.created).isoformat(),
            'level': record.levelname,
            'logger': record.name,
            'message': record.getMessage(),
            'module': record.module,
            'function': record.funcName,
            'line': record.lineno,
        }
        
        # Add extra fields if present
        if hasattr(record, 'stage'):
            log_data['stage'] = record.stage
        if hasattr(record, 'source'):
            log_data['source'] = record.source
        if hasattr(record, 'progress'):
            log_data['progress'] = record.progress
        if hasattr(record, 'metrics'):
            log_data['metrics'] = record.metrics
        
        # Add exception info if present
        if record.exc_info:
            log_data['exception'] = self.formatException(record.exc_info)
        
        return json.dumps(log_data)


class ETLLogger:
    """Centralized logger for ETL operations with metrics tracking."""
    
    def __init__(
        self,
        name: str = 'etl_pipeline',
        log_level: str = 'INFO',
        log_dir: Optional[Path] = None,
        log_to_file: bool = True,
        structured: bool = True,
        max_bytes: int = 10 * 1024 * 1024,
        backup_count: int = 5,
    ):
        self.name = name
        self.log_level = getattr(logging, log_level.upper(), logging.INFO)
        self.log_dir = log_dir
        self.log_to_file = log_to_file
        self.structured = structured
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        
        self.logger = logging.getLogger(name)
        self.logger.setLevel(self.log_level)
        
        # Avoid duplicate handlers
        if not self.logger.handlers:
            self._setup_handlers()
        
        self.metrics: Dict[str, ETLMetrics] = {}
        self._current_stage: Optional[str] = None
    
    def _setup_handlers(self) -> None:
        """Set up logging handlers."""
        # Console handler
        console_handler = logging.StreamHandler()
        console_handler.setLevel(self.log_level)
        
        if self.structured:
            console_handler.setFormatter(StructuredLogFormatter())
        else:
            console_handler.setFormatter(logging.Formatter(
                '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
            ))
        
        self.logger.addHandler(console_handler)
        
        # File handler
        if self.log_to_file and self.log_dir:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            log_file = self.log_dir / f'{self.name}.log'
            
            file_handler = logging.handlers.RotatingFileHandler(
                log_file,
                maxBytes=self.max_bytes,
                backupCount=self.backup_count,
            )
            file_handler.setLevel(self.log_level)
            
            if self.structured:
                file_handler.setFormatter(StructuredLogFormatter())
            else:
                file_handler.setFormatter(logging.Formatter(
                    '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
                ))
            
            self.logger.addHandler(file_handler)
    
    def debug(self, msg: str, **kwargs: Any) -> None:
        self.logger.debug(msg, extra=kwargs)
    
    def info(self, msg: str, **kwargs: Any) -> None:
        self.logger.info(msg, extra=kwargs)
    
    def warning(self, msg: str, **kwargs: Any) -> None:
        self.logger.warning(msg, extra=kwargs)
        if self._current_stage and self._current_stage in self.metrics:
            self.metrics[self._current_stage].add_warning(msg, kwargs)
    
    def error(self, msg: str, **kwargs: Any) -> None:
        self.logger.error(msg, extra=kwargs)
        if self._current_stage and self._current_stage in self.metrics:
            self.metrics[self._current_stage].add_error(msg, kwargs)
    
    def exception(self, msg: str, **kwargs: Any) -> None:
        self.logger.exception(msg, extra=kwargs)
        if self._current_stage and self._current_stage in self.metrics:
            self.metrics[self._current_stage].add_error(msg, kwargs)
    
    def start_stage(self, stage_name: str) -> ETLMetrics:
        """Start tracking a new ETL stage."""
        self._current_stage = stage_name
        self.metrics[stage_name] = ETLMetrics()
        self.info(f"Starting stage: {stage_name}", stage=stage_name)
        return self.metrics[stage_name]
    
    def finish_stage(self, stage_name: str) -> ETLMetrics:
        """Finish tracking an ETL stage."""
        if stage_name in self.metrics:
            self.metrics[stage_name].finish()
            metrics = self.metrics[stage_name]
            self.info(
                f"Finished stage: {stage_name}",
                stage=stage_name,
                metrics=metrics.to_dict(),
            )
            if self._current_stage == stage_name:
                self._current_stage = None
            return metrics
        return ETLMetrics()
    
    def get_stage_metrics(self, stage_name: str) -> Optional[ETLMetrics]:
        """Get metrics for a specific stage."""
        return self.metrics.get(stage_name)
    
    def get_all_metrics(self) -> Dict[str, Dict[str, Any]]:
        """Get all metrics as dictionary."""
        return {name: m.to_dict() for name, m in self.metrics.items()}
    
    @contextmanager
    def stage(self, stage_name: str) -> Generator[ETLMetrics, None, None]:
        """Context manager for tracking a stage."""
        metrics = self.start_stage(stage_name)
        try:
            yield metrics
        except Exception as e:
            metrics.add_error(str(e))
            raise
        finally:
            self.finish_stage(stage_name)
    
    def log_progress(
        self,
        current: int,
        total: int,
        stage: Optional[str] = None,
        interval: int = 1000,
    ) -> None:
        """Log progress at intervals."""
        if current % interval == 0 or current == total:
            pct = (current / total * 100) if total > 0 else 0
            self.info(
                f"Progress: {current:,}/{total:,} ({pct:.1f}%)",
                stage=stage or self._current_stage,
                progress={'current': current, 'total': total, 'percentage': pct},
            )
