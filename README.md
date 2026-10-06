# VCSEL Reliability Tests - Series 2: LIV Constant Stress

## Series Context

This repository is part of the Veronica GaoZhan VCSEL Reliability Test Series.

- Series ID: VGZ-VRLS
- Track: Single-device reliability progression
- Position: 2
- Protocol name: stress_cycle
- Author: Veronica GaoZhan

## Repository Purpose

Series 2 provides constant-stress LIV cycling workflows for single-device reliability tracking,
with synchronized electrical and optical monitoring and cycle-by-cycle degradation summaries.

## Included Scripts

- b1500_stress_measurement_cycle.py: LIV constant-stress cycle test GUI.
- b1500_stress_cycle_spectroscopy.py: spectroscopy-enabled stress-cycle variant aligned to the same linear/log timing definition.

## Quick Start

```bash
python b1500_stress_measurement_cycle.py
python b1500_stress_cycle_spectroscopy.py
```

## Standard Session Fields (Series V1)

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

## Output Files

Typical LIV stress-cycle outputs:

- measurement_cycle_XXX.csv
- stress_cycle_XXX.csv
- cycle_summary.csv

Additional spectroscopy outputs when spectroscopy mode is used:

- iv_cycle_XXX.csv
- stress_cycle_XXX.csv
- meas_spectra_cycle_XXX.csv
- stress_spectra_cycle_XXX.csv
- stress_current_cycle_XXX.csv
- cycle_summary.csv

## Related Series Repositories

- Series 1 (LIV Step Stress): https://github.com/vvvvvero/Laser_Optical_Reliablity_Tests_1_Step_Stress
- Series 2 (LIV Constant Stress): https://github.com/vvvvvero/VCSEL_Reliablity_Tests_2_LIV_Constant_Stress
- Series 3 (LIV Stress Recovery): https://github.com/vvvvvero/VCSEL_Reliablity_Tests_3_LIV_Stress_Recovery
- Series 5 (Optical Spectrum Constant Stress): https://github.com/vvvvvero/VCSEL_Reliablity_Tests_5_Optical_Spectrum_Constant_Stress

## Citation

If you use this repository in research, please cite:

```text
GaoZhan, V. (2026). VCSEL Reliability Tests - Series 2: LIV Constant Stress.
Retrieved from https://github.com/vvvvvero/VCSEL_Reliablity_Tests_2_LIV_Constant_Stress
```

## License

MIT License
