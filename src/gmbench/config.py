"""League configuration and secrets loading."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class TeamSpec:
    id: str
    lab: str
    model: str | None  # None = deterministic control bot
    providers: tuple[str, ...] = ()
    cache: str = "auto"  # auto | top | block
    omit: tuple[str, ...] = ()  # request params this host rejects
    display: str = ""  # human-readable model name, e.g. "Claude Opus 5.5"

    @property
    def is_bot(self) -> bool:
        return self.model is None


@dataclass(frozen=True)
class LeagueConfig:
    raw: dict
    teams: tuple[TeamSpec, ...] = field(default_factory=tuple)

    @property
    def rules(self) -> dict:
        return self.raw["rules"]

    @property
    def harness(self) -> dict:
        return self.raw["harness"]

    def team(self, team_id: str) -> TeamSpec:
        for t in self.teams:
            if t.id == team_id:
                return t
        raise KeyError(team_id)


def load_config(path: Path | None = None) -> LeagueConfig:
    path = path or ROOT / "league.yaml"
    raw = yaml.safe_load(path.read_text())
    teams = tuple(
        TeamSpec(
            id=t["id"],
            lab=t["lab"],
            model=t.get("model"),
            providers=tuple(t.get("providers") or ()),
            cache=t.get("cache", "auto"),
            omit=tuple(t.get("omit") or ()),
            display=t.get("display") or t.get("model") or t["id"],
        )
        for t in raw["teams"]
    )
    return LeagueConfig(raw=raw, teams=teams)


def load_secrets() -> None:
    """Load API keys from the project's gitignored .env (never logged). The project's own keys win over anything
    exported in the shell, so a key from another project can't silently take over; CI has no .env and uses secrets."""
    load_dotenv(ROOT / ".env", override=True)


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is not set (expected in {ROOT / '.env'})")
    return value
