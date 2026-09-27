"""GM-Bench data layer: frozen league snapshots built from NHL and ESPN sources.

- models:      the Snapshot contract (players keyed by NHL player ID)
- http:        shared JSON fetcher with retries; returns payload sha256s
- nhl, espn:   source fetchers + pure parsers
- projections: house baseline projection (pure)
- snapshot:    build_snapshot / save_snapshot / load_snapshot, CLI entry point
"""
from gmbench.data.models import POSITION_GROUP, Injury, NewsItem, Player, Projection, SeasonLine, Snapshot

__all__ = ["POSITION_GROUP", "Injury", "NewsItem", "Player", "Projection", "SeasonLine", "Snapshot"]
