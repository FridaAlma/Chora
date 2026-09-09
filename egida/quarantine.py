"""
Quarantine management for HSD files.

When a file is identified as containing HSD with a score
above the threshold, it is copied to quarantine with a report
of matches and is NOT registered in the graph.

The report includes total score, threshold, and severity detail.
Egida — 4th layer of Oracle (cross-layer HSD guardrail).
"""

import json
import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import EGIDA_QUARANTINE_DIR, EGIDA_QUARANTINE_THRESHOLD
from .filters import HSDMatch

logger = logging.getLogger(__name__)


class Quarantine:
    """
    Isolates HSD files in a separate directory, outside the graph.
    Only files with score >= threshold are actually isolated.
    """

    def __init__(self, base_dir: str | Path | None = None):
        if base_dir:
            self.base_dir = Path(base_dir)
        else:
            # Rende assoluto: se il path è relativo, risolve rispetto alla radice di Egida
            base = Path(EGIDA_QUARANTINE_DIR)
            if not base.is_absolute():
                base = Path(__file__).resolve().parent.parent / EGIDA_QUARANTINE_DIR
            self.base_dir = base

    def isolate(
        self,
        match: HSDMatch,
        source_path: Optional[str | Path] = None,
    ) -> Optional[Path]:
        """
        Copies an infected file to quarantine and generates a JSON report.

        If the score is below threshold, the file is NOT isolated
        (returns None).

        Args:
            match: HSDMatch result of the analyzed file
            source_path: original path (default: match.file_path)

        Returns:
            Path to quarantine directory, or None if below threshold.
        """
        if not match.is_infected:
            logger.debug(
                "File below threshold (%d < %d): %s",
                match.score, EGIDA_QUARANTINE_THRESHOLD, match.file_path,
            )
            return None

        src = Path(source_path or match.file_path)
        if not src.exists():
            logger.error("File not found: %s", src)
            raise FileNotFoundError(f"File not found: {src}")

        # Create dated quarantine directory
        date_prefix = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest_dir = self.base_dir / date_prefix
        dest_dir.mkdir(parents=True, exist_ok=True)

        # Copy the file
        dest_file = dest_dir / src.name
        try:
            shutil.copy2(src, dest_file)
            logger.info("Copied to quarantine: %s → %s", src, dest_file)
        except Exception as e:
            logger.warning("Impossibile copiare in quarantena %s: %s — file saltato, continua", src, e)
            return None

        # Generate enriched JSON report
        report = {
            "original_path": str(src.absolute()),
            "quarantine_path": str(dest_file.absolute()),
            "timestamp": datetime.now().isoformat(),
            "match_count": len(match.matches),
            "score": match.score,
            "threshold": EGIDA_QUARANTINE_THRESHOLD,
            "matches": match.matches,
        }
        report_path = dest_dir / "report.json"
        report_path.write_text(
            json.dumps(report, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.info("Quarantine report: %s", report_path)

        return dest_dir

    def list_quarantine(self) -> list[dict]:
        """Lists all files in quarantine with their reports."""
        entries = []
        if not self.base_dir.exists():
            return entries

        for entry in sorted(self.base_dir.iterdir()):
            if entry.is_dir():
                report_path = entry / "report.json"
                if report_path.exists():
                    try:
                        report = json.loads(report_path.read_text("utf-8"))
                        entries.append(report)
                    except Exception:
                        entries.append({
                            "quarantine_path": str(entry),
                            "timestamp": entry.name,
                            "error": "report not readable",
                        })
                else:
                    entries.append({
                        "quarantine_path": str(entry),
                        "timestamp": entry.name,
                        "match_count": 0,
                        "matches": [],
                    })
        return entries

    def clear(self) -> int:
        """Empties quarantine. Returns the number of removed entries."""
        count = 0
        if self.base_dir.exists():
            for entry in self.base_dir.iterdir():
                if entry.is_dir():
                    shutil.rmtree(entry)
                    count += 1
            logger.info("Quarantine emptied: %d entries removed", count)
        return count
