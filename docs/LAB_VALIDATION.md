# QPBT v0.2 - Lab-Validierungsprotokoll

**Zweck:** Nachweis, dass der generierte NFS-/SMB-Benchmark auf echtem Qumulo mit korrekt gemessenen I/Os, sicheren Schreibgrenzen und vollständiger Telemetrie ausgeführt werden kann. **Noch keine Qumulo-Freigabe erteilt.**

## Phase 0 - Ausrüstung und Freigaben

- Dedizierter Linux-Testclient (vorzugsweise Ubuntu Server 22.04 oder 24.04); Client-Hardware, Kernel und NIC-Speed notieren.
- fio 3.x (Flexible I/O Tester), nfs-common, cifs-utils, util-linux, iproute2 und Python 3.10+.
- Qumulo-Core-Version erfassen; die in QPBT verwendete OpenMetrics-Schnittstelle ist ab 5.3.0 dokumentiert.
- Separater Qumulo-NFS-Export und SMB3-Share; beide können denselben zugrunde liegenden Filesystem-Pfad abbilden.
- Mindestens 2 x 16 GiB nominelle Testdatenmenge plus Reserve für den Standardlauf. Wenn beide Shares dieselbe Volume-Kapazität nutzen, trotzdem kumulativen Bedarf prüfen.
- Qumulo API-Bearer-Token mit minimal nötigen Leseberechtigungen sowie Cluster-CA-Zertifikat.
- Schriftlich festgelegtes Testfenster und Freigabe für die temporäre Testlast; keine Produktivdaten im dedizierten Testbereich.
- **QPBT ist kein offizielles Qumulo-Produkt und wurde nicht auf Qumulo-Endpunkten validiert.**

## Phase 1 - Host Diagnose (schreibfrei, kein Cluster erforderlich)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --no-index --find-links /path/to/offline-wheels -e .  # nur bei vorbereiteten Wheels
qpbt doctor
qpbt plan --config qpbt.yaml
```

Wenn Netzwerkpaketquellen erlaubt sind, alternativ `pip install -e .`. Vor dem Live-Test prüfen:

```bash
which fio
fio --version      # muss fio-3.x anzeigen, NICHT Fiona-GIS CLI
findmnt -T /mnt/qpbt-nfs -n -o TARGET,FSTYPE,SOURCE,OPTIONS
findmnt -T /mnt/qpbt-smb -n -o TARGET,FSTYPE,SOURCE,OPTIONS
```

**Vorsicht:** `findmnt -o OPTIONS` kann je nach Mountmethode benutzerspezifische Optionen anzeigen. Konsolenausgaben mit Credentials nicht in Tickets oder Berichten veröffentlichen. Für QPBT `expected_source` exakt anhand der SOURCE-Spalte setzen.

## Phase 2 - Mount- und TLS-Prüfung

Qumulo-Exports und SMB-Shares durch den verantwortlichen Administrator bereitstellen und außerhalb des QPBT-Tools mounten. Beispielwerte anpassen:

```bash
sudo mkdir -p /mnt/qpbt-nfs /mnt/qpbt-smb
sudo mount -t nfs -o vers=4.1,proto=tcp 10.20.30.40:/qpbt-nfs /mnt/qpbt-nfs
sudo mount -t cifs //10.20.30.40/qpbt-smb /mnt/qpbt-smb \
    -o vers=3.1.1,cache=none,credentials=/root/.qpbt-smb,uid=$(id -u),gid=$(id -g)
```

NFSv3 alternativ mit `vers=3` und den in der Umgebung nötigen RPC-/Mountdiensten. `cache=none` für kontrollierten Linux-SMB-Benchmark dokumentieren; nicht unbemerkt produktive SMB-Einstellungen verändern. SMB Signing/Encryption nicht zur Optimierung deaktivieren.

`targets.nfs.expected_source` und `targets.smb.expected_source` in `qpbt.yaml` verpflichtend auf exakte `findmnt`-Quellwerte setzen. NFS/SMB-Mounts müssen separate Mountpoints sein.

API-Token sicher bereitstellen (niemals in `qpbt.yaml` oder in Shell-History):

```bash
read -r -s -p 'Qumulo API Token: ' QPBT_TOKEN; echo
export QPBT_TOKEN
qpbt preflight --config qpbt.yaml
```

**Erwartung:** Genau ein externer NFS-Mount und ein externer SMB-Mount werden identifiziert, Quellen passen, Versions- und Mount-Identität wird erfasst, Cluster UUID/Version/Name werden aus OpenMetrics ermittelt, keine kritischen Health-Gauges.

## Phase 3 - Kurzer funktionaler Lab-Smoke-Test

Vor dem vollständigen Profil `qpbt.lab-smoke.yaml` kopieren, Quellen, URLs, CA und Mountpoints anpassen. Dieses Profil ändert Laufzeiten und Datensatzgröße und ist **nicht als Performance-Baseline vergleichbar**.

```bash
qpbt plan --config qpbt.lab-smoke.yaml
qpbt preflight --config qpbt.lab-smoke.yaml
qpbt run --config qpbt.lab-smoke.yaml --confirm-write
```

**Abnahme des Smoke-Tests:**

1. NFS und SMB werden separat mit exakt 10 Tests pro Protokoll erfasst.
2. Auf beiden Mounts ist die 4-KiB-O_DIRECT-Probe mit Readback erfolgreich.
3. fio PREPARE erzeugt ausschließlich die erwarteten Worker-Datensätze.
4. Das OpenMetrics-Monitoring enthält je fio-Test mindestens zwei erfolgreiche Samples.
5. In den Resultaten stehen `fio`-I/O-Zahlen, reale Latenzperzentile und keine verdeckten Simulationen.
6. Report existiert als HTML, PDF und JSON; die Reports sind klar mit dem tatsächlich verwendeten Profil-Hash versehen.
7. Der `validate-run`-Befehl gibt ohne künstliche Anmerkung zur Laborfreigabe `valid: true` aus.
8. Nach Erfolg/aktiviertem Cleanup bleiben keine zu diesem Lauf gehörigen Testdateien zurück. Andere QPBT-Runs und Dateien dürfen nicht verändert werden.

```bash
qpbt validate-run reports/<RUN-ID>/result.json
```

## Phase 4 - Standardinstallation-Baseline

Mit unveränderten Standardwerten aus `qpbt.example.yaml` durchführen. Vorher erneut auf freien Speicher, Clientlast und freigegebenes Zeitfenster achten.

```bash
qpbt preflight --config qpbt.yaml
qpbt run --config qpbt.yaml --confirm-write
qpbt validate-run reports/<RUN-ID>/result.json
```

**Prüfpunkte:**

- Testmatrix vollständig, NFS und SMB; Single-Read/Write sowie 16-Worker-Read/Write.
- Random Read/Write und Mixed Random 70/30 mit P95/P99 und ohne I/O-Fehler.
- fio-Version, Clientkernel, NICs, Mountoptionen, Profil-Hash und Qumulo-Core-Version aufgezeichnet.
- Qumulo-Telemetrie erfasst, Clusteridentität konsistent, keine Counter-Resets.
- Validierungsbericht markiert mögliche Warnungen statt Performance-Grenzen zu erfinden.
- Qumulo-Server-Cache wird durch Client-Direct-I/O **nicht** umgangen.
- In den Reportdateien keine Secrets/Passwörter oder uneindeutige „Cluster hat bestanden“-Aussagen.

## Phase 5 - Negativtests (nur freigegebene Laborumgebung)

| Negativtest | Erwartung |
| --- | --- |
| NFS- oder SMB-Mount fehlt | Preflight blockiert; kein lokaler fio-Test |
| Falscher Mount/FSTYPE | Preflight blockiert |
| Falsche SOURCE oder Dialekt | Preflight blockiert |
| `fio` ist das gleichnamige Fiona/GIS-CLI | Preflight blockiert |
| Qumulo-Metrik-API/TLS/Token ungültig | Preflight blockiert (telemetry.required) |
| O_DIRECT auf NFS oder SMB unsupported | Lauf stoppt vor 16-GiB-Datensatzvorbereitung |
| Disk-/Node-Unhealthy | Preflight blockiert |
| Freier Platz unzureichend | Preflight blockiert |
| API während Test unterbrochen | Warning/Telemetrieunvollständigkeit in Report |
| Mount während des Tests gewechselt | Lauf stoppt, Cleanup wird verweigert |
| Unerwartete Datei/Symlink im Run-Verzeichnis | Cleanup wird verweigert |
| NFS+SMB mappen auf denselben Directory-Root | Protokollweise getrennte Run-Verzeichnisse, kein Kollisionsfehler |

## Lab-Freigabedokumentation

Für jeden geprüften Kombinationstyp das folgende Raster ausfüllen:

| Feld | Wert |
| --- | --- |
| Cluster-UUID / Modell / Nodeanzahl | ... |
| Qumulo-Core-Version | ... |
| Host, OS, Kernel, fio 3.x | ... |
| Netzwerk: NIC/Speed/MTU/Switch | ... |
| NFS-Version, rsize, wsize | ... |
| SMB-Dialekt, Signing/Encryption, cache | ... |
| QPBT-Versionsstand und Profil-Hash | ... |
| Smoke Run-ID | ... |
| Baseline Run-ID | ... |
| Negativtests | ... |
| Offene Findings | ... |
| Freigabe Test-/Engineeringverantwortliche | ... |

**Freigabe erst nach dokumentiertem echten Live-Test.** Die automatischen Unit-/Mock-Tests und ein Linux-O_DIRECT-Loopback-Test sind notwendige, aber nicht hinreichende Voraussetzungen.
