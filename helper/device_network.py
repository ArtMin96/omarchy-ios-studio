"""Network tab helpers shared by the real and the demo device daemon (stdlib only, no side effects).

* Conditions (DVT condition inducer): which profiles are safe over which transport, and the view
  of the phone's `availableConditionInducers` list that condition.list returns.
* Packet capture: per-packet bookkeeping and the summary computed when a capture stops. It reads
  only pcapd's per-packet header (process, interface) and the IP/TCP/UDP headers of each frame,
  never the payload.
* A tiny pcapng writer for the demo's made-up capture.

Protocol: docs/deviced-protocol.md#network-conditions and #packet-capture.
"""
from __future__ import annotations

import ipaddress
import re
import struct
import time
from collections import Counter

# ====================================================================== conditions

NETWORK_GROUP = "SlowNetworkCondition"
# These cut the phone's network almost completely. Over Wi-Fi that includes the link to this
# computer, so the reset could never be sent: refused over Wi-Fi.
WIFI_BLOCKED = {"SlowNetwork100PctLoss", "SlowNetworkVeryBadNetwork"}
DEFAULT_TIMEOUT_S = 300
MIN_TIMEOUT_S = 10
MAX_TIMEOUT_S = 1800

KINDS = {"SlowNetworkCondition": "network", "ThermalCondition": "thermal", "GPUPerformanceState": "gpu"}

WIFI_WARNING = ("Your iPhone is connected over Wi-Fi, so this also slows down the connection to this computer. "
                "iOS Studio may lose the phone for a while; the condition then switches off by itself.")
WIFI_BLOCKED_TEXT = ("This one would cut off the Wi-Fi link to this computer, so it couldn't be switched off again. "
                     "Connect your iPhone with a USB cable to use it.")


def group_kind(group_id: str) -> str:
    return KINDS.get(group_id, "other")


def wifi_safety(group_id: str, profile_id: str) -> str:
    """'ok' (fine on any transport), 'warn' (network: allowed over Wi-Fi after a warning) or 'blocked'."""
    if group_id != NETWORK_GROUP:
        return "ok"
    return "blocked" if profile_id in WIFI_BLOCKED else "warn"


_DETAIL = re.compile(r"^\s*([^:]+):\s*(.+?)\s*$")


def parse_details(description: str) -> dict:
    """'Name: …\\nDownlink Bandwidth: 0.24 Mbps\\n…' -> {'Downlink Bandwidth': '0.24 Mbps', …} (Name dropped)."""
    out: dict = {}
    for line in str(description or "").splitlines():
        m = _DETAIL.match(line)
        if m and m.group(1).strip().lower() != "name":
            out[m.group(1).strip()] = m.group(2)
    return out


def short_summary(details: dict, description: str) -> str:
    """One line for a picker: '0.24 Mbps down · 0.2 Mbps up · 400 ms · 1% loss'."""
    if not details:
        return str(description or "").strip().split("\n")[0][:160]
    parts = []
    if "Downlink Bandwidth" in details:
        parts.append(details["Downlink Bandwidth"] + " down")
    if "Uplink Bandwidth" in details:
        parts.append(details["Uplink Bandwidth"] + " up")
    if "Downlink Latency" in details:
        parts.append(details["Downlink Latency"] + " latency")
    loss = details.get("Downlink Packet Loss Ratio")
    if loss and loss not in ("0%", "0.0%"):
        parts.append(loss + " loss")
    if "DNS Delay" in details:
        parts.append(details["DNS Delay"] + " DNS delay")
    return " · ".join(parts) or "; ".join(f"{k} {v}" for k, v in details.items())[:160]


def profile_allowed(group_id: str, profile_id: str, transport: str | None) -> tuple[bool, str]:
    """(allowed now, note). Over USB (or unknown) everything is allowed."""
    safety = wifi_safety(group_id, profile_id)
    if transport != "wifi" or safety == "ok":
        return True, ""
    if safety == "blocked":
        return False, WIFI_BLOCKED_TEXT
    return True, WIFI_WARNING


def view_groups(raw: list, transport: str | None) -> list[dict]:
    """The phone's list -> [{id, name, kind, phoneActive, activeProfile, profiles: [...]}]; internal groups dropped."""
    out = []
    for g in raw or []:
        if not isinstance(g, dict) or g.get("isInternal"):
            continue
        gid = str(g.get("identifier") or "")
        profiles = []
        for p in g.get("profiles") or []:
            if not isinstance(p, dict):
                continue
            pid = str(p.get("identifier") or "")
            desc = str(p.get("description") or "")
            det = parse_details(desc)
            allowed, note = profile_allowed(gid, pid, transport)
            profiles.append({"id": pid, "name": str(p.get("name") or pid), "summary": short_summary(det, desc),
                             "details": det, "description": desc if not det else "",
                             "wifi": wifi_safety(gid, pid), "allowed": allowed, "note": note})
        out.append({"id": gid, "name": str(g.get("name") or gid), "kind": group_kind(gid),
                    "phoneActive": bool(g.get("isActive")), "activeProfile": str(g.get("activeProfile") or ""),
                    "destructive": bool(g.get("isDestructive")), "profiles": profiles})
    return out


def find_profile(raw: list, profile_id: str) -> tuple[dict, dict] | None:
    for g in raw or []:
        for p in (g.get("profiles") or []) if isinstance(g, dict) else []:
            if isinstance(p, dict) and p.get("identifier") == profile_id:
                return g, p
    return None


def clamp_timeout(value) -> int:
    try:
        t = int(float(value))
    except (TypeError, ValueError):
        t = DEFAULT_TIMEOUT_S
    return max(MIN_TIMEOUT_S, min(MAX_TIMEOUT_S, t))


def reason_for(close_reason: str) -> str:
    """deviced_kit.Resources close reason -> the `condition` event's reason."""
    return {"released": "cleared", "client": "client-gone", "timeout": "timeout", "tunnel": "tunnel-lost",
            "session": "tunnel-lost", "shutdown": "shutdown", "error": "tunnel-lost"}.get(close_reason, close_reason)


# ====================================================================== packet capture

# The daemon's own link to this computer: never part of an app's traffic.
TUNNEL_PROCS = {"remoted", "remotepairingd", "pcapd"}
PCAP_DEFAULT_S = 30
PCAP_MAX_S = 300
PCAP_DEFAULT_MB = 50
PCAP_MAX_MB = 500
CAPTURES_KEEP = 20

SERVICES = {("tcp", 443): "HTTPS", ("udp", 443): "QUIC", ("tcp", 80): "HTTP", ("udp", 53): "DNS",
            ("tcp", 53): "DNS", ("udp", 5353): "mDNS", ("udp", 123): "NTP", ("tcp", 5223): "Apple Push",
            ("udp", 853): "DNS over QUIC", ("tcp", 853): "DNS over TLS", ("udp", 67): "DHCP", ("udp", 68): "DHCP",
            ("udp", 547): "DHCPv6", ("udp", 1900): "SSDP", ("tcp", 993): "IMAPS", ("tcp", 587): "SMTP",
            ("udp", 3478): "STUN", ("udp", 4500): "IPsec NAT-T", ("udp", 500): "IKE"}
IP_PROTOS = {6: "TCP", 17: "UDP", 1: "ICMP", 58: "ICMPv6", 2: "IGMP", 50: "ESP"}


def proc_name(comm: str, epid: int | None = None, ecomm: str = "", pid: int | None = None) -> str:
    """Who the packet belongs to: the effective process when a daemon sends on an app's behalf."""
    comm = (comm or "").strip("\x00 ") or "?"
    ecomm = (ecomm or "").strip("\x00 ")
    if ecomm and ecomm != comm:
        return f"{ecomm} (via {comm})"
    return comm


def parse_ip(frame: bytes) -> dict | None:
    """IP/TCP/UDP header fields of an Ethernet frame (pcapd gives every frame an Ethernet header).
    The IP version comes from the first nibble, because pcapd's synthetic header always says IPv4."""
    if len(frame) < 15:
        return None
    ip = frame[14:]
    ver = ip[0] >> 4
    try:
        if ver == 4 and len(ip) >= 20:
            ihl = (ip[0] & 0x0F) * 4
            proto = ip[9]
            src, dst = ipaddress.IPv4Address(ip[12:16]), ipaddress.IPv4Address(ip[16:20])
            l4 = ip[ihl:]
        elif ver == 6 and len(ip) >= 40:
            proto = ip[6]
            src, dst = ipaddress.IPv6Address(ip[8:24]), ipaddress.IPv6Address(ip[24:40])
            l4 = ip[40:]
            # skip the common extension headers (hop-by-hop, routing, destination options)
            hops = 0
            while proto in (0, 43, 60) and len(l4) >= 8 and hops < 4:
                proto, l4 = l4[0], l4[(l4[1] + 1) * 8:]
                hops += 1
        else:
            return None
    except ValueError:
        return None
    sport = dport = None
    if proto in (6, 17) and len(l4) >= 4:
        sport, dport = struct.unpack("!HH", l4[:4])
    return {"v": ver, "proto": proto, "src": src, "dst": dst, "sport": sport, "dport": dport}


def _service_side(h: dict) -> str:
    """'dst' or 'src': which end of the flow is the server (the well-known port)."""
    s, d = h["sport"], h["dport"]
    if s is None or d is None:
        return "dst" if not h["dst"].is_private or h["src"].is_private else "src"
    if d < 49152 and (s >= 49152 or d < s):
        return "dst"
    if s < 49152:
        return "src"
    return "dst"


class CaptureStats:
    """Counts while a capture runs; summary() when it stops."""

    def __init__(self):
        self.packets = 0
        self.bytes = 0
        self.by_proc: Counter = Counter()
        self.by_proc_bytes: Counter = Counter()
        self.hosts: Counter = Counter()
        self.hosts_bytes: Counter = Counter()
        self.protocols: Counter = Counter()
        self.services: Counter = Counter()
        self.ip_versions: Counter = Counter()
        self.interfaces: Counter = Counter()
        self.up = 0
        self.down = 0
        self.excluded = 0     # the daemon's own tunnel traffic
        self.skipped = 0      # other processes when filtering

    def add(self, proc: str, iface: str, frame: bytes, length: int | None = None) -> None:
        n = int(length if length is not None else len(frame))
        self.packets += 1
        self.bytes += n
        self.by_proc[proc] += 1
        self.by_proc_bytes[proc] += n
        self.interfaces[iface or "?"] += 1
        h = parse_ip(frame)
        if h is None:
            self.protocols["other"] += 1
            return
        self.ip_versions[f"IPv{h['v']}"] += 1
        pname = IP_PROTOS.get(h["proto"], f"IP {h['proto']}")
        self.protocols[pname] += 1
        side = _service_side(h)
        host, port = (h["dst"], h["dport"]) if side == "dst" else (h["src"], h["sport"])
        if side == "dst":
            self.up += n
        else:
            self.down += n
        l4 = "tcp" if h["proto"] == 6 else "udp" if h["proto"] == 17 else ""
        svc = SERVICES.get((l4, port)) if l4 and port is not None else None
        if svc:
            self.services[svc] += 1
        elif l4 and port is not None:
            self.services[f"{l4.upper()} {port}"] += 1
        key = (str(host), port, pname, svc or "")
        self.hosts[key] += 1
        self.hosts_bytes[key] += n

    def top_processes(self, n: int = 5) -> list[dict]:
        return [{"process": p, "packets": c, "bytes": self.by_proc_bytes[p]} for p, c in self.by_proc.most_common(n)]

    def summary(self, seconds: float, n: int = 10) -> dict:
        hosts = [{"host": h, "port": port, "protocol": proto, "service": svc or None, "packets": c,
                  "bytes": self.hosts_bytes[(h, port, proto, svc)]}
                 for (h, port, proto, svc), c in self.hosts.most_common(n)]
        return {"packets": self.packets, "bytes": self.bytes, "seconds": round(seconds, 1),
                "sentBytes": self.up, "receivedBytes": self.down,
                "byProcess": self.top_processes(n), "processCount": len(self.by_proc),
                "topHosts": hosts, "hostCount": len(self.hosts),
                "protocols": dict(self.protocols.most_common()), "services": dict(self.services.most_common(n)),
                "ipVersions": dict(self.ip_versions), "interfaces": dict(self.interfaces.most_common(5)),
                "excludedTunnelPackets": self.excluded, "otherProcessPackets": self.skipped}


def capture_name(ts: float | None = None) -> str:
    return time.strftime("cap-%Y%m%d-%H%M%S.pcapng", time.localtime(ts or time.time()))


def prune_captures(directory, keep: int = CAPTURES_KEEP) -> None:
    try:
        files = sorted(directory.glob("cap-*.pcapng"), key=lambda p: p.stat().st_mtime, reverse=True)
        for f in files[keep:]:
            f.unlink(missing_ok=True)
    except OSError:
        pass


# ====================================================================== tiny pcapng writer (demo)

def _block(btype: int, body: bytes) -> bytes:
    body += b"\x00" * (-len(body) % 4)
    total = 12 + len(body)
    return struct.pack("<II", btype, total) + body + struct.pack("<I", total)


def _option(code: int, value: bytes) -> bytes:
    return struct.pack("<HH", code, len(value)) + value + b"\x00" * (-len(value) % 4)


def write_pcapng(path, packets) -> int:
    """packets: [(ts, frame_bytes, comment)] -> a standard pcapng file (Ethernet link type). Returns its size."""
    out = bytearray()
    out += _block(0x0A0D0D0A, struct.pack("<IHHq", 0x1A2B3C4D, 1, 0, -1)
                  + _option(3, b"iOS") + _option(4, b"iOS Studio demo") + _option(0, b""))
    out += _block(0x00000001, struct.pack("<HHI", 1, 0, 0) + _option(3, b"iOS Packet Capture") + _option(0, b""))
    for ts, frame, comment in packets:
        us = int(ts * 1_000_000)
        opts = (_option(1, comment.encode()) + _option(0, b"")) if comment else b""
        body = struct.pack("<IIIII", 0, (us >> 32) & 0xFFFFFFFF, us & 0xFFFFFFFF, len(frame), len(frame))
        body += frame + b"\x00" * (-len(frame) % 4) + opts
        out += _block(0x00000006, body)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(out))
    return len(out)


def make_frame(src: str, dst: str, proto: int, sport: int, dport: int, payload_len: int = 0) -> bytes:
    """An Ethernet + IPv4/IPv6 + TCP/UDP header with a zero-filled payload (demo packets)."""
    a, b = ipaddress.ip_address(src), ipaddress.ip_address(dst)
    if proto == 6:
        l4 = struct.pack("!HHIIBBHHH", sport, dport, 1, 0, 0x50, 0x18, 65535, 0, 0)
    else:
        l4 = struct.pack("!HHHH", sport, dport, 8 + payload_len, 0)
    l4 += b"\x00" * payload_len
    if a.version == 4:
        ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(l4), 0, 0, 64, proto, 0, a.packed, b.packed)
        eth = b"\xbe\xef" * 6 + b"\x08\x00"
    else:
        ip = struct.pack("!IHBB16s16s", 0x60000000, len(l4), proto, 64, a.packed, b.packed)
        eth = b"\xbe\xef" * 6 + b"\x86\xdd"
    return eth + ip + l4
