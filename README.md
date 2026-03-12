# Zonaprop Server Monitor

Bot de monitoreo de propiedades en [Zonaprop](https://www.zonaprop.com.ar). Revisa periódicamente búsquedas configuradas y envía notificaciones por email cuando detecta nuevas publicaciones.

## Funcionalidades

- **Monitoreo de múltiples búsquedas** simultáneamente, cada una con su email de destino
- **Detección de publicaciones nuevas** comparando contra un estado persistente
- **Notificaciones por email** en HTML con foto, precio, ambientes y superficie
- **Anti-bot**: usa `curl_cffi` para impersonar Chrome 120 y evitar bloqueos de Cloudflare
- **Reintentos con backoff exponencial** ante fallos de red o respuestas 403
- **Soporte SMTP dual**: intenta STARTTLS (587) y cae a SSL (465) automáticamente
- **Ejecución via cron** configurable (por defecto cada 60 minutos)

## Requisitos

- Python 3.6+
- `python3-venv`
- Cuenta de email con acceso SMTP (Gmail con contraseña de aplicación recomendado)

## Instalación rápida

```bash
chmod +x setup_server.sh
./setup_server.sh
```

El script interactivo:

1. Pide la configuración SMTP (host, puerto, usuario, contraseña)
2. Permite agregar una o más búsquedas de Zonaprop para monitorear
3. Crea un entorno virtual de Python e instala dependencias (`beautifulsoup4`, `curl_cffi`)
4. Configura un cron job para ejecución periódica

Si ya existe un `config.json`, el setup ofrece reutilizarlo.

## Configuración manual

Copiar el template y editarlo:

```bash
cp config.example.json config.json
```

Estructura de `config.json`:

```json
{
  "smtp": {
    "host": "smtp.gmail.com",
    "port": 587,
    "username": "tu_email@gmail.com",
    "password": "tu_app_password"
  },
  "monitors": [
    {
      "name": "Departamentos Palermo",
      "url": "https://www.zonaprop.com.ar/departamentos-alquiler-palermo-...",
      "notify_email": "destinatario@gmail.com"
    }
  ]
}
```

| Campo | Descripción |
|---|---|
| `smtp.host` | Servidor SMTP (ej: `smtp.gmail.com`) |
| `smtp.port` | Puerto SMTP (`587` para STARTTLS, `465` para SSL) |
| `smtp.username` | Email de envío |
| `smtp.password` | Contraseña o app password |
| `monitors[].name` | Nombre identificatorio del monitor |
| `monitors[].url` | URL completa de búsqueda en Zonaprop |
| `monitors[].notify_email` | Email que recibe las notificaciones |

### Gmail

Para usar Gmail como SMTP, necesitás generar una [contraseña de aplicación](https://myaccount.google.com/apppasswords) (requiere verificación en 2 pasos activada).

## Ejecución manual

```bash
./venv/bin/python3 monitor.py
```

## Estructura de archivos

```
zonaprop-server/
├── setup_server.sh        # Script de instalación interactivo
├── monitor.py             # Aplicación principal
├── config.json            # Configuración (no versionado)
├── config.example.json    # Template de configuración
├── monitor.log            # Logs de la aplicación
├── cron.log               # Logs de ejecución cron
├── venv/                  # Entorno virtual Python
└── data/                  # Estado persistente por monitor
    └── {nombre_monitor}.json
```

## Cómo funciona

1. Lee `config.json` y recorre cada monitor configurado
2. Hace un request a la URL de búsqueda impersonando Chrome 120
3. Parsea el HTML buscando datos JSON de Next.js (`__NEXT_DATA__`), con fallback a parsing HTML clásico
4. Extrae de cada publicación: ID, título, precio, URL, foto, ambientes y m²
5. Compara contra publicaciones ya vistas (almacenadas en `data/`)
6. Si hay publicaciones nuevas, envía un email HTML con los detalles
7. Actualiza el archivo de estado con las publicaciones actuales
8. Espera 10 segundos antes de procesar el siguiente monitor

## Logs

- `monitor.log` — Log principal de la aplicación (errores, publicaciones encontradas, emails enviados)
- `cron.log` — Salida stdout/stderr de las ejecuciones via cron
