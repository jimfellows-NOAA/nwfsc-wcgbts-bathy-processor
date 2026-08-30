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

We manage this project's environment using **uv**, which automatically handles dependencies and local Python virtual environments.

To initialize, ensure you have the `uv` tool installed, then run:
```bash
# Clone the repository
git clone https://github.com/nwfsc-wcgbts-bathy-processor.git
cd nwfsc-wcgbts-bathy-processor

# Synchronize the dependencies and build the virtual environment
uv sync
```

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

## 5. Development & Testing

We provide a complete `pytest` test suite covering NMEA coordinate conversions, chronological interpolation, boundary extrapolation, format exports, and a full end-to-end integration test parsing local binary `.raw` sample files.

Run tests using:
```bash
uv run pytest -v
```
