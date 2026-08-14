#!/usr/bin/env python3
"""
Offline analyzer for USBPcap captures of the potentiostat's USB traffic.

Written to diagnose the extended-CV "Write timeout" / ClearCommError failures:
it parses a Wireshark/USBPcap ``.pcapng`` file with no external dependencies and
surfaces the things that matter for a USB fault:

  * where the sample stream stops, and any transaction/babble errors,
  * whether the device re-enumerates (drops off the bus and comes back),
  * whether the HOST stopped reading (a canceled read URB that is never
    resubmitted — the Windows driver's flow-control cutoff),
  * how far the host application fell behind the device's data stream
    (the "backlog" estimate), which is what triggers that cutoff.

It reads a saved capture only; it never touches hardware.

Usage:
    python dev/diagnose_usb_capture.py path/to/capture.pcapng
    python dev/diagnose_usb_capture.py path/to/capture.pcapng --device 8

Capture the file in Wireshark with the USBPcap interface selected, then save as
pcapng. See dev/README.md for the wider debugging context.
"""
import argparse
import struct
from collections import Counter

# --- USB decode tables --------------------------------------------------------

# USBD_STATUS result codes. SUCCESS/PENDING are normal; everything else is a
# fault worth reporting. Unknown codes are shown as raw hex. Note that CANCELED
# is a *host* action (the Windows driver withdrew the transfer), not a bus error.
USBD_STATUS = {
    0x00000000: "SUCCESS",
    0x40000000: "PENDING",
    0xC0000004: "STALL_PID",
    0xC0000005: "DEV_NOT_RESPONDING",
    0xC0000008: "DATA_OVERRUN",
    0xC0000009: "DATA_UNDERRUN",
    0xC0000011: "XACT_ERROR",
    0xC0000030: "ENDPOINT_HALTED",
    0x80000200: "ERROR_SHORT_TRANSFER",
    0x80000300: "BABBLE_DETECTED",
    0xC0006000: "TIMEOUT",
    0xC0007000: "DEVICE_GONE",
    0xC0010000: "CANCELED",
}

# USB transfer types, as encoded in the USBPcap pseudoheader.
TRANSFER = {0: "ISO", 1: "INT", 2: "CTRL", 3: "BULK"}

# URB function codes (Windows USB request types). 0x0008 is a *generic* control
# transfer whose real meaning lives in its 8-byte setup packet — earlier
# versions of this tool mislabeled it GET_DESCRIPTOR, which led a whole
# investigation astray. Decode the setup packet instead (see describe_setup).
URB_FUNCTION = {
    0x0000: "SELECT_CONFIG",
    0x0001: "SELECT_INTERFACE",
    0x0002: "ABORT_PIPE",
    0x0008: "CONTROL_TRANSFER",
    0x0009: "BULK_OR_INT",
    0x000B: "GET_DESCRIPTOR",
    0x0013: "GET_STATUS",
    0x001E: "RESET_PIPE_AND_CLEAR_STALL",
}

# CDC-ACM class requests (what a USB serial port uses for line settings).
CDC_REQUEST = {
    0x20: "SET_LINE_CODING",
    0x21: "GET_LINE_CODING",
    0x22: "SET_CONTROL_LINE_STATE",
}


def status_name(code):
    return USBD_STATUS.get(code, f"0x{code:08X}")


def function_name(code):
    return URB_FUNCTION.get(code, f"0x{code:04X}")


def describe_setup(payload):
    """Explain the 8-byte setup packet of a control transfer in plain words.

    For CDC serial devices the interesting one is SET_CONTROL_LINE_STATE: its
    wValue bit 0 is DTR ("host application has the port open") and bit 1 is
    RTS. A host dropping DTR right after a failure is the app closing the port.
    """
    if len(payload) < 8:
        return ""
    bm_type, b_req = payload[0], payload[1]
    w_value = struct.unpack_from("<H", payload, 2)[0]
    if (bm_type & 0x60) == 0x20:  # class request (CDC for this device)
        name = CDC_REQUEST.get(b_req, f"class_req=0x{b_req:02X}")
        if b_req == 0x22:
            dtr, rts = w_value & 1, (w_value >> 1) & 1
            return f" [{name} DTR={dtr} RTS={rts}]"
        return f" [{name}]"
    if (bm_type & 0x60) == 0x00 and b_req == 0x06:  # standard GET_DESCRIPTOR
        return f" [GET_DESCRIPTOR type=0x{w_value >> 8:02X}]"
    return ""


# --- File parsing -------------------------------------------------------------

def parse_usbpcap_header(buf):
    """Decode one USBPcap pseudoheader (fixed little-endian struct).

    ``header_len`` marks where the transport payload begins, which varies for
    control transfers, so the payload slice keys off that field. ``irp`` is the
    kernel's identifier for one transfer request: the same value appears on the
    submission and its completion, which lets us pair them up.
    """
    if len(buf) < 27:
        return None
    header_len = struct.unpack_from("<H", buf, 0)[0]
    return {
        "irp": struct.unpack_from("<Q", buf, 2)[0],
        "status": struct.unpack_from("<I", buf, 10)[0],
        "function": struct.unpack_from("<H", buf, 14)[0],
        "info": buf[16],                 # bit 0: 0 = submit, 1 = completion
        "device": struct.unpack_from("<H", buf, 19)[0],
        "endpoint": buf[21],
        "transfer": buf[22],
        "data_len": struct.unpack_from("<I", buf, 23)[0],
        "payload": buf[header_len:] if header_len <= len(buf) else b"",
    }


def iter_packets(path):
    """Yield ``(timestamp_s, usbpcap_header)`` for every packet in a pcapng file.

    Handles only the block types a USBPcap capture actually contains; assumes
    the little-endian byte order and microsecond timestamps that USBPcap always
    writes (verified against the interface block's if_tsresol option).
    """
    with open(path, "rb") as f:
        data = f.read()
    ts_resolution = 1e-6  # microseconds; USBPcap's default
    off, n = 0, len(data)
    while off + 8 <= n:
        block_len = struct.unpack_from("<I", data, off + 4)[0]
        if block_len < 12 or off + block_len > n:
            break
        block_type = struct.unpack_from("<I", data, off)[0]
        if block_type == 0x00000006:  # Enhanced Packet Block
            body = data[off + 8: off + block_len - 4]
            ts_hi = struct.unpack_from("<I", body, 4)[0]
            ts_lo = struct.unpack_from("<I", body, 8)[0]
            cap_len = struct.unpack_from("<I", body, 12)[0]
            header = parse_usbpcap_header(body[20:20 + cap_len])
            if header is not None:
                yield ((ts_hi << 32 | ts_lo) * ts_resolution, header)
        off += block_len


def load(path):
    """Read the whole capture into memory, timestamps rebased to zero."""
    packets = list(iter_packets(path))
    if not packets:
        return []
    t0 = packets[0][0]
    return [(i, ts - t0, h) for i, (ts, h) in enumerate(packets)]


# --- Small helpers ------------------------------------------------------------

def is_submit(h):
    return (h["info"] & 1) == 0


def pick_device(packets, requested):
    """Choose which device address to analyze: the user's --device if given,
    otherwise the address with the most bulk traffic (the potentiostat)."""
    if requested is not None:
        return requested
    bulk = Counter(h["device"] for _, _, h in packets if h["transfer"] == 3)
    return bulk.most_common(1)[0][0] if bulk else None


# --- Reports ------------------------------------------------------------------

def describe(i, ts, h):
    direction = "SUB" if is_submit(h) else "CMP"
    setup = describe_setup(h["payload"]) if h["function"] == 0x0008 and is_submit(h) else ""
    return (f"#{i} t={ts:9.4f}s dev{h['device']} ep0x{h['endpoint']:02X} "
            f"{TRANSFER.get(h['transfer'], h['transfer']):<4} {direction} "
            f"fn={function_name(h['function'])} len={h['data_len']:<4} "
            f"{status_name(h['status'])}{setup}")


def report_traffic(packets):
    """Tally packets by device, endpoint, and transfer type."""
    tally = Counter((h["device"], h["endpoint"], TRANSFER.get(h["transfer"], h["transfer"]))
                    for _, _, h in packets)
    print("=== traffic by (device, endpoint, type) ===")
    for (dev, ep, tt), count in sorted(tally.items(), key=lambda x: -x[1]):
        print(f"  dev {dev:>2}  ep 0x{ep:02X}  {tt:<4}  x{count}")


def report_errors(packets):
    """List every transfer that completed with a fault status."""
    faults = [(i, ts, h) for i, ts, h in packets
              if h["status"] not in (0x00000000, 0x40000000)]
    print(f"\n=== fault statuses: {len(faults)} ===")
    for i, ts, h in faults:
        note = "  <- host withdrew this transfer (not a bus error)" \
            if h["status"] == 0xC0010000 else ""
        print("  " + describe(i, ts, h) + note)
    return faults


def report_enumeration(packets):
    """Show when each device address appears, and its VID:PID from the device
    descriptor — a device that vanishes and returns under a new address with the
    same VID:PID has re-enumerated (dropped off the bus and rebooted)."""
    first_last = {}
    vid_pid = {}
    for _, ts, h in packets:
        dev = h["device"]
        span = first_last.setdefault(dev, [ts, ts])
        span[1] = ts
        p = h["payload"]
        if dev not in vid_pid and len(p) >= 12 and p[0] == 0x12 and p[1] == 0x01:
            vid_pid[dev] = (struct.unpack_from("<H", p, 8)[0],
                            struct.unpack_from("<H", p, 10)[0])
    print("\n=== device address timeline ===")
    for dev in sorted(first_last):
        first, last = first_last[dev]
        ids = vid_pid.get(dev)
        tag = f"  VID:PID={ids[0]:#06x}:{ids[1]:#06x}" if ids else ""
        print(f"  dev {dev:>2}: first={first:9.4f}s  last={last:9.4f}s{tag}")


def report_read_pump(packets, device):
    """Follow the host's bulk-IN read transfers on the data endpoint (0x81).

    The Windows serial driver keeps a read transfer pending at all times so the
    device always has somewhere to send data. If that "pump" gets CANCELED and
    no new submission follows, the host has stopped reading — the device's next
    transmission has nowhere to go. That moment, not the later bus errors, is
    usually the true start of a failure.
    """
    pending = {}
    cancels = []
    last_sub = last_data = None
    for i, ts, h in packets:
        if h["device"] != device or h["endpoint"] != 0x81:
            continue
        if is_submit(h):
            pending[h["irp"]] = ts
            last_sub = ts
        else:
            sub_ts = pending.pop(h["irp"], None)
            if h["status"] == 0xC0010000:
                cancels.append((i, ts, sub_ts))
            elif h["status"] == 0 and h["data_len"] > 0:
                last_data = ts
    print(f"\n=== read pump (dev {device}, ep 0x81) ===")
    if last_data is not None:
        print(f"  last data delivered:      t={last_data:9.4f}s")
    if last_sub is not None:
        print(f"  last read submitted:      t={last_sub:9.4f}s")
    for i, ts, sub_ts in cancels[-5:]:
        age = f" (was pending {ts - sub_ts:.4f}s)" if sub_ts else ""
        print(f"  read CANCELED:            t={ts:9.4f}s{age}")
    if cancels and last_sub is not None and last_sub <= cancels[-1][1]:
        print("  -> after the last cancel the host NEVER resubmitted a read:")
        print("     the host stopped reading (flow-control cutoff, purge, or close).")
    for irp, ts in pending.items():
        print(f"  still pending at capture end: submitted t={ts:9.4f}s")


def report_batch_protocol(packets, device):
    """Reconstruct the MOLES batch protocol from the traffic and estimate how
    far the host application fell behind the device.

    Protocol facts this relies on (see ps4_ref.py / QueryList.c):
      * EXECUTE_VOLTAGE_BATCH is an OUT write ``[id, 0x07, delay:u32, count:u32]``,
      * each host loop iteration reads 6 samples then writes one 26-byte chunk
        (``[id, 0x08]`` + six float32 potentials),
      * each sample is one small fixed-size IN packet (8 or 9 bytes).

    "Backlog" = bytes the device has delivered minus bytes the host loop has
    consumed. It should stay near zero; if it climbs, the host loop is too slow
    and Windows will eventually stop the read pump entirely (observed cutoff:
    ~12 kB), which wedges unpatched firmware.
    """
    executes = []
    chunk_writes = []      # (ts,) per steady-state 26-byte waveform write
    sample_sizes = Counter()
    in_events = []         # (ts, bytes) per delivered IN completion
    for i, ts, h in packets:
        if h["device"] != device:
            continue
        if h["endpoint"] == 0x01 and is_submit(h):
            p = h["payload"]
            if len(p) >= 10 and p[1] == 0x07:
                delay, count = struct.unpack_from("<II", p, 2)
                executes.append((ts, delay, count))
            elif len(p) >= 2 and p[1] == 0x08 and h["data_len"] == 26:
                chunk_writes.append(ts)
        elif h["endpoint"] == 0x81 and not is_submit(h) and h["status"] == 0 \
                and h["data_len"] > 0:
            in_events.append((ts, h["data_len"]))
            if h["data_len"] <= 16:
                sample_sizes[h["data_len"]] += 1

    print(f"\n=== batch protocol (dev {device}) ===")
    for ts, delay, count in executes:
        if count == 0:
            print(f"  t={ts:9.4f}s EXECUTE halt (count=0)")
        else:
            rate = 1000.0 / max(delay, 1)
            print(f"  t={ts:9.4f}s EXECUTE delay={delay}ms count={count} "
                  f"-> {rate:.0f} samples/s for {count / rate:.0f}s; host loop "
                  f"must sustain {rate / 6:.1f} it/s")
    if not chunk_writes or not sample_sizes:
        print("  (no batch-stream traffic found)")
        return
    sample_size = sample_sizes.most_common(1)[0][0]
    n_samples = sample_sizes[sample_size]
    per_iter = 6 * sample_size
    print(f"  samples delivered: {n_samples} x {sample_size}B; "
          f"host iterations (chunk writes): {len(chunk_writes)}")

    # Walk both streams together and track the worst/final backlog.
    merged = [(ts, "in", nbytes) for ts, nbytes in in_events]
    merged += [(ts, "out", 0) for ts in chunk_writes]
    merged.sort()
    delivered = consumed = 0
    peak = (0.0, 0)
    for ts, kind, nbytes in merged:
        if kind == "in":
            delivered += nbytes
        else:
            consumed += per_iter
        backlog = delivered - consumed
        if backlog > peak[1]:
            peak = (ts, backlog)
    print(f"  backlog peak: {peak[1]} bytes at t={peak[0]:9.4f}s "
          f"(final: {delivered - consumed} bytes)")
    if peak[1] > 8000:
        print("  -> backlog exceeded 8 kB: the host loop fell well behind the "
              "device; Windows cuts the read pump near ~12 kB.")

    # Host loop rate over time: dips below the required rate are the disease.
    print("  host loop rate (10 s buckets):")
    buckets = Counter(int(ts // 10) * 10 for ts in chunk_writes)
    for b in sorted(buckets):
        print(f"    t={b:>4d}-{b + 10:<4d} {buckets[b] / 10.0:5.1f} it/s")


def report_failure_window(packets, device, pad=0.1):
    """Zoom in on the failure: print traffic around the first fault, compressing
    the healthy sample stream but keeping OUT writes visible as counts — whether
    the device kept accepting writes after the stream stopped tells you if it
    was still alive at the interrupt level."""
    faults = [(i, ts, h) for i, ts, h in packets
              if h["status"] not in (0x00000000, 0x40000000)]
    if not faults:
        print("\n(no faults found; nothing to zoom into)")
        return
    window_start = faults[0][1] - 2.5  # a little before the first fault
    print(f"\n=== failure window (from t={window_start:.3f}s, "
          f"sample stream compressed) ===")
    stream = {"in": 0, "out": 0, "last": None}

    def flush_stream():
        # Separate IN/OUT counts on purpose: a device that keeps accepting OUT
        # writes after its IN stream died is alive at the interrupt level and
        # only its main loop has stopped.
        if stream["in"] or stream["out"]:
            print(f"  ... healthy stream: {stream['in']} IN, "
                  f"{stream['out']} OUT packets (last t={stream['last']:9.4f}s)")
            stream["in"] = stream["out"] = 0

    for i, ts, h in packets:
        if ts < window_start:
            continue
        if device is not None and h["device"] not in (device,) \
                and h["status"] == 0x00000000 and h["endpoint"] != 0x00:
            continue
        if h["status"] in (0x00000000, 0x40000000):
            if h["endpoint"] == 0x81:
                stream["in"] += 1
                stream["last"] = ts
                continue
            if h["endpoint"] == 0x01:
                stream["out"] += 1
                stream["last"] = ts
                continue
        flush_stream()
        print("  " + describe(i, ts, h))
    flush_stream()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("capture", help="path to a USBPcap .pcapng file")
    ap.add_argument("--device", type=int, default=None,
                    help="device address to analyze (default: busiest bulk device)")
    args = ap.parse_args()

    packets = load(args.capture)
    if not packets:
        print("No USB packets found — is this a USBPcap capture?")
        return
    print(f"parsed {len(packets)} USB packets\n")

    device = pick_device(packets, args.device)
    report_traffic(packets)
    report_errors(packets)
    report_enumeration(packets)
    if device is not None:
        report_read_pump(packets, device)
        report_batch_protocol(packets, device)
    report_failure_window(packets, device)


if __name__ == "__main__":
    main()
