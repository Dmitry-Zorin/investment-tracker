from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class WorkspaceError(RuntimeError):
    pass


@dataclass(frozen=True)
class WorkspacePaths:
    root: Path

    @classmethod
    def from_path(cls, path: str | Path) -> "WorkspacePaths":
        candidate = Path(path).expanduser()
        if not candidate.exists():
            raise WorkspaceError(f"Workspace does not exist: {candidate}")
        if not candidate.is_dir():
            raise WorkspaceError(f"Workspace is not a directory: {candidate}")
        return cls(candidate.resolve())

    @property
    def brokerage_current(self) -> Path:
        return self.root / "brokerage-current.json"

    @property
    def brokerage_ledger(self) -> Path:
        return self.root / "brokerage-ledger.jsonl"

    @property
    def market_manifest(self) -> Path:
        return self.root / "data/market/manifest.json"

    @property
    def market_data(self) -> Path:
        return self.root / "data/market"

    def market_csv(self, secid: str) -> Path:
        return self.market_data / f"{secid}.csv"

    @property
    def reports(self) -> Path:
        return self.root / "reports"

    def _ensure_inside(self, path: Path) -> Path:
        resolved = path.resolve(strict=False)
        if not resolved.is_relative_to(self.root):
            raise WorkspaceError(f"Path escapes workspace: {path}")
        return path

    def require_input(self, path: Path) -> Path:
        self._ensure_inside(path)
        if not path.exists():
            raise WorkspaceError(f"Required workspace input does not exist: {path}")
        return path

    def output_path(self, path: Path) -> Path:
        return self._ensure_inside(path)

    def validate_market_inputs(self) -> None:
        self.require_input(self.market_manifest)
        self.require_input(self.market_data)
        self.output_path(self.reports)

    def validate_portfolio_inputs(self) -> None:
        self.validate_market_inputs()
        self.require_input(self.brokerage_current)
        self.require_input(self.brokerage_ledger)
