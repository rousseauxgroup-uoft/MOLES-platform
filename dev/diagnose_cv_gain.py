#!/usr/bin/env python3
"""
Diagnosis Script for CV Gain Spikes.
Logs raw gain indices and values to detect mismatches.
"""
import sys
import time
import numpy as np
import platform
import os
from pathlib import Path

# Add src to path
current_dir = Path(__file__).resolve().parent
src_dir = current_dir.parent / "src"
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))

try:
    from moles.driver.ps4_ref import Potentiostat, SwitchState, Resistors, Commands
except ImportError:
    print("Error: Could not import Potentiostat module.")
    sys.exit(1)

def get_port():
    # Replace these with the serial port for your own potentiostat.
    # Find it via: `python -m serial.tools.list_ports` (macOS/Linux)
    # or Device Manager → Ports (Windows).
    os_name = platform.system()
    if os_name == "Darwin":
        return "/dev/tty.usbmodemREPLACE"
    elif os_name == "Windows":
        return "COM3"
    else:
        return "/dev/ttyUSB0"

class DebugPotentiostat(Potentiostat):
    def write_voltage_batch(self, voltages, delay):
        v_per_chunk = 6
        bytes_per_v = 4
        tx_len = v_per_chunk * bytes_per_v
        new_voltages = self._scale_input_voltages(voltages)
        v_count = int(len(new_voltages))

        remainder = (v_count % v_per_chunk)
        pad_count = 0
        if remainder > 0:
            pad_count = v_per_chunk - remainder
            for i in range(pad_count):
                new_voltages = np.append(new_voltages,new_voltages[-1])
            v_count += pad_count

        v_bytes = np.float32(new_voltages).tobytes()
        v_byte_count = len(v_bytes)
        first_tx_bytes = tx_len * 4 
        tx_ind = 0 

        self._reset_buffer(0)
        self._write_to_buffer(v_bytes[0:first_tx_bytes])
        time.sleep(0.01)
        tx_ind = first_tx_bytes

        # Auto gain always used for this debug
        rx_len = 9 
        
        temp_adc = np.zeros((v_count,2),np.float32)
        temp_res_val = np.zeros((v_count),np.float32)

        self._write_cmd(Commands.EXECUTE_VOLTAGE_BATCH,np.uint32([delay,v_count]),True)
        
        print("\n--- Starting Batch Execution (DEBUG) ---")
        print("Index | GainIdx | Resistor | Raw_V | Raw_I_ADC | Calc_I (uA)")
        
        for i in range(0,v_count,v_per_chunk):
            data = self._read_from_buffer(rx_len * v_per_chunk)
            if (tx_ind + tx_len) <= v_byte_count:
                self._write_to_buffer(v_bytes[tx_ind:tx_ind+tx_len])
                tx_ind += tx_len

            temp_ind = 0
            for j in range(v_per_chunk):
                temp_adc[i+j,:] = np.frombuffer(data,np.float32,2,temp_ind)
                temp_ind += 8
                
                # Gain Read
                temp_gain = np.frombuffer(data,np.uint8,1,temp_ind)[0]
                temp_res_val[i+j] = self.res_vals[temp_gain]
                temp_ind += 1
                
                # Debug Print for Anomalies
                raw_v = temp_adc[i+j, 1]
                raw_i_adc = temp_adc[i+j, 0]
                res_val = temp_res_val[i+j]
                
                # Manual Calc
                # Scale Output V
                proc_v = self._scale_output_voltages(np.array([raw_v]))[0]
                proc_i_adc = self._scale_output_voltages(np.array([raw_i_adc]))[0]
                
                calc_i_A = (proc_i_adc / res_val) * -1.0
                calc_i_uA = calc_i_A * 1.0989e6 - 0.0632
                
                # Threshold for spike detection (e.g. > 500uA or < -500uA for a simple CV?)
                if abs(calc_i_uA) > 1000.0:
                    print(f"{i+j:5d} | {temp_gain:^7d} | {res_val:^8.0f} | {proc_v:6.3f} | {proc_i_adc:8.3f} | {calc_i_uA:10.2f} <--- SPIKE?")
                elif (i+j) % 50 == 0:
                    print(f"{i+j:5d} | {temp_gain:^7d} | {res_val:^8.0f} | {proc_v:6.3f} | {proc_i_adc:8.3f} | {calc_i_uA:10.2f}")

        return temp_adc # Stub return

def run_debug():
    port = get_port()
    print(f"Connecting to {port}...")
    try:
        ps = DebugPotentiostat(serial_port=port, device_ID=1)
        ps.connect()
        ps.write_switch(SwitchState.On)
        ps.write_gain(Resistors.R_100)
        ps.write_auto_gain(True)
        time.sleep(1)
        
        print("Running CV...")
        # Run a CV that likely crosses range boundaries
        ps.perform_CV(min_V=-1.0, max_V=1.0, cycles=2, mV_s=100, step_hz=50)
        
        ps.write_switch(SwitchState.Off)
        ps.disconnect()
        print("Done.")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    run_debug()
