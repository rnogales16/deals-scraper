#!/usr/bin/env python3
"""Resumen diario de salud (VPS compone + envía). Aislado, solo lectura.
Tira de journald (logs ya existentes) + config + el snapshot que el Mac empuja.
SALVAGUARDA: el envío a Telegram es lo último y lo más blindado — el mensaje
llega SIEMPRE, aunque la recolección falle entera ("⚠️ resumen con errores").
"""
import subprocess, re, json, os, sys, time
import urllib.request, urllib.parse
from datetime import datetime

APP = "/home/scraper/deals-scraper"
ENV = f"{APP}/.env"
CONFIG = f"{APP}/config.yaml"
MAC_STATS = "/home/ubuntu/mac_stats.json"
CHAT_ID = "201330690"   # chat personal, NO los canales


def sh(cmd, timeout=60):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout).stdout


def read_token():
    for line in open(ENV):
        if line.startswith("TELEGRAM_BOT_TOKEN="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError("token no encontrado en .env")


def parse_log_stats(log_text):
    """Extrae de un volcado de log: producing(set), volume(int), alerts(dict)."""
    crudas = re.findall(r"\[([\w]+)\] (\d+) ofertas crudas encontradas", log_text)
    producing = set(st for st, n in crudas if int(n) > 0)
    volume = sum(int(n) for _, n in crudas)
    alerts = {}
    for st, tier in re.findall(r"ALERTA tienda=([\w]+) tier=(\w+)", log_text):
        alerts.setdefault(st, {"ERRORES": 0, "CHOLLOS": 0})
        alerts[st][tier] = alerts[st].get(tier, 0) + 1
    return producing, volume, alerts


def fmt_alerts(alerts):
    if not alerts:
        return "0"
    tot_e = sum(a.get("ERRORES", 0) for a in alerts.values())
    tot_c = sum(a.get("CHOLLOS", 0) for a in alerts.values())
    by = ", ".join(f"{s} {sum(v.values())}" for s, v in sorted(alerts.items(), key=lambda x: -sum(x[1].values())))
    return f"{tot_e + tot_c} (ERRORES {tot_e}, CHOLLOS {tot_c}) → {by}"


def vps_section():
    import yaml
    cfg = yaml.safe_load(open(CONFIG))
    enabled = [s["name"] for s in cfg["stores"] if s.get("enabled")]
    log = sh('journalctl -u deals-scraper --since "24 hours ago" --no-pager -o cat')
    producing, volume, alerts = parse_log_stats(log)
    zero = [s for s in enabled if s not in producing]
    active = (sh("systemctl is-active deals-scraper") or "?").strip()
    restarts = (sh('journalctl -u deals-scraper --since "24 hours ago" --no-pager | grep -c "ciclo inicial"') or "0").strip()
    load = " ".join(open("/proc/loadavg").read().split()[:3])
    icon = "✅" if active == "active" else "🔴"
    return (
        f"🖥️ VPS (24/7)  {icon} {active} | reinicios 24h: {restarts} | load: {load}\n"
        f"  Scraping: {len(producing)}/{len(enabled)} tiendas con ofertas en 24h\n"
        f"  ⚠️ CERO en 24h: {', '.join(zero) if zero else '(ninguna)'}\n"
        f"  Volumen: {volume:,} crudas/24h\n"
        f"  Alertas TOP: {fmt_alerts(alerts)}"
    )


def mac_section():
    if not os.path.exists(MAC_STATS):
        return "💻 Mac: ⚠️ no responde (sin datos recibidos nunca)"
    age_h = (time.time() - os.path.getmtime(MAC_STATS)) / 3600
    if age_h > 7:   # empuja cada 6h → >7h = de verdad caído
        return f"💻 Mac: ⚠️ no responde (último snapshot hace {age_h:.0f}h, ¿apagado?)"
    d = json.load(open(MAC_STATS))
    enabled = d.get("configured", [])
    producing = d.get("producing", [])
    zero = [s for s in enabled if s not in producing]
    alerts = d.get("alerts", {})
    icon = "✅" if d.get("alive") else "🔴"
    alive = "vivo" if d.get("alive") else "CAÍDO"
    return (
        f"💻 Mac ({len(enabled)} residenciales)  {icon} {alive} | load: {d.get('load','?')}\n"
        f"  Scraping: {len(producing)}/{len(enabled)} con ofertas\n"
        f"  ⚠️ CERO en 24h: {', '.join(zero) if zero else '(ninguna)'}\n"
        f"  Volumen: {d.get('volume',0):,} crudas/24h\n"
        f"  Alertas TOP: {fmt_alerts(alerts)}  (snapshot hace {age_h:.0f}h)"
    )


def build_message():
    hdr = f"📊 Resumen diario — {datetime.now().strftime('%d/%m %H:%M')}"
    parts = [hdr]
    try:
        parts.append(vps_section())
    except Exception as e:
        parts.append(f"🖥️ VPS: ⚠️ error recolectando ({type(e).__name__})")
    try:
        parts.append(mac_section())
    except Exception as e:
        parts.append(f"💻 Mac: ⚠️ error recolectando ({type(e).__name__})")
    return "\n\n".join(parts)


def send(token, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": text,
                                   "disable_web_page_preview": "true"}).encode()
    with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=25) as r:
        return r.status


def main():
    # 1) construir mensaje (nunca debe impedir el envío)
    try:
        msg = build_message()
    except Exception as e:
        msg = f"⚠️ Resumen diario: fallo TOTAL al generar ({type(e).__name__}: {e}). Revisar VPS."
    if not msg or not msg.strip():
        msg = "⚠️ Resumen diario vacío — revisar VPS."
    # 2) ENVÍO blindado (lo último, con reintento + fallback curl)
    sent = False
    try:
        token = read_token()
        for _ in range(2):
            try:
                send(token, msg)
                sent = True
                break
            except Exception:
                time.sleep(3)
    except Exception:
        pass
    if not sent:
        # último recurso: curl crudo con mensaje mínimo
        try:
            os.system(
                'curl -s "https://api.telegram.org/bot$(grep TELEGRAM_BOT_TOKEN %s | cut -d= -f2)/sendMessage" '
                '--data-urlencode chat_id=%s '
                '--data-urlencode text="⚠️ Resumen diario falló al enviarse por la vía normal — revisar VPS" >/dev/null'
                % (ENV, CHAT_ID)
            )
        except Exception:
            print("FALLO TOTAL DE ENVÍO", file=sys.stderr)


if __name__ == "__main__":
    main()
