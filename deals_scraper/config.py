"""Carga y validación de config.yaml."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

from .models import StoreConfig

_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """Carga config.yaml y devuelve el dict validado."""
    config_path = Path(path) if path else _DEFAULT_CONFIG_PATH
    if not config_path.exists():
        raise FileNotFoundError(f"No se encontró el archivo de configuración: {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        cfg: dict[str, Any] = yaml.safe_load(f)

    _resolve_env_vars(cfg)
    _validate(cfg)
    return cfg


_ENV_VAR_RE = re.compile(r"^\$\{(\w+)\}$")


def _resolve_env_vars(obj: Any) -> Any:
    """Resuelve valores ``${VAR}`` en el config usando variables de entorno."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(value, str):
                m = _ENV_VAR_RE.match(value)
                if m:
                    env_val = os.environ.get(m.group(1))
                    if env_val:
                        obj[key] = env_val
            elif isinstance(value, (dict, list)):
                _resolve_env_vars(value)
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            if isinstance(item, str):
                m = _ENV_VAR_RE.match(item)
                if m:
                    env_val = os.environ.get(m.group(1))
                    if env_val:
                        obj[i] = env_val
            elif isinstance(item, (dict, list)):
                _resolve_env_vars(item)


def _validate(cfg: dict[str, Any]) -> None:
    """Validaciones básicas de la configuración."""
    tg = cfg.get("telegram", {})
    if not tg.get("bot_token") or tg["bot_token"] == "TU_BOT_TOKEN_AQUI":
        raise ValueError("Configura telegram.bot_token en config.yaml")
    if not tg.get("chat_id") or tg["chat_id"] == "TU_CHAT_ID_AQUI":
        raise ValueError("Configura telegram.chat_id en config.yaml")

    if not cfg.get("stores"):
        raise ValueError("Debe haber al menos una tienda en stores[]")

    for store in cfg["stores"]:
        if "name" not in store:
            raise ValueError("Cada tienda necesita un campo 'name'")
        if not store.get("scrape_urls"):
            raise ValueError(f"Tienda '{store['name']}' necesita al menos una URL en scrape_urls")


def get_store_configs(cfg: dict[str, Any]) -> list[StoreConfig]:
    """Extrae las configuraciones de tiendas habilitadas."""
    return [
        StoreConfig.from_dict(s)
        for s in cfg.get("stores", [])
        if s.get("enabled", True)
    ]


def get_filters(cfg: dict[str, Any]) -> dict[str, Any]:
    """Devuelve la configuración de filtros con valores por defecto."""
    defaults = {
        "min_discount": 15,
        "price_min": 0.0,
        "price_max": float("inf"),
        "keywords": [],
        "categories": [],
    }
    filters = cfg.get("filters", {})
    return {**defaults, **filters}


def get_anti_ban(cfg: dict[str, Any]) -> dict[str, Any]:
    """Devuelve la configuración anti-ban con valores por defecto."""
    defaults = {
        "delay_min": 0.5,
        "delay_max": 1.5,
        "max_requests_per_minute": 15,
        "proxy_url": None,
    }
    anti_ban = cfg.get("anti_ban", {})
    return {**defaults, **anti_ban}


def get_speed(cfg: dict[str, Any]) -> dict[str, Any]:
    """Devuelve la configuración de velocidad con valores por defecto."""
    defaults = {
        "mode": "fast",
        "max_concurrent_stores": 4,
        "max_concurrent_urls_per_store": 3,
        "price_error_threshold": 50,
    }
    speed = cfg.get("speed", {})
    return {**defaults, **speed}
