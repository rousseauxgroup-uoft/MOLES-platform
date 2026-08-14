from pathlib import Path
import numpy as np
from moles.driver.ps4_ref import Potentiostat, DAC
from datetime import datetime
import time

# Replace with the serial port for your potentiostat.
# Find it via: `python -m serial.tools.list_ports` (macOS/Linux)
# or Device Manager → Ports (Windows, looks like "COM3").
COM_PORT = "/dev/tty.usbmodemREPLACE"

# Where CSVs from this demo will be saved. Edit if you want a different location.
data_folder = Path.home() / "moles_data"
data_folder.mkdir(parents=True, exist_ok=True)


# potentiostat instantiation
ps = Potentiostat(serial_port=COM_PORT)
ps.connect()
ps.write_gain(4)
ps.write_switch(0)
ps.write_dac(channels=[DAC.CE_IN, DAC.A_REF, DAC.V_AN], voltages=[0, -5, 0])

# get open circuit potential
ocp = ps.read_ocp()
print(f"OCP = {ocp:.3f} V")

try:
    # # run CV
    # file_name = f"test_{datetime.now().strftime('%Y_%m_%d_%H_%M_%S')}"  # replace with your actual filename
    # data=ps.perform_CV(min_V=-0.5,
    #                     max_V=0.5,
    #                     cycles=1,
    #                     mV_s=100,
    #                     step_hz=1000
    #                     )
    # ps.process_CV(data=data, file_basename=data_folder/file_name)

    # apply potential (volt)
    ps.write_potential_calibrated(potential_V = 1.5)
    ps.write_switch(1)
    time.sleep(3)
    for i in range(0, 5):
        voltage = ps.read_potential_calibrated()[0]
        current = ps.read_current_calibrated()[0]
        print(str(round(voltage, 2)) + " V, " + str(round(current, 2)) + " mA")
        time.sleep(1)
    ps.write_potential_calibrated(potential_V = 0)
    ps.write_switch(0)

    # apply current (mA)
    # The highest target current you can set right now is 5.0 mA. Any more and it outputs 9.55 mA uncontrolled.

    # Current ramping procedure
    target_current = 5.0
    initial_current = 0.01
    current = initial_current
    current_ramp_step = 0.1
    ps.write_current_hold_calibrated(initial_current)
    print("Initial current set to " + str(round(current, 2)))
    ps.write_switch(1)
    print("Switch on, current set to " + str(round(current, 2)))
    while current < target_current:
        print("Ramping to " + str(round(current, 2)) + " mA")
        current = current + current_ramp_step
        ps.write_current_hold_calibrated(current)
        time.sleep(0.1)
    else:
        print("Holding current at " + str(target_current) + " mA, actual current/voltage readouts are:")

    # Write the target current value
    ps.write_current_hold_calibrated(target_current)
    time.sleep(1)
    for i in range(0,10):
        print(str(round(ps.read_current_calibrated()[0], 2)) + " mA, " + str(round(ps.read_potential_calibrated()[0], 2)) + " V")
        time.sleep(0.5)
    ps.write_current_hold_calibrated(0)
    ps.write_current_hold_stop()
    ps.write_switch(0)

# Always close the switch
finally:
    ps.write_switch(0)
    print("Switch closed.")
    ps.disconnect()
    print("Potentiostat disconnected.")