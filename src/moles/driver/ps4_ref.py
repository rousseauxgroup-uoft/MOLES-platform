#!/usr/bin/env python3
"""Serial driver for the STM32G473-based open-source potentiostat.

Original work by Han Hao, Sergio Pablo-García, and Gun Deniz Akkoc, from the
open-source potentiostat platform described in Device 2025, 3, 100567
(doi: 10.1016/j.device.2024.100567). Licensed under the Apache License,
Version 2.0.

This file has been modified for the MOLES project by. The
changes harden the original command set:
  - logging via the standard library in place of print statements
  - SerialReadTimeout / SerialAckError and retry helpers, so a short or
    unacknowledged serial read is retried and reported rather than raising
    an opaque IndexError partway through an experiment
  - _safe_open_switch(), which matches the DAC to the measured cell
    potential before opening the CE-WE switch, suppressing the output spike
  - stream_voltage_batch(), for gapless upload of a precomputed waveform

Original header:
    2023-08-30
    deniz
"""
import logging
import serial
import threading
import time
import numpy as np
from collections import deque
from enum import IntEnum
from pathlib import Path
# import tomllib
import sys
from typing import Union
from .proc_echem import *
from typing import List

logger = logging.getLogger(__name__)


class SerialReadTimeout(Exception):
    """Raised when a serial read returns fewer bytes than expected, even after a retry."""


class SerialAckError(Exception):
    """Raised when the device does not acknowledge a command with "OK", even
    after the command has been re-sent once. Means the command may not have
    taken effect on the hardware."""


class Commands(IntEnum):
    """Contains index for each serial command

    """
    READ_ADC = 1
    WRITE_DAC = 2
    READ_DAC = 3
    WRITE_SWITCH = 4
    WRITE_GAIN = 5
    EXECUTE_VOLTAGE_BATCH = 7
    _WRITE_BUFFER = 8
    _READ_BUFFER = 9
    WRITE_CURRENT_HOLD = 10
    _RESET_BUFFER = 11
    WRITE_SAMPLE_COUNT = 12
    WRITE_AUTO_GAIN = 13
    READ_ANALOG_GAIN = 14
    READ_AUTO_GAIN = 15
    READ_GAIN = 16
    READ_SWITCH = 17
    STOP_CURRENT_HOLD = 18
    READ_DIAGNOSTICS = 19
    # Self-framing positional buffer write ([u16 len][u16 pos] + data);
    # requires July 2026 firmware. See stream_voltage_batch.
    _WRITE_BUFFER_AT = 20
    # Firmware-timed step-response capture for the R_u measurement
    # ([u32 n_samples][u32 period_us]); requires Aug 2026 firmware. The
    # driver probes for it once and falls back to the serial burst.
    STEP_CAPTURE = 21

    
class Resistors():
    """Contains index information of resistor values along with the resistance values for gain.
    This information is then used for V to I conversion (simple V=IR)
    """
    R_100 = 0
    R_1K = 1
    R_10K = 2
    R_100K = 3
    R_1M = 4
    #R_10M = 5

class ADC():
    WE_OUT = 0 #WE-CE potential for conversion to current with resistor multiplier.
    RE_OUT = 1 #WE-RE potential
    VREF = 2
    TEMP = 3 #Temperature
    HUMID = 4 #Humidity

class DAC():
    CE_IN = 0 #RE-WE
    A_REF = 1
    V_AN = 2


class SwitchState(IntEnum):
    On = 1 #CE and WE connected
    Off = 0 #CE and WE disconnected

# Bench-measured output limits (July 2026, dummy-cell tests): the output
# amplifier saturates near +/-4.6 V (-4.60/+4.62 V on a 450-ohm dummy cell)
# and the USB supply tops out near 33 mA (3.35 V on 100 ohm). Setpoints beyond
# these cannot be reached even in an ideal cell; real cells saturate earlier
# because solution resistance and counter-electrode overpotential consume part
# of the headroom. The software input range (+/-5 V) is wider than this, so
# the UIs warn before starting a run whose setpoints exceed the threshold.
HW_VOLTAGE_COMPLIANCE_V = 4.6
VOLTAGE_WARN_THRESHOLD_V = 4.5

# Firmware step-capture (CMD_STEP_CAPTURE) defaults for the R_u measurement:
# 25 us sampling (the firmware's floor) over 150 ms. The fast end matters
# most: small analytical electrodes give R_u*C_dl of only ~20-50 us
# (bench 2026-08-15), so every microsecond of the first samples counts;
# the long tail pins down the faradaic baseline.
RU_CAPTURE_PERIOD_US = 25
RU_CAPTURE_MS = 150.0

# The switch-close instrument artifact the control capture measures lives in
# the first moments of a trace; beyond this window the control is only noise
# and OCP drift, which subtracting would inject into the step traces.
RU_ARTIFACT_WINDOW_MS = 2.0

# The firmware's KADC16: raw 16-bit ADC counts to ADC volts (3.3 V full scale)
ADC16_COUNTS_TO_V = 3.3 / 65535.0


class Potentiostat():
    serial = None
    serial_port = None
    device_ID = 0
    baudrate = 115200
    _chunk_size = 254 #number of bytes that can be communicated at once minus 2 (for ID and command bytes) !!!CONFIG
    _vin_lims = [-5,5]
    _vout_lims = [0,3.3]
    _auto_gain = False
    _switch_state = False
    _rx_buffer_size = 24000 #rx buffer size of the pstat
    _tx_buffer_size = 64000 #tx buffer size of the pstat
    # Whether the connected firmware has CMD_STEP_CAPTURE: None until the
    # first R_u measurement probes for it, then cached for the connection.
    _step_capture_supported = None

    res_vals    = {Resistors.R_100: 1e2,
                Resistors.R_1K: 1e3,
                Resistors.R_10K: 1e4,
                Resistors.R_100K: 1e5,
                Resistors.R_1M: 1e6,
                #R_10M: 1e7,
    }
    
    def _select_resistor(self, current):
        """_summary_

        Args:
            current (float): Target Ampere

        Returns:
            Resistors: Best resistance for highest gain without saturation of the ADC
        """
        max_currents = [0.05, 0.005, 0.0005, 0.00005, 0.000005, 0.0000005] #in Amps
        #max_currents = [0.05, 0.005, 0.0005, 0.00005, 0.000005, 0.0000005, 0.00000005] #in Amps (with 10M resistor)
        for i in range(len(max_currents)):
            if abs(current) > (max_currents[i] * 0.9):
                if i < 1: return 0
                return i-1
        return Resistors.R_1M

    def _select_100ohm_resistor(self, current):
        """_summary_

        Args:
            current (float): Target Ampere

        Returns:
            Resistors: R_100 ohm resistor for electrosynthesis-scale currents (mA level)
        """
        # Hardware appears stuck on R_100, force selection.
        return Resistors.R_100


    class Errors(IntEnum):
        NO_ERROR = 0
        ERROR = 1

    def __init__(self,serial_port='/dev/ttyACM1',baudrate=115200,device_ID = 1):
        self.serial_port = serial_port
        self.baudrate = baudrate
        self.device_ID = device_ID
        # Serializes every command/response transaction on this port. Multiple
        # threads can legitimately reach for the same device (experiment loop,
        # emergency cleanup at shutdown, reconnect) and interleaved commands
        # desync the request/response protocol. RLock so composite operations
        # (e.g. a whole voltage batch) can hold it across nested calls.
        self._port_lock = threading.RLock()

    def connect(self):
        with self._port_lock:
            # timeout=10 ensures readline() gives up after 10 seconds instead of blocking forever.
            # write_timeout=2 ensures a wedged USB device makes writes fail instead of blocking
            # the calling thread forever (a hung write used to lock a channel until app restart).
            # dsrdtr=False prevents pyserial from toggling the DTR line on open, which can trigger
            # an unintended STM32 reset and cause the device to be unresponsive to the first commands.
            # exclusive=True makes the OS refuse a second open of this port by any other process
            # (flock on POSIX; Windows COM ports are always exclusive), so two MOLES apps can
            # never interleave commands to one board.
            self.serial = serial.Serial(self.serial_port, self.baudrate, timeout=10,
                                        write_timeout=2, dsrdtr=False, exclusive=True)
            # Allow time for the device UART stack to initialize after the USB connection is established.
            # 1.0s is conservative but avoids the PermissionError that occurs when commands arrive
            # before the firmware is ready.
            time.sleep(1.0)
            # Ask Windows for a larger receive buffer. NOTE: measured on the
            # bench (July 2026), the USB-serial driver caps its willingness to
            # buffer unread data at ~12 kB regardless of this request — it then
            # simply stops reading from the device. So this call is kept as a
            # harmless hint, but the real protections are the batch-loop
            # backlog guard and the device-side pre-fill (see methods/cv.py).
            try:
                self.serial.set_buffer_size(rx_size=1_048_576)
            except Exception:
                pass
            logger.info("[PS %s] serial dev created on %s", self.device_ID, self.serial_port)
            try:
                # A session that died mid-experiment can leave the device still
                # streaming batch data, which would garble the handshake below.
                self.halt_batch_stream()

                self.write_switch(False)
                # Park at the least-sensitive shunt: safest default if the switch is later
                # turned on without an experiment script having set its own gain.
                self.write_gain(Resistors.R_100)
                self.write_auto_gain(False)
                #Sync current status of the pstat to the class by read commands
                logger.info("[PS %s] switch is %s, gain is %s, auto_gain is %s",
                            self.device_ID, self.read_switch(), self.read_gain(),
                            self.read_auto_gain())
                self.write_dac(channels=[DAC.A_REF,DAC.V_AN], voltages=[-5,0])
            except Exception:
                # Cut potentiostat output before tearing down the connection.
                try:
                    self.write_switch(0)
                except Exception:
                    pass
                self.serial.close()
                self.serial = None
                raise

    def disconnect(self):
        with self._port_lock:
            self.serial.close()

    def force_close(self):
        """Close the underlying port WITHOUT taking the port lock.

        Recovery hatch for watchdogs: if a thread is hung inside a serial
        operation (holding the lock), closing the handle from another thread
        makes the pending read/write raise, which unblocks the hung thread
        into its normal error path. Never use this in normal control flow.
        """
        try:
            if self.serial is not None:
                self.serial.close()
        except Exception:
            logger.exception("[PS %s] force_close failed", self.device_ID)

    def _scale_input_voltages(self,voltages):
        """Map (and limit) to be inputted actual potential(V) values to pstat operation range
        """
        #changes!
        # return (np.interp(voltages,self._vin_lims,self._vout_lims) - 0.0494)/0.9919
        # return np.interp(voltages,self._vin_lims,self._vout_lims)
        voltages_calibrated = (np.array(voltages)- 0.0494)/0.9919
        return np.interp(voltages_calibrated,self._vin_lims,self._vout_lims)
    
    def _scale_output_voltages(self,voltages):
        """Map (and limit) outputted potential(V) values to actual values
        """
        # return np.interp(voltages,self._vout_lims,self._vin_lims)
        voltages = np.interp(voltages,self._vout_lims,self._vin_lims)
        return voltages * 0.9919 + 0.0494
    
    def _current_to_voltage(self,currents):
        """Converts target current value(s) to DAC voltage value to be written.

        Args:
            currents (np.float32): current(I) value(s)

        Returns:
            np.float32: DAC voltage value
        """
        # Derived from empirical data: (0.1V, 0.3mA), (1.0V, 8.9mA) -> Refined with 4-point fit
        # V = 113.0 * I_A + 0.060
        OFFSET = 0.060
        GAIN = 113.0
        
        # Fix for Asymmetry:
        # Previously used logic: V = -(GAIN*|I| + OFFSET) for I>0, V = +(GAIN*|I| + OFFSET) for I<0.
        # This created a discontinuity (Jump from -0.06 to +0.06) and over-drove negative currents.
        # User reported Correct Positive (+1mA) but Excess Negative (-2.25mA for -1mA target).
        # Positive worked implies: V_pos = -113*I - 0.06 is correct.
        # Extending this linearly to negative: V_neg = -113*(-1) - 0.06 = 113 - 0.06 = 53 mV.
        # (Previous logic sent 113 + 0.06 = 173 mV, causing the overshoot).
        
        voltages = (currents * GAIN * -1.0) - OFFSET
        
        voltages = self._scale_input_voltages(voltages)
        return voltages

    def _check_response(self,timeout_s=10):
        """Check whether the response to a command is an OK line.
        Logs a warning if the hardware returns an unexpected response.
        """
        resp = self.serial.readline()
        resp = resp.strip()
        if resp == b"OK":
            return True
        else:
            logger.warning("[PS %s] Unexpected hardware response: %r", self.device_ID, resp)
            return False

    def _write_cmd(self,cmd,data,check_response=True,retry=True):
        """Write command by its data. Adds device ID to the beginning of the data package.

        When ``check_response`` is set, a missing/garbled "OK" acknowledgement
        re-sends the command once (all simple commands are idempotent), and a
        second failure raises ``SerialAckError`` — a command that silently
        never took effect is worse than a loud failure, especially for safety
        commands like opening the cell switch. Pass ``retry=False`` for
        commands that must not be re-sent (e.g. EXECUTE_VOLTAGE_BATCH, where a
        duplicate would corrupt the stream protocol).

        Args:
            cmd (Commands): command index
            data (any): command data
            check_response (bool, optional): Whether to check for OK. Defaults to True.
            retry (bool, optional): Re-send once on a failed acknowledgement.
        """
        msg = bytearray()
        msg.extend(np.uint8(self.device_ID).tobytes()) #device ID
        msg.extend(np.uint8(cmd).tobytes()) #Command
        if not (data is None):
            if str(type(data)) == "<class 'bytes'>" or str(type(data)) == "<class 'bytearray'>":
                msg.extend(data)
            else:
                msg.extend(data.tobytes())

        with self._port_lock:
            self.serial.write(msg)
            if not check_response:
                return None
            if self._check_response():
                return True
            if retry:
                logger.warning("[PS %s] Re-sending unacknowledged command %s",
                               self.device_ID, cmd)
                self.serial.reset_input_buffer()
                self.serial.write(msg)
                if self._check_response():
                    return True
            raise SerialAckError(
                f"Device {self.device_ID} did not acknowledge command {cmd!r}"
            )

    def _read_with_retry(self, n_bytes, cmd, data):
        """Read exactly n_bytes from the serial port. On a short read (transient
        USB latency or firmware-busy window), flush the input buffer to discard
        any late-arriving stragglers, re-issue the command, and retry once.
        Raises SerialReadTimeout if the second attempt also returns short.
        """
        with self._port_lock:
            result = self.serial.read(n_bytes)
            if len(result) < n_bytes:
                self.serial.reset_input_buffer()
                self._write_cmd(cmd, data, False)
                result = self.serial.read(n_bytes)
                if len(result) < n_bytes:
                    self.serial.reset_input_buffer()
                    raise SerialReadTimeout(
                        f"Expected {n_bytes} bytes, received {len(result)} after one retry"
                    )
        return result

    def _reset_buffer(self, pos=0):
        self._write_cmd(Commands._RESET_BUFFER,np.uint32(pos))

    def _write_to_buffer(self, data):
        # Override to force small chunks for flow control
        # Original logic used _chunk_size (254) which might be too large for rapid bursts
        
        SAFE_CHUNK_SIZE = 64 # Bytes. Safe for hardware RX FIFO (usually 64 or 128 bytes)
        tx_delay = 0.01 # 10ms delay to allow hardware interrupt usage
        
        ind = 0
        total_len = len(data)
        
        while ind < total_len:
            end = min(ind + SAFE_CHUNK_SIZE, total_len)
            chunk = data[ind:end]
            
            # Write chunk
            self._write_cmd(Commands._WRITE_BUFFER, chunk, False)
            self.serial.flush() # Force OS to send
            time.sleep(tx_delay) # Wait for hardware to ingest
            
            ind += SAFE_CHUNK_SIZE
            
        return True

    def _read_from_buffer(self, data_len):
        """Read a batch-stream chunk. A short read here means the firmware
        stalled mid-batch; raise cleanly instead of letting np.frombuffer
        parse a truncated buffer into garbage downstream."""
        result = self.serial.read(data_len)
        if len(result) < data_len:
            self.serial.reset_input_buffer()
            raise SerialReadTimeout(
                f"Batch stream returned {len(result)} of {data_len} bytes"
            )
        return result
                    
    def halt_batch_stream(self):
        """Stop any in-progress batch execution and flush its residue.

        A batch-execute with a count of 0 halts the firmware's stepper; the
        pause lets in-flight data frames land so one purge removes them all.
        Required before re-arming the batch protocol after a failed chunk: a
        chunk can keep executing on the board for many seconds after the host
        gives up on it, and a reset/execute sent mid-batch races the running
        stepper over the ring buffer while the old stream drowns out the new
        commands' acknowledgements (log-confirmed July 2026).
        """
        with self._port_lock:
            self._write_cmd(Commands.EXECUTE_VOLTAGE_BATCH, np.uint32([0, 0]), False)
            time.sleep(0.3)
            self.serial.reset_input_buffer()

    def stream_voltage_batch(self, voltages, delay, on_block, block_points=None):
        """Execute a repeating voltage waveform continuously, with no gaps.

        ``write_voltage_batch`` runs one finite pre-uploaded batch per call, so
        a long experiment built from repeated calls has a dead gap between
        batches while the next one uploads (the DAC holds its last value for
        ~0.5 s every chunk). This method instead starts a single hardware-timed
        batch with an effectively endless step count and keeps the firmware's
        ring buffer topped up while it runs, so the output waveform is seamless
        for the whole experiment.

        The waveform repeats until ``on_block`` returns False or a serial error
        occurs; in both cases the stream is halted (``halt_batch_stream``)
        before this method returns or raises.

        Requires firmware with ``CMD_BUFFER_WRITE_AT`` (July 2026 or later):
        the stream feeds the ring buffer with self-framing positional writes,
        which older firmware silently ignores — the symptom on an un-updated
        board is the stream starving into read timeouts right after start.

        Args:
            voltages: one waveform period, in the same units/scale as
                ``write_voltage_batch``'s input. It is tiled endlessly.
            delay: milliseconds between steps (same as write_voltage_batch).
            on_block: called with each parsed block of measurements as an
                [n, 2] array — column 0 CE-WE current (A), column 1 RE-WE
                potential (V), matching write_voltage_batch's return format.
                Return True to keep streaming, False to stop. It runs while
                the port lock is held mid-stream, so it must NOT issue any
                driver commands.
            block_points: measurements per callback, rounded to a multiple of
                6 (the protocol's transfer granularity). Default ~0.5 s worth.
        """
        v_per_chunk = 6 #number of potentials (V) read back from pstat each time
        bytes_per_v = 4
        tx_len = v_per_chunk * bytes_per_v

        # Feed the ring buffer in large, widely-spaced messages: 60 points per
        # write, nominally once per 10 reads. Large messages lift the feed
        # capacity an order of magnitude above per-read 6-point writes (which
        # cap out near 550 points/s and set the old 250 Hz sample-rate
        # ceiling).
        #
        # Message spacing is the safety-critical part. The firmware frames
        # messages by >2 ms of line silence, and its parser (the main loop)
        # can stall for up to ~50 ms while waiting on a slow host to collect
        # a measurement frame. Two feed messages arriving within one such
        # stall get parsed as ONE buffer-write, injecting the second one's
        # 2 header bytes into the waveform ring — every later sample is then
        # read 2 bytes off-register and the DAC rails until the ring's wrap
        # realigns it (bench-observed at 500 Hz, July 2026). So consecutive
        # feed messages must be separated by MORE than the firmware's worst
        # stall, in wall-clock time, even while the host is bursting through
        # a read backlog: hence the minimum interval below rather than a
        # fixed sleep. 80 ms still feeds 750 points/s, above the 500 Hz cap.
        feed_iterations = 10          #reads per feed message, nominal
        feed_len = feed_iterations * tx_len  #bytes per feed message
        feed_points = feed_iterations * v_per_chunk
        min_feed_interval_s = 0.08    #> firmware's ~50 ms max parser stall

        scaled = self._scale_input_voltages(voltages)
        n_cycle = int(len(scaled))
        if n_cycle < 1:
            raise ValueError("stream_voltage_batch requires at least one waveform point")
        # Tile the cycle so its byte length is a multiple of one feed message:
        # every buffer write then stays aligned with the repeating waveform
        # (and with the firmware ring's wrap point), and feed slices never
        # straddle the tile edge.
        reps = feed_points // int(np.gcd(n_cycle, feed_points))
        tile_bytes = np.float32(np.tile(scaled, reps)).tobytes()

        def _feed(ring_pos, data):
            # One self-framing positional buffer write: [u16 len][u16 pos] +
            # data. The explicit length lets the firmware separate messages
            # that land in one RX frame (a late parser used to merge them,
            # splicing header bytes into the waveform — the 500 Hz railing),
            # and the explicit position means a lost message can never shift
            # the ring alignment. Requires firmware with CMD_BUFFER_WRITE_AT
            # (July 2026); older firmware ignores it and the stream times out.
            # Spacing to the next message is the caller's responsibility
            # (see min_feed_interval_s above).
            header = np.uint16([len(data), ring_pos]).tobytes()
            self._write_cmd(Commands._WRITE_BUFFER_AT, header + bytes(data), False)
            self.serial.flush()

        safe_delay = max(1.0, float(delay))
        if block_points is None:
            block_points = int(round((500.0 / safe_delay) / v_per_chunk)) * v_per_chunk
        block_points = max(v_per_chunk, (int(block_points) // v_per_chunk) * v_per_chunk)

        if self._auto_gain:
            rx_len = 9 #1 float32 current(A) + 1 float32 potential(V) + 1 uint8 resistor index
        else:
            rx_len = 8 #1 float32 current(A) + 1 float32 potential(V)

        with self._port_lock:
            try:
                # Same pre-fill margin policy as write_voltage_batch: ~16 s of
                # points, capped well inside the device's 24 kB ring buffer,
                # rounded to whole feed messages so every producer position
                # stays feed-aligned (the firmware's ring wraps at exactly
                # 24 kB, a multiple of the feed size — misaligned writes would
                # desync producer and consumer at the wrap). The steady-state
                # loop below writes exactly as many bytes as the device
                # consumes, so this lead is preserved for the whole stream and
                # the producer can never lap the consumer.
                pre_fill_chunks = max(20, min(int(np.ceil((16000.0 / safe_delay) / v_per_chunk)), 700))
                first_tx_bytes = pre_fill_chunks * tx_len
                first_tx_bytes -= first_tx_bytes % feed_len
                tile_reps = int(np.ceil(first_tx_bytes / len(tile_bytes)))
                prefill = (tile_bytes * max(1, tile_reps))[:first_tx_bytes]
                tx_cursor = first_tx_bytes % len(tile_bytes)

                self._reset_buffer(0)
                for i in range(0, first_tx_bytes, feed_len):
                    _feed(i, prefill[i:i + feed_len])
                    # During pre-fill the batch is not running yet, so the
                    # firmware's parser has no transmit work to stall on and
                    # a short framing gap between messages is sufficient.
                    time.sleep(0.008)
                # Where the next feed lands in the device's ring buffer. The
                # host names every write position (see _feed), so this must
                # track the firmware's CMD_RX_BUFFER_SIZE ring exactly.
                ring_pos = first_tx_bytes % self._rx_buffer_size
                time.sleep(0.01) #wait for pstat to receive the message

                # The step count only bounds the stream's lifetime (~198 days
                # at 4 ms/step); the real terminator is halt_batch_stream.
                self._write_cmd(Commands.EXECUTE_VOLTAGE_BATCH,
                                np.uint32([delay, 0xFFFFFFFF]), True, retry=False)

                block = np.zeros((block_points, 2), np.float32)
                block_res = np.zeros(block_points, np.float32)
                fill = 0
                owed_bytes = 0
                last_feed_t = time.monotonic()
                while True:
                    data = self._read_from_buffer(rx_len * v_per_chunk)
                    # Each read means the device consumed 6 more points; owe
                    # it that many bytes back. Feeding is deferred while the
                    # minimum message interval hasn't elapsed (see above), so
                    # a read backlog being drained in a burst can never put
                    # two messages inside one firmware parser stall; the owed
                    # balance catches back up afterwards, one message per
                    # interval, well faster than the device consumes.
                    owed_bytes += tx_len
                    if owed_bytes >= feed_len and \
                            (time.monotonic() - last_feed_t) >= min_feed_interval_s:
                        _feed(ring_pos, tile_bytes[tx_cursor:tx_cursor + feed_len])
                        ring_pos = (ring_pos + feed_len) % self._rx_buffer_size
                        tx_cursor = (tx_cursor + feed_len) % len(tile_bytes)
                        owed_bytes -= feed_len
                        last_feed_t = time.monotonic()

                    temp_ind = 0
                    for j in range(v_per_chunk):
                        adc_vals = np.frombuffer(data, np.float32, 2, temp_ind)
                        block[fill + j, :] = adc_vals
                        temp_ind += 8

                        # Frame-shift check, as in write_voltage_batch: raw
                        # floats parsed off-register are usually huge numbers.
                        if np.any(np.abs(adc_vals) > 15.0):
                            logger.warning("[PS %s] Frame shift detected! Invalid potential/current raw: %s",
                                           self.device_ID, adc_vals)
                            block[fill + j, :] = np.nan

                        if self._auto_gain:
                            temp_gain = np.frombuffer(data, np.uint8, 1, temp_ind)[0]
                            temp_ind += 1
                            if temp_gain in self.res_vals:
                                block_res[fill + j] = self.res_vals[temp_gain]
                            else:
                                logger.warning("[PS %s] Frame shift detected! Invalid gain index: %s",
                                               self.device_ID, temp_gain)
                                block_res[fill + j] = np.nan
                                block[fill + j, :] = np.nan
                    fill += v_per_chunk
                    if fill < block_points:
                        continue

                    out = self._scale_output_voltages(block)
                    if self._auto_gain:
                        out[:, 0] = np.divide(out[:, 0], block_res)
                        self.Resistor = temp_gain
                        self.ResistorVal = block_res[-1]
                    else:
                        out[:, 0] /= self.ResistorVal
                    out[:, 0] *= -1
                    fill = 0
                    if not on_block(out):
                        return
            finally:
                # Terminate the endless batch however this method exits; an
                # un-halted stream would garble every later command's ack.
                try:
                    self.halt_batch_stream()
                except Exception:
                    logger.warning("[PS %s] Could not halt stream on exit",
                                   self.device_ID, exc_info=True)

    def write_voltage_batch(self, voltages: List[float], delay: np.uint32):
        """Write a list of set voltage values to pstat's buffer. Then this buffer is executed.
        Each set V (WE-RE) will be set and immediately the CE-WE current (A) will be read along with RE-WE potential.
        If auto-gain mode is active, it will automatically return the gain corrected current values.

        Holds the port lock for the whole batch: the stream protocol cannot
        survive another thread's command being interleaved mid-batch.

        Args:
            voltages (float): list of voltages to set
            delay (np.uint32): delay between setting voltages (and reading the current and potential)

        Returns:
            list: 2D array with rows as many as voltages | column 0: CE-WE current (A) | column 1: RE-WE V
        """
        with self._port_lock:
            return self._write_voltage_batch_unlocked(voltages, delay)

    def _write_voltage_batch_unlocked(self, voltages: List[float], delay: np.uint32):
        v_per_chunk = 6 #number of potentials (V) written to pstat each time
        bytes_per_v = 4 #number of bytes used for each V
        tx_len = v_per_chunk * bytes_per_v #number of bytes to write at a time
        new_voltages = self._scale_input_voltages(voltages) #scale potentials for pstat
        v_count = int(len(new_voltages)) #number of data points (# of target V's)

        #ensure multiple of v_per_chunk per data point
        remainder = (v_count % v_per_chunk)
        pad_count = 0
        if remainder > 0:
            pad_count = v_per_chunk - remainder
            for i in range(pad_count):
                new_voltages = np.append(new_voltages,new_voltages[-1]) #pad by the last potential
            v_count += pad_count


        v_bytes = np.float32(new_voltages).tobytes() #entire array of target Vs in bytes
        v_byte_count = len(v_bytes)

        # Pre-fill: how much waveform the device holds before the stream starts.
        # The device consumes points on its own clock regardless of how fast this
        # loop supplies them, so the pre-fill is the safety margin against a slow
        # or briefly-stalled host. Target ~16 s of points, capped at 700 chunks
        # (16.8 kB) to stay within the device's 24 kB buffer. The old fixed value
        # of 4 chunks (~100 ms of margin) let any GUI hiccup starve the device.
        safe_delay = max(1.0, float(delay))
        pre_fill_chunks = max(20, min(int(np.ceil((16000.0 / safe_delay) / v_per_chunk)), 700))
        first_tx_bytes = min(pre_fill_chunks * tx_len, v_byte_count)
        first_tx_bytes -= first_tx_bytes % tx_len #keep chunk-aligned
        tx_ind = 0 #index to iterate on v_bytes

        self._reset_buffer(0) #set the rx buffer of pstat to zero
        self._write_to_buffer(v_bytes[0:first_tx_bytes]) #write the first chunk
        time.sleep(0.01) #wait for pstat to receive the message (1ms delay is end of msg for USB)
        tx_ind = first_tx_bytes #move the ind accordingly

        if self._auto_gain:
            rx_len = 9 #1 float32 current(A) + 1 float32 potential(V) + 1 uint8 resistor index (per target V)
        else:
            rx_len = 8 #1 float32 current(A) + 1 float32 potential(V) (per target V)
        
        temp_adc = np.zeros((v_count,2),np.float32) #array to hold ADC readings
        temp_res_val = np.zeros((v_count),np.float32) #array to hold resistor multipliers

        # retry=False: re-sending EXECUTE while the firmware is already streaming
        # would corrupt the batch protocol; a failed ack must abort instead.
        self._write_cmd(Commands.EXECUTE_VOLTAGE_BATCH,np.uint32([delay,v_count]),True,retry=False) #start the protocol
        for i in range(0,v_count,v_per_chunk): #for each target V
            data = self._read_from_buffer(rx_len * v_per_chunk) #read
            if (tx_ind + tx_len) <= v_byte_count: #write if the target V list is not exhausted
                self._write_to_buffer(v_bytes[tx_ind:tx_ind+tx_len])
                tx_ind += tx_len

            temp_ind = 0 #temp index in data
            for j in range(v_per_chunk):
                # Read raw ADC values (Current, Potential)
                adc_vals = np.frombuffer(data, np.float32, 2, temp_ind)
                temp_adc[i+j,:] = adc_vals 
                temp_ind += 8
                
                # Check for Frame Shift via Potential Range (Approx +/- 15V limit)
                # Raw float interpretation of wrong bytes often yields huge numbers
                if np.any(np.abs(adc_vals) > 15.0):
                     logger.warning("[PS %s] Frame shift detected! Invalid potential/current raw: %s",
                                    self.device_ID, adc_vals)
                     temp_adc[i+j,:] = np.nan
                
                if self._auto_gain:
                    temp_gain = np.frombuffer(data, np.uint8, 1, temp_ind)[0]
                    temp_ind += 1
                    
                    # Validate Resistor Index
                    if temp_gain in self.res_vals:
                         temp_res_val[i+j] = self.res_vals[temp_gain]
                    else:
                         logger.warning("[PS %s] Frame shift detected! Invalid gain index: %s",
                                        self.device_ID, temp_gain)
                         temp_res_val[i+j] = np.nan
                         temp_adc[i+j,:] = np.nan # Invalidate data too

        #Process the data
        temp_adc = self._scale_output_voltages(temp_adc)
        if self._auto_gain:
            temp_adc[:,0] = np.divide(temp_adc[:,0],temp_res_val)
            self.Resistor = temp_gain
            self.ResistorVal = temp_res_val[-1]
        else:
            temp_adc[:,0] /= self.ResistorVal
        temp_adc[:,0] *= -1

        if pad_count == 0:
            return temp_adc
        
        return temp_adc[:-pad_count,:] #get rid of the results of padding

    def write_dac(self, channels, voltages):
        """Write Digital to Analog Converter values to potentiostat.
        Consult "DAC" class for channel information.
        Channels and voltages must refer to same index.
        i.e., channel[1]'s target value should be given in voltages[1]

        Args:
            channels (list): list of channel(s)
            voltages (list): list of voltages(s)

        """
        new_voltages = self._scale_input_voltages(voltages)
        data = bytearray()
        data.extend(np.uint8(channels).tobytes())
        data.extend(np.float32(new_voltages).tobytes())
        self._write_cmd(Commands.WRITE_DAC,data)

    def write_switch(self, state:bool):
        """Change the switch state between CE and WE. True for connected, False for disconnected.

        Args:
            state (bool): Whether the switch is on

        """
        self._write_cmd(Commands.WRITE_SWITCH,np.uint8(state))
        self._switch_state = state

    def read_switch(self)-> bool:
        """Returns the switch state between CE and WE. True for connected, False for disconnected.

        Returns:
            bool: Whether the switch is on
        """
        with self._port_lock:
            self._write_cmd(Commands.READ_SWITCH,None,False)
            result = self._read_with_retry(1, Commands.READ_SWITCH, None)
        result = np.frombuffer(result,np.uint8)
        if result == 0:
            self._switch_state = False
        else:
            self._switch_state = True
        return result
    
    def read_diagnostics(self) -> dict:
        """Read the firmware's health report (requires firmware with the
        diagnostics query, July 2026 or later).

        Returns a dict:
            reset_flags: raw reset-cause register captured when the device
                last booted (each bit is one possible cause).
            watchdog_reset: True if the last reboot was forced by the
                device's watchdog — i.e. the firmware froze mid-run.
            tx_dropped: data messages the device discarded because this
                computer stopped reading in time.
            rx_overflow: corrupted/oversized command frames discarded.
            starved_steps: sampling periods the device spent holding its
                potential while waiting for waveform data from this computer.

        After a clean run everything except a normal power-up flag is zero.
        Non-zero counters mean the run was degraded and should be repeated.
        """
        with self._port_lock:
            self._write_cmd(Commands.READ_DIAGNOSTICS, None, False)
            result = self._read_with_retry(16, Commands.READ_DIAGNOSTICS, None)
        vals = np.frombuffer(result, np.uint32)
        return {
            'reset_flags': int(vals[0]),
            'watchdog_reset': bool(int(vals[0]) & (1 << 29)),  # IWDG flag bit
            'tx_dropped': int(vals[1]),
            'rx_overflow': int(vals[2]),
            'starved_steps': int(vals[3]),
        }

    def write_gain(self, resistor: Resistors = Resistors.R_100):
        """Change gain by switching resistor between CE and WE

        Args:
            resistor (Resistors, optional): Target resistor (gain) index. Defaults to Resistors.R_100.
        """
        # Prevent redundant writes
        if hasattr(self, 'Resistor') and self.Resistor == resistor:
            return

        self._write_cmd(Commands.WRITE_GAIN,np.uint8(resistor))
        self.Resistor = resistor
        self.ResistorVal = self.res_vals[self.Resistor]
    
    def read_gain(self):
        """Returns currently active resistor index (gain) between CE and WE

        Returns:
            Resistor: Current resistor index
        """
        with self._port_lock:
            self._write_cmd(Commands.READ_GAIN,np.uint8(0),False)
            result = self._read_with_retry(1, Commands.READ_GAIN, np.uint8(0))
        result = np.frombuffer(result,np.uint8)
        gain_idx = int(result[0])
        if gain_idx not in self.res_vals:
            # Out-of-range index means the response stream is desynced; treat it
            # like a failed read so callers' timeout tolerance applies.
            self.serial.reset_input_buffer()
            raise SerialReadTimeout(f"Invalid gain index {gain_idx} in READ_GAIN response")
        self.Resistor = gain_idx
        self.ResistorVal = self.res_vals[self.Resistor]
        return self.Resistor
    
    
    # Per-board calibration, measured end-to-end by the onboarding sweep.
    # The set pair maps a requested current to the true delivered current;
    # the read pair maps the board's own current reading (nominal mA) to the
    # true current. Defaults reproduce the historical average-board behavior
    # so an uncalibrated device acts exactly as it did before onboarding
    # existed (write: divide by 1.0989; read: raw_A * 976.9 - 0.6).
    _calib_slope_correction = 1.0989
    _calib_intercept = 0.0
    _calib_read_slope = 0.9769
    _calib_read_intercept = -0.6

    # Nominal internal chain at the 100-ohm gain: while the firmware's hold
    # loop is converged, the board's own reading (mA) = 1.13 * command + 0.6.
    # Follows from the command model in _current_to_voltage (113 ohm, 0.06 V)
    # and the 100-ohm shunt; used only to derive a read fallback below.
    _NOMINAL_READ_GAIN = 1.13
    _NOMINAL_READ_OFFSET = 0.6

    # Per-board voltage calibration, measured by the onboarding voltage sweep.
    # Set pair maps a requested potential to the true applied potential; read
    # pair maps the board's own potential reading to the true value.
    #
    # Defaults are IDENTITY on purpose: the historical global correction
    # (x0.9919 +/- 0.0494 V) already lives inside _scale_input_voltages /
    # _scale_output_voltages, which every raw write and read passes through,
    # and the onboarding sweep measures its fits through those same raw
    # calls — so the stored pairs are corrections ON TOP of raw. The old
    # defaults of +0.0494 applied the historical offset a second time on
    # every *_calibrated call, shifting OCP reads and parked potentials by
    # ~49 mV each way on boards without a stored voltage calibration
    # (bench-diagnosed 2026-08-14: asymmetric R_u steps, +49 mV live-CV
    # axis).
    _calib_v_slope = 1.0
    _calib_v_intercept = 0.0
    _calib_v_read_slope = 1.0
    _calib_v_read_intercept = 0.0

    def set_voltage_calibration_params(self, slope, intercept, read_slope=None, read_intercept=None):
        """Set per-board voltage calibration from the onboarding registry.

        Unlike the current calibration, no read fallback can be derived from
        the set pair — the drive (DAC -> CE) and sense (RE -> ADC) chains are
        physically independent — so a missing read pair keeps the historical
        default until the board is onboarded with the voltage sweep.
        """
        self._calib_v_slope = slope
        self._calib_v_intercept = intercept
        if read_slope is not None and read_intercept is not None:
            self._calib_v_read_slope = read_slope
            self._calib_v_read_intercept = read_intercept
        else:
            logger.info("[PS %s] No voltage read calibration stored; keeping "
                        "the default. Re-onboard this board to measure it.",
                        self.device_ID)
        logger.info("[PS %s] Voltage calibration set: slope=%s, intercept=%s, "
                    "read_slope=%s, read_intercept=%s",
                    self.device_ID, self._calib_v_slope, self._calib_v_intercept,
                    self._calib_v_read_slope, self._calib_v_read_intercept)

    def set_calibration_params(self, slope, intercept, read_slope=None, read_intercept=None):
        """Set per-board calibration from the onboarding registry.

        ``slope``/``intercept`` are the full set-current correction (no global
        hardware factor is applied on top — the onboarding fit already spans
        the whole signal chain). When the read pair is missing (registry rows
        saved before the read fit existed), it is derived from the set pair
        through the nominal internal chain; re-onboarding replaces this
        approximation with measured values.
        """
        self._calib_slope_correction = slope
        self._calib_intercept = intercept
        if read_slope is None or read_intercept is None:
            self._calib_read_slope = slope / self._NOMINAL_READ_GAIN
            self._calib_read_intercept = (
                intercept - self._NOMINAL_READ_OFFSET * self._calib_read_slope
            )
            logger.info("[PS %s] No read calibration stored; derived from set "
                        "calibration. Re-onboard this board to measure it.",
                        self.device_ID)
        else:
            self._calib_read_slope = read_slope
            self._calib_read_intercept = read_intercept
        logger.info("[PS %s] Calibration set: slope=%s, intercept=%s, "
                    "read_slope=%s, read_intercept=%s",
                    self.device_ID, self._calib_slope_correction,
                    self._calib_intercept, self._calib_read_slope,
                    self._calib_read_intercept)

    def write_current_hold(self, target_current_mA=0.1, initial_step_V = 0.00015, learning_rate = 0.05, force_gain = False, voltage_guess=None):
        # Bench-verified July 2026: boards deliver at least 30 mA, so the legacy
        # 28 mA ceiling was raised to match.
        MAX_CURRENT_MA = 30.0
        if abs(target_current_mA) > MAX_CURRENT_MA:
            logger.warning("[PS %s] Target %.2f mA exceeds hardware compliance limit (%s mA). Clamping.",
                           self.device_ID, target_current_mA, MAX_CURRENT_MA)
            target_current_mA = np.sign(target_current_mA) * MAX_CURRENT_MA

        target_A = target_current_mA * 1e-3 
        if force_gain == False:
            self.write_gain(self._select_100ohm_resistor(target_A)) #guess the best resistor for highest gain without saturation
        
        # Use provided voltage guess if available, otherwise calculate from static model
        if voltage_guess is not None:
             dac_val = self._scale_input_voltages(voltage_guess)
        else:
             dac_val = self._current_to_voltage(target_A)
             
        #ADD "LEARNING RATE" AND REPEAT COUNT PARAMS
        msg = bytearray()
        msg.extend(np.float32([dac_val,initial_step_V,learning_rate]).tobytes())
        self._write_cmd(Commands.WRITE_CURRENT_HOLD,msg)

    def write_current_hold_calibrated(self, target_current_mA: np.float32, **kwargs):
        """Hold ``target_current_mA`` corrected by the board's set calibration.

        The stored slope/intercept span the entire signal chain (measured
        against a multimeter during onboarding), so they are the only
        correction applied — no additional global hardware factor.
        """
        slope = self._calib_slope_correction
        # Avoid div by zero on a malformed calibration row
        if abs(slope) < 1e-6:
            slope = 1.0

        current = (target_current_mA - self._calib_intercept) / slope
        self.write_current_hold(current, force_gain=False, **kwargs)

    def set_alternating_current(self, target_current_mA: float, voltage_guess: float = None):
        """
        Sets the current for an AC waveform step. 
        This is a wrapper around write_current_hold_calibrated to make the intent clear.
        """
        self.write_current_hold_calibrated(target_current_mA, voltage_guess=voltage_guess)

    def write_sample_count(self, sample_count=10):
        """Sets the number of samples to be averaged per channel

        Args:
            sample_count (int, optional): Number of samples to be averaged per channel. Defaults to 10.
        """
        self._write_cmd(Commands.WRITE_SAMPLE_COUNT,np.uint32(sample_count))

    def write_current_hold_stop(self):
        """Stop current hold tasl
        """
        self._write_cmd(Commands.STOP_CURRENT_HOLD,None)

    def read_DAC(self,channels):
        """Read Digital to Analog Converter values from potentiostat.
        Consult "DAC" class for channel information

        Args:
            channels (list): list of channel(s)

        Returns:
            list: List of float32 DAC values from the requested channels
        """
        channels = np.ones(1,np.uint8) * channels
        with self._port_lock:
            self._write_cmd(Commands.READ_DAC,np.uint8(channels),False)
            result = self._read_with_retry(
                np.uint8(channels).size * 4, Commands.READ_DAC, np.uint8(channels)
            ) #channel count x float32
        result = np.frombuffer(result,np.float32).copy()
        result = self._scale_output_voltages(result)
        return result
    
    def read_ADC(self,channels):
        """Read Analog to Digital Converter values from potentiostat.
        Consult "ADC" class for channel information

        Args:
            channels (list): list of channel(s)

        Returns:
            list: List of float32 ADC values from the requested channels
        """
        #read ADC values
        channels = np.ones(1,np.uint8) * channels
        with self._port_lock:
            self._write_cmd(Commands.READ_ADC,np.uint8(channels),False)
            result = self._read_with_retry(
                np.uint8(channels).size * 4, Commands.READ_ADC, np.uint8(channels)
            ) #channel count x float32
        result = np.frombuffer(result,np.float32).copy()
        for i in range(len(channels)):
            if channels[i] == ADC.WE_OUT: #V-I conversion channel, process by resistor value
                result[i] = self._scale_output_voltages(result[i])
                result[i] /= (-1 * self.res_vals[self.Resistor])
            elif (channels[i] == ADC.TEMP) or (channels[i] == ADC.HUMID): #Skip temperature and humidiy
               pass 
            else:  #normal ADC, just scale the range
                result[i] = self._scale_output_voltages(result[i])
        return result
    
    def read_ADC_gain(self,channels):
        """Read Analog to Digital Converter values from potentiostat
        Additionally request for gain information (resistor index).
        Consult "ADC" class for channel information.
        This function automatically updates the resistor value of the class.
        Also processes the current reading accordingly.
        
        Args:
            channels (list): list of channel(s)

        Returns:
            list: List of float32 ADC values from the requested channels
        """
        channels = np.ones(1,np.uint8) * channels
        with self._port_lock:
            self._write_cmd(Commands.READ_ANALOG_GAIN,np.uint8(channels),False)
            result = self._read_with_retry(
                (np.uint8(channels).size * 4) + 1,
                Commands.READ_ANALOG_GAIN,
                np.uint8(channels),
            )
        ch_result = result[0:(len(channels) * 4)]
        ch_result = np.frombuffer(ch_result,np.float32).copy()
        gain_result = result[(len(channels) * 4):]
        gain_result = np.frombuffer(gain_result,np.uint8).copy()
        gain_idx = int(gain_result[0])
        if gain_idx not in self.res_vals:
            # An impossible gain index means the response frame is shifted —
            # raise as a read failure (used to be a KeyError that killed the
            # run) so the measurement loops' timeout tolerance applies.
            self.serial.reset_input_buffer()
            raise SerialReadTimeout(
                f"Invalid gain index {gain_idx} in READ_ANALOG_GAIN response"
            )
        self.Resistor = gain_idx
        self.ResistorVal = self.res_vals[self.Resistor]
        for i in range(len(channels)):
            if channels[i] == ADC.WE_OUT:
                ch_result[i] = self._scale_output_voltages(ch_result[i])
                ch_result[i] /= (-1 * self.res_vals[self.Resistor])
            elif (channels[i] == ADC.TEMP) or (channels[i] == ADC.HUMID):
               pass 
            else:
                ch_result[i] = self._scale_output_voltages(ch_result[i])
        return ch_result
    
    def read_auto_gain(self) -> bool:
        """_summary_

        Returns:
            bool: Whether the auto-gain is turned on.
        """
        with self._port_lock:
            self._write_cmd(Commands.READ_AUTO_GAIN,np.uint8(0),False)
            result = self._read_with_retry(1, Commands.READ_AUTO_GAIN, np.uint8(0)) #single byte bool
        result = np.frombuffer(result,np.uint8) #cast to uint8
        if (result == 0):
            self._auto_gain = False
        else:
            self._auto_gain = True
        return self._auto_gain
    
    def write_auto_gain(self,state:bool=False) -> bool:
        """_summary_

        Args:
            state (bool, optional): Whether the auto-gain is turned on(True). Defaults to False(off).

        Returns:
            bool: _description_
        """
        self._write_cmd(Commands.WRITE_AUTO_GAIN,np.uint8(state)) #single byte state bool
        self._auto_gain = state
        return self._auto_gain

    def read_current(self):
        """Read A current (I) between WE and CE

        Returns:
            np.float32: Current (I) between WE and CE (A)
        """
        return self.read_ADC_gain(channels=[ADC.WE_OUT])
    
    def read_current_calibrated(self):
        """Return the current in mA, corrected by the board's read calibration."""
        val_mA = self.read_current() * 1000.0
        return val_mA * self._calib_read_slope + self._calib_read_intercept

    def calibrate_batch_current_uA(self, raw_A):
        """Convert raw batch-stream current (A) to uA with the board's read
        calibration — the same correction as ``read_current_calibrated``, for
        the array data returned by the batch/streaming protocol."""
        return (np.asarray(raw_A, dtype=float) * 1000.0 * self._calib_read_slope
                + self._calib_read_intercept) * 1000.0

    def calibrate_batch_potential_V(self, raw_V):
        """Apply the board's voltage READ calibration to batch-stream
        potentials — the same correction as ``read_potential_calibrated``,
        for the array data returned by the batch/streaming protocol. Identity
        for boards without a stored voltage calibration."""
        return (np.asarray(raw_V, dtype=float) * self._calib_v_read_slope
                + self._calib_v_read_intercept)

    def _waveform_to_raw_V(self, voltages):
        """Apply the board's voltage SET calibration to a target waveform, so
        a batched sweep drives the same true potentials the calibrated
        single-point writes do. Identity for boards without a stored voltage
        calibration."""
        slope = self._calib_v_slope
        # Avoid div by zero on a malformed calibration row
        if abs(slope) < 1e-6:
            slope = 1.0
        return (np.asarray(voltages, dtype=float) - self._calib_v_intercept) / slope

    def read_potential(self):
        """Read potential (V) between WE and RE

        Returns:
            np.float32: Potential (V) between WE and RE
        """
        return self.read_ADC_gain(channels=[ADC.RE_OUT])
    
    def read_potential_calibrated(self):
        """Return the potential in V, corrected by the board's read calibration."""
        return self.read_potential() * self._calib_v_read_slope + self._calib_v_read_intercept
        
    def read_potential_current(self):
        """Read potential (V) between WE-RE and current Ampere (I) between WE-CE

        Returns:
            list: Potential (V) between WE-RE and current (I) between WE-CE (A)
        """
        return self.read_ADC_gain(channels=[ADC.RE_OUT,ADC.WE_OUT])

    def read_potential_current_calibrated(self):
        """
        Read calibrated potential (V) and current (mA).
        Returns: [Potential_V, Current_mA]
        """
        raw_vals = self.read_potential_current()
        raw_v = raw_vals[0]
        raw_i = raw_vals[1]

        # Apply calibrations matches read_potential_calibrated and read_current_calibrated
        cal_v = raw_v * self._calib_v_read_slope + self._calib_v_read_intercept
        cal_i = (raw_i * 1000.0) * self._calib_read_slope + self._calib_read_intercept

        return [cal_v, cal_i]
    
    def write_potential(self,potential_V:np.float32):
        """Read potential (V) between WE-RE in Volts

        Args:
            potential_V (np.float32): potential (V) between WE-RE in Volts
        """
        self.write_dac(DAC.CE_IN,potential_V)

    def write_potential_calibrated(self, potential_V:np.float32):
        """Apply ``potential_V`` corrected by the board's set calibration."""
        slope = self._calib_v_slope
        # Avoid div by zero on a malformed calibration row
        if abs(slope) < 1e-6:
            slope = 1.0
        self.write_potential((potential_V - self._calib_v_intercept) / slope)

    def _safe_open_switch(self):
        """Open the CE-WE switch without a voltage transient.

        Opening the switch while the setpoint disagrees with the cell potential
        causes an output spike, and any running current-hold keeps adjusting
        the DAC after the disconnect until it hits its limit (bench-verified
        July 2026). To prevent both: stop the hold, set the DAC to the
        currently sensed potential so almost no current is flowing, then open.
        """
        with self._port_lock:
            try:
                self.write_current_hold_stop()
                sensed_V = float(self.read_potential_calibrated()[0])
                self.write_potential_calibrated(sensed_V)
                time.sleep(0.05) #let residual current decay before disconnecting
            except Exception:
                # Fail-state: cut the output even if matching the DAC failed
                self.write_switch(SwitchState.Off)
                raise
            self.write_switch(SwitchState.Off)

    def read_ocp(self,restore_switch_state = True)->np.float32:
        """Read Open Circuit Potential in Volts. Disconnects CE-WE connection and reads potential.

        Args:
            restore_switch_state (bool, optional): Whether to restore previous switch state. Defaults to True.

        Returns:
            np.float32: OCP in Volts
        """
        prev_switch_state = self._switch_state
        self._safe_open_switch()
        if self._auto_gain:
            for i in range(Resistors.R_1M):
                self.read_potential_calibrated() #Read current multiple times to allow auto-gain
        result = self.read_potential_calibrated()
        if not (prev_switch_state == SwitchState.Off) and restore_switch_state:
            self.write_switch(SwitchState.On) #restore previous switch state
        return result[0]

    def _build_cv_waveform(self, min_V, max_V, cycles, step_V, start_V, last_V,
                           scan_direction='Negative'):
        """Build the sequence of target potentials for a CV sweep.

        Shared by perform_CV and perform_CV_iR so both drive an identical sweep
        shape. A 'Negative' scan starts by going down to min_V; a 'Positive'
        scan starts by going up to max_V.
        """
        cv = []

        if scan_direction == 'Positive':
            # POSITIVE SCAN: Start -> Max -> Min -> Max ... -> Last

            # 1. Start to Max
            r1 = np.abs(max_V - start_V)
            if r1 > 0:
                p1 = np.linspace(start_V, max_V, int(np.round(r1/step_V)))
                cv = np.append(cv, p1)

            # Cycle Loop
            # For Positive scan, a full cycle is Max -> Min -> Max
            r2 = np.abs(min_V - max_V) # Down
            r3 = np.abs(max_V - min_V) # Up

            p2 = np.linspace(max_V, min_V, int(np.round(r2/step_V)))
            p3 = np.linspace(min_V, max_V, int(np.round(r3/step_V)))

            for i in range(cycles):
                cv = np.append(cv, p2)
                cv = np.append(cv, p3)

            # 4. Max to Last
            r4 = np.abs(last_V - max_V)
            if r4 > 0:
                p4 = np.linspace(max_V, last_V, int(np.round(r4/step_V)))
                cv = np.append(cv, p4)

        else:
            # NEGATIVE SCAN (Default/Legacy): Start -> Min -> Max -> Min ... -> Last

            # 1. Start to Min
            r1 = np.abs(min_V - start_V)
            if r1 > 0:
                p1 = np.linspace(start_V, min_V, int(np.round(r1/step_V)))
                cv = np.append(cv, p1)

            # Cycle Loop
            # For Negative scan, a full cycle is Min -> Max -> Min
            r2 = np.abs(max_V - min_V) # Up
            r3 = np.abs(min_V - max_V) # Down

            p2 = np.linspace(min_V, max_V, int(np.round(r2/step_V)))
            p3 = np.linspace(max_V, min_V, int(np.round(r3/step_V)))

            for i in range(cycles):
                cv = np.append(cv, p2)
                cv = np.append(cv, p3)

            # 4. Min to Last
            r4 = np.abs(last_V - min_V)
            if r4 > 0:
                p4 = np.linspace(min_V, last_V, int(np.round(r4/step_V)))
                cv = np.append(cv, p4)

        return cv

    def perform_CV(self, min_V, max_V, cycles, mV_s, step_hz, start_V=None, last_V=None, scan_direction='Negative'):
        """Performs CV with given parameters."""

        #Stop any running current-hold before the OCP read below opens the
        #switch, otherwise the firmware hold loop drives the DAC to its limit
        self.write_current_hold_stop()

        #if start_V and/or last_V is None, start/end with OCP
        if (start_V is None) or (last_V is None):
            ocp = self.read_ocp()
            if start_V is None:
                start_V = ocp
            if last_V is None:
                last_V = ocp

        # Park the DAC at the sweep's first potential BEFORE closing the
        # switch, so the cell never sees a stale setpoint during the batch
        # buffer pre-fill (bench-observed as ~2 s at an uncontrolled voltage).
        # Uses the same set calibration as the batch points below, so the
        # handoff to the waveform's first point is seamless.
        self.write_potential_calibrated(start_V)
        time.sleep(0.05)
        self.write_switch(True)

        V_s = mV_s / 1000
        step_V = V_s / step_hz

        cv = self._build_cv_waveform(min_V, max_V, cycles, step_V,
                                     start_V, last_V, scan_direction)

        rtn = self.write_voltage_batch(voltages = self._waveform_to_raw_V(cv),
                                       delay = int(np.round(1000/step_hz)))
        num_points = rtn.shape[0]
        cycle_points = num_points // cycles
        duration_s = num_points / step_hz
        time_stamps = np.linspace(0, duration_s, num_points)
        cycle_nums = np.zeros(num_points)
        for i in range(0, cycles):
            cycle_nums[i * cycle_points: (i + 1) * cycle_points] = np.full(cycle_points, i)
        return np.column_stack((time_stamps,
                                self.calibrate_batch_potential_V(rtn[:, 1]),
                                self.calibrate_batch_current_uA(rtn[:, 0]),
                                cycle_nums, np.zeros(num_points), cv))

    # An exponential fit whose time constant is shorter than this many sample
    # intervals cannot be extrapolated back to t=0 with any confidence — the
    # decay was essentially over before the second reading.
    _RU_MIN_TAU_INTERVALS = 2.0

    # Below this relative decay across the trace, the current is treated as
    # flat: purely resistive behaviour (e.g. a dummy cell) rather than an
    # RC transient.
    _RU_FLAT_DECAY_FRACTION = 0.15

    @staticmethod
    def _fit_ru_decay(times_ms, currents_A, step_V):
        """Estimate R_u from one post-step current transient.

        Fits I(t) = I0*exp(-t/tau) + C and evaluates the fit at t=0 — the
        instant the switch closed, when the double-layer had not yet charged
        and the cell was purely resistive — so R_u = step / I(0). This
        replaces taking the largest raw sample, which by the time the serial
        link delivers it is already well down the decay.

        A trace with no visible decay is treated as purely resistive and
        averaged directly (a dummy-cell resistor really behaves this way; for
        a live cell it means the transient was too fast to catch, which the
        message flags). A fitted time constant shorter than a couple of
        sample intervals means the extrapolation is unconstrained, so the
        measurement is refused rather than returning a number that cannot be
        trusted.

        Returns:
            dict: 'R_u_ohm', 'tau_ms' (None when no decay was fit), 'ok',
                and 'message' (empty when there is nothing to flag).
        """
        times_ms = np.asarray(times_ms, dtype=float)
        currents_A = np.asarray(currents_A, dtype=float)
        n_tail = max(3, len(currents_A) // 6)

        if np.max(np.abs(currents_A)) < 1e-9:
            return {'R_u_ohm': float('inf'), 'tau_ms': None, 'ok': False,
                    'I_at_0_A': None,
                    'message': ("No current flowed — check the cell "
                                "connections and that the electrolyte "
                                "conducts.")}

        tail_C = float(np.mean(currents_A[-n_tail:]))

        # Spike-aware flatness: compare the largest excursion from the
        # settled tail against the tail itself, on a lightly smoothed trace
        # so one noisy sample cannot mimic a transient. A head-mean check
        # here once diluted a sub-millisecond transient into invisibility
        # over a long capture window (bench 2026-08-14) and called a
        # faradaically active cell "purely resistive".
        if len(currents_A) >= 3:
            # Pad with the edge values: zero-padding would fake an excursion
            # at both ends of a perfectly flat trace.
            padded = np.concatenate(([currents_A[0]], currents_A,
                                     [currents_A[-1]]))
            smoothed = np.convolve(padded, np.ones(3) / 3.0, mode='valid')
        else:
            smoothed = currents_A
        dev = smoothed - tail_C
        peak_idx = int(np.argmax(np.abs(dev)))
        peak_dev = float(dev[peak_idx])

        if abs(peak_dev) < Potentiostat._RU_FLAT_DECAY_FRACTION * abs(tail_C):
            I_flat = float(np.mean(currents_A))
            return {'R_u_ohm': abs(step_V / I_flat), 'tau_ms': None,
                    'ok': True, 'I_at_0_A': I_flat,
                    'message': ("no decay visible — treated as purely "
                                "resistive; on a real cell this means the "
                                "transient was too fast to sample")}

        from scipy.optimize import curve_fit

        def _model(t, I0, tau, C):
            return I0 * np.exp(-t / tau) + C

        span_ms = max(times_ms[-1] - times_ms[0], 1e-3)
        # Seed the time constant from where the excursion falls to 1/e of
        # its peak, so a transient spanning only a sliver of the capture
        # still starts the fit in the right decade.
        below = np.nonzero(np.abs(dev[peak_idx:]) <= abs(peak_dev) / np.e)[0]
        if len(below):
            tau0 = max(times_ms[peak_idx + below[0]] - times_ms[peak_idx], 1e-3)
        else:
            tau0 = span_ms / 3.0
        p0 = [peak_dev, tau0, tail_C]
        try:
            popt, _ = curve_fit(_model, times_ms, currents_A, p0=p0,
                                bounds=([-np.inf, 1e-6, -np.inf],
                                        [np.inf, np.inf, np.inf]),
                                maxfev=5000)
        except (RuntimeError, ValueError):
            return {'R_u_ohm': float('inf'), 'tau_ms': None, 'ok': False,
                    'I_at_0_A': None,
                    'message': ("the current transient did not fit an "
                                "exponential decay — measurement refused")}

        I0, tau_ms, C = popt
        dt_ms = float(np.median(np.diff(times_ms))) if len(times_ms) > 1 else 0.0
        if tau_ms < Potentiostat._RU_MIN_TAU_INTERVALS * dt_ms:
            return {'R_u_ohm': float('inf'), 'tau_ms': float(tau_ms),
                    'ok': False, 'I_at_0_A': None,
                    'message': (f"decay too fast to extrapolate (tau = "
                                f"{tau_ms:.1f} ms vs {dt_ms:.1f} ms between "
                                f"samples) — measurement refused")}

        I_at_0 = I0 + C
        if abs(I_at_0) < 1e-9:
            return {'R_u_ohm': float('inf'), 'tau_ms': float(tau_ms),
                    'ok': False, 'I_at_0_A': None,
                    'message': "fitted current at t=0 is zero — refused"}

        return {'R_u_ohm': abs(step_V / I_at_0), 'tau_ms': float(tau_ms),
                'ok': True, 'I_at_0_A': float(I_at_0), 'message': ''}

    def step_capture(self, n_samples, period_us):
        """Firmware-timed step-response capture (CMD_STEP_CAPTURE).

        The firmware closes the CE switch itself and burst-samples the raw
        current channel at a cycle-counter-timed pace, so the transient is
        recorded from within microseconds of the switch closing — over the
        serial link the first reading lands one round trip (~3 ms) later,
        past most of the decay at typical cell time constants.

        The DAC must already be parked at the step potential and the switch
        open. On success the switch is left CLOSED; follow with
        ``_safe_open_switch()``. Raises ``SerialReadTimeout`` on firmware
        without the command (it sends no acknowledgement), which is how the
        capability probe in ``_acquire_step_trace`` detects old firmware.

        Args:
            n_samples (int): Samples requested. The firmware clamps to its
                buffer (~32k) and to a 1 s total duration.
            period_us (int): Sample period in microseconds (floor 25).

        Returns:
            tuple: ``(counts, duration_us, gain_idx)`` — raw uint16 ADC
                counts, the measured wall-clock duration of the capture, and
                the gain resistor index it ran at.
        """
        n_samples = int(n_samples)
        period_us = int(period_us)
        expected_s = n_samples * period_us / 1e6
        with self._port_lock:
            old_timeout = self.serial.timeout
            try:
                # The acknowledgement only arrives after the whole capture
                self.serial.timeout = expected_s + 2.0
                self._write_cmd(Commands.STEP_CAPTURE,
                                np.uint32([n_samples, period_us]), False)
                ack = self.serial.read(9)
                if len(ack) < 9:
                    raise SerialReadTimeout(
                        "No step-capture acknowledgement — firmware without "
                        "CMD_STEP_CAPTURE?")
                captured = int(np.frombuffer(ack, np.uint32, 1, 0)[0])
                duration_us = int(np.frombuffer(ack, np.uint32, 1, 4)[0])
                gain_idx = int(ack[8])
                if captured == 0:
                    raise SerialReadTimeout("Step capture returned no samples")
                self._write_cmd(Commands._READ_BUFFER,
                                np.uint32([0, captured * 2]), False)
                raw = self.serial.read(captured * 2)
                if len(raw) < captured * 2:
                    raise SerialReadTimeout(
                        f"Step-capture readback short: {len(raw)} of "
                        f"{captured * 2} bytes")
            finally:
                self.serial.timeout = old_timeout
        counts = np.frombuffer(raw, np.uint16).copy()
        return counts, duration_us, gain_idx

    def _capture_counts_to_mA(self, counts, gain_idx):
        """Convert raw step-capture ADC counts to calibrated mA.

        Same chain as ``read_current_calibrated``: counts -> ADC volts ->
        physical volts -> amps through the gain resistor -> the board's read
        calibration.
        """
        v_adc = np.asarray(counts, dtype=float) * ADC16_COUNTS_TO_V
        v = self._scale_output_voltages(v_adc)
        i_A = v / (-1.0 * self.res_vals[gain_idx])
        return (i_A * 1000.0) * self._calib_read_slope + self._calib_read_intercept

    def _acquire_step_trace(self, n_readings, capture_ms, capture_period_us):
        """Close the switch and record the post-step current transient.

        Prefers the firmware capture; falls back to the serial burst (one
        reading per round trip) on firmware without CMD_STEP_CAPTURE, and
        remembers the answer for the rest of the connection. The DAC must
        already be parked at the step potential with the switch open; the
        switch is left closed either way.

        Returns:
            tuple: ``(times_ms, currents_A, source, baseline_A)`` with t=0 at
                the switch closing, ``source`` either ``'firmware'`` or
                ``'serial'``, and ``baseline_A`` the zero-current reading that
                was subtracted from the whole trace.
        """
        # With the switch still open the true cell current is exactly zero,
        # so whatever the chain reads now is its zero-offset — calibration
        # intercept error plus drift — at this gain, this minute. R_u's
        # transient currents are the same order as the raw offset the
        # calibration cancels (~0.5 mA), so a small residual offset lands
        # directly in R_u unless it is measured and subtracted here.
        baseline_A = float(np.mean([
            float(self.read_current_calibrated()[0]) / 1000.0
            for _ in range(5)]))

        if getattr(self, '_step_capture_supported', False) is not False:
            try:
                n = max(2, int(round(capture_ms * 1000.0 / capture_period_us)))
                counts, duration_us, gain_idx = self.step_capture(
                    n, capture_period_us)
                self._step_capture_supported = True
                dt_ms = (duration_us / 1000.0) / len(counts)
                times_ms = (np.arange(len(counts)) + 0.5) * dt_ms
                currents_A = self._capture_counts_to_mA(counts, gain_idx) / 1000.0
                # Drop railed samples (ADC pinned at either end): they carry
                # no amplitude information and would drag the fit.
                ok = (counts > 100) & (counts < 65435)
                if ok.sum() >= 10:
                    times_ms = times_ms[ok]
                    currents_A = currents_A[ok]
                return times_ms, currents_A - baseline_A, 'firmware', baseline_A
            except SerialReadTimeout as e:
                self._step_capture_supported = False
                logger.info("[PS %s] Firmware step capture unavailable (%s); "
                            "falling back to the serial burst.",
                            self.device_ID, e)
                if getattr(self, 'serial', None) is not None:
                    self.serial.reset_input_buffer()

        times_ms = np.zeros(n_readings)
        currents_A = np.zeros(n_readings)
        # Hold the port for the whole burst so another thread's commands
        # cannot interleave and stretch the decay's timing.
        with self._port_lock:
            self.write_switch(SwitchState.On)
            t0 = time.time()
            for i in range(n_readings):
                currents_A[i] = float(self.read_current_calibrated()[0]) / 1000.0
                times_ms[i] = (time.time() - t0) * 1000.0
        return times_ms, currents_A - baseline_A, 'serial', baseline_A

    def measure_R_u(self, step_mV=50.0, n_readings=30, settle_s=2.0):
        """Measure the cell's uncompensated (solution) resistance, R_u.

        Applies a small, sudden potential step away from OCP and records the
        current decay. On firmware with CMD_STEP_CAPTURE the decay is sampled
        on-device from within microseconds of the step (see
        ``_acquire_step_trace``); otherwise it is read over the serial link,
        one round trip per point. Either way the decay is fitted with an
        exponential and extrapolated back to the instant of the step, when
        the cell behaved like a plain resistor, giving R_u = step / I(0).

        The step is made twice, once in each direction from OCP. Alternating
        polarity cancels the drift a single repeated step leaves behind, and
        the primary estimate is taken from the DIFFERENCE of the two
        extrapolated t=0 currents, so any constant current-reading offset —
        which rides equally on both polarities and is the same order as the
        transient currents themselves — cancels exactly. Each trace is
        additionally zeroed against a switch-open baseline read just before
        its step.

        Before the two steps, a CONTROL capture is taken with the DAC parked
        at OCP itself: no driving force, so no ohmic or charging current,
        and whatever transient it records is the instrument's own
        switch-close artifact (charge injection, amplifier settling —
        bench-observed as a one-sample spike). That template is subtracted
        from both step traces, so the artifact is measured out rather than
        fitted around.

        Args:
            step_mV (float): Step size in mV. Keep it small (20–100 mV) so the
                step does not drive a reaction. Default 50 mV.
            n_readings (int): Number of current readings per step when the
                serial fallback is used. Default 30.
            settle_s (float): Hold time at the parked potential before each
                step, to let the cell relax. Default 2.0 s.

        Returns:
            dict: 'R_u_ohm' (inf when refused), 'fit_ok', 'message',
                'polarities' (per-step fits and full decay traces), plus the
                legacy keys 'step_V', 'I_peak_A', 'times_ms', 'currents_A'
                describing the first step.
        """
        # Force the 100 ohm gain: the current spike right after the step would
        # saturate a higher-gain resistor and clip the transient this
        # measurement depends on.
        prev_gain = getattr(self, 'Resistor', Resistors.R_100)
        prev_auto = self._auto_gain

        polarities = []
        control = None

        try:
            self.write_auto_gain(False)
            self.write_gain(Resistors.R_100)

            # Control capture: park at OCP itself and record the switch
            # closing with zero driving force. This trace IS the instrument
            # artifact, and is subtracted from both step traces below.
            ocp0 = float(self.read_ocp(restore_switch_state=False))
            self.write_potential_calibrated(ocp0)
            time.sleep(settle_s)
            c_times, c_currents, c_source, c_baseline = \
                self._acquire_step_trace(n_readings, RU_CAPTURE_MS,
                                         RU_CAPTURE_PERIOD_US)
            self._safe_open_switch()
            control = {'times_ms': c_times, 'currents_A': c_currents,
                       'baseline_A': c_baseline, 'ocp': ocp0,
                       'source': c_source}
            logger.info("[PS %s] R_u control (no step, %s): OCP=%.4f V, "
                        "baseline=%.1f uA, artifact peak=%.1f uA",
                        self.device_ID, c_source, ocp0, c_baseline * 1e6,
                        float(np.max(np.abs(c_currents))) * 1e6)

            for polarity in (+1.0, -1.0):
                step_V = polarity * step_mV / 1000.0

                # read_ocp leaves the switch open, which is where the step
                # begins; re-read before each step because OCP drifts while
                # the cell recovers from the previous one.
                ocp = float(self.read_ocp(restore_switch_state=False))

                E_step = ocp + step_V
                if abs(E_step) > HW_VOLTAGE_COMPLIANCE_V:
                    raise ValueError(
                        f"Step target {E_step:.3f} V is beyond the board's "
                        f"output limit ({HW_VOLTAGE_COMPLIANCE_V} V). Reduce "
                        f"step_mV.")

                # Park the DAC at the step potential before connecting the
                # cell, so the step is as sharp as the hardware allows.
                self.write_potential_calibrated(E_step)
                time.sleep(settle_s)

                times_ms, currents_A, source, baseline_A = \
                    self._acquire_step_trace(
                        n_readings, RU_CAPTURE_MS, RU_CAPTURE_PERIOD_US)

                self._safe_open_switch()

                # Remove the instrument's switch-close artifact, measured by
                # the control capture: the template is the control minus its
                # own settled tail (so no DC transfers), applied only inside
                # the artifact window (so tail noise and OCP drift do not
                # leak in). Interpolation tolerates rail-masking having
                # dropped different samples from the two traces.
                c_t = control['times_ms']
                c_i = control['currents_A']
                c_tail = float(np.mean(c_i[-max(3, len(c_i) // 6):]))
                template = np.interp(times_ms, c_t, c_i - c_tail)
                template[times_ms > RU_ARTIFACT_WINDOW_MS] = 0.0
                currents_A = currents_A - template

                fit = self._fit_ru_decay(times_ms, currents_A, step_V)
                fit.update(step_V=step_V, ocp=ocp, source=source,
                           baseline_A=baseline_A,
                           times_ms=times_ms, currents_A=currents_A)
                polarities.append(fit)

                logger.info("[PS %s] R_u step %+.0f mV (%s): OCP=%.4f V, "
                            "baseline=%.1f uA, tau=%s ms, R_u=%.1f ohm, "
                            "ok=%s %s",
                            self.device_ID, step_V * 1e3, source, ocp,
                            baseline_A * 1e6,
                            ('%.1f' % fit['tau_ms']) if fit['tau_ms'] is not None else '-',
                            fit['R_u_ohm'], fit['ok'], fit['message'])

        except Exception:
            self.write_switch(SwitchState.Off)  # Fail-state: cut the output before raising
            raise

        finally:
            # Restoring the gain must never mask the original failure
            try:
                self.write_gain(prev_gain)
                self.write_auto_gain(prev_auto)
            except Exception:
                logger.warning("[PS %s] Could not restore the gain setting "
                               "after the R_u measurement.", self.device_ID)

        good = [p for p in polarities if p['ok']]
        # A polarity that went through the flat "resistive" branch has no
        # fitted time constant; one with a real fitted transient is the more
        # trustworthy of the two when they disagree.
        proper = [p for p in good if p['tau_ms'] is not None]
        flat = [p for p in good if p['tau_ms'] is None]

        if good:
            use = good
            fit_ok = True
            notes = []
            lo, hi = (sorted(p['R_u_ohm'] for p in good) + [0, 0])[:2]
            disagree = (len(good) == 2) and (hi > 1.25 * lo)

            # Offset-immune combined estimate: a constant reading offset adds
            # the same phantom current to both polarities, and cancels in the
            # difference of the two extrapolated t=0 currents. Valid when
            # both polarities carry ohmic information — two fitted
            # transients, or two agreeing flat (resistive) traces. A flat
            # trace that disagrees with a fitted one is faradaic, and its
            # current would poison the difference.
            R_diff = None
            if (len(good) == 2
                    and all(p['I_at_0_A'] is not None for p in good)
                    and (len(proper) == 2 or (len(flat) == 2 and not disagree))):
                dI = good[0]['I_at_0_A'] - good[1]['I_at_0_A']
                dE = good[0]['step_V'] - good[1]['step_V']
                if abs(dI) > 1e-9:
                    R_diff = abs(dE / dI)

            if disagree and len(proper) == 1:
                # A genuine resistance is symmetric; the flat polarity is
                # sustained faradaic current, not R_u — drop it.
                use = proper
                notes.append(f"the {flat[0]['step_V'] * 1e3:+.0f} mV step "
                             f"showed no transient ({flat[0]['R_u_ohm']:.0f} "
                             f"ohm apparent, likely sustained faradaic "
                             f"current) and was ignored")
            elif disagree and len(proper) == 0:
                fit_ok = False
            elif disagree and R_diff is not None:
                notes.append(f"per-step estimates disagree ({lo:.0f} vs "
                             f"{hi:.0f} ohm) — consistent with a constant "
                             f"reading offset, which the combined estimate "
                             f"cancels")
            elif disagree:
                notes.append(f"the two step polarities disagree "
                             f"({lo:.0f} vs {hi:.0f} ohm) — let the cell "
                             f"rest and measure again")
            notes = sorted({p['message'] for p in use if p['message']}) + notes

            if fit_ok:
                if R_diff is not None:
                    R_u = float(R_diff)
                else:
                    R_u = float(np.mean([p['R_u_ohm'] for p in use]))
                message = "; ".join(notes)
            else:
                R_u = float('inf')
                message = (f"both step polarities were flat yet disagree "
                           f"({lo:.0f} vs {hi:.0f} ohm) — sustained faradaic "
                           f"current suspected, not solution resistance; "
                           f"reduce the step size and re-measure")
                logger.warning("[PS %s] R_u refused: %s", self.device_ID, message)
        else:
            R_u = float('inf')
            fit_ok = False
            message = "; ".join(sorted({p['message'] for p in polarities
                                        if p['message']})) or "measurement failed"
            logger.warning("[PS %s] R_u refused: %s", self.device_ID, message)

        first = polarities[0]
        return {
            'R_u_ohm':    R_u,
            'fit_ok':     fit_ok,
            'message':    message,
            'polarities': polarities,
            'control':    control,
            'step_V':     first['step_V'],
            'I_peak_A':   float(first['currents_A'][int(np.argmax(np.abs(first['currents_A'])))]),
            'times_ms':   first['times_ms'],
            'currents_A': first['currents_A'],
        }

    def perform_CV_iR(self, min_V, max_V, cycles, mV_s, step_hz, R_u_ohm,
                      start_V=None, last_V=None, scan_direction='Negative',
                      compensation_fraction=0.7, max_consecutive_timeouts=5,
                      data_callback=None, stop_event=None, callback_block=6,
                      feedback_median=3, feedback_alpha=0.4,
                      correction_slew_V=0.010, freeze_auto_gain=True):
        """Run a CV with live software iR compensation (advanced).

        Solution resistance means the electrode never quite reaches the
        potential asked for — it falls short by I times R_u. This method adds
        that missing drop back on, one point at a time, from the current
        measured at the previous points:

            applied = target + (filtered current x R_u x compensation_fraction)

        Unlike perform_CV, which streams the whole waveform to the device at
        once, this loop sets and reads a single point per serial round trip,
        which limits it to roughly 100–170 points per second.

        This is positive feedback, and through a faradaic wave it runs with
        almost no damping: a single bad current sample would echo through
        tens of points as a real potential excursion (bench-diagnosed
        Aug 2026). Three guards keep it in check. Auto-gain is frozen for the
        scan, because a reading taken mid range-change can be wildly
        mis-scaled — the seed of the observed spikes. The feedback current is
        median-filtered then exponentially smoothed, so no single sample
        reaches the setpoint unchecked. And the correction term is
        slew-limited so it can only walk, never jump.

        Only part of the drop is added back, because giving back more than
        the cell actually lost makes the correction feed on itself. The
        default backs off further than analog instruments do (0.7 rather
        than ~0.9) because the one-sample feedback delay eats stability
        margin an analog loop keeps. For a fully corrected axis, prefer the
        post-hoc mode in the UI, which subtracts the drop after a normal
        batched scan.

        As a further guard, every setpoint is capped at the board's output
        limit and the first cap is logged as a warning. If a scan reports
        capping, re-measure R_u before trusting the trace.

        Args:
            min_V (float): Lowest potential of the sweep (V)
            max_V (float): Highest potential of the sweep (V)
            cycles (int): Number of full cycles
            mV_s (float): Scan rate in mV/s
            step_hz (float): Points per second, which sets the step size
            R_u_ohm (float): Uncompensated resistance in ohms, from measure_R_u
            start_V (float, optional): Starting potential. Defaults to OCP.
            last_V (float, optional): Ending potential. Defaults to OCP.
            scan_direction (str): 'Negative' (default) or 'Positive'
            compensation_fraction (float): Portion of the ohmic drop to add
                back, 0 to 1. Default 0.7. Lower it if a scan looks noisy or
                rings; 1.0 fully compensates but has no stability margin.
            max_consecutive_timeouts (int): Dropped readings tolerated in a row
                before the scan is abandoned. Default 5.
            data_callback (callable, optional): Called with
                ``(potential_V, current_uA)`` numpy arrays as the scan runs, so
                a caller can plot it live. Batched into small blocks rather
                than one call per point.
            stop_event (threading.Event, optional): When set, the scan stops
                after the current point, opens the switch safely, and returns
                the points completed so far instead of the full sweep.
            callback_block (int): Points buffered per ``data_callback`` call.
                Default 6, matching the batch path's chunk size.
            feedback_median (int): Window of recent current samples the
                feedback takes its median over. 1 disables the median.
                Default 3.
            feedback_alpha (float): Weight of the newest (median-filtered)
                sample in the exponential smoothing of the feedback current,
                0 to 1. 1.0 disables the smoothing. Default 0.4.
            correction_slew_V (float): Largest change of the correction term
                allowed per point, in volts. ``np.inf`` disables the limit.
                Default 0.010.
            freeze_auto_gain (bool): Disable auto-gain for the scan and
                restore it afterwards, so range changes cannot feed a
                mis-scaled sample into the setpoint. Default True.

        Returns:
            np.ndarray: same columns as perform_CV — time (s), measured
                potential (V), current (uA), cycle number, experiment number,
                target potential (V). Shorter than the requested sweep if
                ``stop_event`` was set part-way through.
        """
        #Stop any running current-hold before the OCP read below opens the
        #switch, otherwise the firmware hold loop drives the DAC to its limit
        self.write_current_hold_stop()

        #if start_V and/or last_V is None, start/end with OCP
        if (start_V is None) or (last_V is None):
            ocp = self.read_ocp()
            if start_V is None:
                start_V = ocp
            if last_V is None:
                last_V = ocp

        V_s = mV_s / 1000
        step_V = V_s / step_hz

        cv = self._build_cv_waveform(min_V, max_V, cycles, step_V,
                                     start_V, last_V, scan_direction)
        num_points = len(cv)
        if num_points == 0:
            raise ValueError("The CV waveform came out empty — check min_V, "
                             "max_V, cycles, and the scan rate.")

        if compensation_fraction > 1.0:
            logger.warning("[PS %s] compensation_fraction=%.2f gives back more "
                           "than the cell lost; the correction may ring or "
                           "amplify noise.", self.device_ID, compensation_fraction)
        R_u_effective = R_u_ohm * compensation_fraction

        delay_s = 1.0 / step_hz
        measured_V = np.zeros(num_points)
        measured_I_mA = np.zeros(num_points)
        actual_times = np.zeros(num_points)

        # Feedback state: the correction is computed from a median-filtered,
        # exponentially smoothed current rather than the last raw sample.
        recent_I_A = deque(maxlen=max(1, int(feedback_median)))
        filtered_I_A = 0.0
        alpha = min(max(float(feedback_alpha), 0.0), 1.0)
        prev_correction_V = 0.0
        consecutive_timeouts = 0
        capped = False

        # A mid-wave gain change can hand the feedback a mis-scaled sample,
        # so the gain is held fixed for the whole scan by default.
        prev_auto_gain = self._auto_gain
        if freeze_auto_gain and prev_auto_gain:
            self.write_auto_gain(False)

        # Park the DAC at the sweep's first potential BEFORE closing the
        # switch, so the cell never sees a stale setpoint as the scan starts.
        self.write_potential_calibrated(cv[0])
        time.sleep(0.05)
        self.write_switch(SwitchState.On)
        t_start = time.time()

        points_done = num_points
        block_start = 0

        def _emit_block(upto):
            """Hand the points since the last emission to the live callback."""
            if data_callback is None or upto <= block_start:
                return
            try:
                data_callback(np.round(measured_V[block_start:upto], 5),
                              np.round(measured_I_mA[block_start:upto] * 1000.0, 5))
            except Exception as e:
                logger.warning("[PS %s] Live callback error: %s", self.device_ID, e)

        try:
            for i in range(num_points):
                # Check for a stop before driving the next point, so a stopped
                # scan never applies a potential it will not read back
                if stop_event is not None and stop_event.is_set():
                    points_done = i
                    break

                t_step_start = time.time()

                # Add back the ohmic drop, from the filtered current of the
                # points before this one, walking no faster than the slew
                # limit allows.
                correction_V = filtered_I_A * R_u_effective
                if np.isfinite(correction_slew_V):
                    correction_V = float(np.clip(
                        correction_V,
                        prev_correction_V - correction_slew_V,
                        prev_correction_V + correction_slew_V))
                prev_correction_V = correction_V
                E_corrected = cv[i] + correction_V

                # Guard against a runaway from an overestimated R_u
                if abs(E_corrected) > HW_VOLTAGE_COMPLIANCE_V:
                    E_corrected = np.sign(E_corrected) * HW_VOLTAGE_COMPLIANCE_V
                    if not capped:
                        capped = True
                        logger.warning(
                            "[PS %s] iR-corrected setpoint hit the %.1f V output "
                            "limit at point %d — R_u may be overestimated.",
                            self.device_ID, HW_VOLTAGE_COMPLIANCE_V, i)

                self.write_potential_calibrated(E_corrected)

                try:
                    reading = self.read_potential_current_calibrated()
                    consecutive_timeouts = 0
                except SerialReadTimeout:
                    # A dropped reading is survivable: keep the previous values
                    # so the scan holds its timing, and give up only if the
                    # link stays broken.
                    consecutive_timeouts += 1
                    if consecutive_timeouts >= max_consecutive_timeouts:
                        raise
                    logger.warning("[PS %s] Dropped reading at point %d of %d.",
                                   self.device_ID, i, num_points)
                    reading = [measured_V[i - 1] if i else 0.0,
                               measured_I_mA[i - 1] if i else 0.0]

                measured_V[i] = reading[0]
                measured_I_mA[i] = reading[1]
                actual_times[i] = time.time() - t_start

                recent_I_A.append(measured_I_mA[i] / 1000.0)
                median_I_A = float(np.median(recent_I_A))
                filtered_I_A = (median_I_A if i == 0
                                else alpha * median_I_A + (1.0 - alpha) * filtered_I_A)

                if (i + 1 - block_start) >= callback_block:
                    _emit_block(i + 1)
                    block_start = i + 1

                # Sleep only the time left in this step, to hold the scan rate
                remaining = delay_s - (time.time() - t_step_start)
                if remaining > 0:
                    time.sleep(remaining)

            _emit_block(points_done)
            self._safe_open_switch()

        except Exception:
            self.write_switch(SwitchState.Off)  # Fail-state: cut the output before raising
            raise

        finally:
            # Restoring auto-gain must never mask the original failure
            if freeze_auto_gain and prev_auto_gain:
                try:
                    self.write_auto_gain(prev_auto_gain)
                except Exception:
                    logger.warning("[PS %s] Could not restore auto-gain after "
                                   "the iR-compensated scan.", self.device_ID)

        # The loop keeps its own pace only while a serial round trip fits in
        # the step period; past that it silently runs slow, so report the
        # rate actually achieved for the caller to record.
        if points_done > 1 and actual_times[points_done - 1] > 0:
            achieved_hz = points_done / actual_times[points_done - 1]
            if achieved_hz < 0.98 * step_hz:
                logger.warning(
                    "[PS %s] iR scan achieved %.0f points/s of the %.0f "
                    "requested — the true scan rate was %.1f%% of nominal.",
                    self.device_ID, achieved_hz, step_hz,
                    100.0 * achieved_hz / step_hz)

        # A stopped scan returns only the points actually measured
        measured_V = measured_V[:points_done]
        measured_I_mA = measured_I_mA[:points_done]
        actual_times = actual_times[:points_done]
        cv = cv[:points_done]

        cycle_points = num_points // cycles if cycles > 0 else num_points
        cycle_nums = np.zeros(points_done)
        for i in range(0, cycles):
            start = i * cycle_points
            if start >= points_done:
                break
            cycle_nums[start: min((i + 1) * cycle_points, points_done)] = i

        return np.column_stack((actual_times, measured_V, measured_I_mA * 1000.0,
                                cycle_nums, np.zeros(points_done), cv))

    def perform_DPV(self, waveform, start_V, sample_hz):
        """Run a pre-built DPV target waveform and stream the response.

        The staircase-plus-pulse waveform is built by
        ``moles.methods.dpv.build_dpv_waveform``, which is where the sweep shape
        (single-sweep or cyclic segments) is decided; this method only drives
        the hardware. ``waveform`` is the per-sample target-potential array and
        ``start_V`` is the potential the DAC is parked at before the cell switch
        closes.

        Returns:
            2 columns, column 0: I (A) | column 1: V
        """
        self.write_current_hold_stop()
        # Park the DAC at the waveform's first potential before closing the
        # switch — same stale-setpoint protection as perform_CV — so the cell
        # never sits at a stale setpoint while the batch buffer pre-fills.
        self.write_potential_calibrated(start_V)
        time.sleep(0.05)
        self.write_switch(True)

        step_ms = int(np.round(np.ceil(1000 / sample_hz)))
        return self.write_voltage_batch(
            voltages=self._waveform_to_raw_V(waveform), delay=step_ms
        )

    def perform_CDPV(self,
                     min_V: float,
                     pulse_V: float,
                     step_V: float,
                     max_V: float,
                     potential_hold_ms: int,
                     pulse_hold_ms: int,
                     voltage_hold_s: float,
                     start_V: float = None,
                     cycles: int = 1,
                     sample_hz = 1000
                     ):
        """
        performs cyclic DPV experiment from ocp and ends up with ocp
        :param min_V:
        :param pulse_V:
        :param step_V:
        :param max_V:
        :param potential_hold_ms:
        :param pulse_hold_ms:
        :param cycles:
        :param sample_hz:
        :return:
        """
        if start_V is None:
            start_V = self.read_ocp()
        stop_V = start_V

        self.write_current_hold_stop()
        self.write_potential_calibrated(start_V)
        self.write_switch(True)
        time.sleep(voltage_hold_s)

        step_ms = int(np.round(np.ceil(1000 / sample_hz)))

        def hold(v, ms):
            n = int(np.round(ms / step_ms))
            return np.zeros(n, np.float32) + v

        dpv = []

        p1 = np.linspace(start_V, min_V, int(np.round(abs(min_V - start_V) / step_V)))
        p2 = np.linspace(min_V, max_V, int(np.round(abs(max_V - min_V) / step_V)))
        p3 = np.linspace(max_V, stop_V, int(np.round(abs(max_V - stop_V) / step_V)))

        for i in range(cycles):
            for step in p1[:-1]:  # start_V to min
                dpv = np.append(dpv, hold(step, potential_hold_ms))  # potential hold at stair step
                dpv = np.append(dpv, hold(step + pulse_V,
                                          pulse_hold_ms))  # potential hold of the peak before the next stair step
            for step in p2[:-1]:  # min to max
                dpv = np.append(dpv, hold(step, potential_hold_ms))
                dpv = np.append(dpv, hold(step - pulse_V, pulse_hold_ms))

            for step in p3[:-1]:  # max to stop_V
                dpv = np.append(dpv, hold(step, potential_hold_ms))
                dpv = np.append(dpv, hold(step + pulse_V, pulse_hold_ms))

        dpv = np.append(dpv, hold(start_V, potential_hold_ms))
        rtn = self.write_voltage_batch(voltages=self._waveform_to_raw_V(dpv), delay=step_ms)
        num_points = rtn.shape[0]
        cycle_points = num_points // cycles
        duration_s = num_points/sample_hz
        time_stamps = np.linspace(voltage_hold_s, voltage_hold_s+duration_s, num_points)
        cycle_nums = np.zeros(num_points)
        for i in range(0, cycles):
            cycle_nums[i * cycle_points: (i+1) * cycle_points] = np.full(cycle_points, i)
        return np.column_stack((time_stamps,
                                self.calibrate_batch_potential_V(rtn[:, 1]),
                                self.calibrate_batch_current_uA(rtn[:, 0]),
                                cycle_nums, np.zeros(num_points), dpv))

    def process_CDPV(self, data: np.ndarray, file_basename: Union[str, Path]):
        base = Path(file_basename)
        base_no_ext = base.with_suffix("")
        out_dir = base_no_ext.parent
        out_dir.mkdir(parents=True, exist_ok=True)
        csv_raw = base_no_ext.with_name(f"{base_no_ext.stem}_CDPV_raw.csv")
        png_raw = base_no_ext.with_name(f"{base_no_ext.stem}_CDPV_proc.png")
        csv_proc = base_no_ext.with_name(f"{base_no_ext.stem}_CDPV_proc.csv")
        
        np.savetxt(csv_raw, data, delimiter=",", fmt="%.3E,%.3E,%.3E,%d,%d,%.3E",
                   header="time,voltage,current,cycle_num,exp_num,voltage_applied")
        proc_cdpv = plot_cdpv(data, png_raw, True, log_file=None)[:,1:3]
        np.savetxt(csv_proc, proc_cdpv, delimiter=",", fmt="%.3E,%.3E",
                   header="voltage,current")
        
    def process_CV(self, data: np.ndarray, file_basename: Union[str, Path]):
        base = Path(file_basename)
        base_no_ext = base.with_suffix("")
        out_dir = base_no_ext.parent
        out_dir.mkdir(parents=True, exist_ok=True)
        csv_raw = base_no_ext.with_name(f"{base_no_ext.stem}_CV_raw.csv")
        png_raw = base_no_ext.with_name(f"{base_no_ext.stem}_CV_proc.png")
        csv_proc = base_no_ext.with_name(f"{base_no_ext.stem}_CV_proc.csv")
        np.savetxt(csv_raw, data, delimiter=",", fmt="%.3E,%.3E,%.3E,%d,%d,%.3E",
                   header="time,voltage,current,cycle_num,exp_num,voltage_applied")
        proc_cv = plot_cv(data, png_raw)
        np.savetxt(csv_proc, proc_cv, delimiter=",",fmt="%.3E,%.3E",
                   header="voltage,current")

if __name__ == "__main__":
    poten_name = "/dev/poten_2" #"/dev/potentiostat0" + sys.argv[1]
    file_name = sys.argv[2]

    ps = Potentiostat(serial_port=poten_name)
    print("ps created, connecting...")
    ps.connect()
    print("ps connected")
    ps.write_switch(0)
    ps.write_dac(channels=[DAC.CE_IN,DAC.A_REF,DAC.V_AN], voltages=[0,-5,0]) #initialize like this
    ocp = ps.read_ocp()
    print(f"ocp is, {ocp}")

    # Test CDPV
    print("CDPV")
    rtn = ps.perform_CDPV(0.0, 0.11, 0.01, 0.8, 500, 50, 5, 0)
    #rtn = ps.perform_CDPV(-0.8, 0.11, 0.01, 0.5, 500, 50, 5, 0)
    np.savetxt(f"{file_name}_CDPV_raw_poten{sys.argv[1]}.csv", rtn, delimiter=",",
               fmt="%.3E,%.3E,%.3E,%d,%d,%.3E")
    proc_cdpv = plot_cdpv(rtn, f"{file_name}_CDPV_proc_poten{sys.argv[1]}.png",
            True, f"ref_fit_log_poten{sys.argv[1]}.log")
    np.savetxt(f"{file_name}_CDPV_proc_poten{sys.argv[1]}.csv", proc_cdpv[:,0:5], delimiter=",",
               fmt="%.3E,%.3E,%.3E,%d,%d")

    # Test CV
    print("CV")
    rtn = ps.perform_CV(-0.2, 1, 5, 200, 250)
    # rtn = ps.perform_CV(-1.2, 1.2, 5, 500, 250)
    np.savetxt(f"{file_name}_CV_raw_poten{sys.argv[1]}.csv", rtn, delimiter=",",
               fmt="%.3E,%.3E,%.3E,%d,%d,%.3E")
    proc_cv = plot_cv(rtn, f"{file_name}_CV_proc_poten{sys.argv[1]}.png")
    np.savetxt(f"{file_name}_CV_proc_poten{sys.argv[1]}.csv", proc_cv, delimiter=",",
               fmt="%.3E,%.3E")

    # close poten
    ps.write_switch(0)
    ps.disconnect()
