# Stress Measurement Cycle Library

## Series Context

This repository is part of the Veronica GaoZhan VCSEL Reliability Test Series.

- Series ID: VGZ-VRLS
- Track: Single-device reliability progression
- Position: 2 (stress cycle)
- Protocol name: stress_cycle
- Author: Veronica GaoZhan

### Related Repositories

- LIV rollover baseline: https://github.com/vvvvvero/b1500_powermeter_LIV_rollover
- Step stress protocol: https://github.com/vvvvvero/Laser_Optical_Reliablity_Tests_1_Step_Stress
- Wafer mapping automation: https://github.com/vvvvvero/Cascade_Summit12k_Keysight_B1500_Thorlabs_Powermeter_TestAutomation
- B1500 + Avantes synchronized spectra: https://github.com/vvvvvero/Keysight-B1500-Avantes-Spectrometer-Synchronized-Measurement

### Standard Session Fields (Series V1)

Runs should include these common identifiers in metadata and outputs:

- project_id
- wafer_id
- device_id
- session_id
- parent_session_id
- protocol_name
- protocol_version
- schema_version

Series data contract: [SERIES_V1_SCHEMA.md](SERIES_V1_SCHEMA.md)

A modular Python library for cyclic reliability tests using B1500 and Thorlabs power meters.

## Features

- Cyclic flow: Measurement -> Stress -> Measurement
- Configurable stress mode (constant voltage or current)
- Real-time stress monitoring for electrical and optical channels
- Cycle-by-cycle summary export and degradation tracking
- PyQt5 GUI with live plots

## Project Structure

```
stress_cycle_measurement_lib/
├── stress_cycle/
│   ├── __init__.py
│   ├── models.py
│   ├── b1500_controller.py
│   ├── thorlabs_power_meter.py
│   ├── measurement_engine.py
│   ├── worker_thread.py
│   ├── gui.py
│   ├── app.py                    # Legacy compatibility re-exports
│   └── main.py
├── examples/
│   └── basic_usage.py
├── main.py
├── setup.py
├── requirements.txt
├── SERIES_V1_SCHEMA.md
└── README.md
```

## Installation

```bash
git clone https://github.com/vvvvvero/Laser_Optical_Reliablity_Tests_2_Stress_Cycle.git
cd stress_cycle_measurement_lib
pip install -r requirements.txt
```

## Quick Start

```bash
python main.py
```

## Latest Integrated Script

The latest single-file GUI implementation is included at:

- b1500_stress_measurement_cycle.py
- b1500_stress_cycle_spectroscopy.py

Run directly:

```bash
python b1500_stress_measurement_cycle.py
python b1500_stress_cycle_spectroscopy.py
```

Or after installation:

```bash
stress-cycle
```

## Output Files

Per run, results are written into a timestamped session folder and include:

- measurement_cycle_XXX.csv
- stress_cycle_XXX.csv
- cycle_summary.csv
- session_manifest.json

## Running the tests

```bash
pip install -e .
pip install pytest
pytest tests/
```

## Citation

If you use this library in research, please cite:

```text
GaoZhan, V. (2026). Stress Measurement Cycle Library.
Retrieved from https://github.com/vvvvvero/Laser_Optical_Reliablity_Tests_2_Stress_Cycle
```

## Support

For issues, questions, or suggestions, please open an issue on GitHub.

## License

MIT License
