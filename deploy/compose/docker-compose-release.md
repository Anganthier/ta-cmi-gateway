# docker-compose-Binary-Pin (S5-Raspi, Q-115)

Statisches `docker-compose` v2.x-Binary für den Pi (<pi-host>, Alpine aarch64,
Docker-Engine ohne compose-Plugin). Das Binary gehört **NUR in den Pi-Tree**
`/var/lib/docker/cmi/bin/docker-compose` (umask 077, `chmod 755`, sda1/USB),
**niemals ins Repo** (≈74 MB) und **nicht** in ein Host-PATH-Verzeichnis.

## Pin (Version/URL/SHA256)

| Feld | Wert |
|---|---|
| Version | `v2.40.3` (neueste stabile v2.x; das Release-Projekt ist seit v3/v5 weitergezogen, v2.x liegt unter `Releases/Archived`) |
| Asset (linux-aarch64) | `docker-compose-linux-aarch64` |
| Download-URL | `https://github.com/docker/compose/releases/download/v2.40.3/docker-compose-linux-aarch64` |
| SHA256 | `d26373b19e89160546d15407516cc59f453030d9bc5b43ba7faf16f7b4980137` |
| Offizielle `.sha256`-Asset | `https://github.com/docker/compose/releases/download/v2.40.3/docker-compose-linux-aarch64.sha256` (identisch) |
| Größe | 73 740 771 Bytes |
| Abgebildet am | 2026-11-02 (R1, S5-Raspi) |

## Verifikation

Die Pin-Datei `docker-compose-sha256` (gleiches Verzeichnis) enthält die
`sha256sum`-Kontrollsumme im Format `<sha256>  docker-compose-linux-aarch64`.
Prüfung (im Verzeichnis des geladenen Binaries, Original-Dateiname):

```sh
sha256sum -c docker-compose-sha256
```

`scripts/pi_deploy_r1.sh` führt exakt diese Prüfung vor dem Übertrag auf den
Pi durch (Batch-SSH, Binary via `cat | ssh 'cat > …'`, danach `chmod +x`).

## Update-Regel

Neuere v2.x (bzw. gezielte Upgrade-Entscheidung): Download + `sha256sum`
hierhertragen, `docker-compose-sha256` aktualisieren, dann
`scripts/pi_deploy_r1.sh` erneut ausführen (idempotent: Binary wird nur
gespiegelt, wenn sich die Pin-Summe vom Pi-Zustand unterscheidet — sonst
überschreiben). Kein `apt install docker-compose` am Pi (Alpine: Paket-
Abhängigkeiten aufs Host-System, widerspricht „alles in Containern“).
