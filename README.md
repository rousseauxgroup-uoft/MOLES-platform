<img src="MOLES_cropped.png" alt="Moley the Mole, the MOLES mascot" width="300" height="auto">

# MOLES
**M**odular **O**pen-source **L**aboratory for **E**lectrosynthesis **S**creening

MOLES is a low-cost platform for running small-scale electrosynthesis and electroanalysis experiments in parallel. This repository provides the firmware and desktop software for controlling multiple open-source potentiostats, based on the design from the Acceleration Consortium and the Matter Lab at the University of Toronto (*Device* **2025**, *3*, 100567, [doi: 10.1016/j.device.2024.100567](https://doi.org/10.1016/j.device.2024.100567)).

## What it does

**Electrolysis**
- Constant current (up to 30 mA)
- Constant potential (up to 4.5 V total cell voltage)
- Alternating current (up to 7 Hz, using feedback control of the current)
- Alternating polarity (up to 50 Hz, with potential control run on the board itself)

**Electroanalysis**
- Cyclic voltammetry (CV) with optional iR drop estimation and compensation 
- Differential pulse voltammetry (DPV)
- An analysis workspace for processing and labelling saved voltammograms

**Independently addressable channels**
- Several experiments can be run in parallel, at any time, from one computer. For example, an electrolysis screen can be run on boards A–H, while a colleague runs CVs on board I. A board in use is reserved for that experiment, and MOLES will not let another experiment start on it.

## Quick install

1. Download the installer for your computer from the [latest release](https://github.com/rousseauxgroup-uoft/MOLES-platform/releases/latest):
   - **Windows 10/11:** `MOLES-Setup-<version>.exe`
   - **macOS 12 or newer** (Apple Silicon and Intel): `MOLES-<version>.pkg`
2. Run the installer. **Stay online until it finishes:** the last step downloads Python and the required packages (about 200 MB), which can take several minutes. If you are prompted with a system security message:
   - **Windows:** at "Windows protected your PC", click **More info** → **Run anyway**. If your browser says the file "isn't commonly downloaded", choose **Keep**.
   - **macOS 15 or newer:** when told "Apple could not verify…", click **Done**, then go to **System Settings → Privacy & Security**, scroll down, click **Open Anyway** next to the MOLES message, and enter your password.
   - **macOS 12–14:** right-click (or Control-click) the `.pkg` file → **Open** → **Open**.
3. Open **MOLES** from the Start Menu (Windows) or the Applications folder (macOS) to run the launcher.
4. Click **Settings** and choose the folders where your data files will be saved.

### Potentiostat firmware
- You will need to flash newly assembled potentiostat boards with the firmware (most current firmware image is in `firmware/prebuilt/`). Follow the instructions in the [Flashing Firmware and Potentiostat Onboarding](https://github.com/rousseauxgroup-uoft/MOLES-platform/wiki/Flashing-Firmware-and-Potentiostat-Onboarding) wiki page of this repository.

### Acquiring potentiostat boards
- Refer to the guide in the [Ordering Potentiostats](https://github.com/rousseauxgroup-uoft/MOLES-platform/wiki/Ordering-Potentiostats) wiki page.

## Using MOLES

Opening MOLES starts the **launcher**. From here you can:
- See every registered potentiostat, whether it is free or in use (and by which app, experiment, and user), and which firmware version it runs. Boards with outdated firmware are flagged.
- Open the **Electrolysis** and **Electroanalysis** apps. Each runs as a separate window, so you can use both at once.

If an app crashes mid-experiment, MOLES detects and releases the board automatically. The launcher's **Force Release** button does this manually if needed.

## Updating and uninstalling

**Updating:** when a new version is available, the launcher offers it on startup.

**Uninstalling:**
- **Windows:** Settings → Apps → MOLES → Uninstall
- **macOS:** drag MOLES from Applications to the Bin

Updating or uninstalling keeps your settings, registered boards, calibrations, and logs. These are stored in the `.moles` folder in your home folder.

## For developers

Install from source:
```
pip install .
```

Run the apps from the terminal:

| Command | Opens |
|---|---|
| `moles` (or `python -m moles`) | Launcher |
| `moles-electrolysis` | Electrolysis app |
| `moles-electroanalysis` | Electroanalysis app |

## Questions and bug reports

Please report bugs or ask questions through the [Issues](https://github.com/rousseauxgroup-uoft/MOLES-platform/issues) tab of this repository.

## Citing MOLES

If you use MOLES in your work, please cite:

> Chiu, B. S. Y.; Kieran, A.; Rousseaux, S. A. L. An open-source electrosynthesis platform for reaction screening enables a nickel-catalyzed electroreductive aryl cyanation. *ChemRxiv* **2026**. DOI: [10.26434/chemrxiv.15010748](https://doi.org/10.26434/chemrxiv.15010748)

## Acknowledgements

Many thanks to Sergio Pablo-García, Ángel García, Gun Deniz Akkoc, Malcolm Sim, Yang Cao, Maxine Somers, Chance Hattrick, Naruki Yoshikawa, Dominik Dworschak, Han Hao, and Alán Aspuru-Guzik for designing the first-generation open potentiostat that MOLES is based on.

*Generative AI tools (Claude Code, Google Gemini, OpenAI GPT-5 Codex) were used to help write code for this project.*
