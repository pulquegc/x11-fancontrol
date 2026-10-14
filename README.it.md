<p align="center"><img src="docs/icon.svg" alt="X11 Fan Control icon" width="128"></p>

<h1 align="center">X11 Fan Control</h1>

[English](README.md) · **Italiano**

Un controller per le ventole delle **schede madri Supermicro X11** su **TrueNAS SCALE**, distribuito come container Docker con una piccola interfaccia web.

Legge la temperatura della CPU dal BMC e quella dei dischi con `smartctl`, calcola la velocità di ogni zona ventole IPMI con curve configurabili e la imposta con i comandi IPMI raw di Supermicro. I dischi in standby non vengono mai risvegliati.

<!-- ![Interfaccia web](docs/screenshot.png) -->

## Funzionalità

- **Due zone ventole**: zona 0 (`FAN1`–`FANx`, CPU e sistema) e zona 1 (`FANA`, periferica, il posto tipico per le ventole dei dischi).
- **Curve separate** per CPU, HDD, SSD e NVMe. Ogni zona segue una qualsiasi combinazione e usa il valore più alto.
- **Comportamento morbido**: le ventole accelerano subito e rallentano gradualmente, con isteresi, senza continui sbalzi.
- **Primo avvio in sola lettura**: l'app parte in modalità `dry_run` e mostra solo cosa *farebbe*, finché non attivi il controllo.
- **Failsafe**: errori di lettura ripetuti portano le ventole al massimo; fermando il container viene ripristinata la modalità ventole originale del BMC.
- **Interfaccia web** (porta 8765): temperature e giri in tempo reale, grafico dell'ultima ora, editor delle curve ed editor JSON avanzato. Funziona da telefono e segue il tema scuro del sistema.
- **Nessuna dipendenza** oltre a `ipmitool`, `smartmontools` e la libreria standard di Python.

## Hardware supportato

| Scheda | Stato |
|---|---|
| Supermicro X11SCA-F | Scheda di riferimento, in test |
| Altre Supermicro X11 con IPMI | Dovrebbero funzionare (stessi comandi raw), non testate |
| X9 / X10 | Potrebbero funzionare, non testate |
| Schede non Supermicro | Non supportate |

Se lo provi su un'altra scheda, apri una issue con l'output di `ipmitool sdr type Fan` e dicci se funziona.

## Installazione su TrueNAS SCALE (24.10+)

1. **Crea un dataset** per la configurazione, ad esempio `tank/fancontrol`.
2. Vai in **Apps → Discover Apps → ⋮ → Install via YAML**, chiama l'app `fancontrol` e incolla:

   ```yaml
   services:
     fancontrol:
       image: ghcr.io/pulquegc/x11-fancontrol:latest
       container_name: x11-fancontrol
       restart: unless-stopped
       privileged: true
       stop_grace_period: 30s
       environment:
         TZ: Europe/Rome
         # UI_TOKEN: "cambiami"
       ports:
         - "8765:8765"
       volumes:
         - /mnt/tank/fancontrol:/data
   ```

   Sostituisci il percorso del volume con quello del tuo dataset.
3. Apri `http://IP-DEL-NAS:8765`. L'indicatore mostra **Read-only**.
4. Lascialo in sola lettura per un po' (meglio se durante uno scrub o un trasferimento grosso), verifica che le temperature di CPU e dischi siano corrette, regola le curve e premi **Enable control**.

`privileged: true` serve per accedere a `/dev/ipmi0` e leggere i dati SMART dei dischi.

## Come funziona

- Ogni `interval` secondi legge i sensori di temperatura e ventole del BMC (`ipmitool sdr`).
- Ogni `disk_interval` secondi cerca i dischi con `smartctl --scan` e ne legge la temperatura con `smartctl -n standby`, classificandoli come HDD, SSD o NVMe.
- Per ogni zona valuta le curve delle sorgenti associate (interpolazione lineare tra i punti) e tiene il valore più alto, entro i limiti `min_duty`/`max_duty`.
- Con il controllo attivo porta il BMC in modalità **Full** (così smette di sovrascrivere i valori) e imposta ogni zona con `ipmitool raw 0x30 0x70 0x66 0x01 <zona> <duty>`.
- Allo spegnimento ripristina la modalità trovata all'avvio (oppure `restore_mode_on_exit`, di default **Optimal**).

## Configurazione

Le impostazioni sono in `/data/config.json` e si modificano dall'interfaccia. Le principali:

| Chiave | Default | Significato |
|---|---|---|
| `dry_run` | `true` | Sola lettura: calcola ma non tocca le ventole |
| `interval` | `10` | Secondi tra un ciclo e l'altro |
| `disk_interval` | `60` | Secondi tra una lettura dei dischi e l'altra |
| `hysteresis` | `2` | °C di cui deve scendere la temperatura prima di rallentare |
| `max_step_down` | `5` | Riduzione massima della velocità (%) per ciclo |
| `failsafe_duty` | `100` | Velocità applicata dopo errori ripetuti |
| `failsafe_after_errors` | `3` | Errori consecutivi prima del failsafe |
| `restore_mode_on_exit` | `2` | Modalità BMC all'uscita se quella originale era Full (0 Standard, 1 Full, 2 Optimal, 4 Heavy IO) |
| `cpu_sensor` | `"CPU Temp"` | Sensore BMC usato per la temperatura CPU |
| `disks_exclude` | `[]` | Parti di percorso dei dischi da ignorare, es. `["/dev/sdz"]` |
| `fan_lower_thresholds` | `null` | Soglie basse RPM del BMC applicate all'attivazione, es. `[100, 200, 300]` |
| `zones` | vedi file | Nome, `sources`, `min_duty`, `max_duty` di ogni zona |
| `curves` | vedi file | `[[temp_c, duty_pct], ...]` per `cpu`, `hdd`, `ssd`, `nvme` |

### Variabili d'ambiente

| Variabile | Default | Significato |
|---|---|---|
| `PORT` | `8765` | Porta dell'interfaccia web |
| `DATA_DIR` | `/data` | Dove viene salvato `config.json` |
| `UI_TOKEN` | *(vuoto)* | Se impostato, serve per salvare dall'interfaccia |
| `IPMI_HOST`, `IPMI_USER`, `IPMI_PASS` | *(vuoti)* | Usa un BMC remoto via rete invece di `/dev/ipmi0` |

## Risoluzione dei problemi

**Le ventole passano di continuo da lente al massimo.** È il classico problema Supermicro: se una ventola scende sotto la soglia minima di giri del BMC, il BMC la considera guasta e porta tutto al massimo. Succede facilmente con ventole silenziose (es. Noctua). Imposta `fan_lower_thresholds` sotto la velocità minima delle tue ventole, ad esempio `[100, 200, 300]`, poi disattiva e riattiva il controllo per applicarle.

**`Sensor 'CPU Temp' not found`.** Esegui `ipmitool sdr type Temperature` sul NAS e metti il nome corretto in `cpu_sensor`.

**Mancano dei dischi.** Verifica che il container giri con `privileged: true`. I dischi in standby compaiono come tali e vengono ignorati finché non si risvegliano.

## Sicurezza

L'interfaccia web non ha login. Tienila nella rete di casa e imposta `UI_TOKEN` se altre persone possono raggiungerla. Il container ha accesso privilegiato al BMC e ai dischi, quindi usa solo immagini di cui ti fidi.

## Avvertenze

Questo software cambia il modo in cui il server viene raffreddato. È fornito così com'è, senza garanzie. Tieni attivi gli avvisi di temperatura di TrueNAS e controlla le temperature dopo aver attivato il controllo.

## Licenza

[MIT](LICENSE)
