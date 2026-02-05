from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, Tuple, Optional
import pandas as pd

REQUIRED_FIELDFORCE_COLS = [
    "Name", "Surname", "User_Id", "Lat", "Long", "BusinessLine", "JobTitle"
]
# Province/Region optional but recommended
OPTIONAL_FIELDFORCE_COLS = [
    "Address", "Province", "Region", "Email", "ManagerEmail", "Key"
]

@dataclass
class LoadedData:
    fieldforce: pd.DataFrame
    weights: pd.DataFrame
    admin_geojson: dict

def read_csv_any(file) -> pd.DataFrame:
    # robust read (comma/semicolon)
    try:
        return pd.read_csv(file)
    except Exception:
        return pd.read_csv(file, sep=";")

def validate_fieldforce(df: pd.DataFrame) -> Tuple[pd.DataFrame, Dict[str, str]]:
    issues: Dict[str, str] = {}
    cols = set(df.columns)
    for c in REQUIRED_FIELDFORCE_COLS:
        if c not in cols:
            issues[c] = "missing"
    # ensure columns exist (create empty if missing optional)
    for c in OPTIONAL_FIELDFORCE_COLS:
        if c not in cols:
            df[c] = ""
    # soft cast
    if "Lat" in df.columns:
        df["Lat"] = pd.to_numeric(df["Lat"], errors="coerce")
    if "Long" in df.columns:
        df["Long"] = pd.to_numeric(df["Long"], errors="coerce")
    return df, issues

def validate_weights(df: pd.DataFrame) -> Tuple[pd.DataFrame, Dict[str, str]]:
    issues: Dict[str, str] = {}
    for c in ["territory_id", "name"]:
        if c not in df.columns:
            issues[c] = "missing"
    # allow flexible column naming for weight
    if "weight" not in df.columns:
        # try detect numeric column
        numeric_cols = [c for c in df.columns if c.lower() not in {"territory_id", "name"}]
        if numeric_cols:
            df["weight"] = pd.to_numeric(df[numeric_cols[0]], errors="coerce")
        else:
            df["weight"] = 0.0
    else:
        df["weight"] = pd.to_numeric(df["weight"], errors="coerce")
    df["weight"] = df["weight"].fillna(0.0)
    return df, issues
