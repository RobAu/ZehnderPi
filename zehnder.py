#!/usr/bin/env python3
"""Zehnder ComfoFan RF tool for a Raspberry Pi with an nRF905 module.

Talks the Zehnder RF protocol (868.4 MHz, 16-byte payload, 4-byte address,
CRC-16). It can listen, pair with the fan unit as an extra remote control,
and then read and set the fan speed.

Protocol references:
  https://github.com/eelcohn/ZehnderComfoair
  https://github.com/Sanderhuisman/ESPHome-Zehnder-RF

Wiring (BCM numbers): SPI0 CE0 for SCK/MOSI/MISO/CSN, PWR_UP=23, TX_EN=24,
TRX_CE=25, DR=22.

Usage:
  ./zehnder.py info                 check that the radio answers
  ./zehnder.py listen               print frames on the link address (receive only)
  ./zehnder.py listen -n            print frames on the paired network (receive only)
  ./zehnder.py pair                 pair with the fan (within 10 min of powering it on)
  ./zehnder.py status               ask the fan for its current settings
  ./zehnder.py speed low|medium|high|max|auto
  ./zehnder.py timer high 30        run a speed for a number of minutes
  ./zehnder.py serve                run the HTTP API (see README)

While the HTTP API service is running it owns the radio, so the other
commands that use the radio will refuse to start; use the API instead.
"""

import argparse
import datetime
import http.server
import json
import pathlib
import random
import signal
import sys
import threading
import time
import urllib.parse

import lgpio
import spidev

PIN_PWR_UP = 23
PIN_TX_EN = 24
PIN_TRX_CE = 25
PIN_DR = 22

# nRF905 SPI instructions
W_CONFIG = 0x00
R_CONFIG = 0x10
W_TX_PAYLOAD = 0x20
W_TX_ADDRESS = 0x22
R_RX_PAYLOAD = 0x24

# Zehnder radio settings
CHANNEL = 118              # 868.4 MHz on the 868 MHz band
PAYLOAD_LEN = 16
LINK_ADDRESS = "A55A5AA5"  # address used while pairing
TTL = 250
REPLY_TIMEOUT = 1.0        # seconds to wait for an answer before sending again
RETRIES = 10

TYPE_FAN = 0x01
TYPE_REMOTE = 0x03

CMD_SET_SPEED = 0x02
CMD_SET_TIMER = 0x03
CMD_JOIN_REQUEST = 0x04
CMD_SET_SPEED_REPLY = 0x05
CMD_JOIN_OPEN = 0x06
CMD_FAN_SETTINGS = 0x07
CMD_LINK_OK = 0x0B
CMD_REMOTE_LINKING = 0x0C
CMD_QUERY_NETWORK = 0x0D
CMD_QUERY_DEVICE = 0x10

CONFIG_FILE = pathlib.Path(__file__).with_name("zehnder.json")
DEFAULT_PORT = 8765
STATUS_MAX_AGE = 30.0       # seconds before /status asks the fan again

DEVICE_TYPES = {
    0x00: "broadcast",
    0x01: "fan unit",
    0x03: "RFZ remote",
    0x16: "timer remote",
    0x18: "CO2 sensor",
}

COMMANDS = {
    0x01: "set voltage",
    0x02: "set speed",
    0x03: "set timer",
    0x04: "join request",
    0x05: "set speed reply",
    0x06: "fan unit open for linking",
    0x07: "fan settings",
    0x0B: "link successful",
    0x0C: "remote open for linking",
    0x0D: "query network",
    0x10: "query device",
    0x1D: "set voltage reply",
}

SPEEDS = {0: "auto", 1: "low", 2: "medium", 3: "high", 4: "max"}
SPEED_BY_NAME = {name: value for value, name in SPEEDS.items()}


def build_config(address: bytes) -> list[int]:
    """The 10-byte nRF905 configuration register for Zehnder reception."""
    return [
        CHANNEL & 0xFF,
        0x02 | (0b11 << 2) | ((CHANNEL >> 8) & 1),  # 868 MHz band, +10 dBm
        0x44,                                       # 4-byte RX and TX address
        PAYLOAD_LEN,
        PAYLOAD_LEN,
        *address,
        0xDB,                                       # CRC-16 on, 16 MHz crystal
    ]


class Radio:
    def __init__(self):
        self.spi = spidev.SpiDev()
        self.spi.open(0, 0)
        self.spi.mode = 0
        self.spi.max_speed_hz = 1_000_000
        self.chip = lgpio.gpiochip_open(0)
        # Standby: powered up, not receiving, not transmitting.
        lgpio.gpio_claim_output(self.chip, PIN_TX_EN, 0)
        lgpio.gpio_claim_output(self.chip, PIN_TRX_CE, 0)
        lgpio.gpio_claim_output(self.chip, PIN_PWR_UP, 1)
        lgpio.gpio_claim_input(self.chip, PIN_DR)
        time.sleep(0.01)

    def close(self):
        lgpio.gpio_write(self.chip, PIN_TRX_CE, 0)
        lgpio.gpio_write(self.chip, PIN_PWR_UP, 0)
        for pin in (PIN_TX_EN, PIN_TRX_CE, PIN_PWR_UP, PIN_DR):
            lgpio.gpio_free(self.chip, pin)
        lgpio.gpiochip_close(self.chip)
        self.spi.close()

    def read_config(self) -> list[int]:
        return self.spi.xfer2([R_CONFIG] + [0] * 10)[1:]

    def configure(self, address: bytes):
        """Write the config in standby and verify it by reading it back."""
        lgpio.gpio_write(self.chip, PIN_TRX_CE, 0)
        wanted = build_config(address)
        self.spi.xfer2([W_CONFIG] + wanted)
        got = self.read_config()
        if got != wanted:
            raise RuntimeError(
                "radio did not accept the configuration: wrote "
                + bytes(wanted).hex(" ") + ", read back " + bytes(got).hex(" ")
            )

    def set_address(self, address: bytes):
        """Use one address for both receiving and transmitting."""
        self.configure(address)
        self.spi.xfer2([W_TX_ADDRESS] + list(address))

    def transmit(self, payload: bytes):
        """Send one frame, then go back to receiving."""
        lgpio.gpio_write(self.chip, PIN_TRX_CE, 0)
        self.spi.xfer2([W_TX_PAYLOAD] + list(payload))
        lgpio.gpio_write(self.chip, PIN_TX_EN, 1)
        lgpio.gpio_write(self.chip, PIN_TRX_CE, 1)
        deadline = time.monotonic() + 0.1
        while not lgpio.gpio_read(self.chip, PIN_DR):
            if time.monotonic() > deadline:
                self.start_receiving()
                raise RuntimeError("the radio did not finish transmitting")
            time.sleep(0.0005)
        lgpio.gpio_write(self.chip, PIN_TRX_CE, 0)
        self.start_receiving()
        time.sleep(0.002)

    def start_receiving(self):
        lgpio.gpio_write(self.chip, PIN_TX_EN, 0)
        lgpio.gpio_write(self.chip, PIN_TRX_CE, 1)

    def receive(self) -> bytes | None:
        """Return a payload if one is waiting, otherwise None."""
        if not lgpio.gpio_read(self.chip, PIN_DR):
            return None
        lgpio.gpio_write(self.chip, PIN_TRX_CE, 0)
        payload = bytes(self.spi.xfer2([R_RX_PAYLOAD] + [0] * PAYLOAD_LEN)[1:])
        lgpio.gpio_write(self.chip, PIN_TRX_CE, 1)
        return payload


def describe(payload: bytes) -> str:
    rx_type, rx_id, tx_type, tx_id, ttl, command, count = payload[:7]
    params = payload[7:7 + min(count, 9)]

    def device(kind, ident):
        return f"{DEVICE_TYPES.get(kind, f'type 0x{kind:02X}')} #{ident:02X}"

    text = (
        f"{device(tx_type, tx_id)} -> {device(rx_type, rx_id)}  ttl={ttl}  "
        f"{COMMANDS.get(command, 'unknown command')} (0x{command:02X})"
    )
    if command == 0x02 and count >= 1:
        text += f": {SPEEDS.get(params[0], params[0])}"
    elif command == 0x03 and count >= 2:
        text += f": {SPEEDS.get(params[0], params[0])} for {params[1]} min"
    elif command == 0x07 and count >= 3:
        text += (
            f": speed {SPEEDS.get(params[0], params[0])}, {params[1]}% power, "
            f"timer {'on' if params[2] else 'off'}"
        )
    elif command in (0x04, 0x06, 0x0C) and count >= 4:
        text += f": network address bytes {params[:4].hex(' ').upper()}"
    elif params:
        text += f": params {params.hex(' ').upper()}"
    return text


def open_radio() -> Radio:
    try:
        return Radio()
    except lgpio.error as error:
        sys.exit(f"Cannot use the radio pins ({error}). If the HTTP API service is "
                 "running it owns the radio: use the API, or stop the service first.")


def cmd_info(_args):
    radio = open_radio()
    try:
        config = radio.read_config()
    finally:
        radio.close()
    print("nRF905 config register:", bytes(config).hex(" ").upper())
    if all(b == 0x00 for b in config) or all(b == 0xFF for b in config):
        sys.exit("The radio does not answer. Check the wiring.")
    print("The radio answers.")


def frame(rx_type, rx_id, tx_type, tx_id, command, params=b"") -> bytes:
    head = bytes([rx_type, rx_id, tx_type, tx_id, TTL, command, len(params)])
    return (head + bytes(params)).ljust(PAYLOAD_LEN, b"\x00")


def exchange(radio, payload, accept, retries=RETRIES, verbose=False):
    """Send a frame and wait for an answer that `accept` approves.

    Sends again when no answer comes within REPLY_TIMEOUT. Returns the answer,
    or None when every attempt went unanswered.
    """
    for _ in range(retries):
        radio.transmit(payload)
        if verbose:
            print("  sent     ", describe(payload))
        deadline = time.monotonic() + REPLY_TIMEOUT
        while time.monotonic() < deadline:
            answer = radio.receive()
            if answer is None:
                time.sleep(0.001)
                continue
            if verbose:
                print("  received ", describe(answer))
            if accept(answer):
                return answer
    return None


def load_pairing() -> dict:
    if not CONFIG_FILE.exists():
        sys.exit(f"Not paired yet: {CONFIG_FILE} does not exist. Run './zehnder.py pair' first.")
    return json.loads(CONFIG_FILE.read_text())


def settings_for(pairing):
    """Accept the fan's settings frame addressed to this remote."""
    def accept(p):
        return (p[5] == CMD_FAN_SETTINGS and p[0] == pairing["my_type"]
                and p[1] == pairing["my_id"])
    return accept


def print_settings(payload):
    speed, power, timer = payload[7], payload[8], payload[9]
    print(f"Fan speed: {SPEEDS.get(speed, speed)}, power {power}%, "
          f"timer {'on' if timer else 'off'}")


def cmd_listen(args):
    if args.network:
        args.address = load_pairing()["network"]
    address = bytes.fromhex(args.address)
    if len(address) != 4:
        sys.exit("The address must be 8 hex digits, for example A55A5AA5.")

    log = open(args.log, "a") if args.log else None

    def out(line):
        print(line, flush=True)
        if log:
            log.write(line + "\n")
            log.flush()

    radio = open_radio()
    count = 0
    try:
        radio.configure(address)
        radio.start_receiving()
        out(f"Listening on 868.4 MHz, address {address.hex().upper()}. "
            "Receive only. Ctrl-C to stop.")
        started = time.monotonic()
        while args.seconds is None or time.monotonic() - started < args.seconds:
            payload = radio.receive()
            if payload is None:
                time.sleep(0.001)
                continue
            count += 1
            stamp = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
            out(f"{stamp}  {payload.hex(' ').upper()}")
            out(f"              {describe(payload)}")
        out(f"Stopped after {args.seconds} s, {count} frame(s) received.")
    except KeyboardInterrupt:
        out(f"Stopped, {count} frame(s) received.")
    finally:
        radio.close()
        if log:
            log.close()


def cmd_pair(args):
    if CONFIG_FILE.exists() and not args.force:
        sys.exit(f"Already paired ({CONFIG_FILE}). Use 'pair --force' to pair again.")
    link = bytes.fromhex(LINK_ADDRESS)
    radio = open_radio()
    try:
        # 1. Announce ourselves on the link address and wait for the fan unit's
        #    "open for linking" frame, which carries its network address.
        radio.set_address(link)
        my_id = random.randint(1, 0xFE)
        print("Looking for a fan unit that is open for linking...")
        hello = frame(0x04, 0x00, TYPE_REMOTE, my_id, CMD_REMOTE_LINKING, link)
        found = exchange(radio, hello, lambda p: p[5] == CMD_JOIN_OPEN and p[6] >= 4,
                         verbose=args.verbose)
        if found is None:
            sys.exit("No fan unit answered. It is only open for linking during the "
                     "first 10 minutes after it is powered on.")
        fan_type, fan_id = found[2], found[3]
        network = found[7:11]
        while my_id == fan_id:
            my_id = random.randint(1, 0xFE)
        print(f"Found fan unit #{fan_id:02X} on network {network.hex().upper()}.")

        # 2. Ask to join that network; the fan answers "link successful".
        radio.set_address(network)
        request = frame(fan_type, fan_id, TYPE_REMOTE, my_id, CMD_JOIN_REQUEST, network)
        linked = exchange(
            radio, request,
            lambda p: p[5] == CMD_LINK_OK and p[:4] == bytes([TYPE_REMOTE, my_id, fan_type, fan_id]),
            verbose=args.verbose)
        if linked is None:
            sys.exit("The fan unit did not accept the join request.")

        pairing = {"network": network.hex().upper(), "my_type": TYPE_REMOTE,
                   "my_id": my_id, "fan_type": fan_type, "fan_id": fan_id}
        CONFIG_FILE.write_text(json.dumps(pairing, indent=2) + "\n")

        # 3. Acknowledge; the fan closes the handshake with a network query.
        ack = frame(fan_type, fan_id, TYPE_REMOTE, my_id, CMD_LINK_OK)
        done = exchange(
            radio, ack,
            lambda p: p[5] == CMD_QUERY_NETWORK and p[2] == fan_type and p[3] == fan_id,
            verbose=args.verbose)
        print(f"Paired as remote #{my_id:02X}; saved to {CONFIG_FILE}.")
        if done is None:
            print("Note: the fan's final confirmation was not seen. "
                  "Check with './zehnder.py status'.")
    finally:
        radio.close()


def send_to_fan(radio, pairing, payload, verbose=False):
    """Send a command on an open radio and return the fan's settings frame."""
    answer = exchange(radio, payload, settings_for(pairing), verbose=verbose)
    if answer is not None and payload[5] in (CMD_SET_SPEED, CMD_SET_TIMER):
        # The original remote confirms the fan's answer; do the same.
        radio.transmit(frame(pairing["fan_type"], pairing["fan_id"], pairing["my_type"],
                             pairing["my_id"], CMD_SET_SPEED_REPLY, b"\x54\x03\x20"))
        time.sleep(0.02)
    return answer


def run_command(pairing, payload, verbose):
    """Open the radio, send one command to the paired fan, close the radio."""
    radio = open_radio()
    try:
        radio.set_address(bytes.fromhex(pairing["network"]))
        radio.start_receiving()
        return send_to_fan(radio, pairing, payload, verbose)
    finally:
        radio.close()


def to_fan(pairing, command, params=b""):
    return frame(pairing["fan_type"], pairing["fan_id"], pairing["my_type"],
                 pairing["my_id"], command, params)


def finish(answer):
    if answer is None:
        sys.exit("The fan did not answer.")
    print_settings(answer)


def cmd_status(args):
    pairing = load_pairing()
    finish(run_command(pairing, to_fan(pairing, CMD_QUERY_DEVICE), args.verbose))


def cmd_speed(args):
    pairing = load_pairing()
    params = bytes([SPEED_BY_NAME[args.speed]])
    finish(run_command(pairing, to_fan(pairing, CMD_SET_SPEED, params), args.verbose))


def cmd_timer(args):
    pairing = load_pairing()
    if not 1 <= args.minutes <= 255:
        sys.exit("Minutes must be between 1 and 255.")
    params = bytes([SPEED_BY_NAME[args.speed], args.minutes])
    finish(run_command(pairing, to_fan(pairing, CMD_SET_TIMER, params), args.verbose))


class FanService:
    """Keeps the radio open and the last known fan settings up to date."""

    def __init__(self, pairing, verbose=False):
        self.pairing = pairing
        self.verbose = verbose
        self.lock = threading.Lock()   # guards the radio
        self.settings = None
        self.updated = 0.0
        self.stopping = False
        self.radio = open_radio()
        self.radio.set_address(bytes.fromhex(pairing["network"]))
        self.radio.start_receiving()

    def close(self):
        self.stopping = True
        with self.lock:
            self.radio.close()

    def remember(self, payload):
        speed = payload[7]
        self.settings = {"speed": SPEEDS.get(speed, str(speed)), "speed_value": speed,
                         "power": payload[8], "timer": bool(payload[9])}
        self.updated = time.time()

    def snapshot(self):
        return {**self.settings, "age": round(time.time() - self.updated, 1)}

    def command(self, command, params=b""):
        """Send a command to the fan. Returns the settings, or None if it stays silent."""
        with self.lock:
            answer = send_to_fan(self.radio, self.pairing,
                                 to_fan(self.pairing, command, params), self.verbose)
        if answer is None:
            return None
        self.remember(answer)
        return self.snapshot()

    def status(self, max_age):
        if self.settings is None or time.time() - self.updated > max_age:
            return self.command(CMD_QUERY_DEVICE)
        return self.snapshot()

    def watch(self):
        """Pick up settings the fan reports to other remotes."""
        fan = bytes([self.pairing["fan_type"], self.pairing["fan_id"]])
        while not self.stopping:
            with self.lock:
                payload = None if self.stopping else self.radio.receive()
            if (payload and payload[5] == CMD_FAN_SETTINGS and payload[2:4] == fan
                    and payload[6] >= 3):
                self.remember(payload)
            time.sleep(0.01)


def make_handler(service):
    pairing = service.pairing

    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "zehnder"

        def reply(self, code, body):
            data = (json.dumps(body) + "\n").encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def fan_reply(self, settings):
            if settings is None:
                self.reply(502, {"error": "the fan did not answer"})
            else:
                self.reply(200, settings)

        def do_GET(self):
            url = urllib.parse.urlsplit(self.path)
            if url.path == "/":
                self.reply(200, {
                    "name": "Zehnder ComfoFan",
                    "id": f"{pairing['network']}-{pairing['fan_id']:02X}",
                    "speeds": list(SPEED_BY_NAME),
                    "endpoints": ["GET /status", "POST /speed/<speed>",
                                  "POST /timer/<speed>/<minutes>"],
                })
            elif url.path == "/status":
                query = urllib.parse.parse_qs(url.query)
                try:
                    max_age = float(query.get("max_age", [STATUS_MAX_AGE])[0])
                except ValueError:
                    return self.reply(400, {"error": "max_age must be a number of seconds"})
                self.fan_reply(service.status(max_age))
            else:
                self.reply(404, {"error": "unknown path"})

        def do_POST(self):
            parts = urllib.parse.urlsplit(self.path).path.strip("/").split("/")
            if parts[0] not in ("speed", "timer"):
                return self.reply(404, {"error": "unknown path"})
            if len(parts) < 2 or parts[1] not in SPEED_BY_NAME:
                return self.reply(400, {"error": "speed must be one of " + ", ".join(SPEED_BY_NAME)})
            speed = SPEED_BY_NAME[parts[1]]
            if parts[0] == "speed" and len(parts) == 2:
                self.fan_reply(service.command(CMD_SET_SPEED, bytes([speed])))
            elif parts[0] == "timer" and len(parts) == 3 and parts[2].isdigit() \
                    and 1 <= int(parts[2]) <= 255:
                self.fan_reply(service.command(CMD_SET_TIMER, bytes([speed, int(parts[2])])))
            else:
                self.reply(400, {"error": "use /speed/<speed> or /timer/<speed>/<minutes 1-255>"})

    return Handler


def cmd_serve(args):
    service = FanService(load_pairing(), args.verbose)
    server = http.server.ThreadingHTTPServer((args.host, args.port), make_handler(service))
    server.daemon_threads = True

    def stop(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    threading.Thread(target=service.watch, daemon=True).start()
    print(f"Zehnder API listening on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        service.close()
        server.server_close()


def main():
    parser = argparse.ArgumentParser(description="Zehnder ComfoFan RF tool (nRF905)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="show every frame sent and received")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("info", help="check that the radio answers").set_defaults(func=cmd_info)

    listen = sub.add_parser("listen", help="print every Zehnder frame received")
    listen.add_argument("-a", "--address", default=LINK_ADDRESS,
                        help="4-byte address in hex (default: link address %(default)s)")
    listen.add_argument("-n", "--network", action="store_true",
                        help="listen on the paired network address")
    listen.add_argument("-s", "--seconds", type=float, default=None,
                        help="stop after this many seconds (default: run until Ctrl-C)")
    listen.add_argument("-l", "--log", help="also append the output to this file")
    listen.set_defaults(func=cmd_listen)

    pair = sub.add_parser("pair", help="pair with a fan unit that was just powered on")
    pair.add_argument("--force", action="store_true", help="replace an existing pairing")
    pair.set_defaults(func=cmd_pair)

    sub.add_parser("status", help="show the fan's current settings").set_defaults(func=cmd_status)

    speed = sub.add_parser("speed", help="set the fan speed")
    speed.add_argument("speed", choices=list(SPEED_BY_NAME))
    speed.set_defaults(func=cmd_speed)

    timer = sub.add_parser("timer", help="run a speed for a number of minutes")
    timer.add_argument("speed", choices=list(SPEED_BY_NAME))
    timer.add_argument("minutes", type=int)
    timer.set_defaults(func=cmd_timer)

    serve = sub.add_parser("serve", help="run the HTTP API")
    serve.add_argument("--host", default="0.0.0.0", help="address to listen on (default: all)")
    serve.add_argument("--port", type=int, default=DEFAULT_PORT, help="default: %(default)s")
    serve.set_defaults(func=cmd_serve)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
