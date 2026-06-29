#!/usr/bin/env python3
"""Pusher de stats del Mac → VPS (para el resumen diario). Aislado, solo lectura.
Lee daemon.err.log (24h), calcula stats de las 6 tiendas residenciales y empuja
un JSON pequeño al VPS por scp. Se ejecuta cada 6h vía launchd. Blindado: si algo
falla, no rompe nada (el resumen del VPS marcará 'Mac no responde').
"""
import re, json, subprocess, os
from datetime import datetime, timedelta

APP = "/Users/rnogales/Desktop/deals-scraper"
LOG = f"{APP}/logs/daemon.err.log"
CONFIG = f"{APP}/config.mac.yaml"
KEY = "/Users/rnogales/.ssh/id_ed25519"
VPS = "ubuntu@51.91.252.157"
DEST = "/home/ubuntu/mac_stats.json"
OUT = "/tmp/mac_stats.json"

_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
_CRUDAS = re.compile(r"\[(\w+)\] (\d+) ofertas crudas encontradas")
_ALERTA = re.compile(r"ALERTA tienda=(\w+) tier=(\w+)")


def main():
    cutoff = (datetime.now() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    enabled = []
    try:
        import yaml
        cfg = yaml.safe_load(open(CONFIG))
        enabled = [s["name"] for s in cfg["stores"] if s.get("enabled")]
    except Exception:
        pass

    producing, volume, alerts = set(), 0, {}
    try:
        with open(LOG, errors="ignore") as f:
            for line in f:
                m = _TS.match(line)
                if not m or m.group(1) < cutoff:
                    continue
                c = _CRUDAS.search(line)
                if c:
                    if int(c.group(2)) > 0:
                        producing.add(c.group(1))
                    volume += int(c.group(2))
                    continue
                a = _ALERTA.search(line)
                if a:
                    alerts.setdefault(a.group(1), {"ERRORES": 0, "CHOLLOS": 0})
                    alerts[a.group(1)][a.group(2)] = alerts[a.group(1)].get(a.group(2), 0) + 1
    except Exception:
        pass

    alive = False
    try:
        alive = bool(subprocess.run("pgrep -f 'main.py.*config.mac'", shell=True,
                                    capture_output=True, text=True).stdout.strip())
    except Exception:
        pass
    load = "?"
    try:
        up = subprocess.run("uptime", shell=True, capture_output=True, text=True).stdout
        load = up.split("load averages:")[-1].strip().split()[0]
    except Exception:
        pass

    data = {"ts": datetime.now().isoformat(), "alive": alive, "load": load,
            "configured": enabled, "producing": sorted(producing),
            "volume": volume, "alerts": alerts}
    try:
        json.dump(data, open(OUT, "w"))
        subprocess.run(f"scp -i {KEY} -P 52242 -o ConnectTimeout=15 {OUT} {VPS}:{DEST}",
                       shell=True, timeout=45)
    except Exception:
        pass


if __name__ == "__main__":
    main()
