# QPBT Changelog

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
