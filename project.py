from __future__ import annotations
from dataclasses import dataclass, asdict, field
from typing import Dict, List, Optional, Any
import json
from datetime import datetime

@dataclass
class Territory:
    territory_name: str = ""
    rep_user_id: str = ""
    am_user_id: str = ""
    admin_unit_ids: List[str] = field(default_factory=list)

@dataclass
class Project:
    project_name: str = "Untitled"
    country: str = "Romania"
    level: str = "Judete+BucharestSectors"
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    territories: List[Territory] = field(default_factory=list)

    def touch(self) -> None:
        self.updated_at = datetime.utcnow().isoformat()

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)

    @staticmethod
    def from_json(s: str) -> "Project":
        raw = json.loads(s)
        p = Project(
            project_name=raw.get("project_name", "Untitled"),
            country=raw.get("country", "Romania"),
            level=raw.get("level", "Judete+BucharestSectors"),
            created_at=raw.get("created_at") or datetime.utcnow().isoformat(),
            updated_at=raw.get("updated_at") or datetime.utcnow().isoformat(),
            territories=[]
        )
        for t in raw.get("territories", []) or []:
            p.territories.append(Territory(
                territory_name=t.get("territory_name", ""),
                rep_user_id=t.get("rep_user_id", ""),
                am_user_id=t.get("am_user_id", ""),
                admin_unit_ids=list(t.get("admin_unit_ids", []) or [])
            ))
        return p
