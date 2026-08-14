import numpy as np
import math

pos_peak_mA = 3.0
neg_peak_mA = -1.0
frequency_hz = 0.1
duty_cycle = 0.75
waveform_type = 'Sine'

period = 1.0 / frequency_hz if frequency_hz > 0 else 10.0
t_pos = period * duty_cycle
t_neg = period * (1.0 - duty_cycle)
offset_mA = (pos_peak_mA + neg_peak_mA) / 2.0
amplitude_mA = (pos_peak_mA - neg_peak_mA) / 2.0

print(f"Period: {period}, t_pos: {t_pos}, t_neg: {t_neg}, offset: {offset_mA}, amp: {amplitude_mA}")

t = np.linspace(0, period, 20)
for elapsed in t:
    phase_time = elapsed % period
    if waveform_type == 'Sine':
        if phase_time < t_pos:
            mapped_phase = (phase_time / t_pos) * math.pi
        else:
            mapped_phase = math.pi + ((phase_time - t_pos) / t_neg) * math.pi
        
        target_current = offset_mA + amplitude_mA * math.sin(mapped_phase)
    print(f"t={elapsed:.2f}, phase_time={phase_time:.2f}, I={target_current:.2f}")

print(f"Expected Peak: {pos_peak_mA}, Min: {neg_peak_mA}")
