# ops/ — scripts operativos de monitorización

Scripts de apoyo, **no parte del scraper**. Solo lectura + envío de un mensaje al día.
Aislados del daemon: no tocan el scraping ni recargan el box.

## `summary_vps.py` — Resumen diario de salud (corre en el **VPS**)

Compone un resumen de salud de **ambas máquinas** y lo envía por Telegram al **chat personal**
(`201330690`, NO a los canales de errores/chollos). Tira de datos que ya existen:

- **VPS**: `journalctl` (`N ofertas crudas encontradas` y `ALERTA tienda=… tier=…`), `config.yaml`,
  `systemctl is-active`, reinicios 24h (cuenta de `ciclo inicial`), `/proc/loadavg`.
- **Mac**: lee `/home/ubuntu/mac_stats.json` (snapshot que el Mac empuja). Si falta o tiene
  >7h → marca **"Mac no responde"**.

**Salvaguarda**: el envío es lo último y va blindado (reintento + fallback `curl`). El mensaje
llega SIEMPRE; si la recolección falla entera, manda "⚠️ resumen con errores, revisar".

- **Despliegue (VPS)**: `/home/scraper/deals-scraper/ops/summary_vps.py`
- **Disparo**: systemd `deals-summary.timer` → `deals-summary.service`
  (`OnCalendar=*-*-* 11:00:00 Europe/Madrid`, `Persistent=true`, corre como **root** para leer
  journald + `.env`).

## `mac_summary_push.py` — Pusher de stats del Mac (corre en el **Mac**)

Lee el log del daemon del Mac (`logs/daemon.err.log`, últimas 24h), calcula stats de las **6
tiendas residenciales** (producen / cero / volumen / alertas TOP / daemon vivo / load) y empuja
un JSON pequeño al VPS por `scp` → `/home/ubuntu/mac_stats.json`.

- **Despliegue (Mac)**: `/Users/rnogales/Desktop/deals-scraper/ops/mac_summary_push.py`
- **Disparo**: launchd `~/Library/LaunchAgents/com.rnogales.deals-summary-push.plist`,
  cada 6h (04:45 / 10:45 / 16:45 / 22:45 hora local) + `RunAtLoad`. El push de 10:45 alimenta el
  envío de las 11:00.

## Recrear una máquina

- **VPS**: copiar `ops/summary_vps.py`, crear `deals-summary.service` (oneshot, `ExecStart=`
  `/home/scraper/deals-scraper/venv/bin/python /home/scraper/deals-scraper/ops/summary_vps.py`)
  + `deals-summary.timer` (ver arriba), `systemctl enable --now deals-summary.timer`.
- **Mac**: copiar `ops/mac_summary_push.py`, crear el plist launchd (ProgramArguments = venv python
  + ese script, `StartCalendarInterval` cada 6h), `launchctl load`.
- El log de alertas (`ALERTA tienda=… tier=…`) que alimenta el punto 3 lo emite
  `deals_scraper/telegram_bot.py` (`_send_deal` y el envío cross-store).

> Nota: rutas hardcodeadas a las ubicaciones estándar (VPS `/home/scraper/deals-scraper`,
> Mac `/Users/rnogales/Desktop/deals-scraper`). `config.yaml` (VPS) y `config.mac.yaml` (Mac) son
> machine-specific y no están en git.
