# NWFSC West Coast Groundfish Bottom Trawl Survey (WCGBTS) Bathymetry Processor

A high-performance hydrographic Python data pipeline built with **Echopype**, **GeoPandas**, and **Rasterio** to parse Simrad EK60/EK80 `.raw` files, extract spatial/depth data, and export professional GIS-ready files (GeoParquet, HDF5, and Cloud Optimized GeoTIFF).

---

## 1. Scientific & Operational Context

The **West Coast Groundfish Bottom Trawl Survey (WCGBTS)** is a critical fishery-independent resource survey conducted annually by the **Northwest Fisheries Science Center (NWFSC)**. The survey spans the US West Coast (Washington, Oregon, and California) at depth zones ranging from **55 to 1,280 meters (30 to 700 fathoms)**. 

### Hydroacoustic Data Integration
During bottom trawl operations, vessels run multi-frequency scientific echosounders—specifically the **Simrad EK60** and **EK80**—operating at frequencies such as 38 kHz and 120 kHz. This acoustic data serves three vital purposes:
1. **Seafloor Bathymetry Mapping**: Generating accurate depth profiles along the survey track.
2. **Backscatter Intensity & Seafloor Hardness**: Assessing bottom substrate composition (rocky, sandy, muddy) which correlates with groundfish habitat preferences.
3. **Data Quality Assurance**: Cross-referencing real-time trawl depths (from sensors on the net) against scientific echosounder readings.

This processor provides a robust pipeline to convert raw binary echosounder telemetry into high-precision, georeferenced spatial vectors and raster datasets for direct GIS integration.

---

## 2. Echosounder `.raw` Data Source

The Simrad `.raw` format is a proprietary binary format containing interleaved datagrams:
* **Configuration XML**: Detailed transmitter, transceiver, and transducer settings.
* **Acoustic Raw Samples**: Complex electrical signals or backscatter power values (`backscatter_r`) mapped chronologically by ping time and sample range.
* **Navigation Datagrams**: Embedded GPS NMEA strings (such as `$GPGGA`, `$GPRMC`, `$GPGLL`) logged at a typical 1 Hz frequency.

### Seafloor Extraction Mechanics
This pipeline supports dual-path depth retrieval:
1. **Embedded Seafloor Picks**: If separate `.BOT` datagram files are present, they are extracted directly.
2. **Fallback Peak Backscatter Tracker**: If proprietary bottom picks are absent, the processor executes a robust acoustic seafloor tracking algorithm. It isolates the first major backscatter peak (excluding near-surface transducer ringing and bubble noise) and calculates depth in meters based on the two-way travel time:
   $$\text{Depth} = \frac{\text{sample\_index} \times \text{sample\_interval} \times \text{sound\_speed}}{2}$$
   Sound speed is retrieved dynamically from the parsed environmental datagrams (typically $\sim 1485\text{ m/s}$ in West Coast waters).

---

## 3. Installation & Setup

We manage this project's environment and dependencies using **uv**, an extremely fast Python package and environment manager written in Rust.

### Step 1: Install `uv`
If you do not have `uv` installed, install it using the command for your operating system:

* **macOS and Linux**:
  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```
* **Windows (PowerShell)**:
  ```powershell
  powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
  ```
* **Via Pip** (if you already have Python/pip installed):
  ```bash
  pip install uv
  ```

### Step 2: Clone the Repository
```bash
git clone https://github.com/nwfsc-wcgbts-bathy-processor.git
cd nwfsc-wcgbts-bathy-processor
```

### Step 3: Synchronize Dependencies & Create Virtual Environment
The `uv sync` command automatically installs the correct Python version (as specified in `requires-python` in `pyproject.toml`), creates a localized virtual environment (`.venv`), and installs all project dependencies and development packages.
```bash
uv sync
```

### Step 4: Verify the Installation
Verify that the virtual environment is working and the command-line application is correctly registered:
```bash
# Verify the CLI tool works and outputs help
uv run bathy-processor --help
```
You should see the help documentation with commands `process`, `visualize`, and `echogram`.

---

---

## 4. CLI Usage Guide

The pipeline is registered as a CLI application called `bathy-processor`. Use `uv run` to execute commands seamlessly within the virtual environment.

To see all commands and options:
```bash
uv run bathy-processor --help
```

---

### Command 1: `process`
Parses a Simrad `.raw` file, merges spatial coordinates with bottom depth picks (with optional external NMEA log overrides), and exports the georeferenced dataset.

#### Syntax:
```bash
uv run bathy-processor process <input_raw_path> --output <output_path> [options]
```

#### Options:
* `--nmea-log <path>`: (Optional) Ingest an external ASCII NMEA file (containing `$GPRMC` or `$GPGGA` sentences). This external stream will be parsed and interpolated, **taking precedence** over embedded echosounder GPS data. This is crucial when high-precision post-processed kinematic (PPK) vessel coordinates are available.
* `--sonar-model {EK60,EK80}`: (Default: `EK80`) Specify the echosounder software/hardware architecture.

#### Intelligent Extension-Based Exporting:
The output format is determined dynamically by your specified file extension:
1. **GeoParquet (`.parquet` / `.geoparquet`)**:
   Standardized point vector format.
   ```bash
   uv run bathy-processor process data/raw/1june2018-20260828T212005Z-1-001/1june2018/EXCal_-D20180601-T183627.raw --output data/processed/bathy_points.parquet
   ```
2. **HDF5 Database (`.h5` / `.hdf5`)**:
   Compressed tabular dataset grouping arrays (`timestamp`, `longitude`, `latitude`, `depth`) with metadata.
   ```bash
   uv run bathy-processor process data/raw/1june2018-20260828T212005Z-1-001/1june2018/EXCal_-D20180601-T183627.raw --output data/processed/bathy_db.h5
   ```
3. **Cloud Optimized GeoTIFF (COG) (`.tif` / `.tiff`)**:
   A regular gridded raster (default resolution 50 meters) projected to **Web Mercator (EPSG:3857)**, compressed via `DEFLATE`, tiled with a $256 \times 256$ block size, and enriched with downsampled overview pyramids. Highly optimized for web mapping and QGIS.
   ```bash
   uv run bathy-processor process data/raw/1june2018-20260828T212005Z-1-001/1june2018/EXCal_-D20180601-T183627.raw --output data/processed/bathymetry_cog.tif
   ```

---

### Command 2: `visualize`
Ingests a processed GeoParquet or HDF5 dataset and generates two high-impact visualizations for analysis:
1. **Interactive Vessel Track Map (`.html`)**: A Leaflet-based map generated via `folium` tracking the vessel's geographic path. Interactive yellow markers highlight depth callouts upon hover/click.
2. **Matplotlib Cross-Section Plot (`.png`)**: A publication-quality profile plotting bottom depth over time. In accordance with standard hydrographic practice, the depth y-axis is **inverted** so the seabed is naturally represented at the bottom of the canvas.

#### Syntax:
```bash
uv run bathy-processor visualize <processed_dataset_path> [options]
```

#### Options:
* `--map-output <path>`: Destination path for interactive track map HTML (Default: `vessel_path.html`).
* `--plot-output <path>`: Destination path for cross-section chart PNG (Default: `depth_profile.png`).

#### Example:
```bash
# Generate visualizations from a processed Parquet file
uv run bathy-processor visualize data/processed/EXCal_-D20180601-T183627.parquet --map-output data/processed/map.html --plot-output data/processed/profile.png
```

---

### Command 3: `echogram`
Processes a Simrad `.raw` file to compute Volume Backscattering Strength ($S_v$), which is the standard acoustic metric for visualizing water-column fish biomass. Supports both high-dimensional quantitative formats and rapid visual inspection image plotting.

#### Syntax:
```bash
uv run bathy-processor echogram <input_raw_path> --output <output_path> [options]
```

#### Parameter & Option Reference:
* `<input_raw_path>`: (Required, positional) Path to the raw echosounder `.raw` file.
* `--output <output_path>`: (Required, option) Path to save the processed output. Output type is automatically routed based on the file extension (`.zarr`, `.nc`, `.png`, `.jpg`, `.jpeg`).
* `--sonar-model {EK60,EK80}`: (Optional) Specify the echosounder architecture. Default is `EK80`.
  - **`EK80`**: Handles both complex broadband data and power narrowband data by automatically executing CW/power mode calibration.
  - **`EK60`**: Calibrates narrowband telemetry using default EK60 constants.

#### Step-by-Step Usage & Examples:

##### Example 1: Quantitative Cloud-Native Export (`.zarr`)
Zarr is the modern industry standard for cloud-optimized, high-performance, multi-dimensional array storage. This command outputs the entire dataset containing coordinates (`channel`, `ping_time`, `range_sample`) and data variables (`Sv`, `echo_range`, calibration variables) to a local Zarr directory structure.
```bash
uv run bathy-processor echogram data/raw/1june2018-20260828T212005Z-1-001/1june2018/EXCal_-D20180601-T183627.raw --output data/processed/echogram.zarr --sonar-model EK80
```

##### Example 2: Tabular/Scientific Legacy Format (`.nc` / `.netcdf`)
Exports the calibrated Volume Backscattering Strength ($S_v$) to a classic binary NetCDF file. Perfect for integration with older software, MATLAB scripts, or archival NOAA databases.
```bash
uv run bathy-processor echogram data/raw/1june2018-20260828T212005Z-1-001/1june2018/EXCal_-D20180601-T183627.raw --output data/processed/echogram.nc --sonar-model EK80
```

##### Example 3: Rapid Visual Inspection PNG Image (`.png`)
Extracts the calibrated $S_v$ matrix for the first transducer frequency channel, maps the pings horizontally, and plots depth bins vertically.
* **Colormap**: Decorated with the high-signal `'viridis'` colormap to clearly highlight pelagic fish schools and the hard seafloor line.
* **Acoustic Bounds**: Scaled between $-80\text{ dB}$ (weak scattering, like small plankton/krill) and $-30\text{ dB}$ (strong scattering, like rocky seafloor or dense pelagic fish swim bladders).
* **Orientation**: Inverts the Y-axis (Range Sample index) so $0$ (sea surface) is at the top of the canvas, mirroring standard hydrographic echogram orientation.
```bash
uv run bathy-processor echogram data/raw/1june2018-20260828T212005Z-1-001/1june2018/EXCal_-D20180601-T183627.raw --output data/processed/fish_profile.png --sonar-model EK80
```

##### Example 4: Rapid Visual Inspection JPEG Image (`.jpg` / `.jpeg`)
Generates the exact same professional water-column profile plot and saves it as a lightweight compressed JPEG.
```bash
uv run bathy-processor echogram data/raw/1june2018-20260828T212005Z-1-001/1june2018/EXCal_-D20180601-T183627.raw --output data/processed/fish_profile.jpg --sonar-model EK80
```

##### Example 5: Processing older Simrad EK60 raw files
To process files generated by older Simrad EK60 echosounders, explicitly set the `--sonar-model` parameter to `EK60`:
```bash
uv run bathy-processor echogram data/raw/calibration-20260828T212013Z-1-001/calibration/EXCal_-D20180518-T154136.raw --output data/processed/ek60_fish_profile.png --sonar-model EK60
```

---

## 5. Development & Testing

We provide a complete `pytest` test suite covering NMEA coordinate conversions, chronological interpolation, boundary extrapolation, format exports, and a full end-to-end integration test parsing local binary `.raw` sample files.

Run tests using:
```bash
uv run pytest -v
```

# Disclaimer
This repository is a scientific product and is not official communication of the National Oceanic and Atmospheric Administration, or the United States Department of Commerce. All NOAA GitHub project content is provided on an "as is" basis and the user assumes responsibility for its use. Any claims against the Department of Commerce or Department of Commerce bureaus stemming from the use of this GitHub project will be governed by all applicable Federal law. Any reference to specific commercial products, processes, or services by service mark, trademark, manufacturer, or otherwise, does not constitute or imply their endorsement, recommendation or favoring by the Department of Commerce. The Department of Commerce seal and logo, or the seal and logo of a DOC bureau, shall not be used in any manner to imply endorsement of any commercial product or activity by DOC or the United States Government.
