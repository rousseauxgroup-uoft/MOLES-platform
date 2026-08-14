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
    # pair maps the board's own potential reading to the true value. Defaults
    # reproduce the historical global correction (+/- 0.0494 V offset) so
    # boards without a voltage calibration behave exactly as before.
    _calib_v_slope = 1.0
    _calib_v_intercept = 0.0494
    _calib_v_read_slope = 1.0
    _calib_v_read_intercept = 0.0494

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
        # Uses the same raw scaling as the batch points, so the handoff to
        # the waveform's first point is seamless.
        self.write_potential(start_V)
        time.sleep(0.05)
        self.write_switch(True)

        V_s = mV_s / 1000
        step_V = V_s / step_hz

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

        rtn = self.write_voltage_batch(voltages = cv,delay = int(np.round(1000/step_hz)))
        num_points = rtn.shape[0]
        cycle_points = num_points // cycles
        duration_s = num_points / step_hz
        time_stamps = np.linspace(0, duration_s, num_points)
        cycle_nums = np.zeros(num_points)
        for i in range(0, cycles):
            cycle_nums[i * cycle_points: (i + 1) * cycle_points] = np.full(cycle_points, i)
        return np.column_stack((time_stamps, rtn[:, 1], rtn[:, 0]* 1.0989e6 - 0.0632, cycle_nums, np.zeros(num_points), cv))

    
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
        self.write_potential(start_V)
        time.sleep(0.05)
        self.write_switch(True)

        step_ms = int(np.round(np.ceil(1000 / sample_hz)))
        return self.write_voltage_batch(
            voltages=np.asarray(waveform, dtype=np.float32), delay=step_ms
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
        self.write_potential(start_V)
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
        rtn = self.write_voltage_batch(voltages=dpv, delay=step_ms)
        num_points = rtn.shape[0]
        cycle_points = num_points // cycles
        duration_s = num_points/sample_hz
        time_stamps = np.linspace(voltage_hold_s, voltage_hold_s+duration_s, num_points)
        cycle_nums = np.zeros(num_points)
        for i in range(0, cycles):
            cycle_nums[i * cycle_points: (i+1) * cycle_points] = np.full(cycle_points, i)
        return np.column_stack((time_stamps, rtn[:, 1], rtn[:, 0]* 1.0989e6 - 0.0632, cycle_nums, np.zeros(num_points), dpv))

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
