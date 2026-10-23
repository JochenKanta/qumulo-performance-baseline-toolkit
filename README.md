# Qumulo Performance Baseline Toolkit (QPBT) - v0.2.0.dev0

**Technischer Linux-Prototyp, kein zertifiziertes Qumulo-Produkt.**

QPBT führt eine versionierte Installation-Baseline (NFS + SMB) über **externe Linux-Testclients** aus. QPBT installiert **keine** Software auf Qumulo-Nodes und ändert **keine** Qumulo-Konfiguration. Die Last entsteht über vorab eingerichtete Netzwerk-Mounts und beschränkt sich auf `.qpbt/<Run-ID>`.

## Neu in 0.2 gegenüber 0.1

- **NFS/SMB-Workspace-Kollision korrigiert:** Getrennte `<run-id>-nfs` und `<run-id>-smb` Verzeichnisse auch bei demselben Qumulo-Zielverzeichnis.
- **Strengeres Preflight:** `expected_source` ist für echte Läufe bei **beiden Protokollen Pflicht**. Zusätzlich werden Linux-`mount_id` und `st_dev` zur Erkennung von Mountwechseln protokolliert.
- **Echter lokaler O_DIRECT-Smoke-Test:** Vor dem großen Datensatz wird innerhalb des isolierten Arbeitsbereichs 4 KiB mittels O_DIRECT geschrieben und read-back verifiziert. Keine Aussage über Qumulo-internes Caching.
- **Verbessertes Metrik-Sampling:** Vorher-Snapshot vor dem Start des Workloads, keine automatischen HTTP-Proxy-Verbindungen oder Redirects bei Bearer-Token-Requests, getrennte Daten-/Metadaten-Zähler.
- **Qualitätssicherung:** Erwartete Read-/Write-Daten und fio-P95/P99 werden strikt geprüft; unbekannte YAML-Konfigurationsfelder werden abgelehnt, damit keine unbeabsichtigten Secrets in den Bericht gelangen.
- Neue Befehle `qpbt doctor` (schreibfreie Hostprüfung ohne Cluster) und `qpbt validate-run` (Offline-Qualitätsprüfung vollständiger Messläufe).
- **29 Unit-/Mock-Tests erfolgreich.** Außerdem wurde die 4-KiB-O_DIRECT-Prüfroutine auf einem lokalen Linux-Dateisystem ausgeführt. **Kein Live-Qumulo-Test.**

**WICHTIG:** Die Benchmark-Testmatrix heißt weiterhin `installation-v0.1` und wurde nicht verändert. Die Softwareversion ist 0.2, das Testprofil 0.1. Echte Läufe erfordern die eindeutige Angabe der `expected_source`-Werte; die Beispielkonfiguration lässt sie absichtlich offen. Der vollständige Ablauf und die Labor-Freigabekriterien stehen in [`docs/LAB_VALIDATION.md`](docs/LAB_VALIDATION.md).

## Lieferumfang

- `doctor`: Lokale Linux-Voraussetzungen prüfen; keine Verbindung zum Cluster und keine Testdaten
- `validate-run`: Gespeicherte Baseline gegen die 20 Testfälle und die neuen Sicherheitsnachweise validieren
- `plan`: Welche Workloads, Zeiten und Testdatenmengen sind vorgesehen? (komplett schreibfrei)
- `preflight`: Linux-Mounts, tatsächliche NFS-/SMB-Version, fio, Qumulo-API, freie Kapazität und Qumulo-Disk-/Node-Gauges prüfen (auf Testvolumes schreibfrei)
- `run --confirm-write`: 9 fio-Tests + Metadata-Operationen über NFS **und** SMB, Datenvorbereitung und Telemetrie
- `simulate`: Berichte ohne Hardware erzeugen, **unübersehbar als SIMULATION** markiert
- `status`, `report`, `compare`: Gespeicherte Runs prüfen und vergleichen
- Ausgabe: JSON-Resultate, fio-Roh-JSON, selektierte OpenMetrics-Zeitreihen, HTML, PDF

## Voraussetzungen

- Ubuntu Server 22.04 / 24.04 oder vergleichbare aktuelle Linux-Distribution (noch nicht auf Qumulo-Hardware validiert)
- Python >=3.10; PyYAML 6.x und ReportLab 4.x
- Das **fio-Benchmark-Werkzeug Version 3.x** (Achtung: Es gibt ein anderes `fio`-CLI für Fiona/GIS!)
- `findmnt`, `nfs-common`, `cifs-utils` und Zugriff auf Qumulo NFS-, SMB- und HTTPS-API-Endpunkte
- Geprüfter TLS-Trust (CA-Datei) und ein read-only Access-Token für OpenMetrics
- Mindestens ca. **40 GiB freier Filesystem-Platz** bei den Standardwerten (2 Protokolle à 16 GiB und 25 % Reserve)

Ubuntu-Pakete (Beispiel, benötigt Paketquellen):

```bash
sudo apt-get install -y fio nfs-common cifs-utils util-linux python3-venv
```

### 1. Installation

```bash
unzip QPBT_v0_2_Lab_Prototyp.zip
cd qpbt_v0_2
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp qpbt.example.yaml qpbt.yaml
qpbt doctor
```

Für einen **vollständig offlinefähigen Installationsstick** muss ein zu Distribution/Architektur passender Wheel-Satz vorher gebündelt werden. Das vorliegende ZIP ist Quellcode, kein USB-Live-Image.

### 2. Testexports vorbereiten (Beispiel)

NFS und SMB können auf dasselbe Qumulo-Filesystem zeigen, sollten aber über verschiedene Mountpoints und isolierte Testverzeichnisse angesprochen werden. **Die Qumulo-Exporte und SMB-Shares werden vorab von der verantwortlichen Person eingerichtet.**

```bash
sudo mkdir -p /mnt/qpbt-nfs /mnt/qpbt-smb
sudo mount -t nfs -o vers=4.1,proto=tcp 10.10.10.10:/qpbt-nfs /mnt/qpbt-nfs
```

SMB-Authentifizierung über Root-only Credentials-Datei (`chmod 600`, niemals im Repository oder Report hinterlegen):

```bash
sudo mount -t cifs //10.10.10.10/qpbt-smb /mnt/qpbt-smb \
  -o vers=3.1.1,cache=none,credentials=/root/.qpbt-smb,uid=$(id -u),gid=$(id -g)
```

**Die Beispiele enthalten Platzhalter**. Rechte, Exportpfad, Signing, Encryption, ACLs, DNS und Cache-Semantik müssen zur Kundeninstallation passen. Das Toolkit verändert sie nicht.

### 3. Konfigurieren

In `qpbt.yaml` zuerst prüfen:

- `cluster.api_url`, Token-Umgebungsvariable und TLS-CA
- `targets.nfs.mountpoint` sowie `expected_version` 3 oder 4.1
- `targets.smb.mountpoint` sowie `expected_dialect` SMB 3.x
- **Pflicht:** `expected_source` muss bei **beiden** Mounts exakt dem Wert der `findmnt`-SOURCE-Spalte entsprechen. Ohne diese Angabe blockiert jeder Live-Run.
- `workload.dataset_mib_per_worker`: Standard 1024 MiB x 16 Worker x 2 Protokolle

Danach:

```bash
export QPBT_TOKEN='DEIN_READ_ONLY_ACCESS_TOKEN'  # nur in sicherer Shell / Secret-Quelle
qpbt plan --config qpbt.yaml
qpbt preflight --config qpbt.yaml
qpbt run --config qpbt.yaml --confirm-write
qpbt validate-run reports/<RUN-ID>/result.json
```

**Keine Benchmarkdaten werden ohne `--confirm-write` geschrieben.** Der Run ist dennoch ein potenziell intensiver I/O-Test: nur im freigegebenen Wartungs-/Abnahmefenster ausführen.

### 4. Simulation (ohne Cluster)

```bash
qpbt simulate --config qpbt.yaml
```

Die erzeugten Leistungswerte sind **frei erfunden**, nur um Format und Verarbeitung zu demonstrieren. Sie dürfen niemals als echte Qumulo-Baseline verwendet werden.

### 5. Ergebnisverzeichnis

```text
reports/<RUN-ID>/
  preflight.json
  result.json
  report.html
  report.pdf
  fio/nfs/*.json
  fio/smb/*.json
  telemetry/nfs/*.json
  telemetry/smb/*.json
```

Testdaten befinden sich **nicht** im Reportverzeichnis, sondern in den jeweiligen Netzwerk-Mounts unter `.qpbt/<RUN-ID>/dataset/`. Der `after_report`-Cleanup entfernt die Testdaten nur nach technisch erfolgreichem oder mit Warnungen abgeschlossenen Run; bei Fehlschlägen bleiben sie zur Analyse erhalten. Unbekannte Dateien oder verdächtige Symlinks verhindern die Löschung.

Die Eingabe `--run-id` bei `qpbt status` oder `qpbt report` muss eine bereits bestehende Run-ID sein.

### 6. Baselines vergleichen

```bash
qpbt compare reports/<RUN_1>/result.json reports/<RUN_2>/result.json
```

Vergleiche zwischen unterschiedlichen Profil-/Client-/Mountbedingungen erscheinen als eingeschränkt vergleichbar. Keine automatische SLA-Bewertung.

## Testmatrix (je NFS und SMB)

| Test | Workload | Blockgröße | Worker |
|---|---|---:|---:|
| T01 | Sequential Write | 1 MiB | 1 |
| T02 | Sequential Read | 1 MiB | 1 |
| T03 | Sequential Write | 1 MiB | 16 |
| T04 | Sequential Read | 1 MiB | 16 |
| T05 | Random Read | 4 KiB | 1 |
| T06 | Random Write | 4 KiB | 1 |
| T07 | Random Read | 4 KiB | 16 |
| T08 | Random Write | 4 KiB | 16 |
| T09 | Random Mixed 70/30 | 4 KiB | 16 |
| T10 | Metadata Create/Stat/Delete | 4 B | 1 |

Die I/O-Workloads sind standardmäßig **30 s Messung + 10 s Warm-up** je Test. Hinzu kommen Datensatzvorbereitung, Mess-/Report-/Cleanup-Overhead. Mit `psync` entsprechen 16 Worker nicht einer asynchronen Queue Depth 16.

## Bekannte Grenzen

- **Kein echter Qumulo-Test in dieser Entwicklungsumgebung.** Der Simulationsbericht dient nur der Funktionsdemonstration.
- fio `direct=1` umgeht möglichst den **Client**-Pagecache, aber nicht automatisch Qumulo-internes Caching.
- **Linux-CIFS/SMB** ist kein nativer Windows-SMB-Test.
- Das Metadata-`stat`-Ergebnis kann vom Client-Cache profitieren.
- `PASS` bedeutet durchgeführter Test, **nicht** Performance-SLA erfüllt.
- OpenMetrics-Metriken sind clusterweit; Fremdlast kann die Attribution verfälschen.
- Abweichende Cache-/Write-Durability-Semantik zwischen NFS/SMB beachten.
- Kein Resume nach Unterbrechung, keine USB-Live-Distribution, noch keine Windows-Agents.

Mehr unter `docs/TECHNICAL_DECISIONS.md`.

## Wichtig: aktuelle Validierungsstufe

Dieser Code wurde mit Unit-Tests und dem Simulationspfad geprüft. Ein End-to-End-Live-Benchmark mit echtem fio 3.x auf einem Qumulo NFS-/SMB-Mount muss im Labor abgeschlossen werden, **bevor** das Tool produktiv eingesetzt wird.

## Validierungsstatus von 0.2

- Host-Unit-/Mock-Tests und Simulationsberichte geprüft; `qpbt doctor` entdeckt ein ggf. falsch installiertes `fio` (Fiona-CLI).
- **Nicht bestätigt:** fio-3.x-Parameter gegen Qumulo, NFS-/SMB-O_DIRECT-Mount-Verhalten, 30-s-Benchmarkwerte, echte Qumulo-Metrik-Zeitreihen und Clustergesundheit in realer Umgebung.
- **Keine Freigabe für Kunden-Abnahme oder SLA-Aussagen.** Erst Lab-Smoke-Test, Negativtests und Standardprofil erfolgreich verifizieren, siehe `docs/LAB_VALIDATION.md`.
