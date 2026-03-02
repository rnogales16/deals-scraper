"""Generación de gráficos de historial de precios."""

from __future__ import annotations

import io
import logging
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


def generate_price_chart(title: str, price_history: list[dict[str, Any]]) -> bytes | None:
    """Genera un gráfico PNG del historial de precios.

    Args:
        title: Título del producto.
        price_history: Lista de dicts con {price, detected_at}.

    Returns:
        Bytes del PNG o None si no hay datos suficientes.
    """
    if not price_history or len(price_history) < 2:
        return None

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
    except ImportError:
        logger.warning("matplotlib no instalado, gráficos desactivados")
        return None

    dates = []
    prices = []
    for entry in price_history:
        try:
            dt = datetime.fromisoformat(entry["detected_at"])
            dates.append(dt)
            prices.append(entry["price"])
        except (ValueError, KeyError):
            continue

    if len(dates) < 2:
        return None

    fig, ax = plt.subplots(figsize=(10, 5))

    # Línea de precio
    ax.plot(dates, prices, color="#2196F3", linewidth=2, marker="o", markersize=4)
    ax.fill_between(dates, prices, alpha=0.1, color="#2196F3")

    # Anotaciones: min, max, actual
    min_price = min(prices)
    max_price = max(prices)
    current_price = prices[-1]
    min_idx = prices.index(min_price)
    max_idx = prices.index(max_price)

    ax.annotate(
        f"Min: {min_price:.2f}\u20ac",
        xy=(dates[min_idx], min_price),
        xytext=(0, -20), textcoords="offset points",
        fontsize=9, color="green", fontweight="bold",
        ha="center",
    )
    ax.annotate(
        f"Max: {max_price:.2f}\u20ac",
        xy=(dates[max_idx], max_price),
        xytext=(0, 15), textcoords="offset points",
        fontsize=9, color="red", fontweight="bold",
        ha="center",
    )
    ax.annotate(
        f"Actual: {current_price:.2f}\u20ac",
        xy=(dates[-1], current_price),
        xytext=(10, 0), textcoords="offset points",
        fontsize=9, color="#2196F3", fontweight="bold",
        ha="left",
    )

    # Formato
    ax.set_title(title[:80], fontsize=12, fontweight="bold", pad=15)
    ax.set_ylabel("Precio (\u20ac)")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m"))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    fig.autofmt_xdate(rotation=45)
    ax.grid(True, alpha=0.3)
    ax.set_ylim(bottom=min_price * 0.9, top=max_price * 1.1)

    plt.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.read()
