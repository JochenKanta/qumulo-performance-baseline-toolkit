# Qumulo Performance Baseline Toolkit (QPBT) - v0.3.0.dev0

**Technischer Linux-Prototyp, kein zertifiziertes Qumulo-Produkt.**

QPBT führt nach einer Qumulo-Installation eine versionierte Baseline (NFS + SMB) über **einen oder mehrere externe Linux-Testclients** aus. QPBT installiert **keine** Software auf Qumulo-Nodes und ändert **keine** Qumulo-Konfiguration. Die Last entsteht über vorab eingerichtete Netzwerk-Mounts und beschränkt sich auf `.qpbt/<Run-ID>`.

## Neu in 0.3 gegenüber 0.2

- **Netzwerk-Vorabtest** (`qpbt netcheck`, automatisch im `preflight`): Erreichbarkeit, Paketverlust und RTT je Qumulo-Node-Adresse, End-to-End-MTU per DF-Ping (Jumbo Frames), Client-Interface und Linkrate je Route, optional iperf3 gegen einen Host im Testnetz, Front-End-Linkraten der Qumulo-Nodes aus OpenMetrics. Ergebnis: eine **Netzwerk-Obergrenze** und die **Netz-Auslastung je Test** im Bericht.
- **Datensatzgröße nach Cluster** (`dataset_sizing: auto`): Datensatz = `cache_multiplier` x `node_count` x `cache_gib_per_node`, damit Lesetests nicht nur den Cluster-Cache messen. Jeder Lauf wird als `OVER_CACHE`, `PARTIALLY_CACHEABLE`, `FITS_IN_CACHE` oder `UNKNOWN` eingeordnet. Die Node-Anzahl wird gegen OpenMetrics geprüft.
- **Single-Stream-Tests über den gesamten Datensatz:** T01/T02/T05/T06 lesen und schreiben nicht mehr nur eine 1-GiB-Datei, sondern alle Worker-Dateien (bisher praktisch reine Cache-Treffer).
- **Mehrclient-Betrieb** über fio Client/Server: beliebig viele Testclients (bis 64), fio-Server werden per SSH gestartet und gestoppt, Mounts aller Clients werden per SSH geprüft, Lastverteilung auf Qumulo-Node-Adressen und Telemetrie je Node im Bericht.
- Telemetrie-Ausfall beim Start eines Einzeltests führt zu einer Warnung statt zum Abbruch des gesamten Laufs.
- **Profilwechsel:** `schema_version: "0.2"`, `profile: installation-v0.2`. Ergebnisse von 0.2 sind nicht direkt vergleichbar (siehe CHANGELOG).

## Lieferumfang

- `doctor`: Lokale Linux-Voraussetzungen prüfen; keine Verbindung zum Cluster und keine Testdaten
- `plan`: Workloads, Zeiten, Clients und Datensatzgröße inkl. Cache-Einordnung (komplett schreibfrei)
- `netcheck`: Nur Netzwerk-Vorabtest (schreibfrei, benötigt `cluster.node_addresses`)
- `preflight`: Mounts (Controller und alle Clients), tatsächliche NFS-/SMB-Version, fio, Netzwerk, Qumulo-API, freie Kapazität und Health-Gauges prüfen (schreibfrei)
- `servers start|stop|check`: fio-Server der Mehrclient-Testclients manuell steuern
- `run --confirm-write`: 9 fio-Tests + Metadaten-Operationen über NFS **und** SMB, Datenvorbereitung und Telemetrie
- `validate-run`: Gespeicherte Baseline offline auf Vollständigkeit prüfen
- `simulate`: Berichte ohne Hardware erzeugen, **unübersehbar als SIMULATION** markiert
- `status`, `report`, `compare`: Gespeicherte Runs prüfen und vergleichen
- Ausgabe: JSON-Resultate, fio-Roh-JSON, fio-Jobdateien je Client, selektierte OpenMetrics-Zeitreihen, HTML, PDF

## Voraussetzungen

- Ubuntu Server 22.04 / 24.04 oder vergleichbare aktuelle Linux-Distribution (noch nicht auf Qumulo-Hardware validiert)
- Python >=3.10; PyYAML 6.x und ReportLab 4.x (nur auf dem Controller)
- **fio-Benchmark-Werkzeug Version 3.x** auf Controller und allen Testclients, möglichst dieselbe Version (Achtung: Es gibt ein anderes `fio`-CLI für Fiona/GIS!)
- `findmnt`, `nfs-common`, `cifs-utils`, `iputils-ping`, `iproute2`; optional `iperf3`
- Zugriff auf Qumulo NFS-, SMB- und HTTPS-API-Endpunkte
- Geprüfter TLS-Trust (CA-Datei) und ein read-only Access-Token für OpenMetrics
- Freier Platz: `plan` zeigt den Bedarf; Preflight fordert konservativ 2 Protokolle x Datensatz x `free_space_multiplier`
- Mehrclient: SSH-Schlüssel-Login vom Controller auf jeden Client, **gleiche UID** des Testbenutzers auf allen Hosts (Run-Verzeichnisse haben Modus 0700)

```bash
sudo apt-get install -y fio nfs-common cifs-utils util-linux iputils-ping iproute2 iperf3 python3-venv
```

### 1. Installation (Controller)

```bash
git clone https://github.com/JochenKanta/qumulo-performance-baseline-toolkit.git
cd qumulo-performance-baseline-toolkit
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp qpbt.example.yaml qpbt.yaml          # Einzelclient
# oder: cp qpbt.multiclient.example.yaml qpbt.yaml
qpbt doctor
```

Auf reinen Testclients (Mehrclient) wird **nur fio** benötigt, kein Python-Paket.

### 2. Testexports vorbereiten (Beispiel)

NFS und SMB können auf dasselbe Qumulo-Filesystem zeigen, sollten aber über verschiedene Mountpoints und isolierte Testverzeichnisse angesprochen werden. **Die Qumulo-Exporte und SMB-Shares werden vorab von der verantwortlichen Person eingerichtet.**

```bash
sudo mkdir -p /mnt/qpbt-nfs /mnt/qpbt-smb
sudo mount -t nfs -o vers=4.1,proto=tcp 10.20.30.41:/qpbt-nfs /mnt/qpbt-nfs
sudo mount -t cifs //10.20.30.41/qpbt-smb /mnt/qpbt-smb \
  -o vers=3.1.1,cache=none,credentials=/root/.qpbt-smb,uid=$(id -u),gid=$(id -g)
```

Credentials-Datei Root-only (`chmod 600`), niemals im Repository oder Report. **Die Beispiele enthalten Platzhalter**. Rechte, Exportpfad, Signing, Encryption, ACLs, DNS und Cache-Semantik müssen zur Kundeninstallation passen. Das Toolkit verändert sie nicht.

### 3. Konfigurieren

- `cluster.api_url`, Token-Umgebungsvariable und TLS-CA
- `targets.nfs/smb`: Mountpoint, `expected_version` 3 oder 4.1, `expected_dialect` SMB 3.x
- **Pflicht:** `expected_source` muss bei **jedem** Mount (Controller und Clients) exakt dem Wert der `findmnt`-SOURCE-Spalte entsprechen. Ohne diese Angabe blockiert jeder Live-Run.
- `cluster.node_addresses`, `node_count`, `cache_gib_per_node` (siehe unten)
- `network.*`, `workload.*`, optional `multi_client.*`

```bash
read -r -s -p 'Qumulo API Token: ' QPBT_TOKEN; echo; export QPBT_TOKEN
qpbt plan --config qpbt.yaml
qpbt netcheck --config qpbt.yaml        # optional vorab, noch ohne Mounts möglich
qpbt preflight --config qpbt.yaml
qpbt run --config qpbt.yaml --confirm-write
qpbt validate-run reports/<RUN-ID>/result.json
```

**Keine Benchmarkdaten werden ohne `--confirm-write` geschrieben.** Der Run ist ein potenziell intensiver I/O-Test: nur im freigegebenen Wartungs-/Abnahmefenster ausführen.

## Netzwerk-Vorabtest

Läuft von **jedem** Testclient zu jeder Adresse in `cluster.node_addresses` (leer: die Adressen, die der Kernel laut `addr=` tatsächlich gemountet hat). Auf Qumulo-Nodes wird nichts gestartet.

| Prüfung | Befund |
|---|---|
| ICMP nicht erreichbar | FAIL |
| DF-Ping mit `expected_mtu` schlägt fehl (z. B. Jumbo Frames nicht durchgängig) | FAIL |
| Paketverlust > 0 %, RTT > `max_rtt_ms` | WARNING |
| Interface-MTU ≠ `expected_mtu`, Linkrate < `min_client_link_gbps` | WARNING |
| iperf3 < `iperf3_min_link_fraction` der Linkrate | WARNING |
| Qumulo-Nodes mit unterschiedlichen Front-End-Linkraten | WARNING |

`network.required: true` lässt FAIL-Befunde den Preflight blockieren. Die **Netzwerk-Obergrenze** = min(Summe Client-Links, Summe Qumulo-Front-End-Links) ergibt im Bericht die Spalte „Netz-Auslastung“: Liegt ein Sequential-Test nahe 100 %, begrenzt das Netz und nicht der Cluster. iperf3 braucht einen `iperf3 -s` auf einem Host im Testnetz, z. B. einem anderen Testclient. **Keine Qumulo-Node.**

## Datensatzgröße und Cache-Einordnung

Ein zu kleiner Datensatz liegt nach der Vorbereitung vollständig im Cluster-Cache, die Lesetests messen dann RAM/SSD statt der eigentlichen Plattform.

```yaml
cluster:
  node_count: 4
  cache_gib_per_node: 64     # Hybrid: SSD-Cache je Node; All-NVMe: RAM je Node
workload:
  dataset_sizing: auto
  cache_multiplier: 2.0      # Datensatz je Protokoll = 2 x 4 x 64 GiB = 512 GiB
```

Die Cache-Größe je Node richtet sich nach dem Plattformdatenblatt und ist **vom SE einzutragen**, QPBT rät sie nicht. `auto` verteilt den Datensatz auf alle Clients und Worker (max. 128 GiB je Worker). Auch bei `fixed` wird der Lauf eingeordnet, sobald `node_count` und `cache_gib_per_node` gesetzt sind. `validate-run` warnt bei allem außer `OVER_CACHE`.

## Mehrclient-Betrieb

```text
Controller (qpbt, Mounts, Telemetrie, Report)
   │  SSH: Mountprüfung, fio --server start/stop
   │  TCP 8765: fio Client/Server
   ├── client01 ──NFS/SMB──► Qumulo Node 1
   ├── client02 ──NFS/SMB──► Qumulo Node 2
   └── ...
```

1. Jeder Client mountet denselben Export, idealerweise über **eine andere Node-Adresse** (Floating IPs). QPBT liest die tatsächlich verbundene Adresse (`addr=`) und warnt bei ungleicher Verteilung.
2. Der Controller mountet selbst ebenfalls und legt dort das Run-Verzeichnis mit einer zufälligen Nonce an. Jeder Client muss diese Nonce über **seinen** Mount lesen können, sonst bricht der Lauf vor dem ersten Byte Testdaten ab (Schutz gegen falsche Exporte).
3. `manage_fio_servers: true` startet `fio --server=<address>,<port>` per SSH und beendet die Server nach dem Lauf, auch bei Fehlern. **Der fio-Server hat keine Authentifizierung:** nur an Adressen im Testnetz binden und nicht dauerhaft laufen lassen.
4. Multi-Worker-Tests laufen auf allen Clients gleichzeitig (`worker_count` je Client). Single-Stream-Tests laufen mit einem Stream auf dem ersten Client über den Datensatz aller Clients.
5. Ergebnisse: Summe aus Durchsatz/IOPS, IOPS-gewichtete mittlere Latenz, **P95/P99 = schlechtester Client** (fio liefert im Client/Server-Modus keine aggregierten Perzentile), Werte je Client, Client-Balance. Fehlt das Ergebnis eines Clients, schlägt der Test fehl (fio selbst meldet dann Exitcode 0).
6. Metadaten-Test (T10) läuft auf dem Controller.

Voraussetzungen je Client: fio 3.x, `findmnt`, `ping`, `ip`, SSH-Schlüssel-Login (`BatchMode`), gleiche UID wie der Controller-Benutzer, erreichbarer TCP-Port für fio vom Controller aus. `timedatectl` wird für den NTP-Hinweis genutzt.

## Ergebnisverzeichnis

```text
reports/<RUN-ID>/
  preflight.json  result.json  report.html  report.pdf
  fio/nfs/*.json  fio/smb/*.json        # fio-Rohdaten
  fio/<proto>/<Test>.<client>.fio       # Mehrclient: exakte Jobdateien je Client
  telemetry/nfs/*.json  telemetry/smb/*.json
```

Testdaten befinden sich **nicht** im Reportverzeichnis, sondern unter `.qpbt/<RUN-ID>-<proto>/` auf dem Export. Der `after_report`-Cleanup entfernt sie nur nach technisch erfolgreichem oder mit Warnungen abgeschlossenen Run. Unbekannte Dateien, Clientverzeichnisse oder Symlinks verhindern die Löschung.

## Baselines vergleichen

```bash
qpbt compare reports/<RUN_1>/result.json reports/<RUN_2>/result.json
```

Vergleiche zwischen unterschiedlichen Profilen, Client-Anzahlen, Clients oder Mounts erscheinen als eingeschränkt vergleichbar. Keine automatische SLA-Bewertung.

## Testmatrix (je NFS und SMB)

| Test | Workload | Blockgröße | Streams |
|---|---|---:|---:|
| T01 | Sequential Write | 1 MiB | 1 (ganzer Datensatz) |
| T02 | Sequential Read | 1 MiB | 1 (ganzer Datensatz) |
| T03 | Sequential Write | 1 MiB | N je Client |
| T04 | Sequential Read | 1 MiB | N je Client |
| T05 | Random Read | 4 KiB | 1 (ganzer Datensatz) |
| T06 | Random Write | 4 KiB | 1 (ganzer Datensatz) |
| T07 | Random Read | 4 KiB | N je Client |
| T08 | Random Write | 4 KiB | N je Client |
| T09 | Random Mixed 70/30 | 4 KiB | N je Client |
| T10 | Metadata Create/Stat/Delete | 4 B | 1 (Controller) |

Standardmäßig **30 s Messung + 10 s Warm-up** je Test. Mit `psync` entsprechen N Worker nicht einer asynchronen Queue Depth N.

## Bekannte Grenzen

- **Kein echter Qumulo-Test.** Mehrclient-Modus, Netzwerktest und Telemetrie wurden lokal mit echtem fio 3.36, echtem SSH, echten fio-Servern und einem simulierten OpenMetrics-Endpunkt end-to-end geprüft, NFS/SMB dabei durch Bind-Mounts ersetzt.
- fio `direct=1` umgeht den **Client**-Pagecache, nicht Qumulo-internes Caching; dafür gibt es die Cache-Einordnung.
- **Linux-CIFS/SMB** ist kein nativer Windows-SMB-Test.
- Das Metadata-`stat`-Ergebnis kann vom Client-Cache profitieren; T10 ist Single-Thread auf dem Controller.
- `PASS` bedeutet durchgeführter Test, **nicht** Performance-SLA erfüllt.
- OpenMetrics-Metriken sind clusterweit; Fremdlast kann die Attribution verfälschen.
- Mehrclient-Start ist nicht taktgenau synchronisiert: fio startet die Jobs auf Kommando des Controllers. Der Versatz bei echten Clients ist noch nicht gemessen, deshalb `warmup_seconds` > 0 lassen.
- Kein Resume nach Unterbrechung, keine USB-Live-Distribution, noch keine Windows-Agents.

Mehr unter `docs/TECHNICAL_DECISIONS.md` und `docs/LAB_VALIDATION.md`.

## Validierungsstatus von 0.3

- 52 Unit-/Mock-Tests; lokaler End-to-End-Lauf mit 2 Clients (SSH + lokal), verwalteten fio-Servern, O_DIRECT-Probe, Auto-Sizing, Netzwerktest, Node-Telemetrie und Cleanup; Negativtest „Client mountet anderes Verzeichnis“ bricht vor dem Datensatz ab.
- **Nicht bestätigt:** fio 3.x gegen echte Qumulo-NFS/SMB-Mounts, O_DIRECT über NFS/CIFS, echte Qumulo-Metrikbezeichner für Link-Speed/Rollen, Verhalten bei vielen Clients.
- **Keine Freigabe für Kunden-Abnahme oder SLA-Aussagen.** Erst Lab-Smoke-Test, Negativtests und Standardprofil verifizieren, siehe `docs/LAB_VALIDATION.md`.
