#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
B1500 + Thorlabs Power Meter Stress-Measurement Cycling Test

This script performs cyclic stress testing with intermittent IV characterization:
  Measurement → Stress → Measurement → Stress → ... 

Features:
1. Configurable stress conditions (constant voltage or current)
2. Periodic IV + optical power sweeps between stress intervals
3. Real-time monitoring of current/power during stress
4. Automatic data logging with timestamps
5. Degradation tracking across cycles
6. PyQt5 GUI with live plotting

Test Flow:
1. Initial IV + Power measurement (baseline)
2. Apply stress bias for configured duration
3. Periodic IV + Power measurement
4. Repeat steps 2-3 for N cycles
5. Final summary and data export

Author: Veronica GaoZhan
Date: February 2026
"""

import sys
import os
import csv
import time
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional, Tuple, Dict, Any
from dataclasses import dataclass, field
from enum import Enum

from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                             QGroupBox, QLabel, QLineEdit, QPushButton, QComboBox, QSpinBox,
                             QDoubleSpinBox, QTextEdit, QFileDialog, QMessageBox, QProgressBar,
                             QGridLayout, QCheckBox, QRadioButton, QButtonGroup, QSplitter,
                             QStatusBar, QFrame, QScrollArea, QTabWidget, QTableWidget,
                             QTableWidgetItem, QHeaderView, QSizePolicy)
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt5.QtGui import QFont, QColor, QBrush

import matplotlib
matplotlib.use('Qt5Agg')
import matplotlib.pyplot as plt
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
import numpy as np

try:
    import pyvisa
    from pyvisa.errors import VisaIOError
    PYVISA_AVAILABLE = True
except ImportError:
    PYVISA_AVAILABLE = False
    print("Warning: pyvisa not installed. Install with: pip install pyvisa pyvisa-py")


# Suppress Windows error dialogs
try:
    import ctypes
    ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002)
except (AttributeError, OSError, TypeError):
    pass


TIME_DESIGN_LINEAR = "linear"
TIME_DESIGN_LOG = "log"
LOG_POINTS_PER_DECADE_OPTIONS = (1, 2, 3, 5, 10)
LOG_CYCLE_MANTISSAS = {
    1: (1.0,),
    2: (1.0, 3.0),
    3: (1.0, 2.0, 5.0),
    5: (1.0, 2.0, 3.0, 5.0, 8.0),
    10: (1.0, 1.3, 1.6, 2.0, 2.5, 3.2, 4.0, 5.0, 6.3, 8.0),
}


# =============================================================================
# Enums and Data Classes
# =============================================================================

class TestPhase(Enum):
    IDLE = "idle"
    MEASUREMENT = "measurement"
    STRESS = "stress"
    COMPLETED = "completed"
    STOPPED = "stopped"


@dataclass
class SweepConfig:
    """Configuration for IV/VI sweep measurement"""
    smu: int = 1
    mode: str = "iv"  # "iv" or "vi"
    start: float = 0.0
    stop: float = 2.0
    steps: int = 21
    dwell_s: float = 0.1
    compliance: float = 0.1
    
    @property
    def setpoints(self) -> List[float]:
        if self.steps < 2:
            return [self.start]
        return [
            self.start + i * (self.stop - self.start) / (self.steps - 1)
            for i in range(self.steps)
        ]


@dataclass
class StressConfig:
    """Configuration for stress phase"""
    mode: str = "voltage"  # "voltage" or "current"
    value: float = 2.0     # Stress voltage (V) or current (A)
    duration_s: float = 60.0  # linear: per-cycle duration; log: t0
    time_design: str = TIME_DESIGN_LINEAR  # "linear" or "log"
    log_points_per_decade: int = 5  # options: 1/2/3/5/10
    sample_interval_s: float = 1.0  # Sampling interval during stress
    compliance: float = 0.1  # Compliance limit


@dataclass
class CycleConfig:
    """Configuration for the entire stress-measurement cycle test"""
    # Sweep settings
    sweep: SweepConfig = field(default_factory=SweepConfig)
    
    # Stress settings
    stress: StressConfig = field(default_factory=StressConfig)
    
    # Cycle settings
    num_cycles: int = 10  # Number of stress-measurement cycles
    initial_measurement: bool = True  # Do initial measurement before first stress
    
    # Power meter settings
    enable_power_meter: bool = True
    power_wavelength_nm: float = 850.0
    
    # Output settings
    output_folder: str = "results"
    device_name: str = "Device_001"
    autosave: bool = True


@dataclass
class MeasurementPoint:
    """Single IV measurement point"""
    cycle: int
    point_index: int
    timestamp: float
    setpoint: float
    voltage: float
    current: float
    optical_power: float
    status: str = "OK"


@dataclass
class StressPoint:
    """Single stress monitoring point"""
    cycle: int
    timestamp: float
    elapsed_s: float
    voltage: float
    current: float
    optical_power: float
    status: str = "OK"


@dataclass
class CycleSummary:
    """Summary of a measurement cycle"""
    cycle: int
    timestamp: str
    peak_current: float
    peak_power: float
    threshold_voltage: float  # Estimated from IV
    series_resistance: float  # Estimated from IV slope


# =============================================================================
# Thorlabs Power Meter Controller
# =============================================================================

class ThorlabsPowerMeterController:
    """Controller for Thorlabs PM100D/PM400 power meters"""
    
    SCPI_IDN = "*IDN?"
    SCPI_MEAS_POWER = "MEAS:POW?"
    SCPI_CONF_POWER = "CONF:POW"
    SCPI_SET_WAVELENGTH = "SENS:CORR:WAV {}"
    SCPI_AUTO_RANGE_ON = "SENS:POW:RANG:AUTO ON"
    SCPI_SET_AVERAGES = "SENS:AVER:COUN {}"
    
    def __init__(self):
        self.rm = None
        self.inst = None
        self.resource: Optional[str] = None
        self.idn: str = ""
        self.lock = threading.Lock()
        self.connected = False
    
    def _resource_manager(self):
        try:
            return pyvisa.ResourceManager()
        except Exception:
            return pyvisa.ResourceManager("@py")
    
    def list_resources(self, filter_pattern: str = "") -> List[str]:
        if not PYVISA_AVAILABLE:
            return []
        rm = self._resource_manager()
        try:
            all_resources = rm.list_resources()
            if filter_pattern:
                filtered = [r for r in all_resources if filter_pattern.upper() in r.upper()]
                return sorted(filtered)
            return sorted(all_resources)
        except Exception:
            return []
        finally:
            try:
                rm.close()
            except:
                pass
    
    def connect(self, resource: str, timeout_ms: int = 5000) -> Tuple[bool, str]:
        self.disconnect()
        try:
            self.rm = self._resource_manager()
            self.inst = self.rm.open_resource(resource)
            self.inst.timeout = timeout_ms
            self.inst.write_termination = "\n"
            self.inst.read_termination = "\n"
            
            with self.lock:
                self.idn = self.inst.query(self.SCPI_IDN).strip()
                self.inst.write(self.SCPI_CONF_POWER)
                time.sleep(0.1)
            
            self.resource = resource
            self.connected = True
            return True, f"Connected: {self.idn}"
        except Exception as exc:
            self.disconnect()
            return False, f"Connection failed: {exc}"
    
    def disconnect(self) -> None:
        if self.inst is not None:
            try:
                self.inst.close()
            except:
                pass
        if self.rm is not None:
            try:
                self.rm.close()
            except:
                pass
        self.inst = None
        self.rm = None
        self.resource = None
        self.idn = ""
        self.connected = False
    
    def configure(self, wavelength_nm: float, auto_range: bool = True, 
                  averages: int = 1) -> bool:
        if not self.inst:
            return False
        try:
            with self.lock:
                self.inst.write(self.SCPI_SET_WAVELENGTH.format(wavelength_nm))
                time.sleep(0.05)
                if auto_range:
                    self.inst.write(self.SCPI_AUTO_RANGE_ON)
                self.inst.write(self.SCPI_SET_AVERAGES.format(averages))
            return True
        except Exception:
            return False
    
    def measure_power(self) -> Tuple[float, str]:
        if not self.inst:
            return 0.0, "Not connected"
        try:
            with self.lock:
                resp = self.inst.query(self.SCPI_MEAS_POWER).strip()
            power = float(resp)
            return power, "OK"
        except ValueError:
            return 0.0, f"Parse error"
        except Exception as e:
            return 0.0, f"Error: {e}"


# =============================================================================
# B1500 Controller
# =============================================================================

class B1500Controller:
    """Controller for Keysight B1500 Semiconductor Parameter Analyzer"""
    
    def __init__(self):
        self.rm = None
        self.inst = None
        self.resource: Optional[str] = None
        self.idn: str = ""
        self.lock = threading.Lock()
        self.connected = False
    
    def _resource_manager(self):
        try:
            return pyvisa.ResourceManager()
        except Exception:
            return pyvisa.ResourceManager("@py")
    
    def list_all_resources(self) -> List[str]:
        if not PYVISA_AVAILABLE:
            return []
        rm = self._resource_manager()
        try:
            return sorted(rm.list_resources())
        except Exception:
            return []
        finally:
            try:
                rm.close()
            except:
                pass
    
    def connect(self, resource: str, timeout_ms: int = 15000) -> Tuple[bool, str]:
        self.disconnect()
        try:
            self.rm = self._resource_manager()
            self.inst = self.rm.open_resource(resource)
            self.inst.timeout = timeout_ms
            self.inst.write_termination = "\n"
            self.inst.read_termination = "\n"
            
            with self.lock:
                self.idn = self.inst.query("*IDN?").strip()
                self.inst.write("FMT 1,0")
                time.sleep(0.1)
            
            self.resource = resource
            self.connected = True
            return True, f"Connected: {self.idn}"
        except Exception as exc:
            self.disconnect()
            return False, f"Connection failed: {exc}"
    
    def disconnect(self) -> None:
        if self.inst is not None:
            try:
                self.inst.close()
            except:
                pass
        if self.rm is not None:
            try:
                self.rm.close()
            except:
                pass
        self.inst = None
        self.rm = None
        self.resource = None
        self.idn = ""
        self.connected = False
    
    def _safe_read(self) -> str:
        if not self.inst:
            return ""
        try:
            raw = self.inst.read_raw()
            for encoding in ['ascii', 'latin-1', 'utf-8']:
                try:
                    return raw.decode(encoding).strip()
                except UnicodeDecodeError:
                    continue
            return raw.decode('ascii', errors='ignore').strip()
        except Exception:
            return ""
    
    def configure_for_sweep(self, smu: int, mode: str, compliance: float) -> None:
        """Configure B1500 for point-by-point measurements"""
        with self.lock:
            if not self.inst:
                raise RuntimeError("Not connected")
            
            # Clear errors
            try:
                for _ in range(5):
                    err = self.inst.query("ERR?")
                    if err.strip().startswith("0"):
                        break
            except:
                pass
            time.sleep(0.1)
            
            self.inst.write("FMT 1,0")
            time.sleep(0.05)
            self.inst.write(f"CN {smu}")
            time.sleep(0.1)
            self.inst.write(f"AAD {smu},1")
            time.sleep(0.05)
            self.inst.write("AV 1,0")
            time.sleep(0.05)
            
            # Set measurement range
            if mode == "iv":
                self.inst.write(f"RI {smu},0")  # Auto range current
            else:
                self.inst.write(f"RV {smu},0")  # Auto range voltage
            time.sleep(0.05)
            
            self.inst.write(f"MM 1,{smu}")  # Spot measurement mode
    
    def set_bias_and_measure(self, smu: int, set_value: float, mode: str, 
                             compliance: float, dwell_s: float = 0.1) -> Tuple[float, float]:
        """Set source and measure at single point"""
        with self.lock:
            if not self.inst:
                raise RuntimeError("Not connected")
            
            try:
                if mode == "iv" or mode == "voltage":
                    self.inst.write(f"DV {smu},0,{set_value},{compliance}")
                else:
                    self.inst.write(f"DI {smu},0,{set_value},{compliance}")
                
                if dwell_s > 0:
                    time.sleep(dwell_s)
                
                self.inst.write("XE")
                
                old_timeout = self.inst.timeout
                self.inst.timeout = 5000
                try:
                    resp = self._safe_read()
                finally:
                    self.inst.timeout = old_timeout
                    
            except Exception as e:
                return (set_value, 0.0) if mode in ["iv", "voltage"] else (0.0, set_value)
        
        # Parse response
        try:
            if not resp:
                return (set_value, 0.0) if mode in ["iv", "voltage"] else (0.0, set_value)
            
            parts = resp.replace(";", ",").split(",")
            values = []
            
            for p in parts:
                p = p.strip()
                if not p:
                    continue
                try:
                    num_start = 0
                    for i, c in enumerate(p):
                        if c in '+-0123456789.':
                            num_start = i
                            break
                    num_str = p[num_start:]
                    if num_str:
                        values.append(float(num_str))
                except:
                    pass
            
            if len(values) >= 1:
                measured = values[0]
                if mode in ["iv", "voltage"]:
                    return set_value, measured  # V_set, I_meas
                else:
                    return measured, set_value  # V_meas, I_set
            
            return (set_value, 0.0) if mode in ["iv", "voltage"] else (0.0, set_value)
                
        except Exception:
            return (set_value, 0.0) if mode in ["iv", "voltage"] else (0.0, set_value)
    
    def output_off(self, smu: int) -> None:
        """Turn off SMU output"""
        if not self.inst:
            return
        with self.lock:
            try:
                self.inst.write(f"DV {smu},0,0,0.01")
                time.sleep(0.05)
                self.inst.write(f"CL {smu}")
            except:
                pass


# =============================================================================
# Stress-Measurement Cycling Engine
# =============================================================================

class StressMeasurementEngine:
    """Engine for running stress-measurement cycling tests"""
    
    def __init__(self, b1500: B1500Controller, power_meter: ThorlabsPowerMeterController,
                 config: CycleConfig):
        self.b1500 = b1500
        self.power_meter = power_meter
        self.config = config
        
        # Data storage
        self.measurement_data: List[MeasurementPoint] = []
        self.stress_data: List[StressPoint] = []
        self.cycle_summaries: List[CycleSummary] = []
        
        # State
        self.running = False
        self.stop_requested = False
        self.current_phase = TestPhase.IDLE
        self.current_cycle = 0
        
        # Callbacks
        self.on_measurement_point = None
        self.on_stress_point = None
        self.on_phase_change = None
        self.on_cycle_complete = None
        self.on_progress = None
        self.on_log = None
        
        # Output paths
        self.session_folder: Optional[Path] = None
    
    def log(self, message: str):
        timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        msg = f"[{timestamp}] {message}"
        print(msg)
        if self.on_log:
            self.on_log(msg)
    
    def set_phase(self, phase: TestPhase):
        self.current_phase = phase
        if self.on_phase_change:
            self.on_phase_change(phase)

    @staticmethod
    def _build_cycle_timing_plan(total_cycles: int, base_duration_s: float,
                                 time_design: str,
                                 log_points_per_decade: int) -> Tuple[List[float], List[float]]:
        """Return per-cycle stress durations and cumulative elapsed times."""
        base = max(1e-9, float(base_duration_s))
        design = (time_design or TIME_DESIGN_LINEAR).strip().lower()

        if design == TIME_DESIGN_LOG:
            ppd = int(log_points_per_decade)
            if ppd not in LOG_POINTS_PER_DECADE_OPTIONS:
                ppd = min(LOG_POINTS_PER_DECADE_OPTIONS, key=lambda v: abs(v - ppd))
            mantissas = LOG_CYCLE_MANTISSAS[ppd]
            durations = [
                base * mantissas[idx % ppd] * (10 ** (idx // ppd))
                for idx in range(total_cycles)
            ]
        else:
            durations = [base for _ in range(total_cycles)]

        durations = [max(1e-9, float(v)) for v in durations]
        cumulative_elapsed: List[float] = []
        running = 0.0
        for dt in durations:
            running += dt
            cumulative_elapsed.append(running)
        return durations, cumulative_elapsed
    
    def run(self) -> Tuple[List[MeasurementPoint], List[StressPoint]]:
        """Run the complete stress-measurement cycle test"""
        self.running = True
        self.stop_requested = False
        self.measurement_data = []
        self.stress_data = []
        self.cycle_summaries = []
        
        # Create session folder
        self._create_session_folder()
        
        # Configure power meter
        if self.power_meter.connected and self.config.enable_power_meter:
            self.power_meter.configure(wavelength_nm=self.config.power_wavelength_nm)
            self.log(f"Power meter configured: λ={self.config.power_wavelength_nm}nm")
        
        # Configure B1500
        if self.b1500.connected:
            try:
                self.b1500.configure_for_sweep(
                    self.config.sweep.smu,
                    self.config.sweep.mode,
                    self.config.sweep.compliance
                )
                self.log("B1500 configured")
            except Exception as e:
                self.log(f"B1500 configuration error: {e}")
                self.running = False
                return self.measurement_data, self.stress_data
        
        total_cycles = self.config.num_cycles
        stress_cfg = self.config.stress
        cycle_durations, cumulative_elapsed = self._build_cycle_timing_plan(
            total_cycles,
            stress_cfg.duration_s,
            stress_cfg.time_design,
            stress_cfg.log_points_per_decade,
        )
        design_normalized = (stress_cfg.time_design or TIME_DESIGN_LINEAR).strip().lower()
        if design_normalized == TIME_DESIGN_LOG:
            design_label = f"LOG({stress_cfg.log_points_per_decade} cycles/decade)"
            duration_label = f"t0={stress_cfg.duration_s}s"
        else:
            design_label = "LINEAR"
            duration_label = f"cycle_dt={stress_cfg.duration_s}s"

        self.log(f"Starting stress-measurement cycling test: {total_cycles} cycles")
        self.log(
            f"Stress: {stress_cfg.value}{'V' if stress_cfg.mode == 'voltage' else 'A'} "
            f"| {duration_label} | time design={design_label}"
        )
        preview_n = min(10, len(cycle_durations))
        preview = ", ".join(f"{v:g}" for v in cycle_durations[:preview_n])
        if len(cycle_durations) > preview_n:
            preview += ", ..."
        self.log(f"Cycle stress durations (s): {preview}")
        
        try:
            # Initial measurement (cycle 0)
            if self.config.initial_measurement:
                self.current_cycle = 0
                self._run_measurement_phase()
                if self.stop_requested:
                    return self.measurement_data, self.stress_data
            
            # Main cycling loop
            for cycle, cycle_duration_s in enumerate(cycle_durations, start=1):
                if self.stop_requested:
                    self.log("Test stopped by user")
                    break
                
                self.current_cycle = cycle
                elapsed_s = cumulative_elapsed[cycle - 1]
                self.log(f"\n{'='*50}")
                self.log(
                    f"CYCLE {cycle}/{total_cycles} | stress={cycle_duration_s:g}s "
                    f"| cumulative elapsed={elapsed_s:g}s"
                )
                self.log(f"{'='*50}")
                
                # Stress phase
                self._run_stress_phase(cycle_duration_s)
                if self.stop_requested:
                    break
                
                # Measurement phase
                self._run_measurement_phase()
                
                # Progress callback
                if self.on_progress:
                    self.on_progress(cycle, total_cycles)
                
                # Cycle complete callback
                if self.on_cycle_complete:
                    self.on_cycle_complete(cycle)
            
            # Turn off output
            if self.b1500.connected:
                self.b1500.output_off(self.config.sweep.smu)
            
            # Save final summary
            self._save_summary()
            
            self.set_phase(TestPhase.COMPLETED if not self.stop_requested else TestPhase.STOPPED)
            self.log(f"\nTest complete. {len(self.measurement_data)} measurement points, "
                    f"{len(self.stress_data)} stress points recorded.")
            
        except Exception as e:
            self.log(f"Test error: {e}")
            import traceback
            traceback.print_exc()
        finally:
            self.running = False
        
        return self.measurement_data, self.stress_data
    
    def _run_measurement_phase(self):
        """Run IV + power measurement sweep"""
        self.set_phase(TestPhase.MEASUREMENT)
        self.log(f"Starting measurement phase (Cycle {self.current_cycle})")
        
        cfg = self.config.sweep
        setpoints = cfg.setpoints
        cycle_data = []
        
        for idx, setpoint in enumerate(setpoints):
            if self.stop_requested:
                break
            
            timestamp = time.time()
            
            # IV measurement
            if self.b1500.connected:
                voltage, current = self.b1500.set_bias_and_measure(
                    cfg.smu, setpoint, cfg.mode, cfg.compliance, cfg.dwell_s
                )
            else:
                voltage = setpoint if cfg.mode == "iv" else 0.0
                current = 0.0 if cfg.mode == "iv" else setpoint
            
            # Optical power
            if self.power_meter.connected and self.config.enable_power_meter:
                power, status = self.power_meter.measure_power()
            else:
                power = 0.0
                status = "No power meter"
            
            point = MeasurementPoint(
                cycle=self.current_cycle,
                point_index=idx,
                timestamp=timestamp,
                setpoint=setpoint,
                voltage=voltage,
                current=current,
                optical_power=power,
                status=status
            )
            
            self.measurement_data.append(point)
            cycle_data.append(point)
            
            if self.on_measurement_point:
                self.on_measurement_point(point)
        
        # Save measurement data for this cycle
        self._save_measurement_cycle(cycle_data)
        
        # Calculate cycle summary
        if cycle_data:
            summary = self._calculate_summary(cycle_data)
            self.cycle_summaries.append(summary)
        
        self.log(f"Measurement phase complete: {len(cycle_data)} points")
    
    def _run_stress_phase(self, duration_s: float):
        """Run stress phase with monitoring"""
        self.set_phase(TestPhase.STRESS)
        
        cfg = self.config.stress
        self.log(f"Starting stress phase: {cfg.value}{'V' if cfg.mode == 'voltage' else 'A'} "
                f"for {duration_s}s")
        
        start_time = time.time()
        end_time = start_time + duration_s
        cycle_stress_data = []
        
        # Configure B1500 for stress
        if self.b1500.connected:
            try:
                smu = self.config.sweep.smu
                with self.b1500.lock:
                    if cfg.mode == "voltage":
                        self.b1500.inst.write(f"DV {smu},0,{cfg.value},{cfg.compliance}")
                    else:
                        self.b1500.inst.write(f"DI {smu},0,{cfg.value},{cfg.compliance}")
            except Exception as e:
                self.log(f"Stress setup error: {e}")
                return
        
        sample_count = 0
        while time.time() < end_time and not self.stop_requested:
            timestamp = time.time()
            elapsed = timestamp - start_time
            
            # Monitor current/voltage
            if self.b1500.connected:
                voltage, current = self.b1500.set_bias_and_measure(
                    self.config.sweep.smu, cfg.value, cfg.mode, 
                    cfg.compliance, dwell_s=0.01
                )
            else:
                voltage = cfg.value if cfg.mode == "voltage" else 0.0
                current = 0.0 if cfg.mode == "voltage" else cfg.value
            
            # Monitor optical power
            if self.power_meter.connected and self.config.enable_power_meter:
                power, status = self.power_meter.measure_power()
            else:
                power = 0.0
                status = "No power meter"
            
            point = StressPoint(
                cycle=self.current_cycle,
                timestamp=timestamp,
                elapsed_s=elapsed,
                voltage=voltage,
                current=current,
                optical_power=power,
                status=status
            )
            
            self.stress_data.append(point)
            cycle_stress_data.append(point)
            sample_count += 1
            
            if self.on_stress_point:
                self.on_stress_point(point)
            
            # Log every 10 seconds
            if sample_count % max(1, int(10 / cfg.sample_interval_s)) == 0:
                self.log(f"  Stress: {elapsed:.1f}s, I={current:.4e}A, P={power:.4e}W")
            
            # Wait for next sample
            next_sample_time = start_time + sample_count * cfg.sample_interval_s
            sleep_time = next_sample_time - time.time()
            if sleep_time > 0:
                time.sleep(min(sleep_time, 0.5))  # Check stop every 0.5s max
        
        # Save stress data for this cycle
        self._save_stress_cycle(cycle_stress_data)
        
        self.log(f"Stress phase complete: {len(cycle_stress_data)} samples, "
                f"duration: {time.time() - start_time:.1f}s")
    
    def _calculate_summary(self, data: List[MeasurementPoint]) -> CycleSummary:
        """Calculate summary parameters from measurement data"""
        voltages = [p.voltage for p in data]
        currents = [p.current for p in data]
        powers = [p.optical_power for p in data]
        
        peak_current = max(currents) if currents else 0.0
        peak_power = max(powers) if powers else 0.0
        
        # Estimate threshold voltage (where current > 1µA)
        threshold_v = 0.0
        for v, i in zip(voltages, currents):
            if abs(i) > 1e-6:
                threshold_v = v
                break
        
        # Estimate series resistance from slope in high-current region
        series_r = 0.0
        if len(voltages) > 5:
            try:
                # Use last 30% of data for linear fit
                n_fit = max(3, len(voltages) // 3)
                v_fit = np.array(voltages[-n_fit:])
                i_fit = np.array(currents[-n_fit:])
                if np.std(i_fit) > 0:
                    slope, _ = np.polyfit(i_fit, v_fit, 1)
                    series_r = abs(slope)
            except:
                pass
        
        return CycleSummary(
            cycle=self.current_cycle,
            timestamp=datetime.now().isoformat(),
            peak_current=peak_current,
            peak_power=peak_power,
            threshold_voltage=threshold_v,
            series_resistance=series_r
        )
    
    def _create_session_folder(self):
        """Create session folder for output files"""
        base_path = Path(self.config.output_folder)
        if not base_path.is_absolute():
            base_path = Path(__file__).parent / self.config.output_folder
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.session_folder = base_path / f"{self.config.device_name}_stress_test_{timestamp}"
        self.session_folder.mkdir(parents=True, exist_ok=True)
        self.log(f"Session folder: {self.session_folder}")
    
    def _save_measurement_cycle(self, data: List[MeasurementPoint]):
        """Save measurement data for a cycle"""
        if not data or not self.config.autosave or not self.session_folder:
            return
        
        filename = f"measurement_cycle_{self.current_cycle:03d}.csv"
        filepath = self.session_folder / filename
        
        with open(filepath, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                "Point", "Timestamp", "Setpoint", "Voltage_V", 
                "Current_A", "Optical_Power_W", "Status"
            ])
            for p in data:
                writer.writerow([
                    p.point_index,
                    datetime.fromtimestamp(p.timestamp).isoformat(),
                    f"{p.setpoint:.6e}",
                    f"{p.voltage:.6e}",
                    f"{p.current:.6e}",
                    f"{p.optical_power:.6e}",
                    p.status
                ])
    
    def _save_stress_cycle(self, data: List[StressPoint]):
        """Save stress data for a cycle"""
        if not data or not self.config.autosave or not self.session_folder:
            return
        
        filename = f"stress_cycle_{self.current_cycle:03d}.csv"
        filepath = self.session_folder / filename
        
        with open(filepath, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                "Timestamp", "Elapsed_s", "Voltage_V", 
                "Current_A", "Optical_Power_W", "Status"
            ])
            for p in data:
                writer.writerow([
                    datetime.fromtimestamp(p.timestamp).isoformat(),
                    f"{p.elapsed_s:.3f}",
                    f"{p.voltage:.6e}",
                    f"{p.current:.6e}",
                    f"{p.optical_power:.6e}",
                    p.status
                ])
    
    def _save_summary(self):
        """Save cycle summaries"""
        if not self.cycle_summaries or not self.session_folder:
            return
        
        filepath = self.session_folder / "cycle_summary.csv"
        with open(filepath, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                "Cycle", "Timestamp", "Peak_Current_A", "Peak_Power_W",
                "Threshold_V", "Series_Resistance_Ohm"
            ])
            for s in self.cycle_summaries:
                writer.writerow([
                    s.cycle, s.timestamp, f"{s.peak_current:.6e}",
                    f"{s.peak_power:.6e}", f"{s.threshold_voltage:.4f}",
                    f"{s.series_resistance:.4f}"
                ])
        
        self.log(f"Summary saved to: {filepath}")
    
    def stop(self):
        """Request stop of test"""
        self.stop_requested = True


# =============================================================================
# GUI Worker Thread
# =============================================================================

class TestWorker(QThread):
    """Worker thread for running the stress-measurement test"""
    measurement_point = pyqtSignal(object)
    stress_point = pyqtSignal(object)
    phase_change = pyqtSignal(object)
    cycle_complete = pyqtSignal(int)
    progress = pyqtSignal(int, int)
    log_message = pyqtSignal(str)
    finished_signal = pyqtSignal()
    
    def __init__(self, engine: StressMeasurementEngine):
        super().__init__()
        self.engine = engine
        self.engine.on_measurement_point = lambda p: self.measurement_point.emit(p)
        self.engine.on_stress_point = lambda p: self.stress_point.emit(p)
        self.engine.on_phase_change = lambda p: self.phase_change.emit(p)
        self.engine.on_cycle_complete = lambda c: self.cycle_complete.emit(c)
        self.engine.on_progress = lambda c, t: self.progress.emit(c, t)
        self.engine.on_log = lambda m: self.log_message.emit(m)
    
    def run(self):
        self.engine.run()
        self.finished_signal.emit()


class ResourceRefreshWorker(QThread):
    """Worker thread for enumerating VISA resources without blocking the GUI."""
    resources_ready = pyqtSignal(object)
    refresh_failed = pyqtSignal(str)

    def __init__(self, fetch_resources: Callable[[], List[str]]):
        super().__init__()
        self.fetch_resources = fetch_resources

    def run(self):
        try:
            resources = self.fetch_resources()
            self.resources_ready.emit(resources)
        except Exception as exc:
            self.refresh_failed.emit(str(exc))


# =============================================================================
# Main GUI
# =============================================================================

class StressMeasurementCycleGUI(QMainWindow):
    """Main GUI for stress-measurement cycling tests"""
    
    def __init__(self):
        super().__init__()
        self.b1500 = B1500Controller()
        self.power_meter = ThorlabsPowerMeterController()
        self.worker = None
        self.resource_worker = None
        
        self.setWindowTitle("B1500 Stress-Measurement Cycling Test")
        self.setMinimumSize(1500, 950)
        
        # Plot data
        self.meas_voltages = []
        self.meas_currents = []
        self.meas_powers = []
        self.meas_cycles = []
        
        self.stress_times = []
        self.stress_currents = []
        self.stress_powers = []
        
        self.summary_cycles = []
        self.summary_peak_currents = []
        self.summary_peak_powers = []
        
        self.setup_ui()

        # Plot throttling: accumulate data and redraw at most every 400 ms
        # to prevent canvas.draw() calls from blocking the Qt event loop.
        self._meas_dirty = False
        self._stress_dirty = False
        self._degradation_dirty = False
        self._plot_timer = QTimer(self)
        self._plot_timer.setInterval(400)
        self._plot_timer.timeout.connect(self._flush_plot_updates)
        self._plot_timer.start()

        self.refresh_resources()
    
    def setup_ui(self):
        """Setup the GUI layout"""
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QHBoxLayout(central_widget)
        main_layout.setContentsMargins(8, 8, 8, 8)
        main_layout.setSpacing(10)
        
        # Left panel - controls
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(10)
        left_panel.setMinimumWidth(440)
        left_panel.setMaximumWidth(520)

        left_scroll = QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_scroll.setFrameShape(QFrame.NoFrame)
        left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        left_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        left_scroll.setMinimumWidth(460)
        left_scroll.setMaximumWidth(540)
        left_scroll.setWidget(left_panel)
        
        # === Device Connection ===
        device_group = QGroupBox("Device Connection")
        device_layout = QGridLayout(device_group)
        device_group.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        
        device_layout.addWidget(QLabel("B1500:"), 0, 0)
        self.combo_b1500 = QComboBox()
        self.combo_b1500.setMinimumWidth(200)
        device_layout.addWidget(self.combo_b1500, 0, 1)
        
        self.btn_connect_b1500 = QPushButton("Connect")
        self.btn_connect_b1500.clicked.connect(self.connect_b1500)
        device_layout.addWidget(self.btn_connect_b1500, 0, 2)
        
        self.label_b1500_status = QLabel("Not connected")
        self.label_b1500_status.setStyleSheet("color: red;")
        device_layout.addWidget(self.label_b1500_status, 1, 0, 1, 3)
        
        device_layout.addWidget(QLabel("Power Meter:"), 2, 0)
        self.combo_power_meter = QComboBox()
        device_layout.addWidget(self.combo_power_meter, 2, 1)
        
        self.btn_connect_pm = QPushButton("Connect")
        self.btn_connect_pm.clicked.connect(self.connect_power_meter)
        device_layout.addWidget(self.btn_connect_pm, 2, 2)
        
        self.label_pm_status = QLabel("Not connected")
        self.label_pm_status.setStyleSheet("color: red;")
        device_layout.addWidget(self.label_pm_status, 3, 0, 1, 3)
        
        self.btn_refresh = QPushButton("Refresh Devices")
        self.btn_refresh.clicked.connect(self.refresh_resources)
        device_layout.addWidget(self.btn_refresh, 4, 0, 1, 3)
        
        left_layout.addWidget(device_group)
        
        # === Measurement Configuration ===
        meas_group = QGroupBox("IV Measurement Configuration")
        meas_layout = QGridLayout(meas_group)
        meas_group.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        
        meas_layout.addWidget(QLabel("SMU:"), 0, 0)
        self.spin_smu = QSpinBox()
        self.spin_smu.setRange(1, 10)
        self.spin_smu.setValue(1)
        meas_layout.addWidget(self.spin_smu, 0, 1)
        
        meas_layout.addWidget(QLabel("Mode:"), 0, 2)
        self.combo_mode = QComboBox()
        self.combo_mode.addItems(["IV (V→I)", "VI (I→V)"])
        meas_layout.addWidget(self.combo_mode, 0, 3)
        
        meas_layout.addWidget(QLabel("Start:"), 1, 0)
        self.spin_start = QDoubleSpinBox()
        self.spin_start.setRange(-200, 200)
        self.spin_start.setDecimals(4)
        self.spin_start.setValue(0)
        meas_layout.addWidget(self.spin_start, 1, 1)
        
        meas_layout.addWidget(QLabel("Stop:"), 1, 2)
        self.spin_stop = QDoubleSpinBox()
        self.spin_stop.setRange(-200, 200)
        self.spin_stop.setDecimals(4)
        self.spin_stop.setValue(2.0)
        meas_layout.addWidget(self.spin_stop, 1, 3)
        
        meas_layout.addWidget(QLabel("Steps:"), 2, 0)
        self.spin_steps = QSpinBox()
        self.spin_steps.setRange(2, 1001)
        self.spin_steps.setValue(21)
        meas_layout.addWidget(self.spin_steps, 2, 1)
        
        meas_layout.addWidget(QLabel("Dwell (s):"), 2, 2)
        self.spin_dwell = QDoubleSpinBox()
        self.spin_dwell.setRange(0, 10)
        self.spin_dwell.setDecimals(3)
        self.spin_dwell.setValue(0.1)
        meas_layout.addWidget(self.spin_dwell, 2, 3)
        
        meas_layout.addWidget(QLabel("Compliance:"), 3, 0)
        self.spin_compliance = QDoubleSpinBox()
        self.spin_compliance.setRange(0.0001, 200)
        self.spin_compliance.setDecimals(6)
        self.spin_compliance.setValue(0.1)
        meas_layout.addWidget(self.spin_compliance, 3, 1)
        
        left_layout.addWidget(meas_group)
        
        # === Stress Configuration ===
        stress_group = QGroupBox("Stress Configuration")
        stress_layout = QGridLayout(stress_group)
        stress_group.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        
        stress_layout.addWidget(QLabel("Stress Mode:"), 0, 0)
        self.combo_stress_mode = QComboBox()
        self.combo_stress_mode.addItems(["Constant Voltage", "Constant Current"])
        stress_layout.addWidget(self.combo_stress_mode, 0, 1)
        
        stress_layout.addWidget(QLabel("Stress Value:"), 0, 2)
        self.spin_stress_value = QDoubleSpinBox()
        self.spin_stress_value.setRange(-200, 200)
        self.spin_stress_value.setDecimals(4)
        self.spin_stress_value.setValue(2.0)
        self.spin_stress_value.setSuffix(" V")
        stress_layout.addWidget(self.spin_stress_value, 0, 3)
        
        self.combo_stress_mode.currentIndexChanged.connect(self.on_stress_mode_changed)
        
        self.lbl_stress_duration_linear = QLabel("Linear cycle duration (s):")
        stress_layout.addWidget(self.lbl_stress_duration_linear, 1, 0)
        self.spin_stress_duration_linear = QDoubleSpinBox()
        self.spin_stress_duration_linear.setRange(1e-6, 1_000_000)
        self.spin_stress_duration_linear.setDecimals(6)
        self.spin_stress_duration_linear.setSingleStep(0.01)
        self.spin_stress_duration_linear.setValue(60.0)
        self.spin_stress_duration_linear.setToolTip(
            "Used only in Linear mode: fixed stress duration per cycle."
        )
        stress_layout.addWidget(self.spin_stress_duration_linear, 1, 1)

        self.lbl_stress_duration_log_t0 = QLabel("Log start duration t0 (s):")
        stress_layout.addWidget(self.lbl_stress_duration_log_t0, 1, 2)
        self.spin_stress_duration_log_t0 = QDoubleSpinBox()
        self.spin_stress_duration_log_t0.setRange(1e-6, 1_000_000)
        self.spin_stress_duration_log_t0.setDecimals(6)
        self.spin_stress_duration_log_t0.setSingleStep(0.01)
        self.spin_stress_duration_log_t0.setValue(0.01)
        self.spin_stress_duration_log_t0.setToolTip(
            "Used only in Log mode: first-cycle stress duration t0."
        )
        stress_layout.addWidget(self.spin_stress_duration_log_t0, 1, 3)

        stress_layout.addWidget(QLabel("Sample Rate (s):"), 2, 0)
        self.spin_stress_interval = QDoubleSpinBox()
        self.spin_stress_interval.setRange(0.1, 60)
        self.spin_stress_interval.setDecimals(2)
        self.spin_stress_interval.setValue(1.0)
        stress_layout.addWidget(self.spin_stress_interval, 2, 1)

        stress_layout.addWidget(QLabel("Time Design:"), 2, 2)
        self.combo_time_design = QComboBox()
        self.combo_time_design.addItem("Linear (equal cycle dt)", TIME_DESIGN_LINEAR)
        self.combo_time_design.addItem("Log", TIME_DESIGN_LOG)
        self.combo_time_design.currentIndexChanged.connect(self.on_time_design_changed)
        stress_layout.addWidget(self.combo_time_design, 2, 3)

        self.lbl_log_ppd = QLabel("Log cycles/decade:")
        stress_layout.addWidget(self.lbl_log_ppd, 3, 0)
        self.combo_log_ppd = QComboBox()
        for option in LOG_POINTS_PER_DECADE_OPTIONS:
            self.combo_log_ppd.addItem(str(option), option)
        self.combo_log_ppd.setCurrentIndex(LOG_POINTS_PER_DECADE_OPTIONS.index(5))
        self.combo_log_ppd.setToolTip(
            "Log density D: how many cycles are placed in each decade. "
            "This controls spacing only, not total cycle count N."
        )
        self.combo_log_ppd.currentIndexChanged.connect(lambda *_: self._update_time_estimate_hint())
        stress_layout.addWidget(self.combo_log_ppd, 3, 1)

        stress_layout.addWidget(QLabel("Stress Compliance:"), 3, 2)
        self.spin_stress_compliance = QDoubleSpinBox()
        self.spin_stress_compliance.setRange(0.0001, 200)
        self.spin_stress_compliance.setDecimals(6)
        self.spin_stress_compliance.setValue(0.1)
        stress_layout.addWidget(self.spin_stress_compliance, 3, 3)

        self.on_time_design_changed()
        
        left_layout.addWidget(stress_group)
        
        # === Cycle Configuration ===
        cycle_group = QGroupBox("Cycle Configuration")
        cycle_layout = QGridLayout(cycle_group)
        cycle_group.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        
        cycle_layout.addWidget(QLabel("Number of Cycles:"), 0, 0)
        self.spin_num_cycles = QSpinBox()
        self.spin_num_cycles.setRange(1, 10000)
        self.spin_num_cycles.setValue(10)
        self.spin_num_cycles.valueChanged.connect(lambda *_: self._update_time_estimate_hint())
        cycle_layout.addWidget(self.spin_num_cycles, 0, 1)
        
        self.check_initial_meas = QCheckBox("Initial Measurement (Baseline)")
        self.check_initial_meas.setChecked(True)
        self.check_initial_meas.toggled.connect(lambda *_: self._update_time_estimate_hint())
        cycle_layout.addWidget(self.check_initial_meas, 0, 2, 1, 2)

        self.lbl_time_estimate_hint = QLabel()
        self.lbl_time_estimate_hint.setStyleSheet("color:#555; font-size:9pt;")
        self.lbl_time_estimate_hint.setWordWrap(True)
        cycle_layout.addWidget(self.lbl_time_estimate_hint, 1, 0, 1, 4)

        self.spin_steps.valueChanged.connect(lambda *_: self._update_time_estimate_hint())
        self.spin_dwell.valueChanged.connect(lambda *_: self._update_time_estimate_hint())
        self.spin_stress_duration_linear.valueChanged.connect(lambda *_: self._update_time_estimate_hint())
        self.spin_stress_duration_log_t0.valueChanged.connect(lambda *_: self._update_time_estimate_hint())

        self._update_time_estimate_hint()
        
        left_layout.addWidget(cycle_group)
        
        # === Power Meter Config ===
        pm_group = QGroupBox("Power Meter")
        pm_layout = QGridLayout(pm_group)
        pm_group.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        
        self.check_enable_pm = QCheckBox("Enable Power Measurement")
        self.check_enable_pm.setChecked(True)
        pm_layout.addWidget(self.check_enable_pm, 0, 0, 1, 2)
        
        pm_layout.addWidget(QLabel("Wavelength (nm):"), 1, 0)
        self.spin_wavelength = QDoubleSpinBox()
        self.spin_wavelength.setRange(200, 2000)
        self.spin_wavelength.setValue(850)
        pm_layout.addWidget(self.spin_wavelength, 1, 1)
        
        left_layout.addWidget(pm_group)
        
        # === Output Configuration ===
        output_group = QGroupBox("Output")
        output_layout = QGridLayout(output_group)
        output_group.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        
        output_layout.addWidget(QLabel("Folder:"), 0, 0)
        self.edit_folder = QLineEdit(str(Path(__file__).parent / "results"))
        output_layout.addWidget(self.edit_folder, 0, 1)
        
        self.btn_browse = QPushButton("Browse")
        self.btn_browse.clicked.connect(self.browse_folder)
        output_layout.addWidget(self.btn_browse, 0, 2)
        
        output_layout.addWidget(QLabel("Device Name:"), 1, 0)
        self.edit_device_name = QLineEdit("Device_001")
        output_layout.addWidget(self.edit_device_name, 1, 1, 1, 2)
        
        self.check_autosave = QCheckBox("Autosave Data")
        self.check_autosave.setChecked(True)
        output_layout.addWidget(self.check_autosave, 2, 0, 1, 3)
        
        left_layout.addWidget(output_group)
        
        # === Control Buttons ===
        btn_layout = QHBoxLayout()
        
        self.btn_start = QPushButton("▶ Start Test")
        self.btn_start.setMinimumHeight(45)
        self.btn_start.setStyleSheet("background-color: #4CAF50; color: white; font-weight: bold; font-size: 14px;")
        self.btn_start.clicked.connect(self.start_test)
        btn_layout.addWidget(self.btn_start)
        
        self.btn_stop = QPushButton("■ Stop")
        self.btn_stop.setMinimumHeight(45)
        self.btn_stop.setEnabled(False)
        self.btn_stop.setStyleSheet("font-size: 14px;")
        self.btn_stop.clicked.connect(self.stop_test)
        btn_layout.addWidget(self.btn_stop)
        
        left_layout.addLayout(btn_layout)
        
        # Progress bar
        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        left_layout.addWidget(self.progress_bar)
        
        # Status label
        self.label_phase = QLabel("Phase: IDLE")
        self.label_phase.setStyleSheet("font-weight: bold; font-size: 12px;")
        left_layout.addWidget(self.label_phase)
        
        # Log
        log_group = QGroupBox("Log")
        log_layout = QVBoxLayout(log_group)
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMaximumHeight(150)
        log_layout.addWidget(self.log_text)
        left_layout.addWidget(log_group)
        
        left_layout.addStretch()
        main_layout.addWidget(left_scroll)
        
        # === Right Panel - Plots ===
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        
        # Create tabs for different views
        self.tab_widget = QTabWidget()
        
        # Tab 1: Live Plots
        plot_tab = QWidget()
        plot_layout = QVBoxLayout(plot_tab)
        
        self.figure = Figure(figsize=(12, 9))
        self.canvas = FigureCanvas(self.figure)
        
        # Create 2x2 subplot grid
        self.ax_iv = self.figure.add_subplot(2, 2, 1)
        self.ax_iv.set_xlabel("Voltage (V)")
        self.ax_iv.set_ylabel("Current (A)")
        self.ax_iv.set_title("I-V Characteristic")
        self.ax_iv.grid(True, alpha=0.3)
        
        self.ax_li = self.figure.add_subplot(2, 2, 2)
        self.ax_li.set_xlabel("Current (A)")
        self.ax_li.set_ylabel("Optical Power (W)")
        self.ax_li.set_title("L-I Characteristic")
        self.ax_li.grid(True, alpha=0.3)
        
        self.ax_stress = self.figure.add_subplot(2, 2, 3)
        self.ax_stress.set_xlabel("Time (s)")
        self.ax_stress.set_ylabel("Current (A) / Power (W)")
        self.ax_stress.set_title("Stress Monitoring")
        self.ax_stress.grid(True, alpha=0.3)
        
        self.ax_degradation = self.figure.add_subplot(2, 2, 4)
        self.ax_degradation.set_xlabel("Cycle")
        self.ax_degradation.set_ylabel("Peak Values")
        self.ax_degradation.set_title("Degradation Tracking")
        self.ax_degradation.grid(True, alpha=0.3)
        
        self.figure.tight_layout()

        # Pre-create twin axes for stress/degradation plots so updates never
        # need to call ax.clear() + twinx() (which leaks axes and is slow).
        self.ax_stress_twin = self.ax_stress.twinx()
        self.ax_stress.set_ylabel("Current (A)", color='blue')
        self.ax_stress.tick_params(axis='y', labelcolor='blue')
        self.ax_stress_twin.set_ylabel("Optical Power (W)", color='red')
        self.ax_stress_twin.tick_params(axis='y', labelcolor='red')

        self.ax_degradation_twin = self.ax_degradation.twinx()
        self.ax_degradation.set_ylabel("Peak Current (A)", color='blue')
        self.ax_degradation.tick_params(axis='y', labelcolor='blue')
        self.ax_degradation_twin.set_ylabel("Peak Power (W)", color='red')
        self.ax_degradation_twin.tick_params(axis='y', labelcolor='red')

        # Pre-create Line2D objects; updates call set_data() instead of re-plotting.
        self._line_stress_current, = self.ax_stress.plot([], [], 'b-', linewidth=1, label='Current')
        self._line_stress_power, = self.ax_stress_twin.plot([], [], 'r-', linewidth=1, label='Power')
        self.ax_stress.legend([self._line_stress_current, self._line_stress_power],
                              ['Current', 'Power'], loc='upper right')

        self._line_deg_current, = self.ax_degradation.plot([], [], 'bo-', markersize=6, label='Peak Current')
        self._line_deg_power, = self.ax_degradation_twin.plot([], [], 'rs-', markersize=6, label='Peak Power')
        self.ax_degradation.legend([self._line_deg_current, self._line_deg_power],
                                   ['Peak Current', 'Peak Power'], loc='upper right')

        plot_layout.addWidget(self.canvas)
        
        self.tab_widget.addTab(plot_tab, "Live Plots")
        
        # Tab 2: Summary Table
        table_tab = QWidget()
        table_layout = QVBoxLayout(table_tab)
        
        self.summary_table = QTableWidget()
        self.summary_table.setColumnCount(6)
        self.summary_table.setHorizontalHeaderLabels([
            "Cycle", "Timestamp", "Peak Current (A)", "Peak Power (W)",
            "Threshold V", "Series R (Ω)"
        ])
        self.summary_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        table_layout.addWidget(self.summary_table)
        
        self.tab_widget.addTab(table_tab, "Cycle Summary")
        
        right_layout.addWidget(self.tab_widget)
        main_layout.addWidget(right_panel, stretch=2)
        
        # Status bar
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Ready")
    
    def log(self, message: str):
        self.log_text.append(message)
        self.log_text.verticalScrollBar().setValue(
            self.log_text.verticalScrollBar().maximum()
        )
    
    def refresh_resources(self):
        if self.resource_worker and self.resource_worker.isRunning():
            self.status_bar.showMessage("Device scan already in progress...")
            return

        self.combo_b1500.clear()
        self.combo_power_meter.clear()

        self.btn_refresh.setEnabled(False)
        self.status_bar.showMessage("Scanning VISA resources...")

        self.resource_worker = ResourceRefreshWorker(self.b1500.list_all_resources)
        self.resource_worker.resources_ready.connect(self.on_resources_refreshed)
        self.resource_worker.refresh_failed.connect(self.on_resource_refresh_failed)
        self.resource_worker.finished.connect(self.on_resource_refresh_finished)
        self.resource_worker.start()

    def on_resources_refreshed(self, all_resources: List[str]):
        gpib_resources = [r for r in all_resources if "GPIB" in r.upper()]
        usb_resources = [r for r in all_resources if "USB" in r.upper()]

        self.combo_b1500.addItems(gpib_resources)
        self.combo_power_meter.addItems(usb_resources)

        message = f"Found {len(gpib_resources)} GPIB and {len(usb_resources)} USB resources"
        self.status_bar.showMessage(message)
        self.log(message)

    def on_resource_refresh_failed(self, error_message: str):
        self.status_bar.showMessage("Device scan failed")
        self.log(f"Device scan failed: {error_message}")

    def on_resource_refresh_finished(self):
        self.btn_refresh.setEnabled(True)
        self.resource_worker = None
    
    def browse_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Output Folder")
        if folder:
            self.edit_folder.setText(folder)
    
    def on_stress_mode_changed(self, index: int):
        if index == 0:
            self.spin_stress_value.setSuffix(" V")
        else:
            self.spin_stress_value.setSuffix(" A")

    def on_time_design_changed(self, *_):
        design = self.active_time_design()
        is_log = design == TIME_DESIGN_LOG
        self.lbl_log_ppd.setVisible(is_log)
        self.combo_log_ppd.setVisible(is_log)
        self.spin_stress_duration_linear.setEnabled(not is_log)
        self.spin_stress_duration_log_t0.setEnabled(is_log)
        self._update_time_estimate_hint()

    def active_time_design(self) -> str:
        return str(self.combo_time_design.currentData() or TIME_DESIGN_LINEAR)

    @staticmethod
    def _build_preview_timing(total_cycles: int, base_duration_s: float,
                              time_design: str, log_points_per_decade: int) -> List[float]:
        base = max(1e-9, float(base_duration_s))
        design = (time_design or TIME_DESIGN_LINEAR).strip().lower()
        if design == TIME_DESIGN_LOG:
            ppd = int(log_points_per_decade)
            if ppd not in LOG_POINTS_PER_DECADE_OPTIONS:
                ppd = min(LOG_POINTS_PER_DECADE_OPTIONS, key=lambda v: abs(v - ppd))
            mantissas = LOG_CYCLE_MANTISSAS[ppd]
            return [base * mantissas[idx % ppd] * (10 ** (idx // ppd)) for idx in range(total_cycles)]
        return [base for _ in range(total_cycles)]

    def _update_time_estimate_hint(self):
        if not hasattr(self, "lbl_time_estimate_hint"):
            return

        n = int(self.spin_num_cycles.value())
        log_density = int(self.combo_log_ppd.currentData() or LOG_POINTS_PER_DECADE_OPTIONS[0])
        linear_dt = float(self.spin_stress_duration_linear.value())
        log_t0 = float(self.spin_stress_duration_log_t0.value())

        linear_bias_total = linear_dt * n
        log_bias_total = sum(self._build_preview_timing(
            n, log_t0, TIME_DESIGN_LOG, log_density
        ))

        steps = int(self.spin_steps.value())
        dwell = float(self.spin_dwell.value())
        init_extra = 1 if self.check_initial_meas.isChecked() else 0
        meas_total = (n + init_extra) * steps * dwell

        active_label = "LOG" if self.active_time_design() == TIME_DESIGN_LOG else "LINEAR"
        self.lbl_time_estimate_hint.setText(
            f"Meaning: D={log_density} cycles/decade is log spacing density only; N={n} is total loop count.\n"
            f"Active mode: {active_label}\n"
            f"Estimated total duration (bias only): Linear={linear_bias_total:g}s, Log={log_bias_total:g}s\n"
            f"Estimated total duration (+measurement approx): "
            f"Linear={linear_bias_total + meas_total:g}s, "
            f"Log={log_bias_total + meas_total:g}s"
        )
    
    def connect_b1500(self):
        if self.b1500.connected:
            self.b1500.disconnect()
            self.label_b1500_status.setText("Not connected")
            self.label_b1500_status.setStyleSheet("color: red;")
            self.btn_connect_b1500.setText("Connect")
            self.log("B1500 disconnected")
        else:
            resource = self.combo_b1500.currentText()
            if not resource:
                QMessageBox.warning(self, "Error", "No B1500 resource selected")
                return
            
            success, msg = self.b1500.connect(resource)
            if success:
                self.label_b1500_status.setText(f"Connected: {self.b1500.idn[:40]}...")
                self.label_b1500_status.setStyleSheet("color: green;")
                self.btn_connect_b1500.setText("Disconnect")
                self.log(f"B1500 connected: {self.b1500.idn}")
            else:
                QMessageBox.warning(self, "Connection Failed", msg)
    
    def connect_power_meter(self):
        if self.power_meter.connected:
            self.power_meter.disconnect()
            self.label_pm_status.setText("Not connected")
            self.label_pm_status.setStyleSheet("color: red;")
            self.btn_connect_pm.setText("Connect")
            self.log("Power meter disconnected")
        else:
            resource = self.combo_power_meter.currentText()
            if not resource:
                QMessageBox.warning(self, "Error", "No power meter resource selected")
                return
            
            success, msg = self.power_meter.connect(resource)
            if success:
                self.label_pm_status.setText(f"Connected: {self.power_meter.idn[:40]}...")
                self.label_pm_status.setStyleSheet("color: green;")
                self.btn_connect_pm.setText("Disconnect")
                self.log(f"Power meter connected: {self.power_meter.idn}")
            else:
                QMessageBox.warning(self, "Connection Failed", msg)
    
    def get_config(self) -> CycleConfig:
        mode = "iv" if self.combo_mode.currentIndex() == 0 else "vi"
        stress_mode = "voltage" if self.combo_stress_mode.currentIndex() == 0 else "current"
        
        return CycleConfig(
            sweep=SweepConfig(
                smu=self.spin_smu.value(),
                mode=mode,
                start=self.spin_start.value(),
                stop=self.spin_stop.value(),
                steps=self.spin_steps.value(),
                dwell_s=self.spin_dwell.value(),
                compliance=self.spin_compliance.value()
            ),
            stress=StressConfig(
                mode=stress_mode,
                value=self.spin_stress_value.value(),
                duration_s=(
                    self.spin_stress_duration_linear.value()
                    if self.active_time_design() == TIME_DESIGN_LINEAR
                    else self.spin_stress_duration_log_t0.value()
                ),
                time_design=self.active_time_design(),
                log_points_per_decade=int(self.combo_log_ppd.currentData()),
                sample_interval_s=self.spin_stress_interval.value(),
                compliance=self.spin_stress_compliance.value()
            ),
            num_cycles=self.spin_num_cycles.value(),
            initial_measurement=self.check_initial_meas.isChecked(),
            enable_power_meter=self.check_enable_pm.isChecked(),
            power_wavelength_nm=self.spin_wavelength.value(),
            output_folder=self.edit_folder.text(),
            device_name=self.edit_device_name.text(),
            autosave=self.check_autosave.isChecked()
        )
    
    def start_test(self):
        if not self.b1500.connected and not self.power_meter.connected:
            QMessageBox.warning(self, "Error", "No devices connected")
            return
        
        # Clear data
        self.meas_voltages = []
        self.meas_currents = []
        self.meas_powers = []
        self.meas_cycles = []
        self.stress_times = []
        self.stress_currents = []
        self.stress_powers = []
        self.summary_cycles = []
        self.summary_peak_currents = []
        self.summary_peak_powers = []
        
        self.summary_table.setRowCount(0)
        self.update_plots()
        
        config = self.get_config()
        engine = StressMeasurementEngine(self.b1500, self.power_meter, config)
        
        self.worker = TestWorker(engine)
        self.worker.measurement_point.connect(self.on_measurement_point)
        self.worker.stress_point.connect(self.on_stress_point)
        self.worker.phase_change.connect(self.on_phase_change)
        self.worker.cycle_complete.connect(self.on_cycle_complete)
        self.worker.progress.connect(self.on_progress)
        self.worker.log_message.connect(self.log)
        self.worker.finished_signal.connect(self.on_test_complete)
        
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.progress_bar.setValue(0)
        
        self.worker.start()
    
    def stop_test(self):
        if self.worker and self.worker.engine:
            self.worker.engine.stop()
            self.log("Stop requested...")
    
    def on_measurement_point(self, point: MeasurementPoint):
        self.meas_voltages.append(point.voltage)
        self.meas_currents.append(point.current)
        self.meas_powers.append(point.optical_power)
        self.meas_cycles.append(point.cycle)
        self._meas_dirty = True
    
    def on_stress_point(self, point: StressPoint):
        self.stress_times.append(point.elapsed_s)
        self.stress_currents.append(point.current)
        self.stress_powers.append(point.optical_power)
        self._stress_dirty = True
    
    def on_phase_change(self, phase: TestPhase):
        phase_colors = {
            TestPhase.IDLE: "gray",
            TestPhase.MEASUREMENT: "blue",
            TestPhase.STRESS: "orange",
            TestPhase.COMPLETED: "green",
            TestPhase.STOPPED: "red"
        }
        color = phase_colors.get(phase, "black")
        self.label_phase.setText(f"Phase: {phase.value.upper()}")
        self.label_phase.setStyleSheet(f"font-weight: bold; font-size: 12px; color: {color};")
        
        # Clear stress data when entering stress phase
        if phase == TestPhase.STRESS:
            self.stress_times = []
            self.stress_currents = []
            self.stress_powers = []
            self._line_stress_current.set_data([], [])
            self._line_stress_power.set_data([], [])
            self._stress_dirty = False
    
    def on_cycle_complete(self, cycle: int):
        # Add to summary table
        if self.worker and self.worker.engine.cycle_summaries:
            summary = self.worker.engine.cycle_summaries[-1]
            
            row = self.summary_table.rowCount()
            self.summary_table.insertRow(row)
            self.summary_table.setItem(row, 0, QTableWidgetItem(str(summary.cycle)))
            self.summary_table.setItem(row, 1, QTableWidgetItem(summary.timestamp[:19]))
            self.summary_table.setItem(row, 2, QTableWidgetItem(f"{summary.peak_current:.4e}"))
            self.summary_table.setItem(row, 3, QTableWidgetItem(f"{summary.peak_power:.4e}"))
            self.summary_table.setItem(row, 4, QTableWidgetItem(f"{summary.threshold_voltage:.4f}"))
            self.summary_table.setItem(row, 5, QTableWidgetItem(f"{summary.series_resistance:.2f}"))
            
            self.summary_cycles.append(summary.cycle)
            self.summary_peak_currents.append(summary.peak_current)
            self.summary_peak_powers.append(summary.peak_power)
            self._degradation_dirty = True
    
    def on_progress(self, current: int, total: int):
        self.progress_bar.setMaximum(total)
        self.progress_bar.setValue(current)
        self.status_bar.showMessage(f"Cycle {current}/{total}")
    
    def on_test_complete(self):
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.status_bar.showMessage("Test complete")
        self.log("=" * 50)
        self.log("TEST COMPLETED")
    
    def _flush_plot_updates(self):
        """Timer callback: flush all dirty plot updates at most every 400 ms.
        Calling canvas.draw_idle() once here instead of draw() inside each
        handler prevents the Qt event loop from being blocked by Matplotlib."""
        needs_draw = False
        if self._meas_dirty:
            self._meas_dirty = False
            self.update_measurement_plots()
            needs_draw = True
        if self._stress_dirty:
            self._stress_dirty = False
            self.update_stress_plot()
            needs_draw = True
        if self._degradation_dirty:
            self._degradation_dirty = False
            self.update_degradation_plot()
            needs_draw = True
        if needs_draw:
            self.canvas.draw_idle()

    def update_measurement_plots(self):
        if not self.meas_voltages:
            return
        
        # Get current cycle data
        current_cycle = self.meas_cycles[-1] if self.meas_cycles else 0
        cycle_mask = [c == current_cycle for c in self.meas_cycles]
        
        v_cycle = [v for v, m in zip(self.meas_voltages, cycle_mask) if m]
        i_cycle = [i for i, m in zip(self.meas_currents, cycle_mask) if m]
        p_cycle = [p for p, m in zip(self.meas_powers, cycle_mask) if m]
        
        # IV plot
        self.ax_iv.clear()
        self.ax_iv.plot(v_cycle, i_cycle, 'b.-', linewidth=1, markersize=3)
        self.ax_iv.set_xlabel("Voltage (V)")
        self.ax_iv.set_ylabel("Current (A)")
        self.ax_iv.set_title(f"I-V Characteristic (Cycle {current_cycle})")
        self.ax_iv.grid(True, alpha=0.3)
        
        # LI plot
        self.ax_li.clear()
        self.ax_li.plot(i_cycle, p_cycle, 'r.-', linewidth=1, markersize=3)
        self.ax_li.set_xlabel("Current (A)")
        self.ax_li.set_ylabel("Optical Power (W)")
        self.ax_li.set_title(f"L-I Characteristic (Cycle {current_cycle})")
        self.ax_li.grid(True, alpha=0.3)
        # draw_idle() is called once by _flush_plot_updates after all dirty plots are updated
    
    def update_stress_plot(self):
        if not self.stress_times:
            return
        
        self._line_stress_current.set_data(self.stress_times, self.stress_currents)
        self._line_stress_power.set_data(self.stress_times, self.stress_powers)
        self.ax_stress.relim()
        self.ax_stress.autoscale_view()
        self.ax_stress_twin.relim()
        self.ax_stress_twin.autoscale_view()
        self.ax_stress.set_title(f"Stress Monitoring ({len(self.stress_times)} samples)")
    
    def update_degradation_plot(self):
        if not self.summary_cycles:
            return
        
        self._line_deg_current.set_data(self.summary_cycles, self.summary_peak_currents)
        self._line_deg_power.set_data(self.summary_cycles, self.summary_peak_powers)
        self.ax_degradation.relim()
        self.ax_degradation.autoscale_view()
        self.ax_degradation_twin.relim()
        self.ax_degradation_twin.autoscale_view()
    
    def update_plots(self):
        """Full reset of all plots. Called once at the start of each test run."""
        self.ax_iv.clear()
        self.ax_iv.set_xlabel("Voltage (V)")
        self.ax_iv.set_ylabel("Current (A)")
        self.ax_iv.set_title("I-V Characteristic")
        self.ax_iv.grid(True, alpha=0.3)
        
        self.ax_li.clear()
        self.ax_li.set_xlabel("Current (A)")
        self.ax_li.set_ylabel("Optical Power (W)")
        self.ax_li.set_title("L-I Characteristic")
        self.ax_li.grid(True, alpha=0.3)
        
        # Reset stress lines without clearing the pre-created twin axis
        self._line_stress_current.set_data([], [])
        self._line_stress_power.set_data([], [])
        self.ax_stress.set_title("Stress Monitoring")
        self.ax_stress.relim()
        self.ax_stress.autoscale_view()
        self.ax_stress_twin.relim()
        self.ax_stress_twin.autoscale_view()
        
        # Reset degradation lines without clearing the pre-created twin axis
        self._line_deg_current.set_data([], [])
        self._line_deg_power.set_data([], [])
        self.ax_degradation.set_title("Degradation Tracking")
        self.ax_degradation.relim()
        self.ax_degradation.autoscale_view()
        self.ax_degradation_twin.relim()
        self.ax_degradation_twin.autoscale_view()
        
        self._meas_dirty = False
        self._stress_dirty = False
        self._degradation_dirty = False
        
        self.figure.tight_layout()
        self.canvas.draw_idle()
    
    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            self.stop_test()
            self.worker.wait(2000)
        
        self.b1500.disconnect()
        self.power_meter.disconnect()
        event.accept()


# =============================================================================
# Main Entry Point
# =============================================================================

def main():
    import argparse
    
    parser = argparse.ArgumentParser(
        description="B1500 + Power Meter Stress-Measurement Cycling Test"
    )
    parser.add_argument('--cli', action='store_true', help='Run in CLI mode (not implemented)')
    
    args = parser.parse_args()
    
    if args.cli:
        print("CLI mode not implemented yet. Please use GUI mode.")
        return
    
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    window = StressMeasurementCycleGUI()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
