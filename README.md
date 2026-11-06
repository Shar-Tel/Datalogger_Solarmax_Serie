# Datalogger Solarmax por puerto serie

Este proyecto consulta inversores Solarmax a través de un puerto serie (RS-485), guarda las lecturas en SQLite y las publica en un broker MQTT. Las lecturas se realizan a los cuartos de hora: minuto 00, 15, 30 y 45. Al iniciar, se puede pulsar cualquier tecla durante los primeros 10 segundos para hacer una lectura inmediata.

## Requisitos

- Python 3.
- Linux u otro sistema POSIX: el programa usa `termios` y `tty` para detectar la tecla al inicio.
- Acceso al puerto serie conectado a los inversores.
- Un broker MQTT accesible desde el equipo.
- Paquetes Python:

  ```bash
  python3 -m pip install paho-mqtt pyserial
  ```

## Configuración

El programa lee `configSerie.xml` y `config.cfg` desde el directorio de trabajo actual. Ejecútalo desde la carpeta que contiene ambos archivos.

### `configSerie.xml`

Define el nombre de la planta, el puerto serie y los inversores que se consultarán:

```xml
<Planta id="1" Nombre="MiPlanta" Puerto="/dev/ttyUSB0">
    <Inverter>
        <Numero>1,2</Numero>
        <Tipo>Solarmax</Tipo>
    </Inverter>
</Planta>
```

- `Nombre`: identificador de planta incluido en cada mensaje.
- `Puerto`: dispositivo serie del adaptador RS-485; ajústalo al equipo, por ejemplo `/dev/ttyUSB0`.
- `Numero`: dirección o direcciones de inversor separadas por comas.
- `Tipo`: se conserva en la configuración, pero este script no lo utiliza.

Adapta los valores de ejemplo a la instalación real. El usuario que ejecute el proceso debe tener permiso para acceder al dispositivo serie.

### `config.cfg`

Configura las credenciales y conexión MQTT. No guardes este archivo en repositorios públicos ni compartas sus credenciales.

```ini
[MQTT]
nombre = identificador_mqtt
broker_address = mqtt.ejemplo.local
port = 1883
user = usuario
password = contraseña

[General]
pausa = 10
```

El programa utiliza `broker_address`, `port`, `user` y `password` de la sección `[MQTT]`. `nombre` y `pausa` no se usan actualmente.

## Ejecución

Desde el directorio del proyecto:

```bash
python3 mqttDataSerie_BD.py
```

El programa permanece en ejecución y consulta los inversores en los minutos programados. Para detenerlo, pulsa `Ctrl+C`.

## Funcionamiento y datos

1. Consulta cada inversor a 19200 baudios con configuración 8N1.
2. Convierte la respuesta Solarmax a un objeto JSON con metadatos de planta, línea, inversor y fecha.
3. Guarda las lecturas en `Lecturas.sqlite`, en la tabla `Lecturas_guardadas_inverters_Solarmax`.
4. Publica los registros pendientes en el tema MQTT `General/datos`, con QoS 2.
5. Elimina una lectura de SQLite solo cuando la publicación se confirma; si falla o vence la espera, la conserva para un intento posterior.

Los mensajes incluyen, según la respuesta del inversor, medidas como energía diaria (`KDY`), potencia AC (`PAC`), energía mensual (`KMT`), alarmas (`SAL`), estado (`SYS`), corrientes y tensiones. El campo `KDY` se pone a cero durante la hora 00:00 para compensar el reinicio tardío del contador diario en el inversor 31.

La tabla de cola debe existir en la base de datos y tener al menos estas columnas:

```sql
CREATE TABLE Lecturas_guardadas_inverters_Solarmax (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dato TEXT NOT NULL
);
```

El programa no crea automáticamente la tabla. La base de datos se abre usando la ruta relativa `Lecturas.sqlite`, por lo que la ubicación efectiva depende del directorio desde el que se inicia.

## Problemas habituales

- **No se abre el puerto serie:** comprueba `Puerto`, el adaptador y los permisos del usuario.
- **No hay lecturas de los inversores:** verifica la conexión RS-485, las direcciones de `Numero` y el bus.
- **No se conecta a MQTT:** revisa host, puerto y credenciales en `[MQTT]`. Las lecturas se mantienen en SQLite mientras no se confirmen.
- **Faltan dependencias:** instala `paho-mqtt` y `pyserial` en el mismo entorno Python con el que ejecutas el programa.
