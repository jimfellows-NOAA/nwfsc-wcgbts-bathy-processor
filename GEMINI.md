# NWFSC WCGBTS Bathymetry Processor - Architectural & Context Preservation

This document maintains architectural layout context, implementation logic details, and testing specifications for developers in future sessions.

---

## 1. Project Directory Layout

```
C:\Users\James.Fellows\dev\github\nwfsc-wcgbts-bathy-processor\
├── pyproject.toml                        # Project metadata, dependencies, & CLI entry points
├── README.md                             # User-facing domain context and operational run commands
├── GEMINI.md                             # Architectural, logical, and context preservation notes [This file]
├── data/                                 # Ignore-filtered directory hosting local assets
│   ├── raw/                              # Source data directories (containing idx, raw and xml files)
│   │   ├── 1june2018-20260828T212005Z-1-001/
│   │   └── calibration-20260828T212013Z-1-001/
│   └── processed/                        # Output directory for GeoParquet, HDF5, and COG rasters
│       ├── EXCal_-D20180601-T183627.parquet
│       ├── EXCal_-D20180601-T183627.h5
│       └── EXCal_-D20180601-T183627.tif
├── src/
│   └── nwfsc_wcgbts_bathy_processor/
│       ├── __init__.py
│       ├── py.typed
│       ├── core.py                       # Pipeline logic (parsers, interpolation, trackers, gridding)
│       └── cli.py                        # Argparse parsing, visualization rendering (folium/matplotlib)
└── tests/
    └── test_pipeline.py                  # Pytest test suite (unit and integration tests)
```

---

## 2. Core Architectural & Algorithmic Design

### A. NMEA Injection Logic (Precedence Override)
The processor supports two navigation positioning tracks:
1. **Embedded Platform Tracks**: Automatically parsed by `echopype` into `ed.platform` (time series indexed by coordinate dimension `time1`).
2. **External NMEA Logs**: An ASCII log file passed to the pipeline via the `--nmea-log` argument, containing raw `$GPRMC` and `$GPGGA` sentences.

#### Precedence Handling:
* When an external NMEA log is supplied and successfully parsed, **it completely overrides the embedded tracks**. This supports post-processed high-precision tracks (e.g., Applanix POS MV or PPK GPS systems) where the on-board echosounder's real-time GPS was less accurate.
* If no external file is provided, or the parser yields an empty dataset, the pipeline automatically falls back to the echosounder's embedded platform GPS logs.

#### Parsing & Interpolation Workflow:
```
[Simrad Raw File] ──> Extract acoustic ping times (ping_time)
                                     │
                                     ▼
[Coordinates Data] ──> Drop NaNs ──> Time-align to (ping_time) using np.interp
```
* **Coordinate Conversion**: Parses NMEA latitude (`ddmm.mmmm`) and longitude (`dddmm.mmmm`) into decimal degrees. Handles negative scales for `'S'` and `'W'` coordinates.
* **Chronological Alignment**: Echosounder pings (typically $>5$ Hz pings) are logged at a different rate than GPS coordinate messages (typically $1$ Hz). We execute linear interpolation using `np.interp` mapped against the numeric timestamp sequence (nanoseconds since epoch).
* **Extrapolation Safety**: `np.interp` executes constant-boundary value extrapolation, ensuring that pings starting fractionally before the first GPS fix or ending after the last position points still obtain valid spatial geometries instead of `NaN` outputs.

### B. Seafloor Tracking Mechanics
* **Proprietary Picks**: The processor first checks for `detected_seafloor_depth` under the `Vendor_specific` dataset group (which is written if a matching Simrad `.BOT` datagram file is present).
* **Peak Intensity Fallback Tracker**: In the absence of separate `.BOT` files, the pipeline executes a custom peak tracker on the raw acoustic power backscatter values (`backscatter_r`). To bypass transducer ringing and water-surface turbulence, it skips the first 200 samples of each ping vector (corresponding to the near-surface zone), identifies the range index containing the absolute maximum return intensity, and translates that bin index to meters using the sound speed retrieved dynamically from the environmental datagrams ($\sim 1485\text{ m/s}$).

### C. Cloud Optimized GeoTIFF (COG) Gridding
Standard GeoDataFrames are vector tables (point geometries). To produce a raster COG:
1. **Geometric Reprojection**: Points are projected from geographical `EPSG:4326` to metric Web Mercator `EPSG:3857`.
2. **Regular Gridding**: Bins are created across the spatial bounding box at a user-defined grid resolution (default $50\text{m}$).
3. **Bin Averaging**: Acoustic depth values falling inside each grid cell are averaged. Empty cells are filled with a standard NoData value (`-9999.0`).
4. **Pyramidal Tiling**: The grid is written with a tiled layout ($256 \times 256$ blocks), compressed via `DEFLATE` compression, and decorated with dynamic power-of-two downsampled resolution overviews (`[2, 4, 8, 16]`). This structures a fully-compliant Cloud Optimized GeoTIFF.

### D. Volume Backscattering Strength (Sv) & Water Column Profiles
To process water column fish biomass:
1. **Calibration Workflow**: The pipeline computes the Volume Backscattering Strength ($S_v$) in dB using `echopype.calibrate.compute_Sv()`. This translates raw, uncalibrated acoustic backscatter samples (`backscatter_r`) into physically-meaningful volumetric backscattering data.
2. **Self-Healing NumPy 2.x Workaround**: In Simrad EK80 raw files that record power-only (CW) data, the `Vendor_specific` metadata group lacks complex filter coefficients, leaving the `filter_time` coordinate empty (size 0). Under NumPy 2.0+, strict coordinate promotion rules raise a `DTypePromotionError` when attempting to intersect empty `float64` coordinates with `datetime64[ns]` ping times. The pipeline programmatically self-heals this by dropping the empty filter coefficient variables and assigning a dummy, single-element `datetime64[ns]` timestamp to the `filter_time` coordinate, allowing calibration to safely execute.
3. **CLI Extension Routing**: Multi-dimensional datasets are stored in Zarr format (`.zarr`) for high-performance cloud-native analytics or classic NetCDF (`.nc`) for legacy software. Rapid visual inspection generates an inverted-Y 2D plot with an adaptive colormap scale (`vmin=-80`, `vmax=-30` dB) to clearly separate fish schools from the seabed.

---

## 3. Environment & Self-Healing Path Configurations

### PROJ Database Resolution
On Windows host environments, the presence of multiple system-wide spatial installations (e.g., PostgreSQL/PostGIS) can contaminate system path variables, leading to PROJ version mismatch errors inside GDAL/Rasterio.
* **Solution**: The core pipeline features a dynamic self-healing import script that locates the virtual environment's own python site-package directory for `rasterio` and programmatically sets `os.environ["PROJ_DATA"]` to rasterio's internal `proj_data` database path before executing spatial operations. This makes the application completely host-independent and robust.

---

## 4. Testing Architecture & Integration Strategy

Our `pytest` suite in `tests/test_pipeline.py` maintains high-signal, zero-side-effect test configurations:
* **Locality Agnostic**: Searches recursively for any raw `.raw` files under `data/raw/` relative to both the execution path and the test folder, automatically skipping integration tests if local raw files are missing (avoiding CI/CD runner failures).
* **Mock Injections**: Tests the optional navigation injection overrides by generating a temporary `.nmea` file string containing coordinates shifted to a completely different bounding box (lat $45.5$, lon $-125.5$), processing the raw file, and asserting that the resulting GeoDataFrame contains only the overridden geographic coordinates.
* **COG Pyramidal Verification**: Tests that the exported Cloud Optimized GeoTIFF is raster-compliant and contains the generated downsampled overview pyramids.
* **Echogram & Zarr Verification**: Evaluates Volume Backscattering Strength ($S_v$) calculation on a local raw file, asserting that the result is a 3-dimensional dataset containing the `Sv` variable, and verifies exporting to a multi-dimensional Zarr store as well as rendering a 2D profile image (.png).
