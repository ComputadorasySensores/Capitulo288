# main.py - Raspberry Pi Pico 2 W + BMP280 -> NATS -> WireClaw (ESP32-S3)
# Publica temperatura (y presion) como texto plano cada pocos segundos.
# WireClaw las lee como sensor "nats_value".

import network, socket, struct, time, json, errno, machine
from machine import Pin, I2C

# ================= CONFIGURACION =================
WIFI_SSID = "Tu_red_WiFi"
WIFI_PASS = "Tu_Contraseña"

NATS_HOST = "IP del servidor NAS"   # IP del servidor NATS (el MISMO que usa WireClaw)
NATS_PORT = 4222
NATS_USER = None             # si tu servidor NATS usa usuario/clave
NATS_PASS = None

SUBJECT_TEMP  = "home.pico.temp"
SUBJECT_PRESS = "home.pico.pressure"
PUBLISH_EVERY_MS = 5000
# ==================================================


# ---------- Driver BMP280 minimo ----------
class BMP280:
    def __init__(self, i2c, addr=None):
        self.i2c = i2c
        if addr is None:
            found = i2c.scan()
            if 0x76 in found:
                addr = 0x76
            elif 0x77 in found:
                addr = 0x77
            else:
                raise OSError("BMP280 no encontrado en I2C (revisa SDA=GP0, SCL=GP1)")
        self.addr = addr
        chip = i2c.readfrom_mem(addr, 0xD0, 1)[0]
        if chip not in (0x58, 0x56, 0x57, 0x60):  # BMP280 / BME280
            raise OSError("Chip ID inesperado: 0x%02X" % chip)
        c = i2c.readfrom_mem(addr, 0x88, 24)
        (self.T1, self.T2, self.T3,
         self.P1, self.P2, self.P3, self.P4, self.P5,
         self.P6, self.P7, self.P8, self.P9) = struct.unpack("<HhhHhhhhhhhh", c)
        i2c.writeto_mem(addr, 0xF5, b"\xA0")  # standby 1s, sin filtro
        i2c.writeto_mem(addr, 0xF4, b"\x57")  # T x2, P x16, modo normal
        time.sleep_ms(100)

    def read(self):
        d = self.i2c.readfrom_mem(self.addr, 0xF7, 6)
        adc_p = (d[0] << 12) | (d[1] << 4) | (d[2] >> 4)
        adc_t = (d[3] << 12) | (d[4] << 4) | (d[5] >> 4)

        # Temperatura (formula del datasheet Bosch)
        v1 = (adc_t / 16384.0 - self.T1 / 1024.0) * self.T2
        v2 = ((adc_t / 131072.0 - self.T1 / 8192.0) ** 2) * self.T3
        t_fine = v1 + v2
        temp = t_fine / 5120.0

        # Presion
        v1 = t_fine / 2.0 - 64000.0
        v2 = v1 * v1 * self.P6 / 32768.0
        v2 = v2 + v1 * self.P5 * 2.0
        v2 = v2 / 4.0 + self.P4 * 65536.0
        v1 = (self.P3 * v1 * v1 / 524288.0 + self.P2 * v1) / 524288.0
        v1 = (1.0 + v1 / 32768.0) * self.P1
        if v1 == 0:
            return temp, 0.0
        p = 1048576.0 - adc_p
        p = (p - v2 / 4096.0) * 6250.0 / v1
        v1 = self.P9 * p * p / 2147483648.0
        v2 = p * self.P8 / 32768.0
        p = p + (v1 + v2 + self.P7) / 16.0
        return temp, p / 100.0  # C, hPa


# ---------- WiFi ----------
wlan = network.WLAN(network.STA_IF)

def wifi_connect():
    wlan.active(True)
    try:
        wlan.config(pm=network.WLAN.PM_NONE)  # mejor latencia/estabilidad
    except Exception:
        pass
    if wlan.isconnected():
        return
    print("WiFi: conectando a", WIFI_SSID)
    wlan.connect(WIFI_SSID, WIFI_PASS)
    t0 = time.ticks_ms()
    while not wlan.isconnected():
        if time.ticks_diff(time.ticks_ms(), t0) > 20000:
            raise OSError("Timeout WiFi")
        time.sleep_ms(250)
    print("WiFi OK:", wlan.ifconfig()[0])


# ---------- Cliente NATS minimo (protocolo de texto) ----------
def nats_connect():
    addr = socket.getaddrinfo(NATS_HOST, NATS_PORT)[0][-1]
    print("NATS: conectando a", addr)
    s = socket.socket()
    s.settimeout(8)
    s.connect(addr)

    # 1) El servidor habla primero: lee su INFO
    raw = s.recv(1024)
    print("NATS INFO:", raw)
    if not raw.startswith(b"INFO"):
        raise OSError("Ese puerto no responde como NATS: %s" % raw)
    try:
        info = json.loads(raw[5:].split(b"\r\n")[0])
    except Exception:
        info = {}
    if info.get("tls_required") or info.get("tls_available") and info.get("tls_required"):
        raise OSError("El servidor NATS exige TLS (no soportado en este script)")
    if info.get("auth_required") and not NATS_USER:
        raise OSError("El servidor NATS exige usuario/clave: completa NATS_USER y NATS_PASS")

    # 2) Responde con CONNECT + PING y espera PONG
    opts = {"verbose": False, "pedantic": False, "name": "pico2w-bmp280",
            "lang": "micropython", "version": "1.0", "protocol": 0,
            "tls_required": False}
    if NATS_USER:
        opts["user"] = NATS_USER
        opts["pass"] = NATS_PASS
    s.send(("CONNECT " + json.dumps(opts) + "\r\n").encode())
    s.send(b"PING\r\n")
    buf = b""
    while b"PONG" not in buf:
        chunk = s.recv(512)
        if not chunk:
            raise OSError("NATS cerro la conexion. Ultima respuesta: %s" % buf)
        buf += chunk
        if b"-ERR" in buf:
            raise OSError("NATS error: %s" % buf)
    s.settimeout(0)                  # no bloqueante a partir de aqui
    print("NATS OK:", NATS_HOST)
    return s

def nats_service(s):
    """Atiende PING del servidor y detecta desconexiones."""
    try:
        data = s.recv(512)
    except OSError as e:
        if e.args and e.args[0] in (errno.EAGAIN, errno.ETIMEDOUT):
            return
        raise
    if data is None:
        return
    if not data:
        raise OSError("NATS desconectado")
    if b"PING" in data:
        s.send(b"PONG\r\n")
    if b"-ERR" in data:
        raise OSError("NATS error: %s" % data)

def nats_pub(s, subject, payload):
    if isinstance(payload, str):
        payload = payload.encode()
    s.send(b"PUB %s %d\r\n%s\r\n" % (subject.encode(), len(payload), payload))


# ---------- Programa principal ----------
def main():
    i2c = I2C(0, sda=Pin(0), scl=Pin(1), freq=100000)
    sensor = BMP280(i2c)
    fails = 0

    while True:
        s = None
        try:
            wifi_connect()
            s = nats_connect()
            fails = 0
            last = time.ticks_add(time.ticks_ms(), -PUBLISH_EVERY_MS)  # publica ya

            while True:
                if not wlan.isconnected():
                    raise OSError("WiFi perdido")
                nats_service(s)

                if time.ticks_diff(time.ticks_ms(), last) >= PUBLISH_EVERY_MS:
                    t, p = sensor.read()
                    nats_pub(s, SUBJECT_TEMP, "%.1f" % t)
                    nats_pub(s, SUBJECT_PRESS, "%.1f" % p)
                    print("T=%.1f C  P=%.1f hPa" % (t, p))
                    last = time.ticks_ms()

                time.sleep_ms(200)

        except Exception as e:
            print("Error:", e)
            fails += 1
            try:
                if s:
                    s.close()
            except Exception:
                pass
            if fails >= 10:
                machine.reset()
            time.sleep(5)


main()
