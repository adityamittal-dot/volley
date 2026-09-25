"""Loads config/profile.yaml and config/settings.yaml, with VOLLEY_* env overrides."""
import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
PROMPTS = ROOT / "prompts"


def _load(name):
    return yaml.safe_load((ROOT / "config" / name).read_text())


def settings():
    s = _load("settings.yaml")
    for key, value in list(s.items()):
        env = os.environ.get(f"VOLLEY_{key.upper()}")
        if env is None or env == "" or isinstance(value, (dict, list)):
            continue
        s[key] = type(value)(env) if not isinstance(value, bool) else env.lower() in ("1", "true", "yes")
    return s


def profile():
    return _load("profile.yaml")


def prompt(name):
    return (PROMPTS / name).read_text()
