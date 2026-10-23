# QPBT v0.2 - Architekturentscheidungen und Grenzen

## Funktionaler MVP

- NFSv3/NFSv4.1 und SMB3 werden über zwei vorab gemountete, explizit geprüfte Netzwerkmounts getestet.
- Der Lastgenerator ist fio 3.x (Flexible I/O Tester) mit synchronem psync-Engine, direktem I/O und 1 bzw. 16 Workern (anpassbare Multi-Worker-Zahl).
- 9 fio Workloads plus ein Single-Thread-Python-Metadaten-Microbenchmark je Protokoll.
- Zunächst 16 GiB Datensatz pro Protokoll; kontrollierbarer Wert in der Konfiguration.
- Qumulo OpenMetrics wird über HTTPS mit prüfbarer CA und Bearer Token abgefragt.
- Automatische JSON-, HTML- und PDF-Berichte mit Rohdateien und Testkonfiguration.

## Performance-Semantik

- `psync` ist synchrones read/write/pread/pwrite; Multi-Worker bedeutet mehrere unabhängige Jobs, **nicht** Queue Depth 16.
- `direct=1` beeinflusst den Linux-Client-Pagecache, garantiert aber **keinen** Qumulo-Cold-Cache.
- SMB unter Linux ist **keine** native Windows-SMB-Messung. SMB Signing/Encryption werden nicht vom Toolkit verändert.
- Mit fio `end_fsync=1` wird zum Job-Ende ein Flush angefordert. Daraus folgt keine Aussage, dass jede Write-I/O synchron bis auf persistenten Speicher bestätigt wurde.
- Quorum-/Disk-Health-Gauges blockieren bei positivem Fehlerbefund. Wenn Gauges fehlen, erscheint eine Warnung; es gibt keine umfassende Cluster-Health-Freigabe ohne Qumulo-spezifische REST-Abfragen.
- Die Cluster-Counter sind clusterweit und können durch fremden Traffic verfälscht werden.
- Ein PASS bedeutet technische Ausführung, keine Abnahme gegenüber einem hardwarebezogenen Sollwert.
- Metadata `stat` kann stark von dentry-/Attribute-Caches profitieren und ist deshalb nicht als reine Server-Metadata-IOPS zu interpretieren.

## Noch nicht produktionsreif / bekannte Einschränkungen

1. Kein realer Test an einem Qumulo-Cluster in der aktuellen Entwicklungsumgebung. Mounts, Direct I/O und tatsächliche fio 3.x-CLI müssen im Labor validiert werden.
2. Native Windows-SMB/SMB Multichannel Benchmarks fehlen.
3. Kein vollständiges Qumulo-Node-Inventar, kein SLA-/Referenzkatalog und keine automatische Netzwerk-Engpassdiagnose.
4. Unterbrochene Runs werden teilweise gespeichert, ein granularer Resume ist noch nicht implementiert.
5. Es gibt noch keinen bootbaren USB-Stick und keine vollständig gebündelten Offline-Wheels.
6. Bei beschädigten Markern oder unbekannten Testdateien wird Cleanup vorsichtshalber verweigert.
7. Die dauerhafte Vergleichbarkeit muss anhand echter, wiederholter Installationsläufe validiert werden.

## Security Gate

Ein `run` verlangt `--confirm-write`, bestätigte Netzwerk-Mounts und (standardmäßig) einen funktionierenden Qumulo-Telemetrie-Zugang. Keine externen HTTP-Requests außer an die konfigurierte Qumulo-API. Kein Cloud-Upload, keine Übermittlung an Qumulo.

## Quellen (Stand 14.10.2026)

- https://docs.qumulo.com/administrator-guide/monitoring-and-metrics/openmetrics-api-specification.html
- https://fio.readthedocs.io/en/master/fio_doc.html
- https://man7.org/linux/man-pages/man8/mount.cifs.8.html
- https://docs.qumulo.com/administrator-guide/connecting-to-external-services/creating-using-access-tokens-to-authenticate-external-services-qumulo-core.html

## Maßnahmen v0.2 (14.10.2026)

- Zusätzliche Qualitätsschranke für Clusteridentität und Workload-Latenzperzentile.
- O_DIRECT-Readback-Check auf jedem Protocol-Mount vor der großen Datenvorbereitung.
- Mount-Wechsel zusätzlich mit Linux mountinfo-ID + Device-ID nachvollziehbar.
- PROTOCOL-Namespace für Run-Verzeichnis-ID, wenn NFS und SMB denselben Qumulo-Pfad zeigen.
- Telemetrie-Tokens dürfen nicht per Proxy/Redirect an andere Hosts wandern.
- Offline-Tool `validate-run` unterscheidet Vollständigkeit eines Reports von einer durch menschliche Laborprüfung bestätigten Freigabe.
