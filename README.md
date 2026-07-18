# fanhist

A small, self-hosted iDRAC fan controller — inspired by [Hush](https://github.com/natankeddem/hush),
but built simpler and with built-in history.

## ⚠️ Warning

This is a v1, built and tested against one specific environment (Dell R720, iDRAC 7). A bug
or a wrong setting could cause the fans to not ramp up (enough) under heat. Use at your own
risk, keep an eye on whether the curve does what you expect, and consider an external
temperature alert (e.g. in Home Assistant or Grafana) as an extra safety net.

## What it does

- Reads CPU/Inlet temperature directly via `ipmitool` (local IPMI, no Redfish/TLS needed —
  that turned out to be unreliably slow on older iDRAC generations like iDRAC 7)
- Optionally reads a disk temperature over SSH (e.g. TrueNAS `drivetemp`/hwmon)
- Computes the fan percentage via a configurable curve (temperature → %)
- Sets the fans via IPMI raw commands (`0x30 0x30 ...`)
- Logs every reading to SQLite and shows a graph + curve editor on a small dashboard

## Quick start

1. Make sure IPMI over LAN is enabled on your iDRAC (iDRAC Settings → Network → IPMI Settings).
2. Start:

   ```bash
   docker compose up -d --build
   ```

3. Open `http://<host>:8181` for the dashboard.
4. Scroll to "Settings" and fill in your iDRAC host, user, password, and sensor name,
   click "Test connection" to check it works, then click "Save settings".
5. (Optional, for disk temperature) In the same panel, click "Generate key" (or
   "Regenerate key"). The public key appears immediately — paste it into `authorized_keys`
   on your NAS/host (or via the TrueNAS UI under Credentials → Users → SSH Public Key).
   Then fill in the SSH host/user, click "Test disk connection", and save.

All settings (including the iDRAC credentials and the SSH key) are stored in the SQLite
database under `./data` — so they survive a container restart or rebuild.

## Settings

Everything is configurable from the dashboard ("Settings" panel), no environment variables
or restarts needed:

- **iDRAC**: host/IP, user, password, sensor name (`ipmitool sensor list` for options)
- **Disk temperature (optional)**: SSH host, SSH user, how multiple disks are combined
  (average/max/min), and the command that reads out the temperatures. The SSH key is
  generated inside the container via the "Generate key" button — nothing needs to be
  copied into the container by hand.
- **General**: measurement interval, IPMI timeout, how long history is kept

Only `DB_PATH` (where the SQLite database lives) is still an environment variable, in case
you want to put it somewhere other than the default `/data/fanhist.db`.

## Adjusting the curve

Open the dashboard, scroll to "Fan curve", adjust or add points, and click "Save". The
curve is linearly interpolated between points; below the lowest point the lowest
percentage applies, above the highest point the highest percentage applies.

## Known limitations (v1)

- No authentication on the dashboard — don't expose it to the public internet.
- One iDRAC per container; run multiple instances for multiple hosts.
- `DISK_TEMP_CMD` assumes Linux-hwmon-like output; adjust for other OSes.

## License

MIT — see [LICENSE](LICENSE).
