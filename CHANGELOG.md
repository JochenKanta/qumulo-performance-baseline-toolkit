# QPBT Changelog

## 0.3.0.dev0 — 24.11.2026

**Neues Benchmarkprofil:** `installation-v0.2` (`schema_version: "0.2"`). Single-Stream-Tests T01/T02/T05/T06 adressieren jetzt den gesamten Datensatz statt einer Worker-Datei. Ergebnisse von `installation-v0.1` sind deshalb nicht direkt vergleichbar.

### Migration 0.2 → 0.3

- `schema_version: "0.2"` und `profile: installation-v0.2` setzen.
- Optional neue Abschnitte `network`, `multi_client` sowie `cluster.node_count`, `cluster.cache_gib_per_node`, `cluster.node_addresses`, `workload.dataset_sizing`, `workload.cache_multiplier` (siehe `qpbt.example.yaml`, `qpbt.multiclient.example.yaml`).

### Netzwerk-Vorabtest

- Neuer Befehl `qpbt netcheck`; automatisch Teil von `preflight` und `run`.
- Ping (Verlust, RTT), DF-Ping mit `expected_mtu`, Route/Interface/Linkrate/MTU je Client, optional iperf3 (beide Richtungen), Front-End-Linkraten je Qumulo-Node aus OpenMetrics.
- Netzwerk-Obergrenze und Netz-Auslastung je Test im Bericht; `network.required` macht FAIL-Befunde blockierend.

### Datensatzgröße nach Cluster

- `dataset_sizing: auto` dimensioniert den Datensatz aus `node_count` x `cache_gib_per_node` x `cache_multiplier`.
- Cache-Einordnung je Lauf (`OVER_CACHE` / `PARTIALLY_CACHEABLE` / `FITS_IN_CACHE` / `UNKNOWN`).
- Node-Anzahl wird gegen OpenMetrics geprüft (Abweichung blockiert bei `auto`).

### Mehrclient-Betrieb

- fio Client/Server-Modus mit bis zu 64 Testclients; fio-Server-Verwaltung per SSH (`qpbt servers start|stop|check`, automatisch im Lauf).
- Read-only Mountprüfung aller Clients per SSH (Typ, Version, SOURCE, mountinfo-ID, st_dev, Rechte), Wiederholung vor jedem Test.
- Nonce im Run-Marker: Jeder Client muss das Run-Verzeichnis des Controllers über seinen eigenen Mount sehen.
- O_DIRECT-Probe mit crc32c-Readback auf jedem Client.
- `unique_filename=0` (fio würde sonst Hostnamen vor Dateinamen setzen); jeder erwartete Client muss im fio-JSON vorhanden sein (fio liefert Exitcode 0, wenn ein Client-Job verworfen wird).
- Aggregation: Summe Durchsatz/IOPS, P95/P99 als schlechtester Client; Werte und Balance je Client.
- Node-Verteilung der Clients (Kernel-`addr=`) und Telemetrie je `node_id` inkl. Node-Balance.

### Weitere Änderungen

- Mountpoint-Erkennung zusätzlich über `/proc/self/mountinfo` (Bind-Mounts auf demselben Gerät wurden von `Path.is_mount()` nicht erkannt).
- Telemetrie-Fehler beim Start eines Einzeltests → Warnung statt Abbruch des Laufs.
- fio-Optionen werden zentral erzeugt; Jobdateien lehnen Zeilenumbrüche, `$`-Expansion und `:` in Verzeichnissen ab.
- `validate-run` prüft Client-Vollständigkeit und warnt ohne Netzwerktest bzw. ohne `OVER_CACHE`.
- README: Installation per `git clone` statt ZIP.

### Teststand

- 52 Unit-/Mock-Tests bestanden.
- Lokaler End-to-End-Lauf: 2 Clients (SSH + lokal), verwaltete fio-Server 3.36, Auto-Sizing, Netzwerktest inkl. iperf3, simulierter OpenMetrics-Endpunkt mit 2 Nodes und eigener CA, 20/20 Tests, Cleanup vollständig. NFS/SMB durch Bind-Mounts ersetzt.
- Negativtest: Client mit falschem Mount → Abbruch vor dem Datensatz, fio-Server beendet.
- **Offen:** Realtest auf echtem Qumulo NFS/SMB mit mehreren physischen Clients.

## 0.2.0.dev0 — 14.10.2026

**Unverändertes Benchmarkprofil:** `installation-v0.1`, 9 fio + 1 Metadaten-Test je Protokoll, also 20 Testfälle für NFS+SMB.

### Sicherheitskorrekturen

- Zwei unabhängige Workspace-Namen (`<Run-ID>-nfs`, `<Run-ID>-smb`) für identische Qumulo-Verzeichnisziele.
- Fail-closed-Verifikation des exakten Mount-SOURCE-Werts; Überwachung von mountinfo-ID und Device-Kennung.
- 4-KiB-O_DIRECT-Probe mit Readback für jedes Protokoll vor dem großen Datensatz.
- Automatische Löschvorgänge verweigert bei unbekannten Dateien, Links, Zusatz-Mounts oder geänderten Mount-Identitäten.
- OpenMetrics-Verbindungen verwenden keine HTTP-Proxies und keine automatischen Redirects mit Bearer-Token.
- Strikte YAML-Schlüsselvalidierung; keine unbekannten Credentials in den Bericht übernehmen.

### Mess- und Reportingqualität

- Qumulo-OpenMetrics-Snapshot vor statt irgendwann nach Benchmarkstart.
- Daten-/Metadaten-Zähler als eigene Dimension im Parser.
- Erwartete fio-I/Os und P95/P99-Perzentile geprüft; fehlende Werte führen nicht zu PASS.
- Profil-Fingerprint basiert ausschließlich auf Testsemantik, nicht auf Reportpfaden oder API-Credentials.
- Baseline-Vergleiche ignorieren volatile mount_id- und Kapazitätswerte, warnen aber bei anderer Cluster-UUID/Versionsständen.
- Neue Befehle: `qpbt doctor`, `qpbt validate-run`.

### Teststand

- 29 Unit-/Mock-Tests bestanden.
- Lokale 4-KiB-O_DIRECT-Readback-Probe erfolgreich.
- Beide Simulationsberichte erzeugt und `validate-run` verweigert deren Verwendung als reale Baseline.
- Ein wheel installierbar mit vorhandenen Laufzeitabhängigkeiten.
- **Offen:** Realtest mit fio 3.x auf echtem Qumulo NFS/SMB inklusive negativen Tests.
